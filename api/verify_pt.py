"""
Portugal company verification via EU VIES (VAT Information Exchange System).

Source: https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
Free API — official EU tax validation, returns company name + address.

Input: nipc (9-digit tax ID / NIF)
Returns: legal_name, nipc, status (valid/invalid), address
"""

import logging
import re
import time

from mlx_http import mlx_post

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def init(get_secret=None):
    log.info("PT VIES ready (EU VAT validation for Portugal)")


def mj_verify(entity_name: str, nipc: str = "") -> dict:
    if not nipc:
        return {
            "entity_name": entity_name, "country_code": "PT",
            "found": False,
            "note": "NIPC/NIF (9 digits) required for Portugal verification. "
                    "Name-only search not available via VIES.",
        }

    clean = re.sub(r"[\s\-.]", "", nipc.strip())
    clean = re.sub(r"^PT", "", clean, flags=re.IGNORECASE)
    if not re.match(r"^\d{9}$", clean):
        return {"nipc": nipc, "found": False,
                "error": "NIPC must be 9 digits"}

    try:
        return _check_vies("PT", clean, entity_name)
    except Exception as e:
        log.error("PT VIES error for %s: %s", nipc, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _check_vies(country: str, vat: str, entity_name: str) -> dict:
    result = mlx_post(
        _VIES_URL,
        json_body={"countryCode": country, "vatNumber": vat},
        headers={"Accept": "application/json"},
        timeout=60, country_code="pt",
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
        "country_code": "PT",
        "found": valid,
        "nipc": vat,
        "vat_number": f"PT{vat}",
        "status": "ACTIVE" if valid else "INVALID",
        "registered_address": address or None,
        "request_date": data.get("requestDate", ""),
        "source": "VIES (EU VAT Information Exchange System)",
        "validation_source": {
            "registry": "VIES — EU VAT Information Exchange System (Autoridade Tributária data)",
            "url": "https://ec.europa.eu/taxation_customs/vies/",
            "record_id": f"PT{vat}",
            "how_to_reproduce": f"Visit VIES portal → Country: PT → VAT: {vat} → Verify",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
