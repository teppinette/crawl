"""
Greece verify — runs on the generic engine via Multilogin.

Source: Γ.Ε.ΜΗ. / GEMI (General Commercial Registry, Hellenic Republic) at
publicity.businessportal.gr — the official Greek company registry. The site is
a JS SPA behind anti-bot protection, so it is hit through the Multilogin
browser (T_MLX_NAVIGATE) with a GR exit IP; a plain GET returns the empty SPA
shell / 403.

Two lookup modes:
  - by GEMI number  → navigate straight to the company publicity page
  - by name         → navigate the public search, parse the first result

When an ΑΦΜ (Greek VAT, 9 digits) is discovered or supplied, VIES (the EU
official VAT system, country code "EL" for Greece — NOT "GR") is used as an
authoritative cross-confirmation of the registered name + address + active
status (source_vies, also Multilogin).
"""

import logging
import re

import verify_engine as eng
import source_vies

log = logging.getLogger("verify-gateway")

# Public GEMI publicity portal (General Commercial Registry).
_COMPANY_URL = "https://publicity.businessportal.gr/company/{gemi}"
_SEARCH_URL = "https://publicity.businessportal.gr/?generalSearch={q}"

# Greek legal forms (Latin + native). Α.Ε.Β.Ε. = industrial-commercial S.A.
_LEGAL_FORMS = [
    ("Α.Ε.Β.Ε.", "AEBE"), ("Α.Β.Ε.Ε.", "ABEE"), ("Α.Ε.", "AE"),
    ("Ε.Π.Ε.", "EPE"), ("Ι.Κ.Ε.", "IKE"), ("Ο.Ε.", "OE"), ("Ε.Ε.", "EE"),
    ("ΜΟΝΟΠΡΟΣΩΠΗ", "MONOPROSOPI"),
]

# GEMI status vocabulary → normalized.
_STATUS_MAP = [
    ("ΕΝΕΡΓΗ", "ACTIVE"), ("ΕΝΕΡΓΟ", "ACTIVE"), ("ΕΝΕΡΓΗ ΕΤΑΙΡΕΙΑ", "ACTIVE"),
    ("ΥΠΟ ΕΚΚΑΘΑΡΙΣΗ", "IN_LIQUIDATION"), ("ΕΚΚΑΘΑΡΙΣΗ", "IN_LIQUIDATION"),
    ("ΑΝΑΣΤΟΛΗ", "SUSPENDED"),
    ("ΔΙΑΓΡΑΦΕΙΣΑ", "DISSOLVED"), ("ΔΙΑΓΡΑΜΜΕΝΗ", "DISSOLVED"),
    ("ΛΥΘΕΙΣΑ", "DISSOLVED"), ("ΛΥΜΕΝΗ", "DISSOLVED"),
    ("ΠΤΩΧΕΥΣΗ", "BANKRUPT"),
]


def init(get_secret=None):
    log.info("GR verify ready (engine) — GEMI publicity via Multilogin + VIES(EL) enrichment")


def _find_status(src_upper: str) -> str:
    for kw, mapped in _STATUS_MAP:
        if kw in src_upper:
            return mapped
    return "UNKNOWN"


def _find_legal_form(src_upper: str) -> str | None:
    for native, latin in _LEGAL_FORMS:
        if native in src_upper:
            return latin
    return None


def _parse_gr(raw: dict, entity_name: str, ids: dict) -> dict:
    src = (raw.get("body") or "") + "\n" + (raw.get("html") or "")
    if not src.strip():
        return {"found": False, "error": "empty_response"}
    up = src.upper()

    # ΑΦΜ (VAT) — label ΑΦΜ / Α.Φ.Μ. followed by 9 digits.
    afm = None
    m = re.search(r"Α\.?\s*Φ\.?\s*Μ\.?[^0-9]{0,12}(\d{9})", src)
    if m:
        afm = m.group(1)
    else:
        m = re.search(r"\bAFM\b[^0-9]{0,12}(\d{9})", up)
        if m:
            afm = m.group(1)

    # GEMI number — label ΓΕΜΗ / ΑΡ.ΓΕΜΗ / Γ.Ε.ΜΗ. followed by ~9-13 digits,
    # or use the id we navigated with.
    gemi = (ids.get("gemi_number") or "").strip() or None
    if not gemi:
        m = re.search(r"Γ\.?\s*Ε\.?\s*ΜΗ?\.?[^0-9]{0,14}(\d{9,13})", src)
        if not m:
            m = re.search(r"\bGEMI\b[^0-9]{0,14}(\d{9,13})", up)
        if m:
            gemi = m.group(1)

    status = _find_status(up)
    legal_form = _find_legal_form(up)

    found = bool(gemi or afm)
    if not found:
        return {"found": False, "note": "GEMI: no ΓΕΜΗ/ΑΦΜ found in page"}

    return {
        "found": True,
        "legal_name": entity_name,
        "business_registration_number": gemi or afm,
        "is_listed": False,
        # GR-specific extras
        "gemi_number": gemi,
        "afm": afm,
        "vat_id": f"EL{afm}" if afm else None,
        "legal_form": legal_form,
        "status": status,
        "summary": (
            f"{entity_name} — "
            + (f"ΓΕΜΗ {gemi}" if gemi else f"ΑΦΜ {afm}")
            + (f" — {status}" if status != "UNKNOWN" else "")
            + (f" — {legal_form}" if legal_form else "")
        ),
    }


def _vies_enrich(entity_name: str, ids: dict, extracted: dict) -> dict:
    """Cross-confirm the GEMI hit against VIES (EU VAT, country code EL)."""
    afm = extracted.get("afm") or ids.get("afm")
    if not afm:
        return {}
    try:
        v = source_vies.vies_verify("EL", extracted.get("legal_name") or entity_name, afm)
    except Exception as e:
        log.debug("GR VIES enrich failed: %s", e)
        return {}
    if not v.get("found"):
        return {}
    out = {
        "enrichment_source": "VIES (EU VAT Information Exchange System) — EL",
        "enrichment_url": "https://ec.europa.eu/taxation_customs/vies/",
        "vies_confirmed": True,
        "status": "ACTIVE",  # VIES only returns a record for a valid/active VAT
    }
    if v.get("legal_name"):
        out["legal_name"] = v["legal_name"]
    if v.get("registered_address"):
        out["registered_address"] = v["registered_address"]
        out["headquarters"] = v["registered_address"]
    return out


GR_CONFIG = eng.CountryConfig(
    country_code="GR",
    source_name="Γ.Ε.ΜΗ. / GEMI (General Commercial Registry), Hellenic Republic",
    transport=eng.T_MLX_NAVIGATE,
    primary_url=_SEARCH_URL,
    parser=_parse_gr,
    enrichment=_vies_enrich,
    wait_s=6,          # SPA renders search results via XHR after load
    timeout=75,
    how_to_reproduce_template=(
        "Visit https://publicity.businessportal.gr/ → search '{entity}' "
        "(General Commercial Registry — Γ.Ε.ΜΗ.)"
    ),
)

# Direct-by-GEMI-number config (navigate straight to the company page).
GR_COMPANY_CONFIG = eng.CountryConfig(
    country_code="GR",
    source_name="Γ.Ε.ΜΗ. / GEMI (General Commercial Registry), Hellenic Republic",
    transport=eng.T_MLX_NAVIGATE,
    primary_url=_COMPANY_URL,
    parser=_parse_gr,
    enrichment=_vies_enrich,
    wait_s=6,
    timeout=75,
    how_to_reproduce_template=(
        "Visit https://publicity.businessportal.gr/company/{entity} "
        "(General Commercial Registry — Γ.Ε.ΜΗ.)"
    ),
)


def gemi_verify(entity_name: str, gemi_number: str = "", afm: str = "") -> dict:
    """GR verify entry point — backward compat with main.py/VM routing.

    - gemi_number given → navigate the company page directly.
    - else name search on GEMI publicity.
    - afm (if supplied) is passed through for VIES(EL) confirmation.
    """
    gemi = re.sub(r"\D", "", gemi_number or "")
    afm = re.sub(r"\D", "", afm or "")

    # VAT-only path: no name, no GEMI, but an ΑΦΜ → VIES(EL) is authoritative.
    if afm and not entity_name and not gemi:
        return source_vies.vies_verify("EL", entity_name, afm)

    if gemi and len(gemi) >= 9:
        return eng.run(GR_COMPANY_CONFIG, entity_name or f"GEMI {gemi}",
                       {"gemi": gemi, "gemi_number": gemi, "afm": afm})
    return eng.run(GR_CONFIG, entity_name, {"afm": afm})
