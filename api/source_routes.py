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
import os
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import evidence_db
import source_gleif
import source_domain
import source_contact
import source_web
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


import re as _re_lf
# Multi-word legal-form phrases (Rosneft case: "Public Joint Stock Company …").
_LEGAL_PHRASES = _re_lf.compile(
    r"\b(public|open|closed)?\s*joint[-\s]stock\s+company\b"
    r"|\blimited\s+liability\s+(company|partnership)\b"
    r"|\bprivate\s+limited\b", _re_lf.IGNORECASE)
# Single legal-form tokens stripped anywhere in the name.
_LEGAL_TOKENS = {
    "pjsc", "ojsc", "cjsc", "jsc", "oao", "zao", "pao", "llc", "llp", "lp",
    "ltd", "ltda", "limited", "pvt", "plc", "inc", "incorporated", "corp",
    "corporation", "co", "company", "gmbh", "ag", "sa", "se", "nv", "bv", "oy",
    "ab", "asa", "spa", "srl", "sarl", "fze", "fzco", "fzllc", "pte", "sdn",
    "bhd", "kg", "kft", "doo", "aps", "tic", "san", "cia",
}


def _strip_legal_form(name: str) -> str:
    """Return `name` with legal-form phrases/tokens removed, for a second
    sanctions-screening pass. Guards against wrongly clearing a sanctioned
    entity submitted under its full legal name. Falls back to the original if
    stripping leaves too little to match on."""
    if not name:
        return name
    s = _LEGAL_PHRASES.sub(" ", name)
    toks = [t for t in _re_lf.split(r"[\s,\.]+", s)
            if t and t.lower() not in _LEGAL_TOKENS]
    out = " ".join(toks).strip(" ,.-&")
    return out if len(out) >= 3 else name


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
    headers = {"User-Agent": _UA, "Accept": "application/json",
               "subscription-key": key}

    def _csl_one(name):
        """One CSL query → (hits, error). Never raises."""
        params = {"name": name, "size": req.limit}
        if req.country:
            params["countries"] = req.country.upper()
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            return (r.json().get("results") or []), None
        except Exception as e:
            log.warning("csl search failed for %r: %s", name, e)
            return [], str(e)[:200]

    # Screen the name AS GIVEN and a LEGAL-FORM-STRIPPED variant, then union.
    # A sanctioned entity submitted under its full legal name (e.g. "Public Joint
    # Stock Company Rosneft Oil Company" or "PJSC Rosneft") must not clear just
    # because the legal-form prefix throws off the CSL fuzzy match — 'Rosneft'
    # hits, the full form does not. Recall is what matters here; the analyst
    # adjudicates the union.
    names = [req.entity_name]
    stripped = _strip_legal_form(req.entity_name)
    if stripped and stripped.lower() != req.entity_name.lower():
        names.append(stripped)

    merged, seen, err = [], set(), None
    for nm in names:
        hits, e = _csl_one(nm)
        if e and err is None:
            err = e
        for h in hits:
            k = h.get("id") or h.get("source_id") or (h.get("name"), h.get("source"))
            if k in seen:
                continue
            seen.add(k)
            merged.append({
                "id": h.get("id") or h.get("source_id"),
                "caption": h.get("name"),
                "schema": h.get("type"),
                "datasets": [h.get("source")] if h.get("source") else [],
                "topics": h.get("federal_register_notice") and ["sanction"] or [],
                "programs": h.get("programs") or [],
                "addresses": h.get("addresses") or [],
                "score": h.get("score"),
                "matched_query": nm,
            })
    out = {
        "source_id": "csl_screening",
        "source_url": f"https://search.api.trade.gov/consolidated_screening_list?name={req.entity_name}",
        "fetched_at": _now_iso(),
        "total": len(merged), "results": merged,
        "queries_screened": names,
    }
    if err and not merged:
        out["error"] = err
    return out


# ---------------------------------------------------------------------------
# OFSI Consolidated (HMG sanctions)
# ---------------------------------------------------------------------------

class OFSISearchRequest(BaseModel):
    entity_name: str = Field(..., max_length=200)
    entity_type: str = Field("entity", pattern=r"^(individual|entity|ship|aircraft)$")


# OFSI list cache: download once per process, refresh hourly. ~3-5 MB.
_OFSI_CACHE = {"fetched_at": 0, "entries": []}
_OFSI_TTL = 3600  # 1 hour
_OFSI_XML = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.xml"


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
# Domain trust — RDAP registry + website check with a name-match verdict
# ---------------------------------------------------------------------------

class DomainTrustRequest(BaseModel):
    entity_name: str = Field(..., max_length=500)
    domain: Optional[str] = Field(None, max_length=253)
    website: Optional[str] = Field(None, max_length=500)
    emails: Optional[list] = Field(None)


@router.post("/sources/domain_trust/check")
async def domain_trust_check(req: DomainTrustRequest):
    """RDAP registry + website check for a company's own domain(s), derived
    from {domain, website, emails}, with a name-match verdict
    (VERIFIED / REVIEW / UNVERIFIED). Reusable Tier-1 verification capability."""
    try:
        out = source_domain.check(
            req.entity_name, domain=req.domain,
            website=req.website, emails=req.emails,
        )
    except Exception as e:
        log.warning("domain_trust check failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e)[:200])
    out["fetched_at"] = _now_iso()
    return out


# ---------------------------------------------------------------------------
# Contact verify — phone + address country-consistency (companion to domain_trust)
# ---------------------------------------------------------------------------

class ContactVerifyRequest(BaseModel):
    entity_name: Optional[str] = Field(None, max_length=500)
    country_code: Optional[str] = Field(None, max_length=8)
    phones: Optional[list] = Field(None)
    addresses: Optional[list] = Field(None)
    emails: Optional[list] = Field(None)
    verify_emails: bool = Field(True)  # False on the recurring bulk scan (WhoisXML credit conservation)


@router.post("/sources/contact_verify/check")
async def contact_verify_check(req: ContactVerifyRequest):
    """Verify a counterparty's contact data off its docs: PHONE (valid + country
    matches the counterparty country) and ADDRESS (geolocates to that country).
    Reusable Tier-1 capability; the fraud LLM scores it, risk consumes it."""
    try:
        out = source_contact.check(
            entity_name=req.entity_name, country_code=req.country_code,
            phones=req.phones, addresses=req.addresses, emails=req.emails,
            verify_emails=req.verify_emails)
    except Exception as e:
        log.warning("contact_verify failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e)[:200])
    out["fetched_at"] = _now_iso()
    return out


# ---------------------------------------------------------------------------
# Generic country registry lookup — used by every per-country collector
# ---------------------------------------------------------------------------

class CountryRegRequest(BaseModel):
    entity_name: str = Field(..., max_length=500)
    registration_number: Optional[str] = Field(None, max_length=100)


@router.post("/sources/country_registry/lookup")
async def country_registry_lookup(req: CountryRegRequest, country: str):
    """Loopback to /api/v1/verify with the given country_code + cross-check
    against OpenCorporates as a secondary source (Flavor B). Returns:

    {
      "primary":    {<gov registry record>, source_id="<cc>_registry"},
      "aggregator": {<OpenCorporates record>, source_id="opencorporates"}   # only if OC token is set + OC returns data
    }

    The collector agent persists BOTH blocks as separate evidence rows with
    distinct source_ids — banker auditors can compare gov vs aggregator
    record per field.
    """
    cc = (country or "").upper().strip()
    if len(cc) != 2:
        raise HTTPException(status_code=400, detail="country must be ISO-2 code")

    # PRIMARY: gov registry via /verify loopback
    payload = {"entity_name": req.entity_name, "country_code": cc}
    if req.registration_number:
        payload["reg_number"] = req.registration_number
        payload["company_number"] = req.registration_number
        payload["cin"] = req.registration_number
    out = _loopback_verify(payload)
    vs = out.get("validation_source") or {}
    primary_block = {
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

    # AGGREGATOR (Flavor B): always cross-check OpenCorporates. Independent of
    # primary success — we want the dual-source comparison for audit. ~250ms
    # added latency per call. Returns None when OC has no record or token
    # missing; agent should skip the persist in that case.
    aggregator_block = None
    try:
        oc_key = get_secret("opencorporates-token") or ""
        if oc_key:
            oc_url = "https://api.opencorporates.com/v0.4/companies/search"
            oc_params = {
                "q": req.registration_number or req.entity_name,
                "jurisdiction_code": cc.lower(),
                "api_token": oc_key,
                "per_page": 3,
            }
            oc_r = requests.get(oc_url, params=oc_params,
                                headers={"User-Agent": _UA, "Accept": "application/json"},
                                timeout=15)
            if oc_r.status_code == 200:
                oc_data = oc_r.json()
                companies = (((oc_data.get("results") or {}).get("companies")) or [])
                if companies:
                    best = (companies[0] or {}).get("company") or {}
                    aggregator_block = {
                        "source_id": "opencorporates",
                        "source_url": best.get("opencorporates_url")
                                      or f"https://opencorporates.com/companies/{cc.lower()}",
                        "fetched_at": _now_iso(),
                        "found": True,
                        "legal_name": best.get("name"),
                        "status": best.get("current_status"),
                        "registration_number": best.get("company_number"),
                        "registration_date": best.get("incorporation_date"),
                        "company_type": best.get("company_type"),
                        "registered_address": (best.get("registered_address_in_full")
                                               or (best.get("registered_address") or {}).get("locality")),
                        "jurisdiction": (best.get("jurisdiction_code") or cc).upper(),
                        "all_matches_count": len(companies),
                        "validation_source": {
                            "primary": "OpenCorporates (paid API, cross-check tier)",
                            "primary_url": best.get("opencorporates_url"),
                            "confidence": "low",
                            "tier": "COMMERCIAL_AGGREGATOR",
                            "note": "Aggregator — useful for cross-checking gov registry data, not primary-tier source",
                        },
                    }
    except Exception as e:
        log.warning("OpenCorporates cross-check failed for %s/%s: %s",
                    cc, req.entity_name[:30], e)
        # aggregator_block stays None — primary still returns

    # COMMERCIAL (CN Tianyancha rich fields). verify_cn._enrich_from_detail_page
    # already harvests shareholders (+pct), actual controller, officers, capital,
    # business scope, adverse flags, former names, affiliates + branches — but the
    # primary_block projection above forwards none of them. Surface them as a
    # distinct evidence row tiered honestly as COMMERCIAL_AGGREGATOR (Tianyancha),
    # NOT laundered under the PRIMARY_GOVERNMENT gov-registry source_id.
    # CN-only today (the only collector that produces these); other countries
    # emit no commercial block (avoids an unknown source_id FK violation).
    commercial_block = None
    if cc == "CN":
        _rich = {k: out.get(k) for k in (
            "shareholders", "actual_controller", "officers", "business_scope",
            "industry", "adverse_flags", "former_names",
            "registered_capital_parsed", "address_parts", "affiliates", "branches",
        ) if out.get(k) not in (None, [], {}, "")}
        if _rich:
            commercial_block = {
                "source_id": "cn_tianyancha",
                "source_url": vs.get("url") or vs.get("primary_url")
                              or "https://www.tianyancha.com",
                "fetched_at": _now_iso(),
                "found": True,
                "legal_name": out.get("legal_name") or out.get("entity_name"),
                **_rich,
                "validation_source": {
                    "primary": "Tianyancha (commercial aggregator, cross-check tier)",
                    "primary_url": vs.get("url"),
                    "confidence": "medium",
                    "tier": "COMMERCIAL_AGGREGATOR",
                    "note": ("Tianyancha-derived shareholders/capital/affiliates — "
                             "corroborate against gov registry, do not treat as primary"),
                },
            }

    return {
        "primary": primary_block,
        "aggregator": aggregator_block,
        "commercial": commercial_block,
    }


# ---------------------------------------------------------------------------
# Website profile — SearXNG discovery + Crawl4AI fetch (free, self-hosted only)
# ---------------------------------------------------------------------------

class WebProfileRequest(BaseModel):
    entity_name: str = Field(..., max_length=500)
    country: Optional[str] = Field("", max_length=10)
    domain_hint: Optional[str] = Field("", max_length=253)


@router.post("/sources/web/profile")
async def web_profile(req: WebProfileRequest):
    """Discover the entity's official website via SearXNG and crawl it via
    Crawl4AI (both self-hosted on copapai-aux — no paid web APIs). Returns a
    light corporate profile {domain, description, products, leadership, contact,
    revenue_claims}. found=false (empty-source) when there is no site — the
    collector then persists an empty evidence row so nothing is fabricated."""
    try:
        return source_web.check(
            req.entity_name,
            country=(req.country or None),
            domain_hint=(req.domain_hint or None),
        )
    except Exception as e:
        log.warning("web_profile failed for %s: %s", req.entity_name[:40], e)
        return {"source_id": "web_profile", "source_url": "", "found": False,
                "reason": f"web_profile error: {str(e)[:160]}"}


# ---------------------------------------------------------------------------
# Dark-web umbrella scan — proxies to crawl-darkweb gateway (37 Tor sources)
# ---------------------------------------------------------------------------

_DARKWEB_BASE = os.environ.get("DARKWEB_BASE_URL",
    f"http://{os.environ.get('DARKWEB_VM_IP', '20.86.161.6')}:{os.environ.get('DARKWEB_VM_PORT', '8450')}")


class DarkwebScanRequest(BaseModel):
    entity_name: str = Field(..., max_length=500)
    country: Optional[str] = Field("", max_length=10)
    owners: Optional[list[str]] = Field(default_factory=list)
    domain: Optional[str] = Field("")
    depth: Optional[str] = Field("heavy")


@router.post("/sources/darkweb/scan")
async def darkweb_scan(req: DarkwebScanRequest):
    """Fan-out scan across the 37 dark-web/OSINT sources hosted on the
    crawl-darkweb VM (Tor exit, NL). Synchronous wrapper — submits to
    the darkweb gateway, waits for completion, returns per-source
    findings + summary. Single evidence row per scan, source_id='darkweb_screen'."""
    dw_key = get_secret("darkweb-api-key") or "dwk_crawl_2026Q2_f8a3b7e1d9c4"
    payload = {
        "entity_name": req.entity_name,
        "country": req.country or "",
        "owners": req.owners or [],
        "domain": req.domain or "",
        "depth": req.depth or "heavy",
    }
    url = f"{_DARKWEB_BASE}/api/v1/research"
    try:
        r = requests.post(url, json=payload,
                          headers={"X-API-Key": dw_key, "Content-Type": "application/json"},
                          timeout=300)
    except Exception as e:
        log.warning("darkweb scan failed: %s", e)
        return {"source_id": "darkweb_screen", "source_url": url,
                "fetched_at": _now_iso(), "summary": {}, "findings": [],
                "error": f"darkweb gateway unreachable: {str(e)[:200]}"}
    if r.status_code != 200:
        return {"source_id": "darkweb_screen", "source_url": url,
                "fetched_at": _now_iso(), "summary": {}, "findings": [],
                "error": f"darkweb gateway returned {r.status_code}"}

    data = r.json()
    summary = data.get("summary", {}) or {}
    findings = data.get("findings", []) or []
    blob_path = data.get("blob_path", "")

    # If full results sit in the blob, try to fetch them for richer evidence
    if blob_path:
        try:
            sas = get_secret("blob-sas-token") or ""
            blob_account = os.environ.get("BLOB_ACCOUNT", "stcrawlosint")
            blob_container = os.environ.get("BLOB_CONTAINER", "osint-staging")
            clean = blob_path.replace(f"{blob_container}/", "", 1)
            blob_url = f"https://{blob_account}.blob.core.windows.net/{blob_container}/{clean}?{sas}"
            br = requests.get(blob_url, timeout=30)
            if br.status_code == 200 and br.text.strip():
                full = br.json()
                summary = full.get("summary", summary)
                findings = full.get("findings", findings)
        except Exception:
            pass

    # Group findings by source for an audit-friendly breakdown
    findings_by_source = {}
    for f in findings:
        src = f.get("source") or "unknown"
        findings_by_source.setdefault(src, []).append({
            "type": f.get("type"),
            "title": (f.get("title") or "")[:300],
            "url": f.get("url"),
            "summary": (f.get("summary") or "")[:400],
            "risk_level": f.get("risk_level"),
        })

    return {
        "source_id": "darkweb_screen",
        "source_url": url,
        "fetched_at": _now_iso(),
        "summary": {
            "total_findings": summary.get("total_findings") or len(findings),
            "sources_searched": summary.get("sources_searched"),
            "sources_with_results": summary.get("sources_with_results") or len(findings_by_source),
            "alert_level": summary.get("alert_level"),
            "tor_exit_ip": summary.get("tor_exit_ip"),
        },
        "findings_by_source": findings_by_source,
        "findings": findings[:50],  # cap for evidence row size
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
