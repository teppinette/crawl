"""
Source: VIES — EU VAT Information Exchange System.

ONE adapter for every EU VAT country (BE, DE, ES, IT, PT — and any other
EU country if added later). The per-country verify_xx.py files are thin
shims that call vies_verify(country_code, entity_name, vat_id).

VIES requires the VAT number — name-only search is not supported. The API
takes {countryCode, vatNumber} POST and returns {valid, name, address,
requestDate}. Multilogin POST with the appropriate country exit IP.
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"

_INITED = False


def init(get_secret=None):
    """Idempotent init. VIES needs no config beyond mlx_http (already initialised by main.py)."""
    global _INITED
    if not _INITED:
        log.info("source_vies ready (engine) — VIES via Multilogin POST")
        _INITED = True


def _body(entity_name: str, ids: dict) -> dict:
    return {
        "countryCode": ids["country_code"],
        "vatNumber": ids["vat_clean"],
    }


def _parse(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    valid = bool(data.get("valid"))
    if not valid:
        return {
            "found": False,
            "note": f"VIES marked VAT {ids['country_code']}{ids['vat_clean']} as invalid",
        }

    name = (data.get("name") or "").strip().replace("---", "")
    address = (data.get("address") or "").strip().replace("---", "")
    cc = ids["country_code"]
    vat = ids["vat_clean"]

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": f"{cc}{vat}",
        "headquarters": address or None,
        "is_listed": False,
        # EU-specific extras (passed through by engine)
        "vat_id": f"{cc}{vat}",
        "registered_address": address or None,
        "request_date": data.get("requestDate", "") or None,
        "status": "ACTIVE",
        "summary": (
            f"{name or entity_name} — VAT {cc}{vat} — VIES verified"
            + (f" — {address.split(',')[0]}" if address else "")
        ),
    }


# Engine config is identical across EU countries except country_code →
# cache one per country. Built lazily on first call.
_CONFIG_CACHE: dict[str, eng.CountryConfig] = {}


def _config_for(cc: str) -> eng.CountryConfig:
    if cc in _CONFIG_CACHE:
        return _CONFIG_CACHE[cc]
    cfg = eng.CountryConfig(
        country_code=cc,
        source_name=f"VIES (EU VAT Information Exchange System) — {cc}",
        transport=eng.T_MLX_HTTP,
        method="POST",
        body_builder=_body,
        primary_url=_VIES_URL,
        parser=_parse,
        timeout=60,
        headers={"Accept": "application/json"},
        how_to_reproduce_template=(
            f"Visit https://ec.europa.eu/taxation_customs/vies/ → "
            f"country {cc} → enter VAT {{entity}} → check"
        ),
    )
    _CONFIG_CACHE[cc] = cfg
    return cfg


def vies_verify(country_code: str, entity_name: str, vat_id: str = "") -> dict:
    """Shared VIES verify — country_code is the ISO 2-char code (BE/DE/ES/IT/PT/...)."""
    cc = (country_code or "").upper().strip()
    if not cc:
        return {
            "entity_name": entity_name, "found": False, "verified": False,
            "error": "country_code required",
        }

    if not vat_id:
        return {
            "entity_name": entity_name, "country_code": cc,
            "found": False, "verified": False,
            "note": (
                f"VAT ID required for {cc} — VIES does not support name-only search."
            ),
            "source": "VIES (EU VAT Information Exchange System)",
        }

    # Strip country prefix + non-alphanumerics
    clean = re.sub(r"[\s\-.]", "", vat_id.strip())
    clean = re.sub(rf"^{cc}", "", clean, flags=re.IGNORECASE)
    if not re.match(r"^[0-9A-Z]+$", clean):
        return {
            "entity_name": entity_name, "country_code": cc,
            "vat_id": vat_id, "found": False, "verified": False,
            "error": "VAT ID must be alphanumeric after country prefix",
        }

    return eng.run(
        _config_for(cc),
        entity_name or f"{cc}{clean}",
        {"country_code": cc, "vat_clean": clean},
    )
