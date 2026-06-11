"""
Denmark verify — runs on the generic engine.

Source: cvrapi.dk — public wrapper around the official Danish CVR
(Centrale Virksomhedsregister). Free for non-commercial use, no auth.
Direct HTTP (dk gov data, no proxy needed).
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_API = "https://cvrapi.dk/api"


def init(get_secret=None):
    log.info("DK verify ready (engine) — cvrapi.dk public CVR wrapper")


def _parse_dk(raw: dict, entity_name: str, ids: dict) -> dict:
    if raw.get("status") == 404:
        return {"found": False, "note": "CVR: not found"}
    if raw.get("status") == 429:
        return {"found": False, "error": "CVR API rate limit hit"}
    data = raw.get("json")
    if not isinstance(data, dict) or data.get("error") or not data.get("name"):
        return {"found": False, "note": (data or {}).get("error") or "CVR: no data"}

    name = data.get("name", "")
    vat = str(data.get("vat", "") or "")
    status = (data.get("status", "") or "UNKNOWN").upper()
    address = ", ".join(
        p for p in (
            data.get("address", ""),
            data.get("zipcode", ""),
            data.get("city", ""),
            data.get("country", ""),
        ) if p
    ) or None
    industry_code = str(data.get("industrycode", "") or "")
    industry_desc = data.get("industrydesc", "") or None
    company_type = data.get("companytype", "") or None
    started = data.get("startdate", "")
    founded_year = started[-4:] if started and len(started) >= 4 and started[-4:].isdigit() else None

    return {
        "found": True,
        "legal_name": name,
        "business_registration_number": vat or None,
        "headquarters": address,
        "founded_year": founded_year,
        "industry": industry_desc,
        "is_listed": False,
        # DK-specific extras
        "cvr": vat or None,
        "vat": vat or None,
        "company_type": company_type,
        "industry_code": industry_code or None,
        "phone": data.get("phone") or None,
        "email": data.get("email") or None,
        "homepage": data.get("homepage") or None,
        "employees": data.get("employees") or None,
        "start_date": started or None,
        "status": status,
        "summary": f"CVR {vat}: {name} (status={status.lower()})",
    }


DK_DIRECT_CONFIG = eng.CountryConfig(
    country_code="DK",
    source_name="cvrapi.dk (Danish CVR wrapper)",
    transport=eng.T_MLX_HTTP,
    primary_url=_API + "?vat={cvr}&country=dk",
    parser=_parse_dk,
    timeout=12,
    headers={"User-Agent": "COPAP-Crawl/1.0"},
    how_to_reproduce_template=(
        "Visit https://datacvr.virk.dk/ → search CVR {entity}"
    ),
)

DK_NAME_CONFIG = eng.CountryConfig(
    country_code="DK",
    source_name="cvrapi.dk (Danish CVR wrapper)",
    transport=eng.T_MLX_HTTP,
    primary_url=_API + "?search={q}&country=dk",
    parser=_parse_dk,
    timeout=12,
    headers={"User-Agent": "COPAP-Crawl/1.0"},
    how_to_reproduce_template=(
        "Visit https://datacvr.virk.dk/ → search '{entity}'"
    ),
)


def cvr_verify(entity_name: str, cvr: str = "") -> dict:
    """DK verify entry point — backward compat with main.py routing."""
    digits = re.sub(r"\D", "", cvr or "")
    if len(digits) == 8:
        return eng.run(DK_DIRECT_CONFIG, digits, {"cvr": digits})
    return eng.run(DK_NAME_CONFIG, entity_name, {})
