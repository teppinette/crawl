"""
Colombia company verification via GLEIF LEI Registry.

Primary source: GLEIF LEI (https://api.gleif.org/api/v1/lei-records)
  - Free public REST API, no auth required (ISO 17442 standard)
  - Covers Colombian companies with LEIs (banks, listed companies, large corporates)
  - Returns: LEI, legal name, status, address, jurisdiction, NIT (in registeredAs)

Note on RUES: The Confecámaras RUES platform (www.rues.org.co) is a SPA backed
by an authenticated API at ruesapi.rues.org.co — it returns HTTP 401 without a
Confecámaras-issued auth token. Token issuance is restricted to Colombian chambers
of commerce and not available for third-party verification. GLEIF is the
bank-grade alternative.

Input: entity_name (search by name) or nit (9-digit NIT, optionally with check digit)
Returns: legal_name, lei, nit (from registeredAs), status, jurisdiction, address.
"""

import logging
import re
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"

# NIT: 9 digits, optional hyphen + check digit
_NIT_RE = re.compile(r"^(\d{9})(?:-?(\d))?$")


def init(get_secret=None):
    log.info("CO verification ready (GLEIF LEI — RUES API requires Confecámaras auth token)")


def _clean_nit(nit: str) -> str:
    clean = re.sub(r"[.\s-]", "", nit.strip())
    m = _NIT_RE.match(clean)
    if m:
        return m.group(1)
    digits = re.sub(r"\D", "", clean)
    return digits[:9] if len(digits) >= 9 else ""


def rues_verify(entity_name: str, nit: str = "") -> dict:
    """Verify a Colombian company via GLEIF LEI Registry."""
    if not entity_name and not nit:
        return {"found": False, "error": "entity_name or nit required"}

    try:
        return _gleif_search(entity_name, nit)
    except Exception as e:
        log.error("CO GLEIF error for %s: %s", entity_name or nit, e)
        return {"entity_name": entity_name, "nit": nit, "found": False, "error": str(e)[:300]}


def _gleif_search(entity_name: str, nit: str) -> dict:
    """Search GLEIF for CO entities by NIT (registeredAs) or name."""
    base_params = {
        "filter[entity.legalAddress.country]": "CO",
        "page[size]": "10",
    }
    headers = {"Accept": "application/vnd.api+json"}

    # Try NIT first — GLEIF stores Colombian NITs with the check digit (e.g. "899999068-1")
    if nit:
        clean = _clean_nit(nit)
        candidates = []
        if clean:
            # Strip any provided check digit, then try common formats
            raw = re.sub(r"\D", "", nit)
            if len(raw) >= 10:
                # User gave NIT + check digit (e.g. "8999990681" or "899999068-1")
                candidates.append(f"{raw[:9]}-{raw[9]}")
            candidates.append(clean)  # 9-digit NIT alone
            candidates.append(re.sub(r"\s", "", nit.strip()))  # As provided

        for cand in dict.fromkeys(candidates):  # dedupe preserving order
            params = dict(base_params)
            params["filter[entity.registeredAs]"] = cand
            result = mlx_get(_GLEIF_URL, params=params, headers=headers,
                             timeout=15, country_code="co")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_gleif(records, entity_name, nit)

    # Try name search
    if entity_name:
        for filter_key in ("filter[entity.legalName]", "filter[fulltext]"):
            params = dict(base_params)
            params[filter_key] = entity_name
            result = mlx_get(_GLEIF_URL, params=params, headers=headers,
                             timeout=15, country_code="co")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_gleif(records, entity_name, nit)

    return {
        "entity_name": entity_name,
        "country_code": "CO",
        "nit": nit or None,
        "found": False,
        "status": "NOT_FOUND",
        "note": (
            "GLEIF covers Colombian companies with Legal Entity Identifiers (banks, "
            "listed companies, large corporates). Smaller entities without LEIs are "
            "not covered. RUES API access is restricted to Colombian chambers of commerce."
        ),
        "validation_source": _gleif_source(entity_name or nit),
    }


def _format_gleif(records: list, query_name: str, query_nit: str) -> dict:
    top = records[0]
    attrs = top["attributes"]
    entity = attrs["entity"]
    lei = attrs["lei"]

    legal_name = entity["legalName"]["name"]
    status = entity.get("status", "UNKNOWN")
    reg_as = entity.get("registeredAs", "") or ""

    addr = entity.get("legalAddress", {}) or {}
    addr_lines = addr.get("addressLines", []) or []
    full_addr = ", ".join(filter(None, addr_lines + [
        addr.get("city", ""), addr.get("region", ""),
        addr.get("postalCode", ""), addr.get("country", "CO"),
    ]))

    legal_form = entity.get("legalForm", {}) or {}
    legal_form_str = legal_form.get("other") or legal_form.get("id") or ""

    others = []
    for rec in records[1:5]:
        e = rec["attributes"]["entity"]
        others.append({
            "name": e["legalName"]["name"],
            "lei": rec["attributes"]["lei"],
            "nit": e.get("registeredAs"),
            "status": e.get("status", "UNKNOWN"),
        })

    return {
        "entity_name": legal_name,
        "query_name": query_name,
        "country_code": "CO",
        "found": True,
        "lei": lei,
        "nit": reg_as or query_nit or None,
        "status": status,
        "legal_form": legal_form_str or None,
        "registered_address": full_addr or None,
        "jurisdiction": entity.get("jurisdiction", "CO"),
        "total_matches": len(records),
        "other_matches": others if others else None,
        "source": "GLEIF LEI Registry (RUES API requires Confecámaras auth)",
        "validation_source": _gleif_source(query_name or query_nit, lei),
    }


def _gleif_source(query: str, lei: str = "") -> dict:
    return {
        "registry": "GLEIF — Global Legal Entity Identifier Foundation (ISO 17442)",
        "url": "https://search.gleif.org/",
        "api": _GLEIF_URL,
        "how_to_reproduce": (
            f"GLEIF record: https://search.gleif.org/#/record/{lei}"
            if lei else
            f"GLEIF search: {_GLEIF_URL}?filter[fulltext]={query}"
            f"&filter[entity.legalAddress.country]=CO"
        ),
        "limitations": (
            "GLEIF covers CO companies with Legal Entity Identifiers (banks, listed "
            "companies, large corporates). Smaller entities without LEIs are not covered."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
