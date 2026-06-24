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
    from azure.identity import ManagedIdentityCredential
    from azure.ai.agents import AgentsClient
    return AgentsClient(endpoint=_PROJECT_ENDPOINT,
                        credential=ManagedIdentityCredential())


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
        if s.endswith(("COMPLETED", "FAILED", "CANCELLED", "EXPIRED")):
            err = None
            if run.last_error:
                err = f"{run.last_error.code}: {run.last_error.message}"
            return s, err
        time.sleep(3)
    return "TIMEOUT", f"agent {agent_id} did not finish within {timeout}s"


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
    instr_collect = (
        f"Collect evidence for entity_name='{entity_name}' with run_id='{run_id}'. "
        f"Execute every step in your system prompt. ALL evidence_add and "
        f"collector_complete calls REQUIRE run_id='{run_id}' as the path "
        f"parameter."
        + (f" Registration number: {registration_id}" if registration_id else "")
    )
    try:
        status, err = await loop.run_in_executor(None, _run_agent_sync, client, collector_id, instr_collect, 300)
    except Exception as e:
        log.exception("orchestrator: collector exception")
        evidence_db.update_run_status(run_id, "failed", error=f"collector exception: {e}")
        return
    if not status.endswith("COMPLETED"):
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"collector {status}: {err or ''}")
        return

    # PHASE 2: claim extractor
    extractor_id = _load_agent_id("claim_extractor")
    if not extractor_id:
        evidence_db.update_run_status(run_id, "failed", error="no deployed claim_extractor")
        return
    instr_extract = (
        f"Extract claims for run_id='{run_id}'. Load the evidence pool with "
        f"list_run_evidence(run_id='{run_id}'). Identify typed claims and "
        f"persist each via add_claim — REMEMBER add_claim REQUIRES "
        f"run_id='{run_id}' as the path parameter. When done call "
        f"extractor_complete(run_id='{run_id}')."
    )
    status, err = await loop.run_in_executor(None, _run_agent_sync, client, extractor_id, instr_extract, 300)
    if not status.endswith("COMPLETED"):
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"extractor {status}: {err or ''}")
        return

    # PHASE 3: CIR markdown synthesizer
    synth_id = _load_agent_id("cir_markdown_synthesizer")
    if not synth_id:
        evidence_db.update_run_status(run_id, "failed",
                                      error="no deployed cir_markdown_synthesizer")
        return
    instr_synth = (
        f"Generate the CIR markdown for run_id='{run_id}'. Load evidence + "
        f"claims, write the banker narrative with [E<id>] citations, then "
        f"call save_render(run_id='{run_id}', render_type='cir_markdown', ...) "
        f"and synthesizer_complete(run_id='{run_id}')."
    )
    status, err = await loop.run_in_executor(None, _run_agent_sync, client, synth_id, instr_synth, 600)
    if not status.endswith("COMPLETED"):
        evidence_db.update_run_status(run_id, "failed",
                                      error=f"synthesizer {status}: {err or ''}")
        return

    log.info("orchestrator: run %s complete", run_id[:8])


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
