"""
Netherlands company verification via KvK (Kamer van Koophandel).

Source: https://zoeken.kvk.nl/ (public search)
Free public search — no auth needed for basic data.
KVK number is 8 digits.

Input: entity_name (search by name) or kvk_number (8 digits)
Returns: legal_name, kvk_number, status, legal_form, address
"""

import logging
import re
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_SEARCH_URL = "https://zoeken.kvk.nl/HandelRegisterAPI/api/handelsregister"
def init(get_secret=None):
    log.info("NL KvK ready (Kamer van Koophandel, public search)")


def kvk_verify(entity_name: str, kvk_number: str = "") -> dict:
    if not entity_name and not kvk_number:
        return {"found": False, "error": "entity_name or kvk_number required"}

    try:
        if kvk_number:
            clean = re.sub(r"[\s\-.]", "", kvk_number.strip())
            if not re.match(r"^\d{8}$", clean):
                return {"kvk_number": kvk_number, "found": False,
                        "error": "KVK number must be 8 digits"}
            return _search(clean, entity_name, by_number=True)
        return _search(entity_name, entity_name, by_number=False)
    except Exception as e:
        log.error("NL KvK error for %s: %s", entity_name or kvk_number, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _search(query: str, entity_name: str, by_number: bool) -> dict:
    """Search KvK public API."""
    params = {
        "kvknummer" if by_number else "handelsnaam": query,
        "pagina": 1,
        "start": 0,
        "pagesize": 10,
    }

    result = mlx_get(
        _SEARCH_URL,
        params=params,
        headers={
            "Accept": "application/json",
        },
        timeout=60, country_code="nl",
    )

    if result.get("status_code") == 404:
        return _not_found(entity_name, query if by_number else "")

    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")

    data = result.get("json")
    if data is None:
        # Try parsing HTML response for data
        return _parse_html_search(result.get("body", ""), entity_name, query if by_number else "")

    results = data.get("resultaten", [])
    if not results:
        return _not_found(entity_name, query if by_number else "")

    best = results[0]
    others = []
    for r in results[1:5]:
        others.append({
            "name": r.get("handelsnaam", ""),
            "kvk_number": r.get("kvkNummer", ""),
            "status": r.get("status", ""),
            "place": r.get("plaats", ""),
        })

    return _format(best, entity_name, len(results), others)


def _parse_html_search(html: str, entity_name: str, kvk_number: str) -> dict:
    """Fallback: parse KvK HTML search results."""
    # Try to extract KVK numbers and names from HTML
    kvk_matches = re.findall(r'KVK(?:\s*nummer)?[:\s]*(\d{8})', html, re.IGNORECASE)
    name_matches = re.findall(r'handelsnaam["\s:>]*([^<"]+)', html, re.IGNORECASE)

    if kvk_matches and name_matches:
        return {
            "entity_name": name_matches[0].strip(),
            "query_name": entity_name,
            "country_code": "NL",
            "found": True,
            "kvk_number": kvk_matches[0],
            "status": "UNKNOWN",
            "note": "Parsed from HTML search results",
            "validation_source": _source(entity_name or kvk_number),
        }

    return _not_found(entity_name, kvk_number)


def _format(record: dict, entity_name: str, total: int, others: list) -> dict:
    name = record.get("handelsnaam", "")
    kvk = record.get("kvkNummer", "")
    status = record.get("status", "")
    legal_form = record.get("rechtsvorm", "")
    place = record.get("plaats", "")
    street = record.get("straat", "")
    house_nr = record.get("huisnummer", "")
    postcode = record.get("postcode", "")

    addr_parts = [street, str(house_nr) if house_nr else "", postcode, place]
    address = " ".join(p for p in addr_parts if p).strip()

    trade_names = record.get("handelsnamen", [])
    sbi = record.get("spiActiviteiten", record.get("sbiActiviteiten", []))

    return {
        "entity_name": name,
        "query_name": entity_name,
        "country_code": "NL",
        "found": True,
        "kvk_number": kvk or None,
        "status": status.upper() if status else "UNKNOWN",
        "legal_form": legal_form or None,
        "registered_address": address or None,
        "city": place or None,
        "postal_code": postcode or None,
        "trade_names": trade_names or None,
        "sbi_codes": sbi or None,
        "total_matches": total,
        "other_matches": others or None,
        "source": "KvK (Kamer van Koophandel), Netherlands",
        "validation_source": _source(entity_name or kvk),
    }


def _not_found(entity_name: str, kvk_number: str) -> dict:
    return {
        "entity_name": entity_name, "kvk_number": kvk_number or None,
        "country_code": "NL",
        "found": False, "status": "NOT_FOUND",
        "validation_source": _source(entity_name or kvk_number),
    }


def _source(query: str) -> dict:
    return {
        "registry": "KvK — Kamer van Koophandel (Chamber of Commerce), Netherlands",
        "url": "https://www.kvk.nl/zoeken/",
        "how_to_reproduce": f"Visit kvk.nl/zoeken → Search: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
