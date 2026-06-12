"""
Source: OpenCorporates — paid public-data aggregator with broad SME coverage
in jurisdictions where free gov sources are paywalled (HK, AR, CL, CO, EG, etc.).

OpenCorporates aggregates official government registries — same underlying
data the gov sources serve, but with a single REST API surface across
~140 jurisdictions. NOT a substitute for true local-gov-direct (per the
verification rule); used as the answer for SMEs where gov-direct is
paywalled and GLEIF only covers listed/large entities.

Token in Key Vault as `opencorporates-token`. Essentials plan as of
2026-06-11 (expires 2026-07-12 — renew before that).
"""

import logging
import re
import urllib.parse

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_API_BASE = "https://api.opencorporates.com/v0.4"
_TOKEN = ""


def init(get_secret):
    global _TOKEN
    _TOKEN = get_secret("opencorporates-token") or ""
    if _TOKEN:
        log.info("source_opencorporates ready (essentials plan, token configured)")
    else:
        log.warning("source_opencorporates: no token — set opencorporates-token in Key Vault")


def is_available() -> bool:
    """Return True if OC token is configured. Callers can short-circuit if False."""
    return bool(_TOKEN)


def _format_oc(c: dict, entity_name: str, country_code: str, all_matches: list) -> dict:
    """Map an OpenCorporates company record to engine response shape."""
    name = c.get("name", "")
    cnum = c.get("company_number", "")
    status = c.get("current_status") or "UNKNOWN"
    company_type = c.get("company_type") or None
    inactive = bool(c.get("inactive"))
    dissolution = c.get("dissolution_date") or None
    incorp_date = c.get("incorporation_date") or None
    founded_year = incorp_date[:4] if incorp_date and len(incorp_date) >= 4 else None

    reg_addr = c.get("registered_address_in_full") or None
    industry_codes = c.get("industry_codes") or []
    industry = (industry_codes[0].get("industry_code", {}).get("description")
                if industry_codes and isinstance(industry_codes[0], dict) else None)

    officers = []
    for o in (c.get("officers") or []):
        of = o.get("officer", o)
        officers.append({
            "name": of.get("name", ""),
            "role": of.get("position", ""),
            "appointed_on": of.get("start_date"),
            "resigned_on": of.get("end_date"),
        })

    others = []
    for r in all_matches[1:5]:
        rc = r.get("company") or {}
        others.append({
            "name": rc.get("name", ""),
            "company_number": rc.get("company_number", ""),
            "status": rc.get("current_status", ""),
            "jurisdiction": rc.get("jurisdiction_code", "").upper(),
            "inactive": rc.get("inactive"),
        })

    # Status normalisation
    norm = (status or "").upper()
    if inactive or "DISSOLVED" in norm or "DEREGISTERED" in norm or "STRUCK" in norm:
        clean_status = "DISSOLVED"
    elif "LIQUIDATION" in norm or "WIND" in norm:
        clean_status = "IN_LIQUIDATION"
    elif "LIVE" in norm or "ACTIVE" in norm or "REGISTERED" in norm:
        clean_status = "ACTIVE"
    else:
        clean_status = norm or "UNKNOWN"

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": cnum or None,
        "headquarters": reg_addr,
        "founded_year": founded_year,
        "industry": industry,
        "directors": officers or None,
        "is_listed": False,
        # OC-specific extras (passed through by engine)
        "company_number": cnum or None,
        "company_type": company_type,
        "current_status": status,
        "inactive": inactive,
        "dissolution_date": dissolution,
        "incorporation_date": incorp_date,
        "jurisdiction_code": (c.get("jurisdiction_code") or "").upper() or None,
        "registered_address": reg_addr,
        "opencorporates_url": c.get("opencorporates_url") or None,
        "previous_names": c.get("previous_names") or None,
        "other_matches": others or None,
        "total_matches": len(all_matches),
        "enrichment_source": "OpenCorporates (essentials plan)",
        "enrichment_url": c.get("opencorporates_url"),
        "status": clean_status,
        "summary": (
            f"{name} — {country_code} CR {cnum} — {clean_status}"
            + (f" — {company_type}" if company_type else "")
        ),
    }


def oc_verify(
    country_code: str,
    entity_name: str = "",
    reg_number: str = "",
    coverage_note: str = "",
) -> dict:
    """
    Search OpenCorporates by name or company number, scoped to country_code.

    Returns engine-shape dict. Caller (a country shim) decides whether to use
    this as primary or as fallback after a gov-direct attempt fails.
    """
    cc = (country_code or "").lower().strip()
    if not cc:
        return {"found": False, "error": "country_code required"}
    if not _TOKEN:
        return {"found": False, "error": "OpenCorporates token not configured"}
    if not entity_name and not reg_number:
        return {"found": False, "error": f"entity_name or reg_number required for {cc.upper()}"}

    # Reg-number lookup first (most precise)
    if reg_number:
        clean = re.sub(r"[\s\-]", "", reg_number.strip())
        # Try without leading zeros and with zero-pads
        candidates = [clean]
        if clean.isdigit():
            candidates.append(clean.zfill(7))  # common CR# width
            candidates.append(clean.lstrip("0"))
        for cand in dict.fromkeys(candidates):
            if not cand:
                continue
            try:
                import requests
                r = requests.get(
                    f"{_API_BASE}/companies/{cc}/{cand}",
                    params={"api_token": _TOKEN}, timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    company = (data.get("results") or {}).get("company")
                    if company:
                        return _format_oc(company, entity_name or cand, cc.upper(), [{"company": company}])
            except Exception as e:
                log.warning("OC reg-number lookup failed for %s/%s: %s", cc, cand, e)

    # Name search
    try:
        import requests
        r = requests.get(
            f"{_API_BASE}/companies/search",
            params={
                "q": entity_name,
                "jurisdiction_code": cc,
                "per_page": 10,
                "api_token": _TOKEN,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return {
                "found": False,
                "error": f"OC search HTTP {r.status_code}",
                "note": coverage_note,
            }
        data = r.json()
        results = (data.get("results") or {}).get("companies") or []
        if not results:
            return {
                "found": False,
                "note": coverage_note or f"OpenCorporates: no match for '{entity_name}' in {cc.upper()}",
                "enrichment_source": "OpenCorporates (essentials plan) — no match",
            }
        best = results[0].get("company") or {}
        return _format_oc(best, entity_name, cc.upper(), results)
    except Exception as e:
        log.warning("OC name search failed for %s/%s: %s", cc, entity_name, e)
        return {"found": False, "error": f"OC search exception: {str(e)[:160]}", "note": coverage_note}
