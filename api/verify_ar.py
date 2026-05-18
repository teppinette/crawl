"""
Argentina company verification via GLEIF LEI API.

Primary source: GLEIF Global LEI Foundation (https://api.gleif.org/)
  - Free public REST API, no auth required
  - Covers listed AR companies, banks, large corporates with LEIs
  - Returns: LEI, legal name, status, address, jurisdiction
  - Reproduce: https://search.gleif.org/#/record/<LEI>

NOTE (2026-05): TangoFactura REST API is permanently DEAD (AFIP rebranded to
ARCA late 2024). AFIP constancia page (afip.gob.ar/genericos/cInscripcion/)
returns 403 even with AR proxy. AFIP SOAP requires digital certificate.
GLEIF is the only free, reliable, gov-grade source for AR entity verification.

Input: entity_name (search by name) or cuit (11 digits)
Returns: entity_name, country_code, found, lei, cuit, status, address
"""

import logging
import re
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"


def init(get_secret=None):
    log.info("AR GLEIF ready (LEI registry for Argentina)")


def afip_verify(entity_name: str, cuit: str = "") -> dict:
    """Verify an Argentine entity via GLEIF LEI lookup."""
    if not entity_name and not cuit:
        return {
            "entity_name": entity_name, "country_code": "AR",
            "found": False,
            "note": "entity_name or CUIT required for Argentina verification.",
        }

    try:
        # Search by name (primary)
        result = _gleif_search(entity_name, cuit)
        return result
    except Exception as e:
        log.error("AR GLEIF error for %s: %s", entity_name, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _gleif_search(entity_name: str, cuit: str) -> dict:
    """Search GLEIF for AR entities by name."""
    params = {
        "filter[entity.legalAddress.country]": "AR",
        "page[size]": "10",
    }

    # If CUIT provided, try searching by registration number first
    if cuit:
        clean_cuit = re.sub(r"[\s\-.]", "", cuit.strip())
        # GLEIF stores AR CUITs as registration authority IDs
        # Try formatted: XX-XXXXXXXX-X
        if len(clean_cuit) == 11:
            formatted = f"{clean_cuit[:2]}-{clean_cuit[2:10]}-{clean_cuit[10:]}"
            params["filter[entity.registeredAs]"] = formatted
            result = mlx_get(_GLEIF_URL, params=params,
                             headers={"Accept": "application/vnd.api+json"},
                             timeout=15, country_code="ar")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_result(records, entity_name, cuit)

            # Try raw CUIT number
            params["filter[entity.registeredAs]"] = clean_cuit
            result = mlx_get(_GLEIF_URL, params=params,
                             headers={"Accept": "application/vnd.api+json"},
                             timeout=15, country_code="ar")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_result(records, entity_name, cuit)

            # Fall through to name search
            del params["filter[entity.registeredAs]"]

    # Search by name
    if entity_name:
        params["filter[entity.legalName]"] = entity_name
        result = mlx_get(_GLEIF_URL, params=params,
                         headers={"Accept": "application/vnd.api+json"},
                         timeout=15, country_code="ar")

        if not result.get("ok"):
            raise RuntimeError(f"GLEIF returned HTTP {result.get('status_code')}")

        data = result.get("json") or {}
        records = data.get("data", [])

        if records:
            return _format_result(records, entity_name, cuit)

        # Try fuzzy: search with fulltext
        params.pop("filter[entity.legalName]", None)
        params["filter[fulltext]"] = entity_name
        result = mlx_get(_GLEIF_URL, params=params,
                         headers={"Accept": "application/vnd.api+json"},
                         timeout=15, country_code="ar")

        if result.get("ok") and result.get("json"):
            records = result["json"].get("data", [])
            if records:
                return _format_result(records, entity_name, cuit)

    # Not found
    return {
        "entity_name": entity_name,
        "country_code": "AR",
        "found": False,
        "status": "NOT_FOUND",
        "cuit": cuit or None,
        "note": ("GLEIF covers listed companies, banks, and large corporates with LEIs. "
                 "Smaller entities may not have a LEI. "
                 "AFIP constancia (afip.gob.ar) requires Argentine IP + browser."),
        "validation_source": _source(entity_name, cuit),
    }


def _format_result(records: list, entity_name: str, cuit: str) -> dict:
    """Format GLEIF records into verification response."""
    top = records[0]
    attrs = top["attributes"]
    entity = attrs["entity"]
    lei = attrs["lei"]

    legal_name = entity["legalName"]["name"]
    status = entity.get("status", "UNKNOWN")
    reg_as = entity.get("registeredAs", "")

    addr = entity.get("legalAddress", {})
    addr_lines = addr.get("addressLines", [])
    city = addr.get("city", "")
    region = addr.get("region", "")
    postal = addr.get("postalCode", "")
    country = addr.get("country", "AR")
    full_addr = ", ".join(filter(None, addr_lines + [city, region, postal, country]))

    # Other matches
    others = []
    for rec in records[1:5]:
        e = rec["attributes"]["entity"]
        others.append({
            "name": e["legalName"]["name"],
            "lei": rec["attributes"]["lei"],
            "status": e.get("status", "UNKNOWN"),
            "jurisdiction": e.get("jurisdiction", "AR"),
        })

    return {
        "entity_name": legal_name,
        "query_name": entity_name,
        "country_code": "AR",
        "found": True,
        "lei": lei,
        "cuit": reg_as or cuit or None,
        "status": status,
        "legal_form": entity.get("legalForm", {}).get("id"),
        "registered_address": full_addr or None,
        "jurisdiction": entity.get("jurisdiction", "AR"),
        "total_matches": len(records),
        "other_matches": others if others else None,
        "source": "GLEIF LEI Registry (Global Legal Entity Identifier Foundation)",
        "validation_source": _source(entity_name, cuit, lei),
    }


def _source(entity_name: str, cuit: str = "", lei: str = "") -> dict:
    src = {
        "registry": "GLEIF — Global Legal Entity Identifier Foundation",
        "url": "https://search.gleif.org/",
        "api": "https://api.gleif.org/api/v1/lei-records",
        "how_to_reproduce": (
            f"GLEIF search: https://search.gleif.org/#/record/{lei}"
            if lei else
            f"GLEIF search: https://api.gleif.org/api/v1/lei-records"
            f"?filter[entity.legalName]={entity_name}&filter[entity.legalAddress.country]=AR"
        ),
        "limitations": (
            "GLEIF covers AR companies with Legal Entity Identifiers (banks, listed companies, "
            "large corporates). Smaller entities without LEIs are not covered. "
            "AFIP constancia requires Argentine IP + browser (geo-blocked). "
            "TangoFactura API is permanently defunct (AFIP→ARCA rebrand, late 2024)."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return src
