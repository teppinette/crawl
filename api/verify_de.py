"""
Germany company verification via EU VIES (VAT Information Exchange System).

Source: https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
Free API — official EU tax validation, returns company name + address.

Input: vat_id (USt-IdNr, 9 digits) or hrb (Handelsregister number)
Returns: legal_name, vat_id, status (valid/invalid), address
"""

import logging
import re
import time

from mlx_http import mlx_post

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def init(get_secret=None):
    log.info("DE VIES ready (EU VAT validation for Germany)")


def handelsregister_verify(entity_name: str, hrb: str = "", vat_id: str = "") -> dict:
    if not vat_id and not hrb:
        return {
            "entity_name": entity_name, "country_code": "DE",
            "found": False,
            "note": "USt-IdNr (VAT ID, 9 digits) or HRB number required for Germany verification. "
                    "Name-only search not available via VIES.",
        }

    if vat_id:
        clean = re.sub(r"[\s\-.]", "", vat_id.strip())
        clean = re.sub(r"^DE", "", clean, flags=re.IGNORECASE)
        if not re.match(r"^\d{9}$", clean):
            return {"vat_id": vat_id, "found": False,
                    "error": "USt-IdNr must be 9 digits (without DE prefix)"}
        try:
            return _check_vies("DE", clean, entity_name)
        except Exception as e:
            log.error("DE VIES error for %s: %s", vat_id, e)
            return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}

    # HRB only — can't use VIES without VAT ID
    return {
        "entity_name": entity_name, "country_code": "DE",
        "hrb": hrb,
        "found": False,
        "note": f"HRB {hrb} provided but VIES requires a USt-IdNr (VAT ID) for verification. "
                "Handelsregister direct lookup not yet implemented.",
    }


def _check_vies(country: str, vat: str, entity_name: str) -> dict:
    result = mlx_post(
        _VIES_URL,
        json_body={"countryCode": country, "vatNumber": vat},
        headers={"Accept": "application/json"},
        timeout=60, country_code="de",
    )
    if not result.get("ok"):
        raise RuntimeError(f"VIES returned HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or {}

    valid = data.get("valid", False)
    name = data.get("name", "").strip().replace("---", "")
    address = data.get("address", "").strip().replace("---", "")

    return {
        "entity_name": name or entity_name,
        "query_name": entity_name,
        "country_code": "DE",
        "found": valid,
        "vat_id": f"DE{vat}",
        "status": "ACTIVE" if valid else "INVALID",
        "registered_address": address or None,
        "request_date": data.get("requestDate", ""),
        "source": "VIES (EU VAT Information Exchange System)",
        "validation_source": {
            "registry": "VIES — EU VAT Information Exchange System (Bundeszentralamt für Steuern data)",
            "url": "https://ec.europa.eu/taxation_customs/vies/",
            "record_id": f"DE{vat}",
            "how_to_reproduce": f"Visit VIES portal → Country: DE → VAT: {vat} → Verify",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
