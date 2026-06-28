"""CIR orchestrator — single endpoint that runs the full agent mesh pipeline
(country collector → claim extractor → cir_markdown synthesizer) and produces
a complete banker-grade CIR for one entity.

Endpoint:
  POST /api/v1/cir/run

Returns immediately with a run_id. Work continues in the background via
asyncio.create_task. Poll status via the existing /api/v1/evidence/runs/{run_id}
endpoint; fetch the final CIR via /api/v1/evidence/runs/{run_id}/renders.

State transitions on cir_runs.status:
  collecting   -> extracting    (after country collector finishes)
  extracting   -> synthesizing  (after claim_extractor finishes)
  synthesizing -> complete      (after cir_markdown synthesizer finishes)
  any -> failed                  (on any agent failure or timeout)
"""

import asyncio
import logging
import time
import yaml
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import evidence_db

log = logging.getLogger("crawl-gateway")
router = APIRouter(prefix="/api/v1", tags=["cir"])

_ROOT = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _ROOT / "agents"
_PROJECT_ENDPOINT = "https://copapfoundry-resource.services.ai.azure.com/api/projects/copapfoundry"

# Per-phase agent_id lookups. Populated on first use; falls back to YAML scan.
_AGENT_IDS_BY_NAME: dict[str, str] = {}


def _load_agent_id(agent_name: str) -> Optional[str]:
    """Find agent_id by name, scanning agents/**/*.yaml for the 'deployed' block."""
    if agent_name in _AGENT_IDS_BY_NAME:
        return _AGENT_IDS_BY_NAME[agent_name]
    for p in _AGENTS_DIR.rglob("*.yaml"):
        try:
            y = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (y or {}).get("name") == agent_name:
            aid = (y.get("deployed") or {}).get("foundry_agent_id")
            if aid:
                _AGENT_IDS_BY_NAME[agent_name] = aid
                return aid
    return None


def _agents_client():
    """Lazy import + construct Foundry Agents client."""
    import os
    from azure.identity import ManagedIdentityCredential
    from azure.ai.agents import AgentsClient
    # Container Apps + user-assigned MI requires client_id explicitly,
    # same as keyvault.py. Falls through to no-arg constructor (system-
    # assigned MI on crawldevvm) when AZURE_CLIENT_ID is unset.
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        cred = ManagedIdentityCredential(client_id=client_id)
    else:
        cred = ManagedIdentityCredential()
    return AgentsClient(endpoint=_PROJECT_ENDPOINT, credential=cred)


def _run_agent_sync(client, agent_id: str, instruction: str, timeout: int = 300) -> tuple[str, Optional[str]]:
    """Open a thread, post the instruction, run the agent, poll to completion.
    Returns (final_status, last_error_message_or_None)."""
    thread = client.threads.create()
    client.messages.create(thread_id=thread.id, role="user", content=instruction)
    run = client.runs.create(thread_id=thread.id, agent_id=agent_id)
    t0 = time.time()
    last_status = run.status
    while time.time() - t0 < timeout:
        run = client.runs.get(thread_id=thread.id, run_id=run.id)
        if run.status != last_status:
            log.info("orchestrator: agent %s status %s", agent_id[:12], run.status)
            last_status = run.status
        s = str(run.status)
        # NOTE: gpt-4.1-mini occasionally ends a run as "incomplete" (raw
        # string, not a RunStatus.* enum) — a transient terminal state. It must
        # be recognised as terminal, else we poll uselessly until `timeout`
        # (was burning the full 300s per failed collector). Match case-insensitively.
        if s.upper().endswith(("COMPLETED", "FAILED", "CANCELLED", "EXPIRED", "INCOMPLETE")):
            err = None
            if run.last_error:
                err = f"{run.last_error.code}: {run.last_error.message}"
            elif s.upper().endswith("INCOMPLETE"):
                err = f"run ended incomplete ({getattr(run, 'incomplete_details', None)})"
            return s, err
        time.sleep(3)
    return "TIMEOUT", f"agent {agent_id} did not finish within {timeout}s"


def _darkweb_fallback_persist(run_id: str, entity_name: str, country: str):
    """Call /sources/darkweb/scan from inside the container and persist
    one darkweb_screen evidence row via evidence_db.add_evidence. Used when
    the Foundry darkweb_collector agent reports COMPLETED but failed to
    actually write the evidence row (occasional gpt-4.1-mini noise)."""
    import os
    import requests as _r
    base = os.environ.get(
        "CRAWL_GATEWAY_INTERNAL_URL",
        "http://127.0.0.1:8400",
    )
    api_key = os.environ.get("CIR_API_KEY", "")
    if not api_key:
        try:
            from keyvault import get_secret
            api_key = get_secret("cir-api-key") or ""
        except Exception:
            api_key = ""
    if not api_key:
        log.warning("orchestrator: darkweb fallback skipped — no cir-api-key")
        return
    try:
        r = _r.post(
            f"{base}/api/v1/sources/darkweb/scan",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"entity_name": entity_name, "country": country,
                  "depth": "heavy"},
            timeout=300,
        )
    except Exception as e:
        log.warning("orchestrator: darkweb fallback scan failed: %s", e)
        return
    if r.status_code != 200:
        log.warning("orchestrator: darkweb fallback scan HTTP %d", r.status_code)
        return
    data = r.json() or {}
    extracted = {
        "summary": data.get("summary") or {},
        "findings_by_source": data.get("findings_by_source") or {},
        "findings": data.get("findings") or [],
    }
    try:
        evidence_db.add_evidence(
            run_id,
            source_id="darkweb_screen",
            source_url=data.get("source_url", ""),
            source_query=entity_name,
            status_code=200,
            extracted=extracted,
            language_original="en",
            parser_version="darkweb_scan_v1_fallback",
            error=data.get("error"),
        )
        log.info("orchestrator: darkweb fallback persisted evidence for %s",
                 run_id[:8])
    except Exception as e:
        log.warning("orchestrator: darkweb fallback persist failed: %s", e)


async def _orchestrate(run_id: str, country_code: str, entity_name: str,
                       registration_id: str = ""):
    """Background orchestration — collector → extractor → synthesizer."""
    log.info("orchestrator: starting run %s for %s/%s", run_id[:8], entity_name, country_code)
    cc = country_code.upper()
    loop = asyncio.get_event_loop()

    # PHASE 1: country collector. ISO-2 normally maps to verify_<cc>_collector.
    # Historical exception: GB collector was created as verify_uk_collector
    # (United Kingdom) before the ISO-2 convention was settled.
    _cc_alias = {"GB": "uk"}
    base = _cc_alias.get(cc, cc.lower())
    collector_name = f"verify_{base}_collector"
    collector_id = _load_agent_id(collector_name)
    if not collector_id:
        log.warning("orchestrator: no collector for %s (name=%s), aborting", cc, collector_name)
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"no deployed collector for country {cc} (looked for {collector_name})")
        return

    client = _agents_client()
    # NB: keep this instruction PLAIN. The previous assertive phrasing
    # ("Execute every step… ALL … calls REQUIRE … as the path parameter")
    # tripped Azure OpenAI's prompt-shield/content filter → the run ended
    # `incomplete (reason: content_filter)` before any tool call (confirmed:
    # the same agent runs fine with a plain instruction). The agent's system
    # prompt already mandates run_id on evidence_add/collector_complete.
    instr_collect = (
        f"Collect evidence for entity_name='{entity_name}' with run_id='{run_id}'."
        + (f" Registration number: {registration_id}" if registration_id else "")
    )

    # PHASE 1b: darkweb_collector runs in parallel with the country collector.
    # OSINT screening is best-effort — failures get logged but do not kill the
    # run. Country-collector failure still kills the run.
    darkweb_id = _load_agent_id("darkweb_collector")
    instr_darkweb = (
        f"Screen entity_name='{entity_name}' country='{cc}' for run_id='{run_id}'. "
        f"Run one darkweb_scan with depth='heavy' and persist one evidence "
        f"row tagged source_id='darkweb_screen'."
    )

    async def _run_country():
        # The country collector is the only phase that can kill the run, and
        # gpt-4.1-mini intermittently returns "incomplete" before firing a
        # single tool call (the agent itself is sound — verified in isolation).
        # Retry up to 3x on any non-COMPLETED terminal status; each attempt is
        # a fresh thread/run so a transient incomplete doesn't fail the CIR.
        local_client = _agents_client()
        last = ("UNKNOWN", None)
        for attempt in range(3):
            last = await loop.run_in_executor(
                None, _run_agent_sync, local_client, collector_id, instr_collect, 300,
            )
            if str(last[0]).upper().endswith("COMPLETED"):
                return last
            log.warning("orchestrator: country collector attempt %d/3 -> %s (%s); retrying",
                        attempt + 1, last[0], last[1])
        return last

    async def _run_darkweb():
        if not darkweb_id:
            log.warning("orchestrator: darkweb_collector not deployed, skipping")
            return ("SKIPPED", "no deployed darkweb_collector")
        local_client = _agents_client()
        return await loop.run_in_executor(
            None, _run_agent_sync, local_client, darkweb_id, instr_darkweb, 300,
        )

    try:
        country_res, darkweb_res = await asyncio.gather(
            _run_country(), _run_darkweb(), return_exceptions=True,
        )
    except Exception as e:
        log.exception("orchestrator: phase-1 gather exception")
        evidence_db.update_run_status(run_id, "failed", error=f"phase-1 exception: {e}")
        return

    if isinstance(country_res, Exception):
        log.exception("orchestrator: country collector exception")
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"collector exception: {country_res}")
        return
    status, err = country_res
    if not status.endswith("COMPLETED"):
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"collector {status}: {err or ''}")
        return

    if isinstance(darkweb_res, Exception):
        log.warning("orchestrator: darkweb collector exception: %s", darkweb_res)
    else:
        dw_status, dw_err = darkweb_res
        if not dw_status.endswith("COMPLETED") and dw_status != "SKIPPED":
            log.warning("orchestrator: darkweb collector %s: %s", dw_status, dw_err or "")

    # FALLBACK for darkweb_collector reporting COMPLETED but not actually
    # writing the evidence row. gpt-4.1-mini occasionally produces a final
    # message claiming success without firing the evidence_add tool. If
    # we're missing the darkweb_screen row, call the scan endpoint directly
    # and persist server-side via evidence_db.add_evidence.
    try:
        existing = evidence_db.list_evidence(run_id)
        has_dw = any((e.get("source_id") == "darkweb_screen") for e in existing)
    except Exception:
        has_dw = True  # Can't tell — assume OK, skip fallback
    if not has_dw:
        log.warning("orchestrator: darkweb_collector completed without writing "
                    "evidence; falling back to direct scan + persist")
        try:
            await loop.run_in_executor(None, _darkweb_fallback_persist,
                                       run_id, entity_name, cc)
        except Exception:
            log.exception("orchestrator: darkweb fallback failed (non-fatal)")

    # PHASE 2: claim extractor
    extractor_id = _load_agent_id("claim_extractor")
    if not extractor_id:
        evidence_db.update_run_status(run_id, "failed", error="no deployed claim_extractor")
        return
    instr_extract = (
        f"Extract claims for run_id='{run_id}'. Load the evidence with "
        f"list_run_evidence(run_id='{run_id}'), persist each typed claim via "
        f"add_claim(run_id='{run_id}'), then call extractor_complete(run_id='{run_id}')."
    )
    status, err = await loop.run_in_executor(None, _run_agent_sync, client, extractor_id, instr_extract, 300)
    if not status.endswith("COMPLETED"):
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"extractor {status}: {err or ''}")
        return

    # PHASE 3: Run all 4 synthesizers in parallel. Same evidence pool, 4
    # different render_types. Each call is fully independent — synthesizer
    # threads don't share state. Parallel execution because Foundry's
    # synthesis is the slowest phase (~30-60s each); serial would be
    # ~2-4 min for 4 synthesizers vs ~30-60s for parallel.
    synth_specs = [
        ("cir_markdown_synthesizer", "cir_markdown",
         f"Generate the CIR markdown for run_id='{run_id}'. Load evidence + "
         f"claims, write the banker narrative with [E<id>] citations, then "
         f"call save_render(run_id='{run_id}', render_type='cir_markdown', ...) "
         f"and synthesizer_complete(run_id='{run_id}')."),
        ("sanctions_screening_synthesizer", "sanctions_screening",
         f"Produce sanctions screening for run_id='{run_id}'. Filter the "
         f"evidence pool to sanctions-tier sources only; emit HIT|CLEAN|ERROR "
         f"structured payload with hits/clean_sources/errors arrays. Call "
         f"save_render(run_id='{run_id}', render_type='sanctions_screening', ...) "
         f"then synthesizer_complete(run_id='{run_id}')."),
        ("ubo_map_synthesizer", "ubo_map",
         f"Build the UBO map for run_id='{run_id}'. Load evidence + claims, "
         f"identify nodes (entities + people) and edges (ownership/director "
         f"relationships) with strength weighted by source tier. Handle "
         f"ownership_undisclosed cases (e.g. PSC exempt). Call "
         f"save_render(run_id='{run_id}', render_type='ubo_map', ...) "
         f"then synthesizer_complete(run_id='{run_id}')."),
        ("banker_audit_pack_synthesizer", "banker_audit_pack",
         f"Produce the banker audit pack for run_id='{run_id}'. Filter "
         f"evidence to PRIMARY_GOVERNMENT and OFFICIAL_LIST tiers ONLY — drop "
         f"all other tiers. Emit structured pack (identity / ownership / "
         f"officers / sanctions / source_coverage). Call "
         f"save_render(run_id='{run_id}', render_type='banker_audit_pack', ...) "
         f"then synthesizer_complete(run_id='{run_id}')."),
    ]

    # Resolve agent IDs; skip any not yet deployed
    synth_tasks = []
    for name, rtype, instr in synth_specs:
        aid = _load_agent_id(name)
        if not aid:
            log.warning("orchestrator: %s not deployed, skipping its render", name)
            continue
        # Each synthesizer needs its own client (the AgentsClient is not
        # known to be thread-safe; cheap to construct)
        synth_tasks.append((rtype, aid, instr))

    if not synth_tasks:
        evidence_db.update_run_status(run_id, "failed",
                                      error="no deployed synthesizers")
        return

    async def _run_one(rtype: str, aid: str, instr: str):
        try:
            local_client = _agents_client()
            status, err = await loop.run_in_executor(
                None, _run_agent_sync, local_client, aid, instr, 600,
            )
            return (rtype, status, err)
        except Exception as e:
            return (rtype, "EXCEPTION", str(e)[:200])

    results = await asyncio.gather(*[_run_one(rt, aid, instr)
                                     for rt, aid, instr in synth_tasks])
    failed = [(rt, s, e) for rt, s, e in results if not s.endswith("COMPLETED")]
    if len(failed) == len(results):
        # All synthesizers failed — mark whole run failed
        evidence_db.update_run_status(run_id, "failed",
            error=f"all synthesizers failed: {failed}")
        return
    if failed:
        # Partial failure — log but don't fail the run; cir_markdown completing
        # is enough for the banker-facing output
        log.warning("orchestrator: %d synthesizer(s) failed: %s",
                    len(failed), failed)

    # synthesizer_complete (called by each successful synthesizer) already
    # transitioned run to 'complete'. Belt-and-suspenders:
    evidence_db.update_run_status(run_id, "complete")
    log.info("orchestrator: run %s complete, %d of %d synthesizers succeeded",
             run_id[:8], len(results) - len(failed), len(results))


class CIRRunRequest(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    entity_name: str = Field(..., max_length=500)
    registration_id: Optional[str] = Field(None, max_length=100,
        description="Optional USCC/CIN/CIK/etc. for deterministic lookup")


class CIRRunResponse(BaseModel):
    run_id: str
    status: str = "collecting"
    entity_name: str
    country_code: str
    next_steps: dict


@router.post("/cir/run")
async def cir_run(req: CIRRunRequest):
    """Fire the full agent-mesh pipeline for one entity. Returns immediately
    with run_id; orchestration continues in background. Poll status via
    /evidence/runs/{run_id}; fetch final CIR via /evidence/runs/{run_id}/renders."""
    cc = req.country_code.upper().strip()
    if len(cc) != 2:
        raise HTTPException(status_code=400, detail="country_code must be ISO-2")

    run_id = evidence_db.create_run(
        entity_name=req.entity_name, country=cc,
        meta={"source": "cir_orchestrator", "registration_id": req.registration_id or ""},
    )
    # Kick off background orchestration — caller doesn't wait
    asyncio.create_task(_orchestrate(
        run_id=run_id, country_code=cc,
        entity_name=req.entity_name,
        registration_id=req.registration_id or "",
    ))
    return CIRRunResponse(
        run_id=run_id,
        entity_name=req.entity_name,
        country_code=cc,
        next_steps={
            "poll_status": f"/api/v1/evidence/runs/{run_id}",
            "fetch_evidence": f"/api/v1/evidence/runs/{run_id}/evidence",
            "fetch_claims": f"/api/v1/evidence/runs/{run_id}/claims",
            "fetch_renders": f"/api/v1/evidence/runs/{run_id}/renders",
            "expected_completion_seconds": 180,
        },
    )
