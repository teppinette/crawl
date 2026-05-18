"""
Spain company verification via EU VIES (VAT Information Exchange System).

Source: https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
Free API — official EU tax validation, returns company name + address.

Input: cif (CIF/NIF, 9 characters: letter + 7 digits + check)
Returns: legal_name, cif, status (valid/invalid), address
"""

import logging
import re
import time

from mlx_http import mlx_post

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def init(get_secret=None):
    log.info("ES VIES ready (EU VAT validation for Spain)")


def borme_verify(entity_name: str, cif: str = "") -> dict:
    if not cif:
        return {
            "entity_name": entity_name, "country_code": "ES",
            "found": False,
            "note": "CIF/NIF required for Spain verification. "
                    "Name-only search not available via VIES.",
        }

    clean = re.sub(r"[\s\-.]", "", cif.strip())
    clean = re.sub(r"^ES", "", clean, flags=re.IGNORECASE)
    if not re.match(r"^[A-Z]\d{7}[A-Z0-9]$", clean, re.IGNORECASE):
        return {"cif": cif, "found": False,
                "error": "CIF must be letter + 7 digits + letter/digit (e.g. B12345678)"}

    try:
        return _check_vies("ES", clean.upper(), entity_name)
    except Exception as e:
        log.error("ES VIES error for %s: %s", cif, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _check_vies(country: str, vat: str, entity_name: str) -> dict:
    result = mlx_post(
        _VIES_URL,
        json_body={"countryCode": country, "vatNumber": vat},
        headers={"Accept": "application/json"},
        timeout=60, country_code="es",
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
        "country_code": "ES",
        "found": valid,
        "cif": vat,
        "vat_number": f"ES{vat}",
        "status": "ACTIVE" if valid else "INVALID",
        "registered_address": address or None,
        "request_date": data.get("requestDate", ""),
        "source": "VIES (EU VAT Information Exchange System)",
        "validation_source": {
            "registry": "VIES — EU VAT Information Exchange System (Agencia Tributaria data)",
            "url": "https://ec.europa.eu/taxation_customs/vies/",
            "record_id": f"ES{vat}",
            "how_to_reproduce": f"Visit VIES portal → Country: ES → VAT: {vat} → Verify",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
