"""
Switzerland company verification via Zefix (FOSC/SOGC) REST API.

Source: https://www.zefix.admin.ch/ZefixPublicREST/api/v1/
Free API — no auth, official Swiss government data, JSON response.

Input: entity_name (search by name) or uid (CHE-xxx.xxx.xxx format)
Returns: legal_name, uid, status, legal_form, purpose, address, canton
"""

import logging
import re
import time

from mlx_http import mlx_get, mlx_post

log = logging.getLogger("verify-gateway")

_BASE = "https://www.zefix.admin.ch/ZefixPublicREST/api/v1"


def init(get_secret=None):
    log.info("CH Zefix ready (official Swiss gov REST API, free)")


def zefix_verify(entity_name: str, uid: str = "") -> dict:
    if not entity_name and not uid:
        return {"found": False, "error": "entity_name or uid required"}

    try:
        if uid:
            clean = re.sub(r"[.\-\s]", "", uid.strip().upper())
            if not re.match(r"^CHE\d{9}$", clean):
                return {"uid": uid, "found": False, "error": "UID must be CHE + 9 digits"}
            return _lookup_uid(clean, entity_name)
        return _search_name(entity_name)
    except Exception as e:
        log.error("CH Zefix error for %s: %s", entity_name or uid, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_uid(uid: str, entity_name: str) -> dict:
    formatted = f"CHE-{uid[3:6]}.{uid[6:9]}.{uid[9:12]}"
    result = mlx_get(
        f"{_BASE}/company/uid/{formatted}",
        headers={"Accept": "application/json"},
        timeout=60, country_code="ch",
    )
    if result.get("status_code") == 404:
        return {
            "entity_name": entity_name, "uid": formatted,
            "found": False, "status": "NOT_FOUND",
            "validation_source": _source(formatted),
        }
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or {}
    return _format(data, entity_name, 1, [])


def _search_name(entity_name: str) -> dict:
    result = mlx_post(
        f"{_BASE}/company/search",
        json_body={"name": entity_name, "maxEntries": 10, "activeOnly": False},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=60, country_code="ch",
    )
    if result.get("status_code") == 404:
        return {
            "entity_name": entity_name, "found": False,
            "status": "NOT_FOUND",
            "validation_source": _source(entity_name),
        }
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    results = result.get("json")
    if not results:
        return {
            "entity_name": entity_name, "found": False,
            "status": "NOT_FOUND",
            "validation_source": _source(entity_name),
        }
    best = results[0]
    others = results[1:5]
    return _format(best, entity_name, len(results), others)


def _format(data: dict, query_name: str, total: int, others: list) -> dict:
    name = data.get("name", "")
    uid = data.get("uid", "")
    chid = data.get("chid", "")
    status_raw = data.get("status", "")
    status_map = {
        "ACTIVE": "ACTIVE", "CANCELLED": "CANCELLED",
        "BEING_CANCELLED": "BEING_CANCELLED",
    }
    status = status_map.get(status_raw, status_raw.upper() if status_raw else "UNKNOWN")

    legal_form = data.get("legalForm", "")
    legal_form_map = {
        "0106": "Aktiengesellschaft (AG)",
        "0107": "GmbH",
        "0108": "Genossenschaft",
        "0109": "Stiftung",
        "0110": "Verein",
        "0101": "Einzelunternehmen",
        "0302": "Branch of foreign company",
    }
    legal_form_display = legal_form_map.get(str(legal_form), str(legal_form))

    purpose = data.get("purpose", "")
    canton = data.get("canton", "")
    municipality = data.get("legalSeat", "")

    addr = data.get("address", {}) or {}
    addr_parts = [
        addr.get("street", ""),
        addr.get("houseNumber", ""),
        addr.get("swissZipCode", ""),
        addr.get("city", ""),
    ]
    address = " ".join(p for p in addr_parts if p).strip()

    other_matches = []
    for o in others:
        other_matches.append({
            "name": o.get("name", ""),
            "uid": o.get("uid", ""),
            "status": o.get("status", ""),
            "canton": o.get("canton", ""),
        })

    return {
        "entity_name": name,
        "query_name": query_name,
        "country_code": "CH",
        "found": True,
        "uid": uid or None,
        "chid": chid or None,
        "status": status,
        "legal_form": legal_form_display,
        "purpose": (purpose[:500] + "...") if purpose and len(purpose) > 500 else purpose or None,
        "canton": canton or None,
        "municipality": municipality or None,
        "registered_address": address or None,
        "total_matches": total,
        "other_matches": other_matches or None,
        "source": "Zefix (Zentraler Firmenindex), Federal Office of Justice, Switzerland",
        "validation_source": _source(query_name),
    }


def _source(query: str) -> dict:
    return {
        "registry": "Zefix — Zentraler Firmenindex, Federal Office of Justice, Switzerland",
        "url": "https://www.zefix.admin.ch/en/search/entity/welcome",
        "api": f"{_BASE}/company/search",
        "how_to_reproduce": f"Visit zefix.admin.ch → Search: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
