"""
Netherlands verify — runs on the generic engine.

Source: KvK (Kamer van Koophandel) public Handelsregister API.
Multilogin HTTP with NL exit IP.
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BASE = "https://zoeken.kvk.nl/HandelRegisterAPI/api/handelsregister"


def init(get_secret=None):
    log.info("NL verify ready (engine) — KvK Handelsregister via Multilogin")


def _parse_nl(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    results = data.get("resultaten") or []
    if not results:
        return {"found": False}

    best = results[0]

    name = best.get("handelsnaam", "")
    kvk = best.get("kvkNummer", "")
    status = (best.get("status") or "").upper() or "UNKNOWN"
    legal_form = best.get("rechtsvorm", "")
    place = best.get("plaats", "")
    street = best.get("straat", "")
    house_nr = best.get("huisnummer", "")
    postcode = best.get("postcode", "")

    addr_parts = [street, str(house_nr) if house_nr else "", postcode, place]
    address = " ".join(p for p in addr_parts if p).strip() or None

    trade_names = best.get("handelsnamen") or None
    sbi = best.get("spiActiviteiten") or best.get("sbiActiviteiten") or None

    others = [
        {
            "name": r.get("handelsnaam", ""),
            "kvk_number": r.get("kvkNummer", ""),
            "status": r.get("status", ""),
            "place": r.get("plaats", ""),
        }
        for r in results[1:5]
    ]

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": kvk or None,
        "headquarters": address,
        "industry": None,
        "is_listed": False,
        # NL-specific extras
        "kvk_number": kvk or None,
        "legal_form": legal_form or None,
        "registered_address": address,
        "city": place or None,
        "postal_code": postcode or None,
        "trade_names": trade_names,
        "sbi_codes": sbi,
        "total_matches": len(results),
        "other_matches": others or None,
        "status": status,
        "summary": (
            f"{name or entity_name} — KvK {kvk or 'N/A'} — {status}"
            + (f" — {legal_form}" if legal_form else "")
            + (f" ({place})" if place else "")
        ),
    }


NL_CONFIG = eng.CountryConfig(
    country_code="NL",
    source_name="KvK (Kamer van Koophandel), Netherlands",
    transport=eng.T_MLX_HTTP,
    primary_url=_BASE + "?{searchparam}={q}&pagina=1&start=0&pagesize=10",
    parser=_parse_nl,
    timeout=60,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://www.kvk.nl/zoeken/ → search '{entity}'"
    ),
)


def kvk_verify(entity_name: str, kvk_number: str = "") -> dict:
    """NL verify entry point — backward compat with main.py routing."""
    kvk_number = (kvk_number or "").strip()
    if kvk_number:
        clean = re.sub(r"[\s\-.]", "", kvk_number)
        if not re.match(r"^\d{8}$", clean):
            # Engine wrapper will surface the error cleanly
            return eng.run(NL_CONFIG, entity_name or "[invalid KVK]",
                           {"searchparam": "handelsnaam", "_error": "KVK must be 8 digits"})
        return eng.run(NL_CONFIG, clean, {"searchparam": "kvknummer"})
    return eng.run(NL_CONFIG, entity_name, {"searchparam": "handelsnaam"})
