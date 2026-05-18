"""
Australia company verification via ABR (Australian Business Register).

Source: https://abr.business.gov.au/json/
Free API — GUID auth (public), JSONP response.

Input: entity_name (search by name) or abn (11 digits)
Returns: legal_name, abn, acn, entity_type, status, state, gst_registered
"""

import logging
import json
import re
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_ABN_URL = "https://abr.business.gov.au/json/AbnDetails.aspx"
_NAME_URL = "https://abr.business.gov.au/json/MatchingNames.aspx"
_GUID = "3e7189e6-4743-4090-a8d1-348e58b498d6"  # Public GUID


def init(get_secret=None):
    log.info("AU ABR ready (Australian Business Register, free JSONP API)")


def abr_verify(entity_name: str, abn: str = "") -> dict:
    if not entity_name and not abn:
        return {"found": False, "error": "entity_name or abn required"}

    try:
        if abn:
            clean = re.sub(r"[\s\-]", "", abn.strip())
            if not re.match(r"^\d{11}$", clean):
                return {"abn": abn, "found": False, "error": "ABN must be 11 digits"}
            return _lookup_abn(clean, entity_name)
        return _search_name(entity_name)
    except Exception as e:
        log.error("AU ABR error for %s: %s", entity_name or abn, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _unwrap_jsonp(text: str) -> dict:
    m = re.search(r"callback\((.*)\)$", text.strip(), re.DOTALL)
    if m:
        return json.loads(m.group(1))
    return json.loads(text)


def _lookup_abn(abn: str, entity_name: str) -> dict:
    result = mlx_get(
        _ABN_URL,
        params={"abn": abn, "callback": "callback", "guid": _GUID},
        timeout=60, country_code="au",
    )
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = _unwrap_jsonp(result.get("body", ""))

    if data.get("Message"):
        return {
            "entity_name": entity_name, "abn": abn,
            "found": False, "status": "NOT_FOUND",
            "note": data["Message"],
            "validation_source": _source(abn),
        }

    return _format_abn(data, entity_name, 1, [])


def _search_name(entity_name: str) -> dict:
    result = mlx_get(
        _NAME_URL,
        params={"name": entity_name, "callback": "callback", "guid": _GUID, "maxResults": 10},
        timeout=60, country_code="au",
    )
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = _unwrap_jsonp(result.get("body", ""))

    names = data.get("Names", [])
    if not names:
        return {
            "entity_name": entity_name, "found": False,
            "status": "NOT_FOUND",
            "validation_source": _source(entity_name),
        }

    best = names[0]
    best_abn = best.get("Abn", "")
    if best_abn:
        result = _lookup_abn(best_abn, entity_name)
        if result.get("found"):
            result["total_matches"] = len(names)
            result["other_matches"] = [
                {"name": n.get("Name", ""), "abn": n.get("Abn", ""),
                 "score": n.get("Score", "")}
                for n in names[1:5]
            ] or None
            return result
        return result

    return {
        "entity_name": entity_name, "found": False,
        "status": "NOT_FOUND",
        "validation_source": _source(entity_name),
    }


def _format_abn(data: dict, query_name: str, total: int, others: list) -> dict:
    abn = data.get("Abn", "")
    acn = data.get("Acn", "")

    # Entity name — business name or entity name
    bn = data.get("BusinessName", [])
    en = data.get("EntityName", "")
    legal_name = en if en else (bn[0] if bn else "")

    entity_type = data.get("EntityTypeName", "")
    entity_code = data.get("EntityTypeCode", "")

    status = data.get("AbnStatus", "")
    status_date = data.get("AbnStatusEffectiveFrom", "")

    gst = data.get("Gst", "")
    state = data.get("AddressState", "")
    postcode = data.get("AddressPostcode", "")
    address = f"{state} {postcode}".strip() if state or postcode else None

    return {
        "entity_name": legal_name,
        "query_name": query_name,
        "country_code": "AU",
        "found": True,
        "abn": abn or None,
        "acn": acn or None,
        "entity_type": entity_type or None,
        "entity_type_code": entity_code or None,
        "status": status.upper() if status else "UNKNOWN",
        "status_effective_from": status_date or None,
        "state": state or None,
        "postcode": postcode or None,
        "registered_address": address,
        "gst_registered": gst or None,
        "business_names": bn or None,
        "total_matches": total,
        "other_matches": others or None,
        "source": "ABR (Australian Business Register), Australian Government",
        "validation_source": _source(query_name),
    }


def _source(query: str) -> dict:
    return {
        "registry": "ABR — Australian Business Register, Australian Taxation Office",
        "url": "https://abr.business.gov.au/",
        "api": _ABN_URL,
        "how_to_reproduce": f"Visit abr.business.gov.au → Search: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
