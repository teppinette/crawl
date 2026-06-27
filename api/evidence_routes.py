"""
Evidence-collection HTTP surface.

CIR shifts from "submit a CIR job" to "submit an entity for evidence
collection" + "render view X." Evidence is the asset; CIR is one renderer.

Endpoints:
  POST   /api/v1/evidence/runs                                — start a new collection run
  GET    /api/v1/evidence/runs/{run_id}                       — run status + counts
  GET    /api/v1/evidence/runs/{run_id}/evidence              — list evidence rows
  GET    /api/v1/evidence/runs/{run_id}/claims                — list claims w/ evidence
  GET    /api/v1/evidence/{evidence_id}                       — single evidence record (audit)
  GET    /api/v1/evidence/claims/{claim_id}/provenance        — full claim provenance
  POST   /api/v1/evidence/runs/{run_id}/render/{render_type}  — render view (cir/screening/ubo/audit-pack)
  GET    /api/v1/evidence/runs/{run_id}/renders               — list renders for a run
  GET    /api/v1/sources                                      — list source catalog

Phase 1 collectors (per-country) and Phase 3 synthesis (COPAPLLM) are NOT
yet wired through these endpoints — they will write into the same tables
via evidence_db.* helpers. These routes are the read/audit surface plus
the run-shell and render-shell creation calls.

Auth: wired at app.include_router(...) in main.py via dependencies=[].
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import evidence_db

log = logging.getLogger("crawl-gateway")

router = APIRouter(prefix="/api/v1", tags=["evidence"])


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class RunCreateRequest(BaseModel):
    entity_name: str = Field(..., max_length=500)
    country: str = Field(..., min_length=2, max_length=10)
    job_id: Optional[str] = None
    meta: Optional[dict] = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    entity_name: str
    country: str
    evidence_count: int = 0
    claim_count: int = 0


class RenderRequest(BaseModel):
    model: Optional[str] = Field(None, description="LLM for synthesis renders; defaults to COPAPLLM")
    payload: Optional[dict] = None


class EvidenceAddRequest(BaseModel):
    source_id: str = Field(..., description="FK to sources_catalog.id")
    source_url: str
    source_query: Optional[str] = None
    status_code: Optional[int] = None
    raw_content: Optional[str] = Field(None, description="Verbatim raw — server SHA256s it")
    raw_blob_path: Optional[str] = Field(None, description="osint-staging path if already uploaded")
    extracted: Optional[dict] = None
    language_original: Optional[str] = None
    extraction_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    parser_version: str
    error: Optional[str] = None


_RENDER_TYPES = {"cir_markdown", "sanctions_screening", "ubo_map", "banker_audit_pack"}


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.post("/evidence/runs")
async def create_run(req: RunCreateRequest):
    """Start a new evidence-collection run for an entity."""
    run_id = evidence_db.create_run(
        entity_name=req.entity_name,
        country=req.country.upper(),
        job_id=req.job_id,
        meta=req.meta,
    )
    return RunResponse(
        run_id=run_id, status="collecting",
        entity_name=req.entity_name, country=req.country.upper(),
    )


@router.get("/evidence/runs/{run_id}")
async def get_run(run_id: str):
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/evidence/runs/{run_id}/evidence")
async def list_run_evidence(run_id: str):
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "evidence": evidence_db.list_evidence(run_id)}


@router.post("/evidence/runs/{run_id}/evidence")
async def add_run_evidence(run_id: str, req: EvidenceAddRequest):
    """Write one evidence row. Called by collector agents once per source.
    Matches agents/tools/evidence_add.openapi.yaml."""
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        ev_id = evidence_db.add_evidence(
            run_id,
            source_id=req.source_id,
            source_url=req.source_url,
            source_query=req.source_query,
            status_code=req.status_code,
            raw_content=req.raw_content,
            raw_blob_path=req.raw_blob_path,
            extracted=req.extracted,
            language_original=req.language_original,
            extraction_confidence=req.extraction_confidence,
            parser_version=req.parser_version,
            error=req.error,
        )
    except Exception as e:
        # ForeignKeyViolation on source_id is the most likely caller error
        msg = str(e)
        if "sources_catalog" in msg or "foreign key" in msg.lower():
            raise HTTPException(
                status_code=400,
                detail=f"unknown source_id '{req.source_id}' — must exist in sources_catalog",
            )
        raise
    run_after = evidence_db.get_run(run_id)
    return {
        "evidence_id": ev_id,
        "run_id": run_id,
        "evidence_count": run_after["evidence_count"] if run_after else None,
    }


@router.get("/evidence/runs/{run_id}/claims")
async def list_run_claims(run_id: str):
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "claims": evidence_db.list_claims(run_id)}


class AddClaimRequest(BaseModel):
    claim_type: str
    subject: str = Field(..., max_length=500)
    predicate: str
    object: dict = Field(..., description="Typed payload, shape varies by claim_type")
    evidence_ids: list[str] = Field(..., min_items=1)
    confidence: Optional[str] = "medium"
    rationale: Optional[str] = None
    support: Optional[str] = "primary"
    quoted_values: Optional[dict] = None


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@router.post("/evidence/runs/{run_id}/claims")
async def add_run_claim(run_id: str, req: AddClaimRequest):
    """Persist one claim with linked evidence. Called by claim_extractor."""
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    bad = [e for e in (req.evidence_ids or []) if not _UUID_RE.match(e or "")]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"malformed evidence_id(s): {bad}. Expected UUID format "
                   "8-4-4-4-12 hex chars. Re-check the evidence_id you copied "
                   "from list_run_evidence — last segment is exactly 12 hex chars.",
        )
    try:
        claim_id = evidence_db.add_claim(
            run_id,
            claim_type=req.claim_type,
            subject=req.subject,
            predicate=req.predicate,
            object_=req.object,
            evidence_ids=req.evidence_ids,
            confidence=req.confidence or "medium",
            rationale=req.rationale,
            support=req.support or "primary",
            quoted_values=req.quoted_values or {},
        )
    except Exception as e:
        msg = str(e)
        if "evidence" in msg.lower() and "foreign" in msg.lower():
            raise HTTPException(status_code=400,
                                detail="one or more evidence_ids do not belong to this run")
        raise
    run_after = evidence_db.get_run(run_id)
    return {
        "claim_id": claim_id,
        "run_id": run_id,
        "claim_count": run_after["claim_count"] if run_after else None,
    }


@router.post("/evidence/runs/{run_id}/extractor_complete")
async def extractor_complete(run_id: str):
    """Mark extractor done — transitions cir_runs.status to 'synthesizing'."""
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    evidence_db.update_run_status(run_id, "synthesizing")
    run_after = evidence_db.get_run(run_id)
    return {
        "run_id": run_id,
        "status": run_after["status"] if run_after else "synthesizing",
        "claim_count": run_after["claim_count"] if run_after else None,
    }


@router.post("/evidence/runs/{run_id}/synthesizer_complete")
async def synthesizer_complete(run_id: str):
    """Mark synthesizer done — transitions cir_runs.status to 'complete'."""
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    evidence_db.update_run_status(run_id, "complete")
    run_after = evidence_db.get_run(run_id)
    return {"run_id": run_id, "status": run_after["status"] if run_after else "complete"}


@router.get("/evidence/runs/{run_id}/renders")
async def list_run_renders(run_id: str):
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "renders": evidence_db.list_renders(run_id)}


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str):
    """Single evidence record — for the 'show me the raw' audit query."""
    ev = evidence_db.get_evidence(evidence_id)
    if not ev:
        raise HTTPException(status_code=404, detail="evidence not found")
    return ev


@router.get("/evidence/claims/{claim_id}/provenance")
async def claim_provenance(claim_id: str):
    """All evidence behind a single claim, tiered. Banker's audit query."""
    rows = evidence_db.claim_provenance(claim_id)
    return {"claim_id": claim_id, "evidence": rows}


@router.post("/evidence/runs/{run_id}/render/{render_type}")
async def render(run_id: str, render_type: str, req: RenderRequest):
    """
    Render a view from the evidence + claims pool. Same evidence, many views.

    render_type:
      cir_markdown         — narrative CIR (LLM synthesis with cited evidence)
      sanctions_screening  — sanctions-only structured output
      ubo_map              — UBO / director graph
      banker_audit_pack    — gov-source-tier-only audit pack

    Synthesis backend not yet wired — this persists the render shell so the
    synthesis worker can fill response_parsed + blob_path.
    """
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if render_type not in _RENDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown render_type; allowed: {sorted(_RENDER_TYPES)}",
        )
    r_id = evidence_db.save_render(
        run_id=run_id, render_type=render_type,
        payload={"requested_model": req.model, "input_payload": req.payload or {}},
    )
    return {"run_id": run_id, "render_id": r_id,
            "render_type": render_type, "status": "queued"}


@router.get("/sources")
async def list_sources(country: Optional[str] = None):
    """List the source catalog. Optional ?country=ISO2 filter."""
    return {"sources": evidence_db.list_sources(country.upper() if country else None)}
