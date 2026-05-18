"""
Belgium company verification via EU VIES (VAT Information Exchange System).

Source: https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
Free API — official EU tax validation, returns company name + address.

Input: cbe_number (KBO/BCE number, 10 digits: 0xxx.xxx.xxx)
Returns: legal_name, cbe_number, status (valid/invalid), address
"""

import logging
import re
import time

from mlx_http import mlx_post

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def init(get_secret=None):
    log.info("BE VIES ready (EU VAT validation for Belgium)")


def kbo_verify(entity_name: str, cbe_number: str = "") -> dict:
    if not cbe_number:
        return {
            "entity_name": entity_name, "country_code": "BE",
            "found": False,
            "note": "CBE/KBO number (10 digits, e.g. 0123.456.789) required for Belgium verification. "
                    "Name-only search not available via VIES.",
        }

    clean = re.sub(r"[\s\-.]", "", cbe_number.strip())
    clean = re.sub(r"^BE", "", clean, flags=re.IGNORECASE)
    if not re.match(r"^\d{10}$", clean):
        return {"cbe_number": cbe_number, "found": False,
                "error": "CBE number must be 10 digits (e.g. 0123456789)"}

    try:
        return _check_vies("BE", clean, entity_name)
    except Exception as e:
        log.error("BE VIES error for %s: %s", cbe_number, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _check_vies(country: str, vat: str, entity_name: str) -> dict:
    result = mlx_post(
        _VIES_URL,
        json_body={"countryCode": country, "vatNumber": vat},
        headers={"Accept": "application/json"},
        timeout=60, country_code="be",
    )
    if not result.get("ok"):
        raise RuntimeError(f"VIES returned HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or {}

    valid = data.get("valid", False)
    name = data.get("name", "").strip().replace("---", "")
    address = data.get("address", "").strip().replace("---", "")

    formatted_cbe = f"{vat[0]}{vat[1:4]}.{vat[4:7]}.{vat[7:10]}"

    return {
        "entity_name": name or entity_name,
        "query_name": entity_name,
        "country_code": "BE",
        "found": valid,
        "cbe_number": formatted_cbe,
        "vat_number": f"BE{vat}",
        "status": "ACTIVE" if valid else "INVALID",
        "registered_address": address or None,
        "request_date": data.get("requestDate", ""),
        "source": "VIES (EU VAT Information Exchange System)",
        "validation_source": {
            "registry": "VIES — EU VAT Information Exchange System (KBO/BCE data)",
            "url": "https://ec.europa.eu/taxation_customs/vies/",
            "record_id": f"BE{vat}",
            "how_to_reproduce": f"Visit VIES portal → Country: BE → VAT: {vat} → Verify",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
