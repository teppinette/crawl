"""
Italy company verification via EU VIES (VAT Information Exchange System).

Source: https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
Free API — official EU tax validation, returns company name + address.

Input: partita_iva (P.IVA, 11 digits)
Returns: legal_name, partita_iva, status (valid/invalid), address
"""

import logging
import re
import time

from mlx_http import mlx_post

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


def init(get_secret=None):
    log.info("IT VIES ready (EU VAT validation for Italy)")


def registroimprese_verify(entity_name: str, partita_iva: str = "") -> dict:
    if not partita_iva:
        return {
            "entity_name": entity_name, "country_code": "IT",
            "found": False,
            "note": "Partita IVA (P.IVA, 11 digits) required for Italy verification. "
                    "Name-only search not available via VIES.",
        }

    clean = re.sub(r"[\s\-.]", "", partita_iva.strip())
    clean = re.sub(r"^IT", "", clean, flags=re.IGNORECASE)
    if not re.match(r"^\d{11}$", clean):
        return {"partita_iva": partita_iva, "found": False,
                "error": "P.IVA must be 11 digits"}

    try:
        return _check_vies("IT", clean, entity_name)
    except Exception as e:
        log.error("IT VIES error for %s: %s", partita_iva, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _check_vies(country: str, vat: str, entity_name: str) -> dict:
    result = mlx_post(
        _VIES_URL,
        json_body={"countryCode": country, "vatNumber": vat},
        headers={"Accept": "application/json"},
        timeout=60, country_code="it",
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
        "country_code": "IT",
        "found": valid,
        "partita_iva": vat,
        "vat_number": f"IT{vat}",
        "status": "ACTIVE" if valid else "INVALID",
        "registered_address": address or None,
        "request_date": data.get("requestDate", ""),
        "source": "VIES (EU VAT Information Exchange System)",
        "validation_source": {
            "registry": "VIES — EU VAT Information Exchange System (Agenzia delle Entrate data)",
            "url": "https://ec.europa.eu/taxation_customs/vies/",
            "record_id": f"IT{vat}",
            "how_to_reproduce": f"Visit VIES portal → Country: IT → VAT: {vat} → Verify",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
