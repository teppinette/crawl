"""
Latvia verify — runs on the generic engine.

Source: Latvian Register of Enterprises via data.gov.lv CKAN datastore.
Free public JSON, no auth. Direct HTTP.

Two configs sharing parser:
  LV_REGCODE_CONFIG — filter on regcode (11-digit registration code)
  LV_NAME_CONFIG    — full-text q search
"""

import json
import logging
import re
import urllib.parse

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BASE = "https://data.gov.lv/dati/lv/api/3/action/datastore_search"
_RESOURCE_ID = "25e80bf3-f107-4ab4-89ef-251b5b9374e9"

# LV entity type codes
_TYPE_MAP = {
    "SIA": "Sabiedrība ar ierobežotu atbildību (LLC)",
    "AS":  "Akciju sabiedrība (JSC)",
    "IK":  "Individuālais komersants (sole proprietor)",
    "PS":  "Personālsabiedrība (partnership)",
    "KS":  "Komandītsabiedrība (limited partnership)",
    "FIL": "Filiāle (branch)",
    "VAS": "Valsts akciju sabiedrība (state JSC)",
    "PAS": "Pašvaldības akciju sabiedrība (municipal JSC)",
    "PSIA": "Pašvaldības SIA (municipal LLC)",
}


def init(get_secret=None):
    log.info("LV verify ready (engine) — Latvian Register via data.gov.lv CKAN")


def _format_lv(rec: dict, entity_name: str) -> dict:
    regcode = str(rec.get("regcode", "") or "")
    name = rec.get("name", "")
    name_in_quotes = rec.get("name_in_quotes", "")
    regtype_text = rec.get("regtype_text", "") or rec.get("regtype", "")
    type_code = rec.get("type", "") or ""
    type_text = rec.get("type_text", "") or _TYPE_MAP.get(type_code, type_code)
    registered = (rec.get("registered") or "")[:10]
    terminated = (rec.get("terminated") or "")[:10]
    closed = rec.get("closed", "")
    address = rec.get("address", "")
    postal = rec.get("index", "")
    address_full = f"{address}, LV-{postal}" if address and postal else address

    is_dissolved = bool(terminated) or closed == "L"
    is_branch = type_code == "FIL"
    if is_dissolved:
        status = "DISSOLVED"
    elif is_branch:
        status = "ACTIVE (branch)"
    else:
        status = "ACTIVE"

    founded_year = registered[:4] if registered else None

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": regcode or None,
        "headquarters": address_full or None,
        "founded_year": founded_year,
        "is_listed": False,
        # LV-specific extras
        "regcode": regcode or None,
        "sepa": rec.get("sepa") or None,
        "name_in_quotes": name_in_quotes or None,
        "regtype": rec.get("regtype") or None,
        "regtype_text": regtype_text or None,
        "type_code": type_code or None,
        "type": type_text or None,
        "registered": registered or None,
        "terminated": terminated or None,
        "closed": closed if closed else None,
        "address": address or None,
        "postal_code": str(postal) if postal else None,
        "is_branch": is_branch,
        "status": status,
        "summary": (
            f"{name or entity_name} — regcode {regcode}"
            + (f" — {type_text}" if type_text else "")
            + f" — {status}"
        ),
    }


def _parse_lv(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    if not data.get("success"):
        return {"found": False, "error": "data.gov.lv returned success=false"}
    records = (data.get("result") or {}).get("records") or []
    if not records:
        return {"found": False}

    # Prefer main entities over branches (FIL)
    main = next((r for r in records if r.get("type") != "FIL"), None)
    best = main or records[0]
    result = _format_lv(best, entity_name)

    if len(records) > 1:
        result["alternatives"] = [
            {
                "regcode": str(r.get("regcode", "") or ""),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "closed": r.get("closed", ""),
            }
            for r in records if r is not best
        ][:5]
        result["total_matches"] = len(records)

    return result


LV_REGCODE_CONFIG = eng.CountryConfig(
    country_code="LV",
    source_name="Latvian Register of Enterprises (via data.gov.lv)",
    transport=eng.T_DIRECT_API,
    primary_url=(
        _BASE + "?resource_id=" + _RESOURCE_ID
        + "&filters=" + urllib.parse.quote('{"regcode": ') + "{q}" + urllib.parse.quote("}")
        + "&limit=5"
    ),
    parser=_parse_lv,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://www.ur.gov.lv/ → search regcode {entity}"
    ),
)

LV_NAME_CONFIG = eng.CountryConfig(
    country_code="LV",
    source_name="Latvian Register of Enterprises (via data.gov.lv)",
    transport=eng.T_DIRECT_API,
    primary_url=(
        _BASE + "?resource_id=" + _RESOURCE_ID
        + "&q={q}&limit=10"
    ),
    parser=_parse_lv,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://www.ur.gov.lv/ → search '{entity}'"
    ),
)


def lursoft_verify(entity_name: str, regcode: str = "") -> dict:
    """LV verify entry point — backward compat with main.py routing."""
    digits = re.sub(r"\D", "", regcode or "")
    if len(digits) == 11:
        return eng.run(LV_REGCODE_CONFIG, digits, {"regcode": digits})
    return eng.run(LV_NAME_CONFIG, entity_name, {})
