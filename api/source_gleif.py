"""
Source: GLEIF — Global LEI Foundation (api.gleif.org).

ONE adapter for every country we cover via GLEIF only. Per-country
verify_xx.py files (AR, CL, CO, HK ...) are thin shims that call
gleif_verify(country_code, entity_name, reg_number, terminology).

GLEIF covers entities that have a Legal Entity Identifier — that's
listed companies, banks, insurers, regulated funds, large corporates
with derivatives reporting obligations, etc. Smaller / private entities
without an LEI are not in GLEIF; the shim returns a clear NOT_FOUND with
a country-specific note explaining the coverage limit.

Multilogin HTTP with the country exit IP (GLEIF accepts direct too, but
keeping mlx_http preserves the "all outbound through proxy" rule).
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"

# Substrings that mark a record as a fund / scheme / subsidiary product
# rather than the parent operating company. Used to demote in ranking.
_SUBSIDIARY_TERMS = (
    "scheme", "fund", "trust", "sub-fund", "subfund", "retirement",
    "provident", "pension", "compartment", "umbrella", "feeder",
)

_INITED = False


def init(get_secret=None):
    global _INITED
    if not _INITED:
        log.info("source_gleif ready (engine) — GLEIF LEI registry via Multilogin")
        _INITED = True


def _rank_records(records: list, query: str) -> list:
    """Prefer parent operating companies over funds/schemes/sub-funds."""
    q = (query or "").lower().strip()
    query_mentions_sub = any(t in q for t in _SUBSIDIARY_TERMS) if q else False

    def score(rec):
        name = rec["attributes"]["entity"]["legalName"]["name"].lower()
        q_match = q and q in name
        has_sub = any(t in name for t in _SUBSIDIARY_TERMS)
        sub_penalty = 0 if query_mentions_sub else (1 if has_sub else 0)
        if q_match and not sub_penalty:
            return (0, len(name))
        if q_match:
            return (10, len(name))
        if not sub_penalty:
            return (20, len(name))
        return (30, len(name))

    return sorted(records, key=score)


def _format_addr(addr: dict) -> str:
    if not addr:
        return ""
    parts = list(addr.get("addressLines") or [])
    parts += [
        addr.get("city", ""),
        addr.get("region", ""),
        addr.get("postalCode", ""),
        addr.get("country", ""),
    ]
    return ", ".join(p for p in parts if p)


def _parse(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    records = data.get("data") or []
    if not records:
        return {"found": False}

    # Re-rank to prefer parent over funds/schemes when name search was used
    if not ids.get("reg_number"):
        records = _rank_records(records, entity_name)

    top = records[0]
    attrs = top["attributes"]
    entity = attrs["entity"]
    lei = attrs.get("lei", "")

    legal_name = entity.get("legalName", {}).get("name", "")
    status_raw = entity.get("status", "UNKNOWN")
    reg_as = entity.get("registeredAs", "") or ""

    addr_full = _format_addr(entity.get("legalAddress") or {})
    legal_form = entity.get("legalForm", {}) or {}
    legal_form_str = legal_form.get("other") or legal_form.get("id") or None

    # Foundation / inception isn't part of LEI base record — leave None.
    # GLEIF returns status like ACTIVE, INACTIVE, MERGED, RETIRED, DUPLICATE.
    status_map = {
        "ACTIVE":   "ACTIVE",
        "INACTIVE": "INACTIVE",
        "MERGED":   "MERGED",
        "RETIRED":  "RETIRED",
        "DUPLICATE": "DUPLICATE",
        "NULL":     "NULL",
    }
    status = status_map.get(status_raw, status_raw or "UNKNOWN")

    others = []
    for r in records[1:5]:
        rentity = (r.get("attributes") or {}).get("entity") or {}
        others.append({
            "lei": (r.get("attributes") or {}).get("lei", ""),
            "name": (rentity.get("legalName") or {}).get("name", ""),
            "status": rentity.get("status", ""),
            "registered_as": rentity.get("registeredAs", ""),
            "country": (rentity.get("legalAddress") or {}).get("country", ""),
        })

    return {
        "found": True,
        "legal_name": legal_name or entity_name,
        "business_registration_number": reg_as or None,
        "headquarters": addr_full or None,
        "is_listed": False,  # GLEIF doesn't expose listing status directly
        # GLEIF/jurisdiction-specific extras
        "lei": lei or None,
        "registered_as": reg_as or None,  # local registration number per jurisdiction
        "legal_form": legal_form_str,
        "total_matches": len(records),
        "other_matches": others or None,
        "status": status,
        "summary": (
            f"{legal_name or entity_name} — LEI {lei}"
            + (f" — reg# {reg_as}" if reg_as else "")
            + f" — {status}"
        ),
    }


_CONFIG_CACHE: dict[tuple, eng.CountryConfig] = {}


def _config_for(cc: str, mode: str) -> eng.CountryConfig:
    """mode: 'name_legalName', 'name_fulltext', 'reg'."""
    key = (cc, mode)
    if key in _CONFIG_CACHE:
        return _CONFIG_CACHE[key]

    filter_part = {
        "name_legalName": f"filter%5Bentity.legalName%5D={{q}}",
        "name_fulltext":  f"filter%5Bfulltext%5D={{q}}",
        "reg":            f"filter%5Bentity.registeredAs%5D={{q}}",
    }[mode]

    url = (
        f"{_GLEIF_URL}"
        f"?filter%5Bentity.legalAddress.country%5D={cc}"
        f"&page%5Bsize%5D=20&{filter_part}"
    )

    cfg = eng.CountryConfig(
        country_code=cc,
        source_name=f"GLEIF LEI Registry — {cc}",
        transport=eng.T_MLX_HTTP,
        primary_url=url,
        parser=_parse,
        timeout=15,
        headers={"Accept": "application/vnd.api+json"},
        how_to_reproduce_template=(
            "Visit https://search.gleif.org/#/search → "
            f"filter jurisdiction {cc} → search '{{entity}}'"
        ),
    )
    _CONFIG_CACHE[key] = cfg
    return cfg


def gleif_verify(
    country_code: str,
    entity_name: str = "",
    reg_number: str = "",
    coverage_note: str = "",
) -> dict:
    """
    Shared GLEIF verify — country_code is the ISO 2-char code.

    Try reg_number first (if provided), then name search (legalName, then
    fulltext fallback). Returns a NOT_FOUND with `coverage_note` if neither
    surfaces a record — coverage_note explains why this jurisdiction is
    GLEIF-only (deprecated registry, paywalled SPA, etc.) so the caller
    knows what's missing.
    """
    cc = (country_code or "").upper().strip()
    if not cc:
        return {"found": False, "error": "country_code required"}
    if not entity_name and not reg_number:
        return {"found": False, "error": f"entity_name or reg_number required for {cc}"}

    # Reg-number lookup first (most precise)
    if reg_number:
        clean = re.sub(r"[\s\-]", "", reg_number.strip())
        # Try the raw and a few zero-pad variants for the GLEIF filter
        candidates = [clean]
        if clean.isdigit():
            candidates.append(clean.zfill(8))
            candidates.append(clean.lstrip("0"))
        for cand in dict.fromkeys(candidates):
            if not cand:
                continue
            r = eng.run(_config_for(cc, "reg"), cand, {"reg_number": cand})
            if r.get("verified"):
                return r
        # Reg specified but not found — return explicit NOT_FOUND
        return _not_found(cc, entity_name, reg_number, coverage_note, by_reg=True)

    # Name search — try legalName then fulltext
    for mode in ("name_legalName", "name_fulltext"):
        r = eng.run(_config_for(cc, mode), entity_name, {})
        if r.get("verified"):
            return r

    return _not_found(cc, entity_name, "", coverage_note, by_reg=False)


def _not_found(cc: str, entity_name: str, reg_number: str,
               coverage_note: str, by_reg: bool) -> dict:
    msg = coverage_note or (
        f"GLEIF covers {cc} entities with Legal Entity Identifiers "
        "(listed companies, banks, insurers, regulated funds, large "
        "corporates with derivatives reporting). Smaller entities without "
        "LEIs are not in GLEIF."
    )
    if by_reg:
        msg = f"Registration number {reg_number} not found in GLEIF ({cc}). " + msg
    return {
        "entity_name": entity_name,
        "country_code": cc,
        "found": False, "verified": False,
        "reg_number": reg_number or None,
        "status": "NOT_FOUND",
        "note": msg,
        "source": f"GLEIF LEI Registry — {cc}",
    }
