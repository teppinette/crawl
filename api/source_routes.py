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
    """Sanctions screening via US Consolidated Screening List (CSL) — the
    keyed alternative to OpenSanctions /match. CSL aggregates OFAC SDN,
    BIS Denied Persons / Entity List / MEU, State ITAR + AECA debarred,
    plus selected EU/UN/UK records. Free API, US Trade Department
    (api.trade.gov). Auth via the csl-subscription-key in crawlkeyvault.

    Returns source_id="csl_screening" so the evidence row points at the
    actual upstream (not OpenSanctions). Route name kept as
    /sources/opensanctions/search for tool-spec compatibility with the
    existing agent YAML."""
    key = get_secret("csl-subscription-key") or ""
    url = "https://data.trade.gov/consolidated_screening_list/v1/search"
    if not key:
        return {
            "source_id": "csl_screening",
            "source_url": url,
            "fetched_at": _now_iso(),
            "total": 0, "results": [],
            "error": "csl-subscription-key missing from crawlkeyvault",
        }
    params = {"name": req.entity_name, "size": req.limit}
    if req.country:
        params["countries"] = req.country.upper()
    headers = {"User-Agent": _UA, "Accept": "application/json",
               "subscription-key": key}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("csl search failed: %s", e)
        return {
            "source_id": "csl_screening",
            "source_url": url,
            "fetched_at": _now_iso(),
            "total": 0, "results": [],
            "error": str(e)[:200],
        }

    hits = data.get("results") or []
    results = []
    for h in hits:
        results.append({
            "id": h.get("id") or h.get("source_id"),
            "caption": h.get("name"),
            "schema": h.get("type"),  # Individual / Entity / Vessel / Aircraft
            "datasets": [h.get("source")] if h.get("source") else [],
            "topics": h.get("federal_register_notice") and ["sanction"] or [],
            "programs": h.get("programs") or [],
            "addresses": h.get("addresses") or [],
            "score": h.get("score"),
        })
    return {
        "source_id": "csl_screening",
        "source_url": f"https://search.api.trade.gov/consolidated_screening_list?name={req.entity_name}",
        "fetched_at": _now_iso(),
        "total": data.get("total") or len(results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# OFSI Consolidated (HMG sanctions)
# ---------------------------------------------------------------------------

class OFSISearchRequest(BaseModel):
    entity_name: str = Field(..., max_length=200)
    entity_type: str = Field("entity", pattern=r"^(individual|entity|ship|aircraft)$")


# OFSI list cache: download once per process, refresh hourly. ~3-5 MB.
_OFSI_CACHE = {"fetched_at": 0, "entries": []}
_OFSI_TTL = 3600  # 1 hour
_OFSI_XML = "https://ofsistorage.blob.core.windows.net/publishlive/ConList.xml"


def _ofsi_refresh():
    """Download OFSI ConList.xml from HMG. PRIMARY_GOVERNMENT source — no
    intermediary. Returns list of {name, aliases, listed_on, regime}."""
    import xml.etree.ElementTree as ET
    try:
        r = requests.get(_OFSI_XML, headers={"User-Agent": _UA}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.warning("ofsi xml fetch failed: %s", e)
        return []

    entries = []
    # OFSI XML: each <FinancialSanctionsTarget> has Names, Type, GroupTypeDescription,
    # plus a parent <DesignationDetails> with regime + listed_on.
    for tgt in root.iter():
        if not tgt.tag.endswith("FinancialSanctionsTarget"):
            continue
        names = []
        ttype = None
        regime = None
        listed_on = None
        last_upd = None
        for child in tgt.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag in ("Name6", "Name1", "Name2", "FullName"):
                if child.text and child.text.strip():
                    names.append(child.text.strip())
            elif tag == "AliasTypeName" and child.text:
                names.append(child.text.strip())
            elif tag == "GroupTypeDescription" and child.text:
                ttype = child.text.strip()
            elif tag == "RegimeName" and child.text:
                regime = child.text.strip()
            elif tag == "ListedOn" and child.text:
                listed_on = child.text.strip()
            elif tag == "LastUpdated" and child.text:
                last_upd = child.text.strip()
        if names:
            entries.append({
                "name": names[0],
                "aliases": names[1:],
                "type": ttype,
                "regime": regime,
                "listed_on": listed_on,
                "last_updated": last_upd,
            })
    return entries


def _ofsi_entries():
    now = time.time() if (time := __import__("time")) else 0
    if now - _OFSI_CACHE["fetched_at"] > _OFSI_TTL:
        entries = _ofsi_refresh()
        if entries:
            _OFSI_CACHE["entries"] = entries
            _OFSI_CACHE["fetched_at"] = now
    return _OFSI_CACHE["entries"]


@router.post("/sources/ofsi_consolidated/search")
async def ofsi_consolidated_search(req: OFSISearchRequest):
    """OFSI Consolidated List — direct download from HMG (PRIMARY_GOVERNMENT).
    Cached per process for 1 hour. Case-insensitive substring match on name
    + aliases. No API key required — primary source, free."""
    q = (req.entity_name or "").strip().lower()
    if not q:
        return {"source_id": "ofsi_consolidated", "source_url": _OFSI_XML,
                "fetched_at": _now_iso(), "results": [],
                "error": "entity_name required"}
    entries = _ofsi_entries()
    if not entries:
        return {"source_id": "ofsi_consolidated", "source_url": _OFSI_XML,
                "fetched_at": _now_iso(), "results": [],
                "error": "OFSI XML fetch failed or empty"}
    results = []
    for e in entries:
        candidates = [e["name"]] + (e.get("aliases") or [])
        if any(q in c.lower() for c in candidates if c):
            results.append({
                "name": e["name"],
                "aliases": (e.get("aliases") or [])[:10],
                "type": e.get("type"),
                "regime": e.get("regime"),
                "listed_on": e.get("listed_on"),
                "last_updated": e.get("last_updated"),
            })
            if len(results) >= 25:
                break
    return {
        "source_id": "ofsi_consolidated",
        "source_url": _OFSI_XML,
        "fetched_at": _now_iso(),
        "total_targets_scanned": len(entries),
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
# Generic country registry lookup — used by every per-country collector
# ---------------------------------------------------------------------------

class CountryRegRequest(BaseModel):
    entity_name: str = Field(..., max_length=500)
    registration_number: Optional[str] = Field(None, max_length=100)


@router.post("/sources/country_registry/lookup")
async def country_registry_lookup(req: CountryRegRequest, country: str):
    """Loopback to /api/v1/verify with the given country_code. Source attribution
    captured in extracted.validation_source. Returns source_id="<cc>_registry"
    keyed to the per-country sources_catalog entry."""
    cc = (country or "").upper().strip()
    if len(cc) != 2:
        raise HTTPException(status_code=400, detail="country must be ISO-2 code")
    payload = {"entity_name": req.entity_name, "country_code": cc}
    if req.registration_number:
        # The /verify dispatcher reads multiple aliases — reg_number is the
        # canonical generic one (CIN for IN, USCC for CN, company_number for GB).
        payload["reg_number"] = req.registration_number
        payload["company_number"] = req.registration_number
        payload["cin"] = req.registration_number
    out = _loopback_verify(payload)
    vs = out.get("validation_source") or {}
    return {
        "source_id": f"{cc.lower()}_registry",
        "source_url": vs.get("primary_url") or vs.get("url")
                      or f"https://crawl-verify-gateway/{cc.lower()}",
        "fetched_at": _now_iso(),
        "found": bool(out.get("verified") or out.get("found")),
        "legal_name": out.get("legal_name") or out.get("entity_name"),
        "status": out.get("status"),
        "registration_number": (out.get("registration_number")
                                or out.get("company_number") or out.get("cin")
                                or out.get("uscc")),
        "registration_date": (out.get("incorporated_on")
                              or out.get("incorporation_date")
                              or out.get("established_date")),
        "registered_address": out.get("registered_address") or out.get("address"),
        "directors": out.get("directors") or out.get("partners") or [],
        "validation_source": vs,
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
