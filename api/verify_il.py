"""
Israel verify — runs on the generic engine.

Source: ICA (Israel Companies Authority) via data.gov.il CKAN datastore.
Free, no auth. Direct HTTP (Bright Data blocks .gov.il).

Two configs sharing parser: IL_NAME_CONFIG, IL_NUMBER_CONFIG.
"""

import logging
import urllib.parse

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_API_URL = "https://data.gov.il/api/3/action/datastore_search"
_RESOURCE_ID = "f004176c-b85f-4542-8901-7b3176f9a054"

_STATUS_MAP = {
    "פעילה": "ACTIVE",
    "לא פעילה": "INACTIVE",
    "בהליכי מחיקה": "DISSOLVING",
    "נמחקה": "DISSOLVED",
    "בפירוק": "IN_LIQUIDATION",
}


def init(get_secret=None):
    log.info("IL verify ready (engine) — data.gov.il CKAN API direct")


def _parse_il(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    if not data.get("success"):
        return {"found": False}
    records = (data.get("result") or {}).get("records") or []
    if not records:
        return {"found": False}

    best = records[0]

    hebrew_name = best.get("שם חברה", "")
    english_name = best.get("שם באנגלית", "")
    comp_number = str(best.get("מספר חברה", ""))
    company_type = best.get("סוג תאגיד", "")
    status_he = best.get("סטטוס חברה", "")
    purpose = best.get("מטרת החברה", "")
    incorporation_date = best.get("תאריך התאגדות", "")
    is_government = best.get("חברה ממשלתית", "")
    limitations = best.get("מגבלות", "")
    violator = best.get("מפרה", "")
    last_annual_report = best.get("שנה אחרונה של דוח שנתי (שהוגש)", "")

    city = best.get("שם עיר", "")
    street = best.get("שם רחוב", "")
    house_num = best.get("מספר בית", "")
    zipcode = best.get("מיקוד", "")
    country = best.get("מדינה", "")

    addr_parts = []
    if street: addr_parts.append(street)
    if house_num: addr_parts.append(str(house_num))
    if city: addr_parts.append(city)
    if zipcode: addr_parts.append(str(zipcode))
    if country and country != "ישראל": addr_parts.append(country)
    address = ", ".join(p for p in addr_parts if p and str(p).strip()) or None

    status = _STATUS_MAP.get(status_he, status_he.upper() if status_he else "UNKNOWN")

    others = [
        {
            "name_hebrew": m.get("שם חברה", ""),
            "name_english": m.get("שם באנגלית", ""),
            "company_number": str(m.get("מספר חברה", "")),
            "status": m.get("סטטוס חברה", ""),
        }
        for m in records[1:5]
    ]

    display_name = english_name.strip() if english_name.strip() else hebrew_name

    founded_year = None
    if incorporation_date and len(incorporation_date) >= 4:
        # Format usually YYYY-MM-DD or DD/MM/YYYY
        if incorporation_date[:4].isdigit():
            founded_year = incorporation_date[:4]
        elif incorporation_date[-4:].isdigit():
            founded_year = incorporation_date[-4:]

    return {
        "found": True,
        "legal_name": display_name or entity_name,
        "legal_name_en": english_name or None,
        "business_registration_number": comp_number or None,
        "headquarters": address,
        "founded_year": founded_year,
        "industry": purpose or None,
        "is_listed": False,
        # IL-specific extras
        "company_number": comp_number or None,
        "legal_name_hebrew": hebrew_name or None,
        "company_type": company_type or None,
        "incorporation_date": incorporation_date or None,
        "purpose": purpose or None,
        "is_government_company": (is_government == "כן") if is_government else None,
        "limitations": limitations or None,
        "violator": bool(violator) if violator else False,
        "last_annual_report": last_annual_report or None,
        "city": city or None,
        "street": street or None,
        "house_number": str(house_num) if house_num else None,
        "zip_code": str(zipcode) if zipcode else None,
        "total_matches": len(records),
        "other_matches": others or None,
        "status": status,
        "summary": (
            f"{display_name or entity_name} — #{comp_number} — {status}"
            + (f" — {company_type}" if company_type else "")
        ),
    }


IL_NAME_CONFIG = eng.CountryConfig(
    country_code="IL",
    source_name="ICA (Israel Companies Authority), Israel",
    transport=eng.T_MLX_HTTP,
    primary_url=_API_URL + "?resource_id=" + _RESOURCE_ID + "&q={q}&limit=10",
    parser=_parse_il,
    timeout=20,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://data.gov.il/dataset/company → search '{entity}'"
    ),
)

IL_NUMBER_CONFIG = eng.CountryConfig(
    country_code="IL",
    source_name="ICA (Israel Companies Authority), Israel",
    transport=eng.T_MLX_HTTP,
    primary_url=(
        _API_URL + "?resource_id=" + _RESOURCE_ID +
        "&filters=" + urllib.parse.quote('{"מספר חברה": ') + "{q}" +
        urllib.parse.quote("}") + "&limit=1"
    ),
    parser=_parse_il,
    timeout=20,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://data.gov.il/dataset/company → search by company number {entity}"
    ),
)


def ica_verify(entity_name: str, company_number: str = "") -> dict:
    """IL verify entry point — backward compat with main.py routing."""
    company_number = (company_number or "").strip().lstrip("0")
    if company_number:
        return eng.run(IL_NUMBER_CONFIG, company_number, {"company_number": company_number})
    return eng.run(IL_NAME_CONFIG, entity_name, {})
