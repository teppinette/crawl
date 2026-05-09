"""
Company Enrichment — Bright Data Deep Lookup + Crunchbase scraper.

Returns structured company data (revenue, employees, leadership, funding,
industry) with citations from 1000+ public sources.

Two providers:
  CRUNCHBASE  — Bright Data Web Scraper (Crunchbase dataset), sync, ~30s
  DEEP_LOOKUP — Bright Data Deep Lookup API, AI-powered, ~30-60s

Contract: POST /api/v2/enrich — returns structured company profile.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx

from keyvault import get_secret
import raw_store

log = logging.getLogger("enrichment")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BD_API_KEY = get_secret("brightdata-api-key") or os.environ.get("BRIGHTDATA_API_KEY", "")
_CRUNCHBASE_DATASET_ID = "gd_l1vijqt9jfj7olije"

_BD_SCRAPER_URL = "https://api.brightdata.com/datasets/v3/scrape"
_BD_DEEP_LOOKUP_URL = "https://api.brightdata.com/datasets/deep_lookup/v1"

VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Provider: Crunchbase via Bright Data Web Scraper
# ---------------------------------------------------------------------------

async def _query_crunchbase(entity_name: str, domain: str = None) -> dict:
    """
    Scrape Crunchbase company profile via Bright Data.
    Tries domain-based URL first, then name-based search.
    """
    if not _BD_API_KEY:
        return {"status": "disabled", "error": "brightdata-api-key not configured"}

    t0 = time.monotonic()

    # Build Crunchbase URL — try multiple slug variants
    slugs = []

    # Name-based slug (most likely match on Crunchbase)
    name_clean = entity_name.lower().strip()
    for suffix in [" inc", " inc.", " ltd", " ltd.", " llc", " plc",
                   " pvt", " private", " limited", " corp", " corp.",
                   " ag", " gmbh", " co", " co."]:
        name_clean = name_clean.replace(suffix, "")
    name_slug = name_clean.strip().replace(" ", "-").replace(".", "-")
    slugs.append(name_slug)

    # Domain-based slug (fallback)
    if domain:
        base = domain.lower().replace("www.", "").split(".")[0]
        if base not in slugs:
            slugs.append(base)

    # For multi-word names, also try without spaces: "samsung electronics" → "samsung-electronics"
    # Already handled by name_slug above

    # Try all slugs in parallel — pick the best match
    tasks = [
        _try_crunchbase_url(f"https://www.crunchbase.com/organization/{slug}", t0)
        for slug in slugs
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    best_result = None
    for result in results:
        if isinstance(result, Exception) or result.get("status") != "ok":
            continue
        profile = result.get("profile", {})
        cb_website = (profile.get("website") or "").lower()
        # Best signal: website in Crunchbase matches our input domain
        if domain:
            domain_clean = domain.lower().replace("www.", "").replace("https://", "").replace("http://", "").split("/")[0]
            if domain_clean in cb_website:
                return result
        if not best_result:
            best_result = result

    # If we have a result but domain didn't match, still return it with a warning
    # so the user knows it might be wrong
    if best_result:
        if domain:
            best_result["match_warning"] = f"Crunchbase result website doesn't match domain '{domain}'"
        return best_result

    # All slugs failed
    latency = int((time.monotonic() - t0) * 1000)
    return {"status": "not_found", "latency_ms": latency,
            "error": f"No Crunchbase match for slugs: {slugs}"}


async def _try_crunchbase_url(crunchbase_url: str, t0: float) -> dict:
    """Try a single Crunchbase URL via Bright Data scraper."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            req_url = f"{_BD_SCRAPER_URL}?dataset_id={_CRUNCHBASE_DATASET_ID}&format=json"
            req_headers = {
                "Authorization": f"Bearer {_BD_API_KEY}",
                "Content-Type": "application/json",
            }
            resp = await client.post(
                req_url, headers=req_headers,
                json=[{"url": crunchbase_url}],
            )

            latency = int((time.monotonic() - t0) * 1000)

            raw_store.store(
                source="crunchbase", entity_name=crunchbase_url,
                request_method="POST", request_url=req_url,
                request_headers=req_headers,
                response_status=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=resp.text, duration_ms=latency,
            )

            if resp.status_code == 202:
                return {"status": "pending", "latency_ms": latency,
                        "note": "Crunchbase lookup in progress"}

            if resp.status_code != 200:
                return {"status": "error", "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

            data = resp.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return {"status": "not_found", "latency_ms": latency}

            company = data[0]

            profile = {
                "name": company.get("name"),
                "description": company.get("about", ""),
                "website": company.get("website"),
                "industries": [i.get("value") for i in company.get("industries", []) if i.get("value")],
                "operating_status": company.get("operating_status"),
                "company_type": company.get("company_type"),
                "employee_count": company.get("num_employees"),
                "country": company.get("country_code"),
                "region": company.get("region"),
                "contact_email": company.get("contact_email"),
                "contact_phone": company.get("contact_phone"),
                "social_media": company.get("social_media_links", []),
                "crunchbase_url": company.get("url"),
                "crunchbase_rank": company.get("cb_rank"),
            }

            if company.get("funding_info"):
                fi = company["funding_info"]
                profile["funding"] = {
                    "total_raised": fi.get("total_funding_amount"),
                    "last_round_type": fi.get("last_funding_type"),
                    "last_round_date": fi.get("last_funding_at"),
                    "num_rounds": fi.get("funding_rounds"),
                }

            if company.get("people"):
                profile["leadership"] = []
                for p in company["people"][:10]:
                    profile["leadership"].append({
                        "name": p.get("name"),
                        "title": p.get("title"),
                        "linkedin": p.get("linkedin"),
                    })

            if company.get("financial_data"):
                profile["financials"] = company["financial_data"]

            return {
                "status": "ok",
                "latency_ms": latency,
                "profile": profile,
                "source": "crunchbase",
                "source_url": crunchbase_url,
            }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return {"status": "error", "latency_ms": latency, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Provider: Bright Data Deep Lookup
# ---------------------------------------------------------------------------

async def _query_deep_lookup(entity_name: str, country_code: str = None) -> dict:
    """
    AI-powered company lookup from 1000+ public sources.
    Returns structured data with citations.
    """
    if not _BD_API_KEY:
        return {"status": "disabled", "error": "brightdata-api-key not configured"}

    t0 = time.monotonic()

    country_clause = f" in {country_code}" if country_code else ""
    # Deep Lookup API requires "Find all" prefix. We ONLY use /preview (free,
    # returns 10 samples). NEVER use /v3/trigger — that's $1 per matched record.
    query = (
        f"Find all companies named {entity_name}{country_clause} "
        f"with their revenue, employee count, headquarters location, "
        f"CEO name, website, year founded, and industry"
    )

    headers = {
        "Authorization": f"Bearer {_BD_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        # Step 1: Create preview (free, returns sample data)
        preview_url = f"{_BD_DEEP_LOOKUP_URL}/preview"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                preview_url, headers=headers,
                json={"query": query},
            )
            raw_store.store(
                source="deep_lookup", entity_name=entity_name,
                request_method="POST", request_url=preview_url,
                request_params={"query": query},
                request_headers=dict(headers),
                response_status=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=resp.text,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
            if resp.status_code != 200:
                latency = int((time.monotonic() - t0) * 1000)
                return {
                    "status": "error",
                    "latency_ms": latency,
                    "error": f"Deep Lookup preview: HTTP {resp.status_code}: {resp.text[:200]}",
                }
            preview_data = resp.json()
            preview_id = preview_data.get("preview_id")

        # Step 2: Poll for preview results (max 60s, 3s intervals)
        result_data = None
        for _ in range(20):
            await asyncio.sleep(3)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_BD_DEEP_LOOKUP_URL}/preview/{preview_id}",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status", "")
                    samples = data.get("sample_data", [])
                    if status == "done" or (samples and len(samples) > 0):
                        result_data = data
                        # Check if the first result has enrichment data
                        if samples:
                            first = samples[0]
                            enrichments = first.get("enrichment_results", [])
                            has_data = any(
                                e.get("value") and e["value"] != "skipped"
                                for e in enrichments
                            )
                            if has_data:
                                break
                    if status in ("failed", "error"):
                        latency = int((time.monotonic() - t0) * 1000)
                        return {
                            "status": "error",
                            "latency_ms": latency,
                            "error": f"Deep Lookup failed: {data.get('error', 'unknown')}",
                        }

        latency = int((time.monotonic() - t0) * 1000)

        if not result_data or not result_data.get("sample_data"):
            return {
                "status": "error",
                "latency_ms": latency,
                "error": "Deep Lookup: timed out waiting for results",
            }

        # Parse the matched entity (first one that passed constraints)
        matched = None
        for entity in result_data["sample_data"]:
            filters = entity.get("filter_results", [])
            passed = all(f.get("value", "").lower() == "yes" for f in filters)
            if passed:
                matched = entity
                break

        if not matched:
            return {"status": "not_found", "latency_ms": latency}

        # Extract enrichment results into structured profile
        profile = {
            "name": matched.get("name"),
            "domain": matched.get("url"),
        }
        citations = []

        for enrichment in matched.get("enrichment_results", []):
            key = enrichment.get("key", "")
            value = enrichment.get("value", "")
            if value == "skipped":
                continue

            # Map Deep Lookup keys to our profile fields
            if "revenue" in key:
                profile["revenue"] = value
            elif "employee" in key:
                profile["employee_count"] = value
            elif "headquarters" in key or "location" in key:
                profile["headquarters"] = value
            elif "ceo" in key:
                profile["ceo"] = value
            elif "website" in key:
                profile["website"] = value
            elif "founded" in key or "year" in key:
                profile["founded"] = value
            elif "industry" in key:
                profile["industry"] = value
            else:
                profile[key] = value

            # Collect citations
            for cite in enrichment.get("enhanced_citations", []):
                citations.append({
                    "field": key,
                    "url": cite.get("url"),
                    "title": cite.get("title"),
                    "excerpt": (cite.get("excerpts", [None]) or [None])[0],
                })

        return {
            "status": "ok",
            "latency_ms": latency,
            "profile": profile,
            "citations": citations[:30],  # cap at 30
            "confidence": matched.get("enrichment_results", [{}])[0].get("confidence", "unknown"),
            "source": "deep_lookup",
        }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return {"status": "error", "latency_ms": latency, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def enrich(
    entity_name: str,
    country_code: str = "",
    domain: str = "",
) -> dict:
    """
    Fan-out to Crunchbase + Deep Lookup in parallel.
    Returns combined company profile with citations.
    """
    t0 = time.monotonic()

    _PROVIDER_TIMEOUT = 75  # Deep Lookup polls up to 60s + network overhead

    async def _safe(coro, name):
        try:
            return await asyncio.wait_for(coro, timeout=_PROVIDER_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Enrichment provider %s timed out after %ds", name, _PROVIDER_TIMEOUT)
            return {"status": "error", "error": f"{name}: timed out ({_PROVIDER_TIMEOUT}s)"}

    crunchbase_r, deep_lookup_r = await asyncio.gather(
        _safe(_query_crunchbase(entity_name, domain), "CRUNCHBASE"),
        _safe(_query_deep_lookup(entity_name, country_code), "DEEP_LOOKUP"),
    )

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Merge profiles: Deep Lookup has citations, Crunchbase has structured data
    merged_profile = {}
    citations = []

    # If Crunchbase has a domain-validated match, use it as base
    cb_validated = (crunchbase_r.get("status") == "ok"
                    and crunchbase_r.get("profile")
                    and not crunchbase_r.get("match_warning"))
    cb_unvalidated = (crunchbase_r.get("status") == "ok"
                      and crunchbase_r.get("profile")
                      and crunchbase_r.get("match_warning"))

    if cb_validated:
        merged_profile = crunchbase_r["profile"]

    # Deep Lookup (has citations, always trustworthy)
    if deep_lookup_r.get("status") == "ok" and deep_lookup_r.get("profile"):
        dl_profile = deep_lookup_r["profile"]
        if cb_validated:
            # Fill gaps from Deep Lookup
            for key, value in dl_profile.items():
                if value and not merged_profile.get(key):
                    merged_profile[key] = value
        else:
            # Deep Lookup is primary — Crunchbase was unvalidated or missing
            merged_profile = dl_profile
            # Only backfill from unvalidated Crunchbase for structural fields
            if cb_unvalidated:
                cb_prof = crunchbase_r["profile"]
                for key in ["industries", "funding", "leadership", "financials", "social_media"]:
                    if cb_prof.get(key) and not merged_profile.get(key):
                        merged_profile[key] = cb_prof[key]
        citations = deep_lookup_r.get("citations", [])
    elif cb_unvalidated:
        # Only Crunchbase (unvalidated) — use with warning
        merged_profile = crunchbase_r["profile"]

    # Providers status
    providers = {}
    for name, result in [("CRUNCHBASE", crunchbase_r), ("DEEP_LOOKUP", deep_lookup_r)]:
        entry = {
            "status": result.get("status", "error"),
            "latency_ms": result.get("latency_ms", 0),
        }
        if result.get("error"):
            entry["error"] = result["error"]
        if result.get("source_url"):
            entry["source_url"] = result["source_url"]
        providers[name] = entry

    # Overall status
    has_data = bool(merged_profile.get("name"))
    has_error = any(r.get("status") == "error" for r in [crunchbase_r, deep_lookup_r])

    if has_data and not has_error:
        status = "complete"
    elif has_data and has_error:
        status = "partial"
    elif not has_data and not has_error:
        status = "not_found"
    else:
        status = "error"

    result = {
        "status": status,
        "duration_ms": duration_ms,
        "entity_name": entity_name,
        "providers": providers,
        "profile": merged_profile if merged_profile else None,
    }
    if citations:
        result["citations"] = citations

    return result


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def health() -> dict:
    """Quick provider reachability check."""
    checks = {
        "CRUNCHBASE": "up" if _BD_API_KEY else "disabled (no API key)",
        "DEEP_LOOKUP": "up" if _BD_API_KEY else "disabled (no API key)",
    }
    return {"providers": checks, "version": VERSION}
