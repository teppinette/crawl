"""
Germany verify — runs on the generic engine.

Source: EU VIES (VAT Information Exchange System).
Returns legal name + address for valid VAT IDs (USt-IdNr).
Multilogin POST with DE exit IP. VIES doesn't expose name search;
USt-IdNr or HRB is required.
"""

import logging
import re
import time

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def init(get_secret=None):
    log.info("DE verify ready (engine) — VIES via Multilogin POST")


def _de_body(entity_name: str, ids: dict) -> dict:
    vat = ids.get("vat_clean") or ""
    return {"countryCode": "DE", "vatNumber": vat}


def _parse_de(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    valid = bool(data.get("valid"))
    if not valid:
        return {"found": False, "note": "VIES marked VAT ID invalid"}

    name = (data.get("name") or "").strip().replace("---", "")
    address = (data.get("address") or "").strip().replace("---", "")
    vat = ids.get("vat_clean") or ""

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": f"DE{vat}",
        "headquarters": address or None,
        "is_listed": False,
        # DE-specific extras
        "vat_id": f"DE{vat}",
        "registered_address": address or None,
        "request_date": data.get("requestDate", "") or None,
        "status": "ACTIVE",
        "summary": f"{name or entity_name} — VAT DE{vat} — VIES verified",
    }


DE_CONFIG = eng.CountryConfig(
    country_code="DE",
    source_name="VIES (EU VAT Information Exchange System)",
    transport=eng.T_MLX_HTTP,
    method="POST",
    body_builder=_de_body,
    primary_url=_VIES_URL,
    parser=_parse_de,
    timeout=60,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://ec.europa.eu/taxation_customs/vies/ → "
        "country DE → enter VAT {entity} → check"
    ),
)


def handelsregister_verify(entity_name: str, hrb: str = "", vat_id: str = "") -> dict:
    """DE verify entry point — backward compat with main.py routing."""
    if not vat_id:
        return {
            "entity_name": entity_name, "country_code": "DE",
            "found": False, "verified": False,
            "note": ("USt-IdNr (VAT ID, 9 digits) required for Germany verification. "
                     "VIES does not support name-only search. "
                     "If only HRB provided, direct Handelsregister lookup not yet implemented."),
            "hrb": hrb or None,
            "source": "VIES (EU VAT Information Exchange System)",
        }

    clean = re.sub(r"[\s\-.]", "", vat_id.strip())
    clean = re.sub(r"^DE", "", clean, flags=re.IGNORECASE)
    if not re.match(r"^\d{9}$", clean):
        return {
            "entity_name": entity_name, "country_code": "DE",
            "vat_id": vat_id, "found": False, "verified": False,
            "error": "USt-IdNr must be 9 digits (without DE prefix)",
        }

    return eng.run(DE_CONFIG, entity_name or f"DE{clean}", {"vat_clean": clean})
