"""
Egypt company verification via GLEIF LEI Registry + GAFI Commercial Register.

Primary source: GLEIF (Global Legal Entity Identifier Foundation)
  - URL: https://api.gleif.org/api/v1/lei-records
  - Free, no auth, no rate limit enforced. ~322 Egyptian entities with LEI.
  - Returns: legal name (AR+EN), LEI, status, commercial reg number,
    registered address, legal form, creation date.

GAFI portal (www.gafi.gov.eg): SharePoint-based, no machine-readable API.
  Requires Multilogin anti-detect browser — not implemented here.

ETA e-Invoice API (sdk.invoicing.eta.gov.eg): Requires tax authority credentials
  and a registered taxpayer account — not publicly accessible.

OpenCorporates: EG is partially indexed (some GCR records), but requires
  a paid API token. If an opencorporates-token secret is configured it is
  attempted as a secondary fallback.

Verdict: GLEIF is the only reliable, free, machine-readable source for Egypt.
  Coverage is limited to entities that have voluntarily obtained an LEI
  (~322 Egyptian entities as of May 2026, mainly banks, large corporates).
  For smaller entities, the adapter returns found=False with a manual
  verification note pointing to gafi.gov.eg.

Input: entity_name (name search) or commercial_reg (CR number lookup via
  GLEIF registeredAs field — only works if the entity has an LEI).
Returns: entity_name, country_code="EG", found, commercial_reg, legal_name,
         status, registered_address, validation_source.
"""

import logging
import re
import time
from urllib.parse import quote

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"
_GLEIF_SEARCH_URL = "https://api.gleif.org/api/v1/lei-records"
_OC_URL = "https://api.opencorporates.com/v0.4/companies/search"

_OC_TOKEN = ""


def init(get_secret):
    global _OC_TOKEN
    _OC_TOKEN = get_secret("opencorporates-token") or ""
    if _OC_TOKEN:
        log.info("EG verify ready: GLEIF (primary) + OpenCorporates (secondary, token configured)")
    else:
        log.info("EG verify ready: GLEIF (primary). OpenCorporates not configured "
                 "(set opencorporates-token in Key Vault for broader coverage)")


def gafi_verify(entity_name: str, commercial_reg: str = "") -> dict:
    """
    Verify an Egyptian company via GLEIF LEI Registry.

    Searches by legal name or, if provided, by commercial registration number
    (matched against the GLEIF registeredAs field).

    entity_name:    Company name (English or Arabic)
    commercial_reg: Egyptian Commercial Registration number (optional)

    Returns dict with: entity_name, country_code, found, commercial_reg,
                        legal_name, status, registered_address, validation_source
    """
    if not entity_name and not commercial_reg:
        return {"found": False, "error": "entity_name or commercial_reg required"}

    try:
        # Prefer CR lookup when provided — more precise
        if commercial_reg:
            result = _gleif_lookup_by_cr(commercial_reg.strip(), entity_name)
        else:
            result = _gleif_lookup_by_name(entity_name.strip())

        # If GLEIF found nothing and we have an OC token, try OpenCorporates
        if not result.get("found") and _OC_TOKEN and entity_name:
            oc = _oc_lookup(entity_name.strip(), commercial_reg)
            if oc.get("found"):
                return oc

        return result

    except Exception as e:
        log.error("EG verify error for %s / CR %s: %s", entity_name, commercial_reg, e)
        return {
            "entity_name": entity_name,
            "country_code": "EG",
            "found": False,
            "error": str(e)[:300],
        }


# ---------------------------------------------------------------------------
# GLEIF — primary source
# ---------------------------------------------------------------------------

def _gleif_lookup_by_name(name: str) -> dict:
    """Search GLEIF by legal name, filtered to EG jurisdiction."""
    log.info("EG GLEIF name search: %s", name[:60])
    params = {
        "filter[entity.legalName]": name,
        "filter[entity.legalAddress.country]": "EG",
        "page[size]": "10",
    }
    result = mlx_get(_GLEIF_URL, params=params, headers={"Accept": "application/vnd.api+json"}, timeout=60, country_code="eg")
    if not result.get("ok"):
        raise RuntimeError(f"GLEIF HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or {}
    records = data.get("data", [])
    total = data.get("meta", {}).get("pagination", {}).get("total", 0)

    if not records:
        return _not_found(name, "", "GLEIF name search returned no results")

    best = records[0]
    return _format_gleif(best, name, "", total, records[:5])


def _gleif_lookup_by_cr(cr: str, entity_name: str) -> dict:
    """Search GLEIF by commercial registration number (registeredAs field)."""
    log.info("EG GLEIF CR search: %s", cr)
    # GLEIF registeredAs filter: exact match
    params = {
        "filter[entity.registeredAs]": cr,
        "filter[entity.legalAddress.country]": "EG",
        "page[size]": "5",
    }
    result = mlx_get(_GLEIF_URL, params=params, headers={"Accept": "application/vnd.api+json"}, timeout=60, country_code="eg")
    if not result.get("ok"):
        raise RuntimeError(f"GLEIF HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or {}
    records = data.get("data", [])
    total = data.get("meta", {}).get("pagination", {}).get("total", 0)

    if records:
        return _format_gleif(records[0], entity_name, cr, total, records[:5])

    # CR not found in GLEIF — fall back to name search if name provided
    if entity_name:
        log.info("EG GLEIF CR %s not found — falling back to name search", cr)
        result = _gleif_lookup_by_name(entity_name)
        # Attach the CR we searched for even if GLEIF didn't know it
        if not result.get("commercial_reg") and cr:
            result["commercial_reg_searched"] = cr
            result["note"] = (
                "Commercial registration number not found in GLEIF. "
                "Name search result shown. Verify CR directly at gafi.gov.eg."
            )
        return result

    return _not_found(entity_name, cr, f"CR {cr} not found in GLEIF")


def _format_gleif(record: dict, query_name: str, query_cr: str,
                  total: int, top_records: list) -> dict:
    """Format a GLEIF API record into the standard verification response."""
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    registration = attrs.get("registration", {})

    lei = attrs.get("lei", "")

    # Legal name — prefer English, fall back to Arabic
    legal_name_obj = entity.get("legalName", {})
    legal_name = legal_name_obj.get("name", "")

    # Transliterated names (ASCII versions of Arabic names)
    transliterated = entity.get("transliteratedOtherNames", [])
    ascii_name = ""
    for t in transliterated:
        if t.get("type") == "PREFERRED_ASCII_TRANSLITERATED_LEGAL_NAME":
            ascii_name = t.get("name", "")
            break

    display_name = legal_name or ascii_name

    # Status (entity status, not LEI registration status)
    entity_status = entity.get("status", "")
    status_map = {
        "ACTIVE": "ACTIVE",
        "INACTIVE": "INACTIVE",
        "NULL": "UNKNOWN",
    }
    status = status_map.get(entity_status, entity_status or "UNKNOWN")

    # Commercial registration number
    commercial_reg = entity.get("registeredAs", "") or query_cr

    # Legal form
    legal_form_obj = entity.get("legalForm", {})
    legal_form = legal_form_obj.get("other", "") or legal_form_obj.get("id", "")

    # Registered address
    addr_obj = entity.get("legalAddress", {})
    addr_lines = addr_obj.get("addressLines", [])
    city = addr_obj.get("city", "")
    postal = addr_obj.get("postalCode", "")
    region = addr_obj.get("region", "")
    addr_parts = [l for l in addr_lines if l] + ([city] if city else [])
    if postal:
        addr_parts.append(postal)
    registered_address = ", ".join(addr_parts) if addr_parts else None

    # Headquarters address (if different)
    hq_obj = entity.get("headquartersAddress", {})
    hq_lines = hq_obj.get("addressLines", [])
    hq_city = hq_obj.get("city", "")
    hq_parts = [l for l in hq_lines if l] + ([hq_city] if hq_city else [])
    hq_address = ", ".join(hq_parts) if hq_parts else None
    if hq_address == registered_address:
        hq_address = None  # Don't duplicate

    # Creation date
    creation_date_raw = entity.get("creationDate", "")
    creation_date = creation_date_raw[:10] if creation_date_raw else None

    # LEI registration metadata
    lei_status = registration.get("status", "")
    last_update = registration.get("lastUpdateDate", "")
    last_update_clean = last_update[:10] if last_update else None

    # Other matches
    other_matches = []
    for r in top_records[1:]:
        a = r.get("attributes", {})
        e = a.get("entity", {})
        n = e.get("legalName", {})
        other_matches.append({
            "lei": a.get("lei", ""),
            "legal_name": n.get("name", ""),
            "status": e.get("status", ""),
            "registered_as": e.get("registeredAs", ""),
        })

    return {
        "entity_name": display_name,
        "query_name": query_name or None,
        "country_code": "EG",
        "found": True,
        "status": status,
        "lei": lei or None,
        "lei_status": lei_status or None,
        "commercial_reg": commercial_reg or None,
        "legal_name": display_name or None,
        "legal_name_arabic": ascii_name if legal_name_obj.get("language") == "ar" else None,
        "legal_form": legal_form or None,
        "registered_address": registered_address,
        "headquarters_address": hq_address,
        "city": city or None,
        "region": region or None,
        "creation_date": creation_date,
        "lei_last_updated": last_update_clean,
        "total_gleif_matches": total,
        "other_matches": other_matches if other_matches else None,
        "source": "GLEIF (Global Legal Entity Identifier Foundation), Egypt",
        "coverage_note": (
            "GLEIF covers ~322 Egyptian entities (banks, large corporates) that have "
            "voluntarily registered an LEI. For full coverage use gafi.gov.eg."
        ),
        "validation_source": _validation_source(lei, display_name, commercial_reg, query_name),
    }


# ---------------------------------------------------------------------------
# OpenCorporates — secondary fallback (requires paid token)
# ---------------------------------------------------------------------------

def _oc_lookup(entity_name: str, commercial_reg: str) -> dict:
    """OpenCorporates EG search (requires api_token)."""
    log.info("EG OpenCorporates fallback for: %s", entity_name[:60])
    params = {
        "q": entity_name,
        "jurisdiction_code": "eg",
        "api_token": _OC_TOKEN,
        "per_page": "5",
    }
    result = mlx_get(_OC_URL, params=params, timeout=60, country_code="eg")
    if result.get("status_code") == 401:
        log.warning("EG OpenCorporates: invalid or expired token")
        return _not_found(entity_name, commercial_reg, "OpenCorporates token invalid")
    if result.get("status_code") == 404:
        return _not_found(entity_name, commercial_reg, "Not found in OpenCorporates")
    if not result.get("ok"):
        raise RuntimeError(f"OC HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")

    data = result.get("json") or {}
    companies = (
        data.get("results", {})
            .get("companies", [])
    )
    if not companies:
        return _not_found(entity_name, commercial_reg, "Not found in OpenCorporates")

    best = companies[0].get("company", {})
    name = best.get("name", "")
    number = best.get("company_number", "")
    status = best.get("current_status", "unknown").upper()
    inc_date = (best.get("incorporation_date") or "")[:10]
    address_raw = best.get("registered_address", {}) or {}
    addr = ", ".join(filter(None, [
        address_raw.get("street_address", ""),
        address_raw.get("locality", ""),
        address_raw.get("region", ""),
        address_raw.get("postal_code", ""),
    ]))
    oc_url = best.get("opencorporates_url", "")

    return {
        "entity_name": name,
        "query_name": entity_name,
        "country_code": "EG",
        "found": True,
        "status": status,
        "commercial_reg": number or commercial_reg or None,
        "legal_name": name or None,
        "registered_address": addr or None,
        "incorporation_date": inc_date or None,
        "source": "OpenCorporates (EG — General Commercial Register), Egypt",
        "coverage_note": (
            "OpenCorporates EG covers some GAFI/GCR records but is not exhaustive. "
            "Source is not the primary government registry."
        ),
        "validation_source": {
            "registry": "General Commercial Register (GCR) via OpenCorporates, Egypt",
            "url": oc_url or "https://opencorporates.com/companies/eg",
            "record_id": number or None,
            "how_to_reproduce": (
                f"Visit opencorporates.com/companies/eg → "
                f"Search: {entity_name} → View company record"
            ),
            "authoritative": False,
            "note": "OpenCorporates is a third-party aggregator. "
                    "For authoritative data use gafi.gov.eg directly.",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _not_found(entity_name: str, commercial_reg: str, reason: str = "") -> dict:
    """Standard NOT_FOUND response with manual verification guidance."""
    return {
        "entity_name": entity_name,
        "country_code": "EG",
        "found": False,
        "status": "NOT_FOUND",
        "commercial_reg": commercial_reg or None,
        "note": (
            "Entity not found in GLEIF. "
            "GLEIF coverage for Egypt is limited to ~322 entities with an LEI "
            "(mainly banks and large corporates). "
            "For full commercial registry access, visit gafi.gov.eg manually. "
            f"Reason: {reason}" if reason else
            "For full commercial registry access, visit gafi.gov.eg manually."
        ),
        "manual_verification": {
            "primary": "https://www.gafi.gov.eg/English/Epractices/Pages/default.aspx",
            "description": "GAFI (General Authority for Investment) — company search requires portal access (no public API)",
            "tax_authority": "https://www.eta.gov.eg/",
            "description_tax": "Egyptian Tax Authority (ETA) — Tax Registration Number (TRN) lookup",
        },
        "source": "GLEIF (Global Legal Entity Identifier Foundation), Egypt",
        "validation_source": _validation_source("", entity_name, commercial_reg, entity_name),
    }


def _validation_source(lei: str, legal_name: str, commercial_reg: str,
                        query: str) -> dict:
    gleif_url = (
        f"https://www.gleif.org/en/lei/{lei}"
        if lei else
        "https://www.gleif.org/en/lei-data/global-lei-index/lei-search"
    )
    search_desc = (
        f"Visit gleif.org → LEI Search → "
        f"Filter by country=EG → Search: {query or legal_name}"
    )
    if lei:
        search_desc = f"Visit gleif.org/en/lei/{lei}"
    return {
        "registry": "GLEIF — Global Legal Entity Identifier Foundation (GAFI-issued LEIs), Egypt",
        "url": gleif_url,
        "api": "https://api.gleif.org/api/v1/lei-records",
        "record_id": lei or commercial_reg or None,
        "authoritative": True,
        "how_to_reproduce": search_desc,
        "note": (
            "GLEIF data is corroborated against GAFI (RA000179) for Egyptian entities. "
            "registeredAs field contains the Egyptian commercial registration number."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
