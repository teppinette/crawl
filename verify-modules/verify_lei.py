"""
GLEIF LEI (Legal Entity Identifier) lookup.

Endpoint: https://api.gleif.org/api/v1/lei-records
Free API, no auth, no rate limit (reasonable use).

Returns: LEI, entity name, legal address, HQ address, registration status,
         direct parent, ultimate parent, registration authority, entity category.

The ONLY global standard for corporate hierarchy mapping.
Covers 2.6M+ entities across all jurisdictions.
"""

import logging
import time

from curl_cffi import requests as cffi_requests

log = logging.getLogger("verify-gateway")

_BASE_URL = "https://api.gleif.org/api/v1"


def init(get_secret):
    log.info("GLEIF LEI ready (free API, no auth, global corporate hierarchy)")


def lei_lookup(entity_name: str = "", lei: str = "", country_code: str = "") -> dict:
    """
    Look up entity in GLEIF LEI database.
    Can search by lei (20-char code) or entity_name (+ optional country_code).
    """
    if not entity_name and not lei:
        return {"found": False, "error": "entity_name or lei required"}

    try:
        if lei:
            return _lookup_by_lei(lei.strip().upper())
        return _search_by_name(entity_name.strip(), country_code.strip().upper())
    except Exception as e:
        log.error("GLEIF LEI error: %s", e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_by_lei(lei: str) -> dict:
    resp = cffi_requests.get(
        f"{_BASE_URL}/lei-records/{lei}",
        headers={"Accept": "application/vnd.api+json"},
        impersonate="chrome",
        timeout=15,
    )
    if resp.status_code == 404:
        return {"lei": lei, "found": False, "status": "NOT_FOUND"}

    resp.raise_for_status()
    data = resp.json().get("data", {})
    result = _format_lei_record(data)
    _enrich_relationships(lei, result)
    return result


def _search_by_name(entity_name: str, country_code: str = "") -> dict:
    params = {"filter[entity.legalName]": entity_name, "page[size]": 5}
    if country_code:
        params["filter[entity.legalAddress.country]"] = country_code

    resp = cffi_requests.get(
        f"{_BASE_URL}/lei-records", params=params,
        headers={"Accept": "application/vnd.api+json"},
        impersonate="chrome", timeout=15,
    )
    resp.raise_for_status()
    records = resp.json().get("data", [])

    # Fallback to fulltext search
    if not records:
        params_fuzzy = {"filter[fulltext]": entity_name, "page[size]": 5}
        if country_code:
            params_fuzzy["filter[entity.legalAddress.country]"] = country_code
        resp2 = cffi_requests.get(
            f"{_BASE_URL}/lei-records", params=params_fuzzy,
            headers={"Accept": "application/vnd.api+json"},
            impersonate="chrome", timeout=15,
        )
        resp2.raise_for_status()
        records = resp2.json().get("data", [])

    if not records:
        return {
            "entity_name": entity_name, "found": False, "status": "NOT_FOUND",
            "note": "No LEI record found. Not all entities have LEIs — "
                    "primarily financial institutions and large corporates.",
            "source": "GLEIF (Global Legal Entity Identifier Foundation)",
        }

    best = records[0]
    result = _format_lei_record(best)
    _enrich_relationships(result["lei"], result)

    if len(records) > 1:
        result["alternatives"] = [
            {
                "lei": r.get("id", ""),
                "entity_name": r.get("attributes", {}).get("entity", {}).get("legalName", {}).get("name", ""),
                "country": r.get("attributes", {}).get("entity", {}).get("legalAddress", {}).get("country", ""),
                "status": r.get("attributes", {}).get("registration", {}).get("status", ""),
            }
            for r in records[1:]
        ]
    return result


def _format_lei_record(record: dict) -> dict:
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})
    reg = attrs.get("registration", {})
    lei = record.get("id", "")
    legal_name = entity.get("legalName", {}).get("name", "")

    # Legal address
    legal_addr = entity.get("legalAddress", {})
    addr_lines = legal_addr.get("addressLines", [])
    legal_address = ", ".join(addr_lines) if addr_lines else ""
    if legal_addr.get("city"):
        legal_address += f", {legal_addr['city']}"
    if legal_addr.get("postalCode"):
        legal_address += f" {legal_addr['postalCode']}"
    if legal_addr.get("country"):
        legal_address += f", {legal_addr['country']}"

    # HQ address
    hq_addr = entity.get("headquartersAddress", {})
    hq_lines = hq_addr.get("addressLines", [])
    hq_address = ", ".join(hq_lines) if hq_lines else ""
    if hq_addr.get("city"):
        hq_address += f", {hq_addr['city']}"
    if hq_addr.get("country"):
        hq_address += f", {hq_addr['country']}"

    other_names = [n.get("name", "") for n in entity.get("otherNames", [])]

    return {
        "lei": lei,
        "entity_name": legal_name,
        "found": True,
        "status": reg.get("status", "").upper(),
        "entity_category": entity.get("category", ""),
        "legal_form": entity.get("legalForm", {}).get("id", ""),
        "jurisdiction": entity.get("jurisdiction", ""),
        "registered_as": entity.get("registeredAs", ""),
        "registration_authority": entity.get("registeredAt", {}).get("id", ""),
        "legal_address": legal_address,
        "hq_address": hq_address,
        "other_names": other_names or None,
        "registration_date": reg.get("initialRegistrationDate", ""),
        "last_update": reg.get("lastUpdateDate", ""),
        "next_renewal": reg.get("nextRenewalDate", ""),
        "managing_lou": reg.get("managingLou", ""),
        "parent": None,
        "ultimate_parent": None,
        "source": "GLEIF (Global Legal Entity Identifier Foundation)",
        "validation_source": {
            "registry": "GLEIF — Global Legal Entity Identifier Foundation",
            "url": f"https://search.gleif.org/#/record/{lei}",
            "record_id": lei,
            "how_to_reproduce": f"Visit https://search.gleif.org → Search '{legal_name}' or LEI {lei}",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }


def _enrich_relationships(lei: str, result: dict):
    """Fetch direct and ultimate parent from GLEIF relationship endpoints."""
    for rel_type, key in [("direct-parent", "parent"), ("ultimate-parent", "ultimate_parent")]:
        try:
            resp = cffi_requests.get(
                f"{_BASE_URL}/lei-records/{lei}/{rel_type}",
                headers={"Accept": "application/vnd.api+json"},
                impersonate="chrome", timeout=10,
            )
            if resp.status_code == 200:
                pdata = resp.json().get("data", {})
                if pdata and pdata.get("id"):
                    p_ent = pdata.get("attributes", {}).get("entity", {})
                    result[key] = {
                        "lei": pdata["id"],
                        "name": p_ent.get("legalName", {}).get("name", ""),
                        "country": p_ent.get("legalAddress", {}).get("country", ""),
                    }
        except Exception as e:
            log.debug("GLEIF %s lookup failed for %s: %s", rel_type, lei, e)
