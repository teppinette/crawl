"""
Evidence-collection persistence (crawl_reports database).

Tables (see sql/2026-06-22_evidence_schema.sql):
  cir_runs         — one row per evidence-collection run for an entity
  evidence         — atomic evidence records (raw + parsed). Append-only.
  claims           — structured assertions about the entity
  claim_evidence   — m2m linking claims to supporting evidence
  synthesis_runs   — exact prompt + response sent to the synthesis LLM
  renders          — outputs produced from evidence+claims (CIR, screening, etc.)
  sources_catalog  — registry of sources, tiered for bank audit

CIR is now a render of the evidence pool, not the primary output.
"""

import hashlib
import json
import logging
import threading
import uuid

from keyvault import load_db_config

log = logging.getLogger("crawl-gateway")

_db_cfg = None
_lock = threading.Lock()


def _get_conn():
    global _db_cfg
    if _db_cfg is None:
        base = load_db_config()
        _db_cfg = dict(base)
        _db_cfg["dbname"] = "crawl_reports"
    import psycopg2
    return psycopg2.connect(**_db_cfg, connect_timeout=5)


def _exec(sql: str, params: tuple = ()) -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        rc = cur.rowcount
        cur.close()
        conn.close()
        return rc


def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows


def _fetchone(sql: str, params: tuple = ()) -> tuple | None:
    rows = _fetchall(sql, params)
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

def create_run(entity_name: str, country: str, *, job_id: str = None,
               meta: dict = None) -> str:
    run_id = str(uuid.uuid4())
    _exec(
        """INSERT INTO cir_runs (id, job_id, entity_name, country, meta)
           VALUES (%s, %s, %s, %s, %s)""",
        (run_id, job_id, entity_name[:500], country[:10],
         json.dumps(meta or {}, default=str)),
    )
    log.info("evidence_db: created run %s for %s/%s", run_id[:8], entity_name, country)
    return run_id


def update_run_status(run_id: str, status: str, *, error: str = None):
    if status in ("complete", "failed"):
        _exec(
            "UPDATE cir_runs SET status=%s, completed_at=NOW(), error=%s WHERE id=%s",
            (status, error, run_id),
        )
    else:
        _exec("UPDATE cir_runs SET status=%s WHERE id=%s", (status, run_id))


def get_run(run_id: str) -> dict | None:
    row = _fetchone(
        """SELECT id, job_id, entity_name, country, status, started_at,
                  completed_at, evidence_count, claim_count, error, meta
           FROM cir_runs WHERE id=%s""",
        (run_id,),
    )
    if not row:
        return None
    return {
        "run_id": str(row[0]), "job_id": row[1], "entity_name": row[2],
        "country": row[3], "status": row[4],
        "started_at": row[5].isoformat() if row[5] else None,
        "completed_at": row[6].isoformat() if row[6] else None,
        "evidence_count": row[7], "claim_count": row[8],
        "error": row[9], "meta": row[10],
    }


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

def add_evidence(run_id: str, *, source_id: str, source_url: str,
                 raw_content: bytes | str | None = None,
                 raw_blob_path: str = None,
                 source_query: str = None, status_code: int = None,
                 extracted: dict = None, language_original: str = None,
                 extraction_confidence: float = None,
                 parser_version: str = "v1", error: str = None) -> str:
    """
    Persist one evidence record. Returns evidence_id.

    raw_content (bytes/str) is hashed (SHA256) for tamper detection.
    raw_blob_path should point at the verbatim raw stored in osint-staging.
    """
    if raw_content is None:
        content_hash = hashlib.sha256(b"").hexdigest()
    else:
        if isinstance(raw_content, str):
            raw_content = raw_content.encode("utf-8", errors="replace")
        content_hash = hashlib.sha256(raw_content).hexdigest()

    ev_id = str(uuid.uuid4())
    _exec(
        """INSERT INTO evidence
             (id, run_id, source_id, source_url, source_query, status_code,
              raw_blob_path, raw_content_hash, extracted, language_original,
              extraction_confidence, parser_version, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (ev_id, run_id, source_id, source_url[:2000], source_query,
         status_code, raw_blob_path, content_hash,
         json.dumps(extracted, default=str) if extracted else None,
         language_original, extraction_confidence, parser_version, error),
    )
    _exec(
        "UPDATE cir_runs SET evidence_count = evidence_count + 1 WHERE id=%s",
        (run_id,),
    )
    return ev_id


def list_evidence(run_id: str) -> list[dict]:
    rows = _fetchall(
        """SELECT e.id, e.source_id, sc.name, sc.source_tier,
                  sc.auditable_for_banks, e.source_url, e.fetched_at,
                  e.status_code, e.raw_blob_path, e.raw_content_hash,
                  e.extracted, e.language_original, e.extraction_confidence,
                  e.parser_version, e.error
           FROM evidence e
           JOIN sources_catalog sc ON sc.id = e.source_id
           WHERE e.run_id = %s
           ORDER BY e.fetched_at""",
        (run_id,),
    )
    return [
        {
            "id": str(r[0]), "source_id": r[1], "source_name": r[2],
            "source_tier": r[3], "auditable_for_banks": r[4],
            "source_url": r[5],
            "fetched_at": r[6].isoformat() if r[6] else None,
            "status_code": r[7], "raw_blob_path": r[8],
            "raw_content_hash": r[9], "extracted": r[10],
            "language_original": r[11],
            "extraction_confidence": float(r[12]) if r[12] is not None else None,
            "parser_version": r[13], "error": r[14],
        }
        for r in rows
    ]


def get_evidence(evidence_id: str) -> dict | None:
    row = _fetchone(
        """SELECT e.id, e.run_id, e.source_id, sc.name, sc.source_tier,
                  sc.auditable_for_banks, e.source_url, e.source_query,
                  e.fetched_at, e.status_code, e.raw_blob_path,
                  e.raw_content_hash, e.extracted, e.language_original,
                  e.extraction_confidence, e.parser_version, e.error
           FROM evidence e
           JOIN sources_catalog sc ON sc.id = e.source_id
           WHERE e.id = %s""",
        (evidence_id,),
    )
    if not row:
        return None
    return {
        "id": str(row[0]), "run_id": str(row[1]), "source_id": row[2],
        "source_name": row[3], "source_tier": row[4],
        "auditable_for_banks": row[5], "source_url": row[6],
        "source_query": row[7],
        "fetched_at": row[8].isoformat() if row[8] else None,
        "status_code": row[9], "raw_blob_path": row[10],
        "raw_content_hash": row[11], "extracted": row[12],
        "language_original": row[13],
        "extraction_confidence": float(row[14]) if row[14] is not None else None,
        "parser_version": row[15], "error": row[16],
    }


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------

def add_claim(run_id: str, *, claim_type: str, subject: str, predicate: str,
              object_: dict, evidence_ids: list[str],
              confidence: str = "medium", rationale: str = None,
              support: str = "primary",
              quoted_values: dict[str, str] = None) -> str:
    """
    Persist a claim and link it to supporting evidence.

    quoted_values: optional {evidence_id: quoted_string} per-evidence excerpt.
    """
    claim_id = str(uuid.uuid4())
    _exec(
        """INSERT INTO claims
             (id, run_id, claim_type, subject, predicate, object,
              confidence, rationale)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (claim_id, run_id, claim_type, subject[:500], predicate,
         json.dumps(object_, default=str), confidence, rationale),
    )
    qv = quoted_values or {}
    for ev_id in evidence_ids:
        _exec(
            """INSERT INTO claim_evidence (claim_id, evidence_id, support, quoted_value)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (claim_id, ev_id, support, qv.get(ev_id)),
        )
    _exec(
        "UPDATE cir_runs SET claim_count = claim_count + 1 WHERE id=%s",
        (run_id,),
    )
    return claim_id


def list_claims(run_id: str) -> list[dict]:
    rows = _fetchall(
        """SELECT c.id, c.claim_type, c.subject, c.predicate, c.object,
                  c.confidence, c.rationale,
                  array_agg(ce.evidence_id::text) AS evidence_ids,
                  array_agg(ce.support)            AS supports,
                  array_agg(ce.quoted_value)       AS quoted_values
           FROM claims c
           LEFT JOIN claim_evidence ce ON ce.claim_id = c.id
           WHERE c.run_id = %s
           GROUP BY c.id, c.claim_type, c.subject, c.predicate, c.object,
                    c.confidence, c.rationale, c.created_at
           ORDER BY c.created_at""",
        (run_id,),
    )
    return [
        {
            "id": str(r[0]), "claim_type": r[1], "subject": r[2],
            "predicate": r[3], "object": r[4],
            "confidence": r[5], "rationale": r[6],
            "evidence_ids": [e for e in (r[7] or []) if e],
            "supports": [s for s in (r[8] or []) if s],
            "quoted_values": [q for q in (r[9] or []) if q is not None],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# Synthesis (LLM)
# --------------------------------------------------------------------------

def save_synthesis(run_id: str, *, model: str, system_prompt: str,
                   user_prompt: str, evidence_ids: list[str],
                   response_raw: str = None, response_parsed: dict = None,
                   uncited_claims: list = None,
                   tokens_in: int = None, tokens_out: int = None,
                   latency_ms: int = None, cost_usd: float = None) -> str:
    syn_id = str(uuid.uuid4())
    sp_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    _exec(
        """INSERT INTO synthesis_runs
             (id, run_id, model, system_prompt_hash, user_prompt, evidence_ids,
              response_raw, response_parsed, uncited_claims, tokens_in,
              tokens_out, latency_ms, cost_usd)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (syn_id, run_id, model, sp_hash, user_prompt, evidence_ids,
         response_raw,
         json.dumps(response_parsed, default=str) if response_parsed else None,
         json.dumps(uncited_claims, default=str) if uncited_claims else None,
         tokens_in, tokens_out, latency_ms, cost_usd),
    )
    return syn_id


# --------------------------------------------------------------------------
# Renders
# --------------------------------------------------------------------------

def save_render(run_id: str, *, render_type: str, payload: dict = None,
                blob_path: str = None, synthesis_id: str = None) -> str:
    r_id = str(uuid.uuid4())
    _exec(
        """INSERT INTO renders (id, run_id, render_type, synthesis_id,
                                blob_path, payload)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (r_id, run_id, render_type, synthesis_id, blob_path,
         json.dumps(payload, default=str) if payload else None),
    )
    return r_id


def list_renders(run_id: str) -> list[dict]:
    rows = _fetchall(
        """SELECT id, render_type, synthesis_id, blob_path, payload, created_at
           FROM renders WHERE run_id=%s ORDER BY created_at""",
        (run_id,),
    )
    return [
        {
            "id": str(r[0]), "render_type": r[1],
            "synthesis_id": str(r[2]) if r[2] else None,
            "blob_path": r[3], "payload": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# Sources catalog
# --------------------------------------------------------------------------

def list_sources(country: str = None) -> list[dict]:
    if country:
        rows = _fetchall(
            """SELECT id, name, country, source_type, source_tier,
                      auditable_for_banks, base_url, notes
               FROM sources_catalog
               WHERE country = %s OR country IS NULL
               ORDER BY country NULLS LAST, source_tier, name""",
            (country.upper(),),
        )
    else:
        rows = _fetchall(
            """SELECT id, name, country, source_type, source_tier,
                      auditable_for_banks, base_url, notes
               FROM sources_catalog
               ORDER BY country NULLS LAST, source_tier, name"""
        )
    return [
        {
            "id": r[0], "name": r[1], "country": r[2],
            "source_type": r[3], "source_tier": r[4],
            "auditable_for_banks": r[5], "base_url": r[6], "notes": r[7],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# Audit: full provenance for one claim — the "show me the raw" query
# --------------------------------------------------------------------------

def claim_provenance(claim_id: str) -> list[dict]:
    rows = _fetchall(
        """SELECT e.source_id, sc.name, sc.source_tier,
                  sc.auditable_for_banks, e.source_url, e.fetched_at,
                  e.raw_blob_path, e.raw_content_hash,
                  ce.support, ce.quoted_value
           FROM claim_evidence ce
           JOIN evidence       e  ON e.id  = ce.evidence_id
           JOIN sources_catalog sc ON sc.id = e.source_id
           WHERE ce.claim_id = %s
           ORDER BY sc.source_tier, e.fetched_at""",
        (claim_id,),
    )
    return [
        {
            "source_id": r[0], "source_name": r[1], "source_tier": r[2],
            "auditable_for_banks": r[3], "source_url": r[4],
            "fetched_at": r[5].isoformat() if r[5] else None,
            "raw_blob_path": r[6], "raw_content_hash": r[7],
            "support": r[8], "quoted_value": r[9],
        }
        for r in rows
    ]
