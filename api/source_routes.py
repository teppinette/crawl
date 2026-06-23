"""
Gateway proxy routes for individual evidence sources.

Each route is the HTTP face of one source the collector agents call.
Routes return a consistent shape: {source_id, source_url, fetched_at, ...}
so the agent can pass results straight into evidence_add.

Pattern: thin wrappers over existing source modules (verify_uk, source_gleif,
screening) plus direct calls to free APIs where no module exists yet.
Routes mounted under /api/v1/sources/<source_id>/<op>.

Matches the agents/tools/*.openapi.yaml specs.
"""

import datetime
import logging
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import evidence_db
import source_gleif
from keyvault import get_secret

log = logging.getLogger("crawl-gateway")

router = APIRouter(prefix="/api/v1", tags=["sources"])

_UA = "Crawl-Research-Gateway/3.0 (+evidence-collector)"

# Loopback to /api/v1/verify (which routes GB to crawl-verify VM).
_GATEWAY_INTERNAL = "http://127.0.0.1:8400"


def _loopback_verify(payload: dict) -> dict:
    api_key = get_secret("cir-api-key") or ""
    try:
        r = requests.post(
            f"{_GATEWAY_INTERNAL}/api/v1/verify",
            json=payload,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=60,
        )
        return r.json() if r.status_code < 500 else {"found": False, "error": f"upstream {r.status_code}"}
    except Exception as e:
        return {"found": False, "error": str(e)[:200]}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# UK Companies House
# ---------------------------------------------------------------------------

class CHSearchRequest(BaseModel):
    entity_name: str = Field(..., max_length=200)
    items_per_page: int = Field(5, ge=1, le=50)


class CHProfileRequest(BaseModel):
    company_number: str = Field(..., max_length=20)


@router.post("/sources/gb_companies_house/search")
async def gb_companies_house_search(req: CHSearchRequest):
    """Loopback to /api/v1/verify with country_code=GB — that's the working
    Companies House path (proxies to crawl-verify VM)."""
    out = _loopback_verify({"entity_name": req.entity_name, "country_code": "GB"})
    results = []
    if out.get("verified") or out.get("found"):
        results.append({
            "company_number": out.get("company_number") or out.get("registration_number"),
            "title": out.get("legal_name") or out.get("entity_name"),
            "company_status": (out.get("status") or "").lower(),
            "company_type": out.get("company_type"),
            "date_of_creation": out.get("incorporated_on") or out.get("incorporation_date"),
            "address_snippet": out.get("registered_address"),
            "sic_codes": out.get("sic_codes") or [],
        })
    src = out.get("validation_source") or {}
    return {
        "source_id": "gb_companies_house",
        "source_url": src.get("primary_url") or src.get("url")
                      or f"https://find-and-update.company-information.service.gov.uk/search/companies?q={req.entity_name}",
        "fetched_at": _now_iso(),
        "total_results": len(results),
        "results": results,
        "raw_summary": out.get("summary"),
    }


@router.post("/sources/gb_companies_house/profile")
async def gb_companies_house_profile(req: CHProfileRequest):
    """Companies House profile by company_number. Loopback via /verify with
    company_number filled in — crawl-verify resolves the full record."""
    # Profile lookup via the same loopback. /verify requires entity_name, so
    # we pass the company_number as the name and rely on the number field
    # to drive the lookup. crawl-verify treats company_number as authoritative.
    out = _loopback_verify({
        "entity_name": req.company_number,
        "country_code": "GB",
        "company_number": req.company_number,
    })
    if not (out.get("verified") or out.get("found")):
        raise HTTPException(status_code=404, detail=f"company_number {req.company_number} not found")
    return {
        "source_id": "gb_companies_house",
        "source_url": f"https://find-and-update.company-information.service.gov.uk/company/{req.company_number}",
        "fetched_at": _now_iso(),
        "profile": {
            "company_number": out.get("company_number") or req.company_number,
            "company_name": out.get("legal_name"),
            "company_status": out.get("status"),
            "company_type": out.get("company_type"),
            "date_of_creation": out.get("incorporated_on") or out.get("incorporation_date"),
            "registered_office_address": out.get("registered_address"),
            "sic_codes": out.get("sic_codes") or [],
            "previous_names": out.get("previous_names") or [],
        },
    }


@router.post("/sources/gb_companies_house/psc")
async def gb_companies_house_psc(req: CHProfileRequest):
    """PSC list for a UK company. Companies House exposes the PSC register
    publicly at /company/<num>/persons-with-significant-control."""
    url = f"https://find-and-update.company-information.service.gov.uk/company/{req.company_number}/persons-with-significant-control"
    try:
        r = requests.get(url, headers={"User-Agent": _UA, "Accept": "text/html"}, timeout=20)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {str(e)[:200]}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail=f"company_number {req.company_number} not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"upstream {r.status_code}")

    # Conservative HTML parse — extract names + control natures from the
    # rendered PSC list block. Companies House renders each PSC in
    # <div class="appointment-1"> blocks with h2 name + ul of natures.
    import re
    psc_list = []
    blocks = re.findall(
        r'<h2[^>]*>\s*([^<]{2,200}?)\s*</h2>(.*?)(?=<h2[^>]*>|</main>|</body>)',
        r.text, re.DOTALL | re.IGNORECASE,
    )
    for name, body_html in blocks[:50]:
        natures = re.findall(r'<li[^>]*>\s*([^<]{3,200}?)\s*</li>', body_html, re.IGNORECASE)
        natures = [re.sub(r'\s+', ' ', n).strip() for n in natures if n.strip()]
        kind_m = re.search(r'(individual|corporate|legal)\s+person', body_html, re.IGNORECASE)
        nationality_m = re.search(r'Nationality\s*[:\-]?\s*([A-Za-z ]{3,40})', body_html, re.IGNORECASE)
        psc_list.append({
            "name": name.strip(),
            "kind": (kind_m.group(0).lower() if kind_m else None),
            "natures_of_control": natures[:10],
            "nationality": nationality_m.group(1).strip() if nationality_m else None,
        })
    return {
        "source_id": "gb_companies_house",
        "source_url": url,
        "fetched_at": _now_iso(),
        "total_results": len(psc_list),
        "psc": psc_list,
    }


# ---------------------------------------------------------------------------
# OpenSanctions
# ---------------------------------------------------------------------------

class OSSearchRequest(BaseModel):
    entity_name: str = Field(..., max_length=200)
    country: Optional[str] = Field(None, max_length=10)
    schema_: Optional[str] = Field("LegalEntity", alias="schema")
    limit: int = Field(10, ge=1, le=50)

    class Config:
        populate_by_name = True


@router.post("/sources/opensanctions/search")
async def opensanctions_search(req: OSSearchRequest):
    """Search OpenSanctions. /match is now paywalled — use the free /search
    endpoint (less precise, no scoring, but no key required)."""
    params = {"q": req.entity_name, "limit": req.limit}
    if req.country:
        params["countries"] = req.country.lower()
    if req.schema_:
        params["schema"] = req.schema_
    url = "https://api.opensanctions.org/search/sanctions"
    api_key = get_secret("opensanctions-api-key") or ""
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        if r.status_code == 401:
            return {
                "source_id": "opensanctions",
                "source_url": url,
                "fetched_at": _now_iso(),
                "total": 0, "results": [],
                "error": "OpenSanctions requires API key — set 'opensanctions-api-key' in crawlkeyvault",
            }
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("opensanctions search failed: %s", e)
        return {
            "source_id": "opensanctions",
            "source_url": url,
            "fetched_at": _now_iso(),
            "total": 0, "results": [],
            "error": str(e)[:200],
        }

    hits = data.get("results") or []
    results = []
    for h in hits:
        props = h.get("properties") or {}
        results.append({
            "id": h.get("id"),
            "caption": h.get("caption"),
            "schema": h.get("schema"),
            "datasets": h.get("datasets") or [],
            "topics": props.get("topics") or [],
        })
    return {
        "source_id": "opensanctions",
        "source_url": f"https://www.opensanctions.org/search/?q={req.entity_name}",
        "fetched_at": _now_iso(),
        "total": data.get("total", {}).get("value") if isinstance(data.get("total"), dict) else len(results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# OFSI Consolidated (HMG sanctions)
# ---------------------------------------------------------------------------

class OFSISearchRequest(BaseModel):
    entity_name: str = Field(..., max_length=200)
    entity_type: str = Field("entity", pattern=r"^(individual|entity|ship|aircraft)$")


@router.post("/sources/ofsi_consolidated/search")
async def ofsi_consolidated_search(req: OFSISearchRequest):
    """OFSI consolidated list via OpenSanctions gb_hmt_sanctions dataset
    (OS pulls it daily from HMG). Uses free /search endpoint."""
    schema = "LegalEntity" if req.entity_type == "entity" else "Person"
    url = "https://api.opensanctions.org/search/gb_hmt_sanctions"
    api_key = get_secret("opensanctions-api-key") or ""
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    try:
        r = requests.get(url,
                         params={"q": req.entity_name, "limit": 10, "schema": schema},
                         headers=headers, timeout=20)
        if r.status_code == 401:
            return {
                "source_id": "ofsi_consolidated",
                "source_url": url,
                "fetched_at": _now_iso(),
                "results": [],
                "error": "OpenSanctions requires API key — set 'opensanctions-api-key' in crawlkeyvault",
            }
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("ofsi search failed: %s", e)
        return {
            "source_id": "ofsi_consolidated",
            "source_url": url,
            "fetched_at": _now_iso(),
            "results": [],
            "error": str(e)[:200],
        }

    hits = data.get("results") or []
    results = []
    for h in hits:
        props = h.get("properties") or {}
        results.append({
            "name": h.get("caption"),
            "group_id": h.get("id"),
            "regime": (props.get("program") or [None])[0],
            "listed_on": (props.get("listingDate") or [None])[0],
            "last_updated": (props.get("modifiedAt") or [None])[0],
            "aliases": props.get("alias") or [],
        })
    return {
        "source_id": "ofsi_consolidated",
        "source_url": "https://www.gov.uk/government/publications/financial-sanctions-consolidated-list-of-targets",
        "fetched_at": _now_iso(),
        "results": results,
    }


# ---------------------------------------------------------------------------
# GLEIF LEI
# ---------------------------------------------------------------------------

class GLEIFRequest(BaseModel):
    entity_name: str = Field(..., max_length=200)
    country: Optional[str] = Field(None, max_length=10)


@router.post("/sources/gleif_lei/lookup")
async def gleif_lei_lookup(req: GLEIFRequest):
    """GLEIF LEI lookup. Wraps source_gleif.gleif_verify."""
    cc = (req.country or "GB").upper()
    try:
        out = source_gleif.gleif_verify(cc, req.entity_name)
    except Exception as e:
        log.warning("gleif lookup failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e)[:200])

    found = bool(out.get("verified") or out.get("found") or out.get("lei"))
    return {
        "source_id": "gleif_lei",
        "source_url": (out.get("validation_source") or {}).get("primary_url")
                      or "https://api.gleif.org/api/v1/lei-records",
        "fetched_at": _now_iso(),
        "found": found,
        "lei": out.get("lei"),
        "legal_name": out.get("legal_name"),
        "status": out.get("status"),
        "legal_address": out.get("registered_address") or out.get("legal_address"),
        "direct_parent": out.get("direct_parent_lei"),
        "ultimate_parent": out.get("ultimate_parent_lei"),
        "note": out.get("note"),
    }


# ---------------------------------------------------------------------------
# Collector lifecycle
# ---------------------------------------------------------------------------

@router.post("/evidence/runs/{run_id}/complete")
async def collector_complete(run_id: str):
    """Collector agent signals it's done. Transitions cir_runs.status to
    'extracting' so the claim_extractor agent can pick it up."""
    run = evidence_db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    evidence_db.update_run_status(run_id, "extracting")
    run_after = evidence_db.get_run(run_id)
    return {
        "run_id": run_id,
        "status": run_after["status"] if run_after else "extracting",
        "evidence_count": run_after["evidence_count"] if run_after else None,
    }
