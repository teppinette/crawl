"""
Switzerland company verification via Zefix (FOSC/SOGC) REST API + GLEIF LEI fallback.

Primary source: Zefix (https://www.zefix.admin.ch/ZefixPublicREST/api/v1/)
  - Official Swiss government data, Basic auth required (email zefix@bj.admin.ch)
  - Covers all Swiss companies in the commercial register
  - Returns: legal_name, uid, status, legal_form, purpose, address, canton

Fallback source: GLEIF LEI (https://api.gleif.org/api/v1/lei-records)
  - Free public REST API, no auth required
  - Covers Swiss companies with LEIs (banks, listed companies, large corporates)
  - Returns: LEI, legal name, status, address, jurisdiction

Input: entity_name (search by name) or uid (CHE-xxx.xxx.xxx format)
Returns: legal_name, uid, status, legal_form, purpose, address, canton
"""

import logging
import re
import time

from mlx_http import mlx_get, mlx_post

log = logging.getLogger("verify-gateway")

_BASE = "https://www.zefix.admin.ch/ZefixPublicREST/api/v1"
_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"

_zefix_user = None
_zefix_pass = None


def init(get_secret=None):
    global _zefix_user, _zefix_pass
    if get_secret:
        _zefix_user = get_secret("zefix-api-user")
        _zefix_pass = get_secret("zefix-api-pass")
    if _zefix_user and _zefix_pass:
        log.info("CH Zefix ready (Basic auth credentials loaded)")
    else:
        log.info("CH Zefix: no credentials — GLEIF LEI fallback only")


def zefix_verify(entity_name: str, uid: str = "") -> dict:
    if not entity_name and not uid:
        return {"found": False, "error": "entity_name or uid required"}

    try:
        # Try Zefix first if credentials are available
        if _zefix_user and _zefix_pass:
            if uid:
                clean = re.sub(r"[.\-\s]", "", uid.strip().upper())
                if not re.match(r"^CHE\d{9}$", clean):
                    return {"uid": uid, "found": False, "error": "UID must be CHE + 9 digits"}
                result = _lookup_uid(clean, entity_name)
            else:
                result = _search_name(entity_name)
            if result.get("found"):
                return result

        # Fallback to GLEIF LEI
        gleif_result = _gleif_search(entity_name, uid)
        return gleif_result

    except Exception as e:
        log.error("CH verify error for %s: %s", entity_name or uid, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


# ── Zefix (primary, needs credentials) ──────────────────────────

def _zefix_headers():
    import base64
    creds = base64.b64encode(f"{_zefix_user}:{_zefix_pass}".encode()).decode()
    return {"Accept": "application/json", "Authorization": f"Basic {creds}"}


def _lookup_uid(uid: str, entity_name: str) -> dict:
    formatted = f"CHE-{uid[3:6]}.{uid[6:9]}.{uid[9:12]}"
    result = mlx_get(
        f"{_BASE}/company/uid/{formatted}",
        headers=_zefix_headers(),
        timeout=60, country_code="ch",
    )
    if result.get("status_code") == 404:
        return {
            "entity_name": entity_name, "uid": formatted,
            "found": False, "status": "NOT_FOUND",
            "validation_source": _zefix_source(formatted),
        }
    if not result.get("ok"):
        log.warning("Zefix HTTP %s for UID %s, falling back to GLEIF",
                     result.get("status_code"), formatted)
        return {"found": False}
    data = result.get("json") or {}
    return _format_zefix(data, entity_name, 1, [])


def _search_name(entity_name: str) -> dict:
    result = mlx_post(
        f"{_BASE}/company/search",
        json_body={"name": entity_name, "maxEntries": 10, "activeOnly": False},
        headers=_zefix_headers(),
        timeout=60, country_code="ch",
    )
    if not result.get("ok"):
        log.warning("Zefix HTTP %s for %s, falling back to GLEIF",
                     result.get("status_code"), entity_name)
        return {"found": False}
    results = result.get("json")
    if not results:
        return {"found": False}
    best = results[0]
    others = results[1:5]
    return _format_zefix(best, entity_name, len(results), others)


def _format_zefix(data: dict, query_name: str, total: int, others: list) -> dict:
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
        "validation_source": _zefix_source(query_name),
    }


def _zefix_source(query: str) -> dict:
    return {
        "registry": "Zefix — Zentraler Firmenindex, Federal Office of Justice, Switzerland",
        "url": "https://www.zefix.admin.ch/en/search/entity/welcome",
        "api": f"{_BASE}/company/search",
        "how_to_reproduce": f"Visit zefix.admin.ch → Search: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ── GLEIF LEI (fallback) ────────────────────────────────────────

def _gleif_search(entity_name: str, uid: str) -> dict:
    """Search GLEIF for CH entities by name."""
    params = {
        "filter[entity.legalAddress.country]": "CH",
        "page[size]": "10",
    }

    # Search by name
    if entity_name:
        params["filter[entity.legalName]"] = entity_name
        result = mlx_get(_GLEIF_URL, params=params,
                         headers={"Accept": "application/vnd.api+json"},
                         timeout=15, country_code="ch")

        if result.get("ok") and result.get("json"):
            records = result["json"].get("data", [])
            if records:
                return _format_gleif(records, entity_name, uid)

        # Try fulltext
        params.pop("filter[entity.legalName]", None)
        params["filter[fulltext]"] = entity_name
        result = mlx_get(_GLEIF_URL, params=params,
                         headers={"Accept": "application/vnd.api+json"},
                         timeout=15, country_code="ch")

        if result.get("ok") and result.get("json"):
            records = result["json"].get("data", [])
            if records:
                return _format_gleif(records, entity_name, uid)

    # Not found
    zefix_note = ""
    if not (_zefix_user and _zefix_pass):
        zefix_note = " Zefix API credentials pending (email zefix@bj.admin.ch)."
    return {
        "entity_name": entity_name,
        "country_code": "CH",
        "found": False,
        "status": "NOT_FOUND",
        "uid": uid or None,
        "note": ("GLEIF covers Swiss companies with LEIs (banks, listed companies, "
                 "large corporates). Smaller entities may not have a LEI." + zefix_note),
        "validation_source": _gleif_source(entity_name),
    }


def _format_gleif(records: list, entity_name: str, uid: str) -> dict:
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
    country = addr.get("country", "CH")
    full_addr = ", ".join(filter(None, addr_lines + [city, region, postal, country]))

    others = []
    for rec in records[1:5]:
        e = rec["attributes"]["entity"]
        others.append({
            "name": e["legalName"]["name"],
            "lei": rec["attributes"]["lei"],
            "status": e.get("status", "UNKNOWN"),
            "jurisdiction": e.get("jurisdiction", "CH"),
        })

    return {
        "entity_name": legal_name,
        "query_name": entity_name,
        "country_code": "CH",
        "found": True,
        "lei": lei,
        "uid": reg_as or uid or None,
        "status": status,
        "legal_form": entity.get("legalForm", {}).get("id"),
        "registered_address": full_addr or None,
        "jurisdiction": entity.get("jurisdiction", "CH"),
        "total_matches": len(records),
        "other_matches": others if others else None,
        "source": "GLEIF LEI Registry (fallback — Zefix credentials pending)",
        "validation_source": _gleif_source(entity_name, lei),
    }


def _gleif_source(query: str, lei: str = "") -> dict:
    return {
        "registry": "GLEIF — Global Legal Entity Identifier Foundation (fallback for Zefix)",
        "url": "https://search.gleif.org/",
        "api": _GLEIF_URL,
        "how_to_reproduce": (
            f"GLEIF search: https://search.gleif.org/#/record/{lei}"
            if lei else
            f"GLEIF search: https://api.gleif.org/api/v1/lei-records"
            f"?filter[entity.legalName]={query}&filter[entity.legalAddress.country]=CH"
        ),
        "limitations": (
            "GLEIF covers CH companies with Legal Entity Identifiers (banks, listed companies, "
            "large corporates). Smaller entities without LEIs are not covered. "
            "Zefix API credentials pending — email zefix@bj.admin.ch for Basic auth access."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
