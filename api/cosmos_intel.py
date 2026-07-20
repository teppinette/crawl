"""Cosmos DB — accumulating per-entity counterparty intelligence.

Each completed CIR upserts into ONE document per entity (partition/key =
"<cc>::<normalized-name>"), appending run history and refreshing the latest
grounded report. This is the living-intelligence substrate: a counterparty's
record GROWS across runs instead of a cold CIR each time — and the foundation
for the vector-RAG grounding phase (embeddings added later on the same docs).

Fail-soft by contract: a Cosmos outage must NEVER break a CIR — every entry
point returns False/None on any error and logs.

Config: COSMOS_ENDPOINT (env), key from env COSMOS_KEY or KV secret `cosmos-key`,
COSMOS_DB (default 'cir'), COSMOS_CONTAINER (default 'entity_intelligence').
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata

log = logging.getLogger("crawl-gateway")

_ENDPOINT = os.environ.get(
    "COSMOS_ENDPOINT", "https://copap-cir-intel.documents.azure.com:443/")
_DB = os.environ.get("COSMOS_DB", "cir")
_CONTAINER = os.environ.get("COSMOS_CONTAINER", "entity_intelligence")
_client = None
_cont = None


def _key() -> str | None:
    k = os.environ.get("COSMOS_KEY")
    if k:
        return k
    try:
        from keyvault import get_secret
        return get_secret("cosmos-key")
    except Exception:
        return None


def _container():
    global _client, _cont
    if _cont is not None:
        return _cont
    key = _key()
    if not key:
        log.info("cosmos: no key configured — skipping accumulation")
        return None
    try:
        from azure.cosmos import CosmosClient
        _client = CosmosClient(_ENDPOINT, credential=key)
        _cont = _client.get_database_client(_DB).get_container_client(_CONTAINER)
        return _cont
    except Exception as e:
        log.warning("cosmos: connect failed: %s", e)
        return None


def entity_key(name: str, country: str) -> str:
    """Stable per-entity key: '<cc>::<normalized-name>'. Same as the Cosmos
    partition key so one counterparty = one accumulating document."""
    n = unicodedata.normalize("NFKC", (name or "").strip().lower())
    n = re.sub(r"[^\w\s&.-]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return f"{(country or '').lower()[:2]}::{n}"


def upsert_from_run(run_id: str) -> bool:
    """Accumulate a run's grounded CIR into its entity's Cosmos document.
    Reads the run + cir_markdown render from evidence_db. Fail-soft."""
    cont = _container()
    if cont is None:
        return False
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        import evidence_db
        from datetime import datetime, timezone

        run = evidence_db.get_run(run_id) or {}
        name = run.get("entity_name") or ""
        country = run.get("country") or ""
        if not name:
            return False

        # Latest cir_markdown render payload (markdown + grounding + model).
        payload = {}
        try:
            for r in (evidence_db.list_renders(run_id) or []):
                if r.get("render_type") == "cir_markdown":
                    payload = r.get("payload") or {}
                    break
        except Exception:
            pass

        now = datetime.now(timezone.utc).isoformat()
        ek = entity_key(name, country)
        try:
            doc = cont.read_item(item=ek, partition_key=ek)
        except Exception:
            doc = {"id": ek, "entity_key": ek, "entity_name": name,
                   "country": (country or "").upper()[:2], "first_seen": now,
                   "runs": []}

        g = payload.get("grounding") or {}
        gs = g.get("grounding_score")
        phantom = g.get("phantom_count")
        min_g = float(os.environ.get("COSMOS_MIN_GROUNDING", "90"))
        # ── QUALITY GATE ──────────────────────────────────────────────────────
        # Cosmos is the shared brain the 3 LLMs read — a hallucinated or thin CIR
        # would poison everything downstream. So ONLY a complete, grounded
        # (>= min), hallucination-free (0 phantom citations) CIR is PROMOTED to
        # the SERVED record. Anything else is recorded in history but quarantined
        # — never served as trustworthy intelligence.
        passed = bool(run.get("status") == "complete" and gs is not None
                      and phantom == 0 and float(gs) >= min_g)
        run_entry = {
            "run_id": run_id, "at": now, "status": run.get("status"),
            "model": payload.get("model"),
            "grounding_score": gs, "verdict": g.get("verdict"),
            "phantom_count": phantom,
            "evidence_count": run.get("evidence_count"),
            "claim_count": run.get("claim_count"),
            "passed_quality_gate": passed,
        }
        prior = [r for r in doc.get("runs", []) if r.get("run_id") != run_id]
        doc["runs"] = ([run_entry] + prior)[:20]
        doc.update({"updated_at": now, "run_count": len(doc["runs"]),
                    "entity_name": name,
                    # latest_* = most recent attempt (any quality), for reference.
                    "latest_run_id": run_id, "latest_status": run.get("status"),
                    "latest_grounding": g})
        if passed:
            # PROMOTE — this is what the LLMs actually read.
            doc.update({
                "served_run_id": run_id, "served_at": now,
                "served_markdown": payload.get("markdown"),
                "served_grounding": g, "served_grounding_score": gs,
                "served_verdict": g.get("verdict"), "served_model": payload.get("model"),
                "evidence_count": run.get("evidence_count"),
                "claim_count": run.get("claim_count"),
                "confidence": "grounded",
                # keep latest_markdown in sync only when trustworthy
                "latest_markdown": payload.get("markdown"),
                "latest_model": payload.get("model"),
            })
        else:
            # QUARANTINE — do not overwrite the served (trustworthy) record.
            doc.setdefault("served_run_id", None)
            doc["confidence"] = ("grounded" if doc.get("served_run_id")
                                 else ("review" if gs is not None else "ungraded"))
            doc.setdefault("served_markdown", None)
        cont.upsert_item(doc)
        log.info("cosmos: %s run %s grounding=%s phantom=%s -> %s (confidence=%s)",
                 ek, run_id[:8], gs, phantom, "PROMOTED" if passed else "quarantined",
                 doc.get("confidence"))
        # When a CIR is PROMOTED to served, mirror it into the semantic search
        # index so the brain is searchable by meaning, not just by exact name.
        # Fail-soft: never let an index hiccup break the Cosmos write.
        if passed:
            try:
                import search_index
                search_index.upsert_cir(doc)
            except Exception as _sx:
                log.warning("cosmos: search_index mirror skipped: %s", _sx)
        return True
    except Exception as e:
        log.warning("cosmos upsert_from_run failed for %s: %s",
                    (run_id or "")[:8], e)
        return False


def backfill_all(limit: int = 2000) -> dict:
    """Rebuild the Cosmos DERIVED view from Postgres (the source of truth): upsert
    every completed run. This is the whole point of 'Cosmos is derived, not a
    second truth' — it can always be rebuilt from crawl_reports. Fail-soft."""
    cont = _container()
    if cont is None:
        return {"error": "cosmos not configured"}
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import evidence_db
    conn = evidence_db._get_conn()
    ids = []
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM cir_runs WHERE status='complete' "
                        "ORDER BY started_at DESC LIMIT %s", (int(limit),))
            ids = [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
    ok = sum(1 for rid in ids if upsert_from_run(rid))
    log.info("cosmos backfill: %d/%d completed runs upserted", ok, len(ids))
    return {"completed_runs": len(ids), "upserted": ok}


def get_entity(name: str, country: str) -> dict | None:
    """Read the accumulated intelligence record for an entity (or None)."""
    cont = _container()
    if cont is None:
        return None
    try:
        ek = entity_key(name, country)
        return cont.read_item(item=ek, partition_key=ek)
    except Exception:
        return None


def find_entity(name: str, country: str = "") -> dict | None:
    """Fuzzy lookup for CONSUMERS (copapllm, fraud, mobile) that don't know the
    exact stored country/name. Tries the exact key first; then a normalized-name
    query across partitions (optionally scoped by country), preferring a record
    that has a SERVED (quality-gated) CIR and, among those, the most recently
    updated. Returns the whole doc, or None."""
    cont = _container()
    if cont is None:
        return None
    # 1) exact hit is cheapest.
    if country:
        exact = get_entity(name, country)
        if exact:
            return exact
    # 2) normalized-name contains-query.
    try:
        n = unicodedata.normalize("NFKC", (name or "").strip().lower())
        n = re.sub(r"[^\w\s&.-]", "", n)
        n = re.sub(r"\s+", " ", n).strip()
        if not n:
            return None
        cc = (country or "").lower()[:2]
        params = [{"name": "@n", "value": n}]
        where = "CONTAINS(c.entity_key, @n)"
        if cc:
            where = f"STARTSWITH(c.entity_key, @cc) AND {where}"
            params.append({"name": "@cc", "value": f"{cc}::"})
        q = (f"SELECT * FROM c WHERE {where} "
             "ORDER BY c.updated_at DESC")
        rows = list(cont.query_items(query=q, parameters=params,
                                     enable_cross_partition_query=True))
        if not rows:
            return None
        # Prefer a served (trustworthy) record; else most-recent.
        served = [r for r in rows if r.get("served_run_id")]
        return (served or rows)[0]
    except Exception:
        return None
