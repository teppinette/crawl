"""
Czech Republic verify — runs on the generic engine.

Source: ARES (Administrativní registr ekonomických subjektů) — Czech
Ministry of Finance & Justice. Free public JSON, no auth.
Direct HTTP (gov.cz, no proxy needed).

Two configs sharing parser:
  CZ_ICO_CONFIG  — exact lookup by IČO (8 digits, the Czech company ID)
  CZ_NAME_CONFIG — name search
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BASE = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty"

# ARES returns pravniForma as a 3-digit code (not the description text).
# Map the common ones inline so callers see something readable.
_LEGAL_FORM_MAP = {
    "111": "Veřejná obchodní společnost",
    "112": "Komanditní společnost",
    "113": "Společnost s ručením omezeným (s.r.o.)",
    "121": "Akciová společnost (a.s.)",
    "205": "Družstvo",
    "421": "Družstvo",
    "601": "Státní podnik",
    "706": "Příspěvková organizace",
    "711": "Obecně prospěšná společnost",
    "805": "Sdružení",
    "101": "Fyzická osoba podnikající",
    "102": "Zahraniční fyzická osoba",
    "107": "Zemědělský podnikatel",
}


def init(get_secret=None):
    log.info("CZ verify ready (engine) — ARES (Justice/Finance Ministry) direct JSON")


def _format_addr(sidlo: dict) -> str:
    if not sidlo:
        return ""
    parts = [
        sidlo.get("nazevUlice", ""),
        str(sidlo.get("cisloDomovni", "")) if sidlo.get("cisloDomovni") else "",
        sidlo.get("nazevCastiObce", "") or sidlo.get("nazevMestskeCastiObvodu", ""),
        sidlo.get("nazevObce", ""),
        str(sidlo.get("psc", "")) if sidlo.get("psc") else "",
        sidlo.get("nazevStatu", ""),
    ]
    return ", ".join(p for p in parts if p)


def _format_cz(data: dict, entity_name: str) -> dict:
    ico = data.get("ico", "")
    name = data.get("obchodniJmeno", "")
    sidlo = data.get("sidlo") or {}
    address = _format_addr(sidlo) or None

    # ARES v3: pravniForma and pravniFormaRos are 3-digit codes, not descriptions.
    # Map to readable name via _LEGAL_FORM_MAP, fall back to the raw code.
    legal_form_code = data.get("pravniForma", "") or data.get("pravniFormaRos", "") or ""
    legal_form = _LEGAL_FORM_MAP.get(str(legal_form_code), str(legal_form_code) if legal_form_code else "")
    dic = data.get("dic", "") or ""  # Tax ID
    nace_list = data.get("czNace") or data.get("czNace2008") or []
    # NACE entries are strings like "55900" — keep the first as primary code
    nace_code = nace_list[0] if nace_list and isinstance(nace_list[0], str) else None
    industry = None  # NACE description requires separate ciselniky lookup; leave None

    datum_vzniku = data.get("datumVzniku", "")  # Registration date YYYY-MM-DD
    datum_zaniku = data.get("datumZaniku", "")  # Dissolution date if any
    founded_year = datum_vzniku[:4] if datum_vzniku and len(datum_vzniku) >= 4 else None
    is_dissolved = bool(datum_zaniku)
    status = "DISSOLVED" if is_dissolved else "ACTIVE"

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": ico or None,
        "headquarters": address,
        "founded_year": founded_year,
        "industry": industry,
        "is_listed": False,
        # CZ-specific extras
        "ico": ico or None,
        "dic": dic or None,
        "legal_form": legal_form or None,
        "legal_form_code": str(legal_form_code) if legal_form_code else None,
        "nace_code": nace_code,
        "datum_vzniku": datum_vzniku or None,
        "datum_zaniku": datum_zaniku or None,
        "city": sidlo.get("nazevObce") or None,
        "region": sidlo.get("nazevKraje") or None,
        "postal_code": str(sidlo.get("psc")) if sidlo.get("psc") else None,
        "status": status,
        "summary": (
            f"{name} — IČO {ico} — {status}"
            + (f" — {legal_form}" if legal_form else "")
        ),
    }


def _parse_cz_direct(raw: dict, entity_name: str, ids: dict) -> dict:
    if raw.get("status") == 404:
        return {"found": False, "note": "IČO not found in ARES"}
    data = raw.get("json")
    if not isinstance(data, dict) or not data.get("ico"):
        return {"found": False}
    return _format_cz(data, entity_name)


def _parse_cz_search(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    hits = data.get("ekonomickeSubjekty") or []
    if not hits:
        return {"found": False}
    best = hits[0]
    result = _format_cz(best, entity_name)
    if len(hits) > 1:
        result["alternatives"] = [
            {
                "ico": h.get("ico", ""),
                "obchodniJmeno": h.get("obchodniJmeno", ""),
                "city": (h.get("sidlo") or {}).get("nazevObce", ""),
            }
            for h in hits[1:5]
        ]
        result["total_matches"] = len(hits)
    return result


CZ_ICO_CONFIG = eng.CountryConfig(
    country_code="CZ",
    source_name="ARES (Justice/Finance Ministry), Czech Republic",
    transport=eng.T_MLX_HTTP,
    primary_url=_BASE + "/{ico}",
    parser=_parse_cz_direct,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://ares.gov.cz/ekonomicke-subjekty → enter IČO {entity}"
    ),
)

CZ_NAME_CONFIG = eng.CountryConfig(
    country_code="CZ",
    source_name="ARES (Justice/Finance Ministry), Czech Republic",
    transport=eng.T_MLX_HTTP,
    method="POST",
    body_builder=lambda entity, ids: {"obchodniJmeno": entity, "start": 0, "pocet": 10},
    primary_url=_BASE + "/vyhledat",
    parser=_parse_cz_search,
    timeout=15,
    headers={"Accept": "application/json", "Content-Type": "application/json"},
    how_to_reproduce_template=(
        "Visit https://ares.gov.cz/ekonomicke-subjekty → search '{entity}'"
    ),
)


def ares_verify(entity_name: str, ico: str = "") -> dict:
    """CZ verify entry point — backward compat with main.py routing."""
    digits = re.sub(r"\D", "", ico or "")
    if len(digits) == 8:
        return eng.run(CZ_ICO_CONFIG, digits, {"ico": digits})
    return eng.run(CZ_NAME_CONFIG, entity_name, {})
