"""AI Search layer over the SHARED counterparty brain.

The Cosmos brain (`copap-cir-intel`) answers exact/fuzzy lookup BY ENTITY NAME.
This module adds SEMANTIC / cross-entity retrieval: every served (quality-gated)
CIR is pushed into the `cir_intel` index on `copapllm-search-s1` with a
text-embedding-3-large vector, so a consumer can ask "which counterparties have
environmental adverse media" or "entities tied to <person/address>" and get a
ranked list of entities + CIR excerpts.

Push model (consistent with 'Cosmos is derived, rebuildable'): cosmos_intel calls
`upsert_cir(doc)` the moment a CIR is PROMOTED to served; `backfill(...)` rebuilds
the whole index from Cosmos. Index-time embedding uses the Azure OpenAI deployment
directly; QUERY-time embedding uses the index's integrated vectorizer (the search
service's managed identity), so `search()` just sends text. All calls fail-soft —
a search hiccup never breaks a CIR.
"""
from __future__ import annotations

import base64
import logging
import os

import requests

log = logging.getLogger("search_index")

_SEARCH_EP = (os.environ.get("SEARCH_ENDPOINT")
              or "https://copapllm-search-s1.search.windows.net").rstrip("/")
_SEARCH_KEY = os.environ.get("SEARCH_ADMIN_KEY") or ""
_INDEX = os.environ.get("CIR_SEARCH_INDEX") or "cir_intel"
_API = "2024-07-01"

_AOAI_EP = (os.environ.get("AOAI_EMBED_ENDPOINT")
            or "https://copapfoundry-resource.openai.azure.com").rstrip("/")
_AOAI_KEY = os.environ.get("AOAI_EMBED_KEY") or ""
_EMBED_DEPLOY = os.environ.get("AOAI_EMBED_DEPLOYMENT") or "text-embedding-3-large-363224"


def _doc_id(entity_key: str) -> str:
    """AI Search doc keys allow only letters/digits/_/-/=; entity_key has '::' and
    spaces. base64url is a stable, reversible-enough id."""
    return base64.urlsafe_b64encode((entity_key or "").encode()).decode().rstrip("=")


def _embed(text: str):
    """Embed text via the Azure OpenAI embedding deployment. Returns list[float]
    or None (fail-soft)."""
    if not _AOAI_KEY or not text:
        return None
    try:
        r = requests.post(
            f"{_AOAI_EP}/openai/deployments/{_EMBED_DEPLOY}/embeddings?api-version=2024-02-01",
            headers={"api-key": _AOAI_KEY, "Content-Type": "application/json"},
            json={"input": text[:24000]}, timeout=25)
        if r.status_code >= 400:
            log.warning("search_index embed HTTP %s: %s", r.status_code, r.text[:200])
            return None
        return r.json()["data"][0]["embedding"]
    except Exception as e:  # noqa: BLE001
        log.warning("search_index embed failed: %s", e)
        return None


def upsert_cir(doc: dict) -> bool:
    """Index (or refresh) one entity's SERVED CIR. `doc` is the Cosmos entity
    document. No-op unless it has a served markdown. Fail-soft."""
    if not _SEARCH_KEY:
        return False
    md = doc.get("served_markdown")
    if not md:
        return False
    vec = _embed(md)
    g = doc.get("served_grounding") or {}
    row = {
        "@search.action": "mergeOrUpload",
        "id": _doc_id(doc.get("entity_key") or ""),
        "entity_key": doc.get("entity_key"),
        "entity_name": doc.get("entity_name"),
        "country": doc.get("country"),
        "verdict": doc.get("served_verdict") or g.get("verdict"),
        "grounding_score": float(doc.get("served_grounding_score") or g.get("grounding_score") or 0),
        "confidence": doc.get("confidence"),
        "served_run_id": doc.get("served_run_id"),
        "served_at": doc.get("served_at"),
        "markdown": md[:32000],
    }
    if vec:
        row["content_vector"] = vec
    try:
        r = requests.post(
            f"{_SEARCH_EP}/indexes/{_INDEX}/docs/index?api-version={_API}",
            headers={"api-key": _SEARCH_KEY, "Content-Type": "application/json"},
            json={"value": [row]}, timeout=30)
        if r.status_code >= 300:
            log.warning("search_index upsert HTTP %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("search_index upsert failed: %s", e)
        return False


def search(query: str, top: int = 8, country: str = "") -> list:
    """Hybrid (BM25 + vector + semantic rerank) search over served CIRs. Query is
    embedded by the index's integrated vectorizer, so we just send text. Returns a
    ranked list of {entity_name, country, verdict, grounding_score, confidence,
    served_run_id, excerpt}. Empty list on any failure."""
    if not (_SEARCH_KEY and query):
        return []
    body = {
        "search": query, "top": int(top),
        "queryType": "semantic", "semanticConfiguration": "sem",
        "vectorQueries": [{"kind": "text", "text": query,
                           "fields": "content_vector", "k": int(top)}],
        "select": ("entity_key,entity_name,country,verdict,grounding_score,"
                   "confidence,served_run_id,served_at,markdown"),
    }
    if country:
        body["filter"] = f"country eq '{country.upper()[:2]}'"
    try:
        r = requests.post(
            f"{_SEARCH_EP}/indexes/{_INDEX}/docs/search?api-version={_API}",
            headers={"api-key": _SEARCH_KEY, "Content-Type": "application/json"},
            json=body, timeout=25)
        if r.status_code >= 400:
            log.warning("search_index search HTTP %s: %s", r.status_code, r.text[:200])
            return []
        out = []
        for h in (r.json() or {}).get("value", []):
            md = h.get("markdown") or ""
            out.append({
                "entity_name": h.get("entity_name"),
                "country": h.get("country"),
                "verdict": h.get("verdict"),
                "grounding_score": h.get("grounding_score"),
                "confidence": h.get("confidence"),
                "served_run_id": h.get("served_run_id"),
                "score": h.get("@search.rerankerScore") or h.get("@search.score"),
                "excerpt": md[:600],
            })
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("search_index search failed: %s", e)
        return []


def backfill(limit: int = 5000) -> dict:
    """Rebuild the cir_intel index from Cosmos: index every entity that has a
    served CIR. Idempotent (mergeOrUpload). Fail-soft per entity."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import cosmos_intel
    cont = cosmos_intel._container()
    if cont is None:
        return {"error": "cosmos not configured"}
    n = ok = 0
    try:
        rows = cont.query_items(
            query=("SELECT * FROM c WHERE IS_DEFINED(c.served_run_id) "
                   "AND c.served_run_id != null"),
            enable_cross_partition_query=True)
        for doc in rows:
            n += 1
            if upsert_cir(doc):
                ok += 1
            if n >= limit:
                break
    except Exception as e:  # noqa: BLE001
        log.warning("search_index backfill error after %d: %s", n, e)
    log.info("search_index backfill: %d/%d served entities indexed", ok, n)
    return {"served_entities": n, "indexed": ok}
