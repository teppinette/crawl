"""
Israel ICA company verification via data.gov.il CKAN API.

Source: https://data.gov.il/dataset/company
Free API — no auth, no rate limit observed, JSON response.
Full Israeli company registry with Hebrew + English names.

Input: entity_name (search by name) or company_number (9-digit company number)
Returns: legal_name (Hebrew + English), status, company_type, incorporation_date,
         address, government_company flag, limitations
"""

import logging
import time

from curl_cffi import requests as cffi_requests
from proxy_cfg import get_proxy

log = logging.getLogger("verify-gateway")

_API_URL = "https://data.gov.il/api/3/action/datastore_search"
_RESOURCE_ID = "f004176c-b85f-4542-8901-7b3176f9a054"
_PROXY = None


def init(get_secret):
    # data.gov.il is a public CKAN API — no proxy needed
    # Bright Data blocks .gov.il by policy (403)
    log.info("IL ICA ready (data.gov.il CKAN API, no auth required, direct access)")


def ica_verify(entity_name: str, company_number: str = "") -> dict:
    """
    Verify an Israeli company via the ICA company registry.

    Searches by company name (Hebrew or English) or company number.
    """
    if not entity_name and not company_number:
        return {"found": False, "error": "entity_name or company_number required"}

    try:
        if company_number:
            records = _search_by_number(company_number)
        else:
            records = _search_by_name(entity_name)

        if not records:
            return {
                "entity_name": entity_name,
                "company_number": company_number or None,
                "found": False,
                "status": "NOT_FOUND",
                "source": "ICA (Israel Companies Authority), Israel",
                "validation_source": _validation_source(entity_name or company_number),
            }

        best = records[0]
        return _format_result(best, entity_name, company_number, len(records), records[:5])

    except Exception as e:
        log.error("IL ICA error for %s: %s", entity_name or company_number, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _search_by_name(name: str) -> list:
    """Search by company name (works with Hebrew or English)."""
    resp = cffi_requests.get(
        _API_URL,
        params={
            "resource_id": _RESOURCE_ID,
            "q": name,
            "limit": 10,
        },
        impersonate="chrome",
        proxy=None,
        timeout=20,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success") and data.get("result", {}).get("records"):
        return data["result"]["records"]
    return []


def _search_by_number(number: str) -> list:
    """Search by company number (exact match)."""
    clean = number.strip().lstrip("0")
    resp = cffi_requests.get(
        _API_URL,
        params={
            "resource_id": _RESOURCE_ID,
            "filters": f'{{"מספר חברה": {clean}}}',
            "limit": 1,
        },
        impersonate="chrome",
        proxy=None,
        timeout=20,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success") and data.get("result", {}).get("records"):
        return data["result"]["records"]
    return []


def _format_result(record: dict, query_name: str, company_number: str,
                   total_matches: int, top_matches: list) -> dict:
    """Format CKAN record into standard verification response."""
    hebrew_name = record.get("שם חברה", "")
    english_name = record.get("שם באנגלית", "")
    comp_number = record.get("מספר חברה", "")
    company_type = record.get("סוג תאגיד", "")
    status_he = record.get("סטטוס חברה", "")
    description = record.get("תאור חברה", "")
    purpose = record.get("מטרת החברה", "")
    incorporation_date = record.get("תאריך התאגדות", "")
    is_government = record.get("חברה ממשלתית", "")
    limitations = record.get("מגבלות", "")
    violator = record.get("מפרה", "")
    last_annual_report = record.get("שנה אחרונה של דוח שנתי (שהוגש)", "")

    # Address
    city = record.get("שם עיר", "")
    street = record.get("שם רחוב", "")
    house_num = record.get("מספר בית", "")
    zipcode = record.get("מיקוד", "")
    country = record.get("מדינה", "")
    care_of = record.get("אצל", "")

    addr_parts = []
    if street:
        addr_parts.append(street)
    if house_num:
        addr_parts.append(str(house_num))
    if city:
        addr_parts.append(city)
    if zipcode:
        addr_parts.append(str(zipcode))
    if country and country != "ישראל":
        addr_parts.append(country)
    address = ", ".join(p for p in addr_parts if p and str(p).strip())

    # Status mapping
    status_map = {
        "פעילה": "ACTIVE",
        "לא פעילה": "INACTIVE",
        "בהליכי מחיקה": "DISSOLVING",
        "נמחקה": "DISSOLVED",
        "בפירוק": "IN_LIQUIDATION",
    }
    status = status_map.get(status_he, status_he.upper() if status_he else "UNKNOWN")

    # Other matches
    other_matches = []
    for m in top_matches[1:]:
        other_matches.append({
            "name_hebrew": m.get("שם חברה", ""),
            "name_english": m.get("שם באנגלית", ""),
            "company_number": str(m.get("מספר חברה", "")),
            "status": m.get("סטטוס חברה", ""),
        })

    display_name = english_name.strip() if english_name.strip() else hebrew_name

    return {
        "entity_name": display_name,
        "query_name": query_name,
        "found": True,
        "status": status,
        "company_number": str(comp_number),
        "legal_name_hebrew": hebrew_name or None,
        "legal_name_english": english_name or None,
        "company_type": company_type or None,
        "incorporation_date": incorporation_date or None,
        "purpose": purpose or None,
        "is_government_company": is_government == "כן" if is_government else None,
        "limitations": limitations or None,
        "violator": bool(violator) if violator else False,
        "last_annual_report_year": last_annual_report or None,
        "registered_address": address or None,
        "city": city or None,
        "care_of": care_of.strip() if care_of and care_of.strip() else None,
        "total_matches": total_matches,
        "other_matches": other_matches if other_matches else None,
        "source": "ICA (Israel Companies Authority / רשות התאגידים), Israel",
        "validation_source": _validation_source(query_name or str(comp_number)),
    }


def _validation_source(query: str) -> dict:
    return {
        "registry": "ICA — Israel Companies Authority (רשות התאגידים), Ministry of Justice",
        "url": "https://ica.justice.gov.il/GenericCorporarionInfo/SearchCorporation",
        "api": "https://data.gov.il/dataset/company",
        "how_to_reproduce": (
            f"Visit data.gov.il/dataset/company → "
            f"Search: {query} → View company record"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
