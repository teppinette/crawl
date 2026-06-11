"""
Norway verify — runs on the generic engine.

Source: Brønnøysundregistrene (BRREG) Enhetsregisteret REST API.
Free, no auth, public JSON. Direct HTTP (gov.no, no proxy needed).

Two configs sharing parser:
  NO_DIRECT_CONFIG — exact lookup by organisasjonsnummer (9 digits)
  NO_NAME_CONFIG   — name search via ?navn=<query>
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BASE = "https://data.brreg.no/enhetsregisteret/api/enheter"


def init(get_secret=None):
    log.info("NO verify ready (engine) — Brønnøysundregistrene direct JSON")


def _parse_no_direct(raw: dict, entity_name: str, ids: dict) -> dict:
    if raw.get("status") == 404:
        return {"found": False, "note": "Org-nr not found in BRREG"}
    data = raw.get("json")
    if not isinstance(data, dict) or not data.get("navn"):
        return {"found": False}
    return _format_no(data, entity_name)


def _parse_no_search(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    hits = (data.get("_embedded") or {}).get("enheter") or []
    if not hits:
        return {"found": False}
    best = hits[0]
    result = _format_no(best, entity_name)
    if len(hits) > 1:
        result["alternatives"] = [
            {
                "navn": h.get("navn", ""),
                "organisasjonsnummer": h.get("organisasjonsnummer", ""),
                "organisasjonsform": (h.get("organisasjonsform") or {}).get("kode", ""),
            }
            for h in hits[1:5]
        ]
        result["total_matches"] = len(hits)
    return result


def _format_no(data: dict, entity_name: str) -> dict:
    org_nr = data.get("organisasjonsnummer", "")
    navn = data.get("navn", "")
    org_form = (data.get("organisasjonsform") or {}).get("beskrivelse", "")
    reg_date = data.get("registreringsdatoEnhetsregisteret", "")
    addr_obj = data.get("forretningsadresse") or {}
    addr_lines = addr_obj.get("adresse") or []
    addr_full = ", ".join(
        p for p in (
            *(addr_lines or []),
            addr_obj.get("postnummer", ""),
            addr_obj.get("poststed", ""),
            addr_obj.get("land", ""),
        ) if p
    ) or None

    is_konkurs = bool(data.get("konkurs"))
    is_dissolved = bool(data.get("slettet"))
    status = "DISSOLVED" if is_dissolved else ("BANKRUPT" if is_konkurs else "ACTIVE")
    founded_year = reg_date[:4] if reg_date else None

    return {
        "found": True,
        "legal_name": navn or entity_name,
        "business_registration_number": org_nr or None,
        "headquarters": addr_full,
        "founded_year": founded_year,
        "is_listed": False,
        # NO-specific extras
        "organisasjonsnummer": org_nr or None,
        "organisasjonsform": org_form or None,
        "registreringsdato": reg_date or None,
        "konkurs": is_konkurs,
        "under_avvikling": bool(data.get("underAvvikling")),
        "slettet": is_dissolved,
        "homepage": data.get("hjemmeside") or None,
        "naeringskode": (data.get("naeringskode1") or {}).get("kode") or None,
        "industry": (data.get("naeringskode1") or {}).get("beskrivelse") or None,
        "antall_ansatte": data.get("antallAnsatte"),
        "status": status,
        "summary": (
            f"{navn} — org-nr {org_nr} — {status}"
            + (f" — {org_form}" if org_form else "")
        ),
    }


NO_DIRECT_CONFIG = eng.CountryConfig(
    country_code="NO",
    source_name="Brønnøysundregistrene (Enhetsregisteret), Norway",
    transport=eng.T_DIRECT_API,
    primary_url=_BASE + "/{org_nr}",
    parser=_parse_no_direct,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://w2.brreg.no/enhetsregisteret/ → enter org-nr {entity}"
    ),
)

NO_NAME_CONFIG = eng.CountryConfig(
    country_code="NO",
    source_name="Brønnøysundregistrene (Enhetsregisteret), Norway",
    transport=eng.T_DIRECT_API,
    primary_url=_BASE + "?navn={q}&size=5",
    parser=_parse_no_search,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://w2.brreg.no/enhetsregisteret/ → search '{entity}'"
    ),
)


def brreg_verify(entity_name: str, org_number: str = "") -> dict:
    """NO verify entry point — backward compat with main.py routing."""
    digits = re.sub(r"\D", "", org_number or "")
    if len(digits) == 9:
        return eng.run(NO_DIRECT_CONFIG, digits, {"org_nr": digits})
    return eng.run(NO_NAME_CONFIG, entity_name, {})
