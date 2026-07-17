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


_CN_CORP_MARKERS = ("公司", "有限", "集团", "厂", "中心", "合伙", "企业")


def _affiliate_expansion(run_id: str, cc: str, entity_name: str,
                         max_seeds: int = 5):
    """Depth-1 affiliate/UBO expansion. Reads the CN commercial evidence already
    collected, pulls CORPORATE seeds (对外投资 affiliates, corporate shareholders,
    branches), and re-queries the registry for each — persisting one
    `cn_affiliates` evidence row per seed. The registry lookup applies the
    _name_match_cn 0.75 gate internally, so a non-matching name returns
    found=false and is stored as an empty-source row (no fabrication). Bounded to
    max_seeds; drops beyond the cap are logged. CN-only for now (the only
    collector producing affiliate signals).

    Seed people (legal rep / individual shareholders) are NOT re-queried here:
    the registry search is by company name, so a person's name can't resolve to
    their other companies via this path — that person→companies linkage needs a
    Tianyancha person endpoint we don't yet call (noted as a follow-up)."""
    if cc != "CN":
        return
    import os
    import requests as _r
    try:
        rows = evidence_db.list_evidence(run_id)
    except Exception as e:
        log.warning("orchestrator: affiliate expansion could not load evidence: %s", e)
        return

    seeds: list[tuple[str, str]] = []  # (name, relation)
    seen = {entity_name.strip()}

    def _add_seed(name, relation):
        n = (name or "").strip()
        if not n or n in seen or not any(m in n for m in _CN_CORP_MARKERS):
            return
        seen.add(n)
        seeds.append((n, relation))

    for row in rows:
        if row.get("source_id") not in ("cn_tianyancha", "cn_registry"):
            continue
        ex = row.get("extracted") or {}
        for a in (ex.get("affiliates") or []):
            _add_seed(a.get("name") if isinstance(a, dict) else a, "outbound_investment")
        for s in (ex.get("shareholders") or []):
            _add_seed(s.get("name") if isinstance(s, dict) else s, "corporate_shareholder")
        for b in (ex.get("branches") or []):
            _add_seed(b if isinstance(b, str) else (b or {}).get("name"), "branch")

    if not seeds:
        log.info("orchestrator: affiliate expansion — no corporate seeds for %s", run_id[:8])
        return
    if len(seeds) > max_seeds:
        log.info("orchestrator: affiliate expansion capped %d→%d seeds for %s (dropped: %s)",
                 len(seeds), max_seeds, run_id[:8],
                 ", ".join(n for n, _ in seeds[max_seeds:]))
        seeds = seeds[:max_seeds]

    base = os.environ.get("CRAWL_GATEWAY_INTERNAL_URL", "http://127.0.0.1:8400")
    api_key = os.environ.get("CIR_API_KEY", "")
    if not api_key:
        try:
            from keyvault import get_secret
            api_key = get_secret("cir-api-key") or ""
        except Exception:
            api_key = ""

    for name, relation in seeds:
        try:
            r = _r.post(
                f"{base}/api/v1/sources/country_registry/lookup",
                params={"country": "CN"},
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"entity_name": name},
                timeout=120,
            )
            data = r.json() if r.status_code < 500 else {}
        except Exception as e:
            log.warning("orchestrator: affiliate lookup failed for %s: %s", name[:30], e)
            data = {}
        primary = (data or {}).get("primary") or {}
        found = bool(primary.get("found"))
        try:
            if found:
                evidence_db.add_evidence(
                    run_id, source_id="cn_affiliates",
                    source_url=primary.get("source_url", ""),
                    source_query=name, status_code=200,
                    extracted={**primary, "subject": entity_name,
                               "relation_to_subject": relation, "depth": 1},
                    language_original="zh", parser_version="affiliate_expansion_v1",
                )
            else:
                # queried, no registry match — empty-source row (raw_content=None
                # → sentinel hash), so it can't be cited as corroboration.
                evidence_db.add_evidence(
                    run_id, source_id="cn_affiliates",
                    source_url=primary.get("source_url", ""),
                    source_query=name, status_code=200,
                    extracted={}, language_original="zh",
                    parser_version="affiliate_expansion_v1",
                    error=f"no registry match for affiliate seed ({relation})",
                )
        except Exception as e:
            log.warning("orchestrator: affiliate persist failed for %s: %s", name[:30], e)
    log.info("orchestrator: affiliate expansion persisted %d seed(s) for %s",
             len(seeds), run_id[:8])


def _parent_chain_imputation(run_id: str, cc: str, entity_name: str,
                             max_parents: int = 5):
    """Walk UP the ownership chain and screen each corporate parent/controller
    for sanctions, recording IMPUTED exposure.

    A per-entity sanctions screen misses the case where the SUBJECT is clean but
    a PARENT is OFAC/CSL-listed — control imputes that exposure downward (the
    parent→branch nexus a flat screen never surfaces). For each corporate
    shareholder / actual-controller already collected, we screen it against CSL
    (OFAC SDN / BIS / etc.) and persist a `parent_chain_sanctions` evidence row
    plus a `relationship` claim so the UBO map + narrative show the chain.

    Hits are recorded as INDIRECT/IMPUTED (direct_listing=False), clearly
    separated from a direct listing of the subject, and NEVER auto-block — an
    imputed parent hit is an analyst-review flag, not a determination. CN-only
    for now (only collector exposing owners). Best-effort / non-fatal."""
    if cc != "CN":
        return
    import os
    import requests as _r
    try:
        rows = evidence_db.list_evidence(run_id)
    except Exception as e:
        log.warning("orchestrator: parent-chain could not load evidence: %s", e)
        return

    parents: list[tuple[str, str, object]] = []  # (name, relation, pct)
    seen = {entity_name.strip()}

    def _add_parent(name, relation, pct=None):
        n = (name or "").strip()
        if not n or n in seen or not any(m in n for m in _CN_CORP_MARKERS):
            return
        seen.add(n)
        parents.append((n, relation, pct))

    for row in rows:
        if row.get("source_id") not in ("cn_tianyancha", "cn_registry", "cn_affiliates"):
            continue
        ex = row.get("extracted") or {}
        for s in (ex.get("shareholders") or []):
            if isinstance(s, dict):
                _add_parent(s.get("name"), "corporate_shareholder", s.get("percent"))
            else:
                _add_parent(s, "corporate_shareholder")
        ac = ex.get("actual_controller")
        if isinstance(ac, str):
            _add_parent(ac, "actual_controller")

    if not parents:
        log.info("orchestrator: parent-chain — no corporate parents for %s", run_id[:8])
        return
    parents = parents[:max_parents]

    base = os.environ.get("CRAWL_GATEWAY_INTERNAL_URL", "http://127.0.0.1:8400")
    api_key = os.environ.get("CIR_API_KEY", "")
    if not api_key:
        try:
            from keyvault import get_secret
            api_key = get_secret("cir-api-key") or ""
        except Exception:
            api_key = ""

    imputed = 0
    for name, relation, pct in parents:
        hits, err = [], None
        try:
            r = _r.post(
                f"{base}/api/v1/sources/opensanctions/search",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"entity_name": name, "country": "CN"}, timeout=60,
            )
            data = r.json() if r.status_code < 500 else {}
            hits = data.get("results") or []
            err = data.get("error")
        except Exception as e:
            err = str(e)[:200]
            log.warning("orchestrator: parent-chain screen failed for %s: %s", name[:30], e)

        pct_txt = str(pct) if pct is not None else "undisclosed"
        if hits:
            extracted = {
                "subject": entity_name, "parent": name,
                "relation_to_subject": relation, "ownership_pct": pct,
                "imputed": True, "direct_listing": False,
                "sanctions_hits": hits[:5], "hit_count": len(hits),
                "nexus": (f"INDIRECT/IMPUTED: parent '{name}' ({relation}, {pct_txt}% of "
                          f"subject) matches a CSL/sanctions record — exposure imputes to "
                          f"'{entity_name}' via ownership/control. NOT a direct listing of "
                          f"the subject; analyst review required, no auto-block."),
            }
            ev = evidence_db.add_evidence(
                run_id, source_id="parent_chain_sanctions",
                source_url="https://data.trade.gov/consolidated_screening_list/v1/search",
                source_query=name, status_code=200, extracted=extracted,
                language_original="zh", parser_version="parent_chain_imputation_v1")
            imputed += 1
            impute_obj = {"parent": name, "relation": relation, "ownership_pct": pct,
                          "imputed_sanctions_exposure": True, "hit_count": len(hits)}
            rat = (f"Parent '{name}' screened with {len(hits)} CSL/sanctions hit(s); "
                   f"exposure imputed to subject via {relation}.")
        else:
            extracted = {
                "subject": entity_name, "parent": name,
                "relation_to_subject": relation, "ownership_pct": pct,
                "imputed": True, "direct_listing": False, "sanctions_hits": [],
                "nexus": f"Parent '{name}' ({relation}, {pct_txt}%) screened clean on CSL.",
            }
            ev = evidence_db.add_evidence(
                run_id, source_id="parent_chain_sanctions",
                source_url="https://data.trade.gov/consolidated_screening_list/v1/search",
                source_query=name, status_code=200, extracted=extracted,
                language_original="zh", parser_version="parent_chain_imputation_v1",
                error=err or None)
            impute_obj = {"parent": name, "relation": relation, "ownership_pct": pct,
                          "imputed_sanctions_exposure": False}
            rat = f"Ownership relationship: {relation} '{name}' (screened clean)."
        try:
            evidence_db.add_claim(
                run_id, claim_type="relationship", subject=entity_name,
                predicate="controlled_by", object_=impute_obj,
                evidence_ids=[ev], confidence="medium", rationale=rat)
        except Exception as e:
            log.warning("orchestrator: parent-chain claim persist failed for %s: %s", name[:30], e)

    log.info("orchestrator: parent-chain imputation screened %d parent(s) for %s (%d imputed hit rows)",
             len(parents), run_id[:8], imputed)


async def _orchestrate(run_id: str, country_code: str, entity_name: str,
                       registration_id: str = "", cn_name: str = ""):
    """Background orchestration — collector → extractor → synthesizer."""
    log.info("orchestrator: starting run %s for %s/%s", run_id[:8], entity_name, country_code)
    cc = country_code.upper()
    loop = asyncio.get_event_loop()

    # ENTITY RESOLUTION (identifiers-first). The China registry (Tianyancha) is
    # indexed by Chinese name — a Latin/English entity_name cannot match it, which
    # silently produced empty CIRs (registration_id="" → the whole deep/relationship
    # path never fired). Resolve the name the COUNTRY collector searches with, in
    # priority order: explicit Chinese name (中文名) > raw entity_name. A provided
    # USCC (registration_id) is handled deterministically downstream by the collector
    # and bypasses name matching. When only a non-Chinese name is given with no
    # identifier, we do NOT guess here — the collector's cross-script gate returns
    # UNRESOLVED rather than a wrong entity (LLM auto-propose is a staged follow-on).
    search_name = entity_name
    if cc == "CN" and cn_name and cn_name.strip():
        search_name = cn_name.strip()
        log.info("orchestrator: run %s — CN registry search resolved to Chinese name "
                 "'%s' (display entity: '%s')", run_id[:8], search_name, entity_name)

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
        f"Collect evidence for entity_name='{search_name}' with run_id='{run_id}'."
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

    # PHASE 1c: web_profile_collector — official-website discovery + crawl via
    # the self-hosted SearXNG + Crawl4AI (free tools only). Best-effort like the
    # dark-web collector: failure is logged, never kills the run.
    web_id = _load_agent_id("web_profile_collector")
    instr_web = (
        f"Collect the website profile for entity_name='{entity_name}' "
        f"country='{cc}' with run_id='{run_id}'. Run one web_profile call and "
        f"persist one evidence row tagged source_id='web_profile'."
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

    async def _run_web():
        if not web_id:
            log.warning("orchestrator: web_profile_collector not deployed, skipping")
            return ("SKIPPED", "no deployed web_profile_collector")
        local_client = _agents_client()
        return await loop.run_in_executor(
            None, _run_agent_sync, local_client, web_id, instr_web, 180,
        )

    try:
        country_res, darkweb_res, web_res = await asyncio.gather(
            _run_country(), _run_darkweb(), _run_web(), return_exceptions=True,
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

    # web_profile is best-effort too — log but never fail the run.
    if isinstance(web_res, Exception):
        log.warning("orchestrator: web collector exception: %s", web_res)
    else:
        w_status, w_err = web_res
        if not w_status.endswith("COMPLETED") and w_status != "SKIPPED":
            log.warning("orchestrator: web collector %s: %s", w_status, w_err or "")

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

    # PHASE 1d: depth-1 affiliate / UBO expansion (best-effort, non-fatal).
    # Runs BEFORE the extractor so the new cn_affiliates evidence rows are turned
    # into relationship claims and flow into the UBO map.
    try:
        await loop.run_in_executor(None, _affiliate_expansion, run_id, cc, entity_name)
    except Exception:
        log.exception("orchestrator: affiliate expansion failed (non-fatal)")

    # PHASE 1e: parent-chain sanctions imputation (best-effort, non-fatal). Walks
    # UP the ownership chain and screens each corporate parent/controller against
    # CSL/OFAC — a clean subject with a sanctioned PARENT inherits that exposure
    # via control. Runs after affiliate expansion (which may add parent rows) and
    # BEFORE the extractor so imputed-exposure evidence + relationship claims flow
    # into the sanctions screen, UBO map, and narrative.
    try:
        await loop.run_in_executor(None, _parent_chain_imputation, run_id, cc, entity_name)
    except Exception:
        log.exception("orchestrator: parent-chain imputation failed (non-fatal)")

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
    cn_name: Optional[str] = Field(None, max_length=500,
        description="Optional Chinese registered name (中文名) for CN entities. "
                    "The China registry (Tianyancha) is indexed by Chinese name, so "
                    "a Latin/English entity_name cannot match it — pass this to "
                    "resolve deterministically (identifiers-first).")


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
        meta={"source": "cir_orchestrator", "registration_id": req.registration_id or "",
              "cn_name": req.cn_name or ""},
    )
    # Kick off background orchestration — caller doesn't wait
    asyncio.create_task(_orchestrate(
        run_id=run_id, country_code=cc,
        entity_name=req.entity_name,
        registration_id=req.registration_id or "",
        cn_name=req.cn_name or "",
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
