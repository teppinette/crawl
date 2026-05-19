"""
Chile company verification via GLEIF LEI Registry.

Primary source: GLEIF LEI (https://api.gleif.org/api/v1/lei-records)
  - Free public REST API, no auth required (ISO 17442 standard)
  - Covers Chilean companies with LEIs (banks, listed companies, large corporates)
  - Returns: LEI, legal name, status, address, jurisdiction, RUT (in registeredAs)

Note on SII: The legacy SII RUT lookup (zeus.sii.cl/cvc_cgi/stc/getstc) was
deprecated in 2024 and now returns HTTP 500. SII's modern public endpoints
require Clave Tributaria authentication and cannot be used for third-party
verification. GLEIF is the bank-grade alternative.

Input: entity_name (search by name) or rut (RUT format like 76123456-7)
Returns: legal_name, lei, rut (from registeredAs), status, jurisdiction, address.
"""

import logging
import re
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"

# RUT: 7-8 digits + hyphen + check digit (0-9 or K)
_RUT_RE = re.compile(r"^(\d{1,8})-?([\dkK])$")


def init(get_secret=None):
    log.info("CL verification ready (GLEIF LEI — SII public lookup deprecated)")


def _format_rut(rut: str) -> str:
    """Normalize RUT to XX.XXX.XXX-D format if possible."""
    clean = re.sub(r"[.\s]", "", rut.strip().upper())
    m = _RUT_RE.match(clean)
    if not m:
        return rut
    body, dv = m.group(1), m.group(2)
    b = body.zfill(8)
    return f"{int(b):,}".replace(",", ".") + f"-{dv}"


def sii_rut_verify(entity_name: str, rut: str = "") -> dict:
    """Verify a Chilean company via GLEIF LEI Registry."""
    if not entity_name and not rut:
        return {"found": False, "error": "entity_name or rut required"}

    try:
        return _gleif_search(entity_name, rut)
    except Exception as e:
        log.error("CL GLEIF error for %s: %s", entity_name or rut, e)
        return {"entity_name": entity_name, "rut": rut, "found": False, "error": str(e)[:300]}


def _gleif_search(entity_name: str, rut: str) -> dict:
    """Search GLEIF for CL entities by RUT (registeredAs) or name."""
    base_params = {
        "filter[entity.legalAddress.country]": "CL",
        "page[size]": "10",
    }
    headers = {"Accept": "application/vnd.api+json"}

    # Try RUT first (more precise) — clean and try both raw and formatted
    if rut:
        clean = re.sub(r"[.\s]", "", rut.strip().upper())
        # Format with hyphen if missing
        m = _RUT_RE.match(clean)
        candidates = []
        if m:
            body, dv = m.group(1), m.group(2)
            candidates.append(f"{body}-{dv}")
            candidates.append(f"{int(body):,}".replace(",", ".") + f"-{dv}")
        candidates.append(clean)

        for cand in candidates:
            params = dict(base_params)
            params["filter[entity.registeredAs]"] = cand
            result = mlx_get(_GLEIF_URL, params=params, headers=headers,
                             timeout=15, country_code="cl")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_gleif(records, entity_name, rut)

    # Try name search
    if entity_name:
        for filter_key in ("filter[entity.legalName]", "filter[fulltext]"):
            params = dict(base_params)
            params[filter_key] = entity_name
            result = mlx_get(_GLEIF_URL, params=params, headers=headers,
                             timeout=15, country_code="cl")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_gleif(records, entity_name, rut)

    return {
        "entity_name": entity_name,
        "country_code": "CL",
        "rut": _format_rut(rut) if rut else None,
        "found": False,
        "status": "NOT_FOUND",
        "note": (
            "GLEIF covers Chilean companies with Legal Entity Identifiers (banks, "
            "listed companies, large corporates). Smaller entities without LEIs are "
            "not covered. SII public RUT lookup was deprecated — third-party "
            "verification now requires Clave Tributaria authentication."
        ),
        "validation_source": _gleif_source(entity_name or rut),
    }


def _format_gleif(records: list, query_name: str, query_rut: str) -> dict:
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
        addr.get("postalCode", ""), addr.get("country", "CL"),
    ]))

    legal_form = entity.get("legalForm", {}) or {}
    legal_form_str = legal_form.get("other") or legal_form.get("id") or ""

    others = []
    for rec in records[1:5]:
        e = rec["attributes"]["entity"]
        others.append({
            "name": e["legalName"]["name"],
            "lei": rec["attributes"]["lei"],
            "rut": e.get("registeredAs"),
            "status": e.get("status", "UNKNOWN"),
        })

    return {
        "entity_name": legal_name,
        "query_name": query_name,
        "country_code": "CL",
        "found": True,
        "lei": lei,
        "rut": reg_as or _format_rut(query_rut) if query_rut else (reg_as or None),
        "status": status,
        "legal_form": legal_form_str or None,
        "registered_address": full_addr or None,
        "jurisdiction": entity.get("jurisdiction", "CL"),
        "total_matches": len(records),
        "other_matches": others if others else None,
        "source": "GLEIF LEI Registry (SII RUT lookup deprecated)",
        "validation_source": _gleif_source(query_name or query_rut, lei),
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
            f"&filter[entity.legalAddress.country]=CL"
        ),
        "limitations": (
            "GLEIF covers CL companies with Legal Entity Identifiers (banks, listed "
            "companies, large corporates). Smaller entities without LEIs are not covered."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
