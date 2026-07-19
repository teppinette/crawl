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
        run_entry = {
            "run_id": run_id, "at": now, "status": run.get("status"),
            "model": payload.get("model"),
            "grounding_score": g.get("grounding_score"),
            "verdict": g.get("verdict"),
            "evidence_count": run.get("evidence_count"),
            "claim_count": run.get("claim_count"),
        }
        # De-dupe by run_id, newest first, keep last 20.
        prior = [r for r in doc.get("runs", []) if r.get("run_id") != run_id]
        doc["runs"] = ([run_entry] + prior)[:20]
        doc.update({
            "updated_at": now, "run_count": len(doc["runs"]),
            "latest_run_id": run_id, "latest_status": run.get("status"),
            "latest_model": payload.get("model"),
            "latest_grounding": g,
            "latest_markdown": payload.get("markdown"),
            "evidence_count": run.get("evidence_count"),
            "claim_count": run.get("claim_count"),
            "entity_name": name,
        })
        cont.upsert_item(doc)
        log.info("cosmos: accumulated CIR for %s (run %s; %d runs on record)",
                 ek, run_id[:8], len(doc["runs"]))
        return True
    except Exception as e:
        log.warning("cosmos upsert_from_run failed for %s: %s",
                    (run_id or "")[:8], e)
        return False


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
