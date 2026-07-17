"""
US verify — runs on the generic engine.

Source: SEC EDGAR. Free JSON API, no auth (User-Agent required).
Direct HTTP (Bright Data blocks .gov).

Three entry paths:
  - CIK lookup       → US_CIK_CONFIG    → submissions/CIK{cik}.json
  - Ticker lookup    → resolve ticker→CIK first, then CIK lookup
  - Name search      → US_NAME_CONFIG   → efts.sec.gov/LATEST/search-index
"""

import logging

from curl_cffi import requests as cffi_requests

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_SUBMISSIONS = "https://data.sec.gov/submissions"
_SEARCH      = "https://efts.sec.gov/LATEST/search-index"
_TICKERS     = "https://www.sec.gov/files/company_tickers.json"
_UA          = "CrawlVerifyPlatform admin@crawl.dev"


def init(get_secret=None):
    log.info("US verify ready (engine) — SEC EDGAR direct")


def _format_addr(addr: dict) -> str:
    if not addr:
        return ""
    parts = [addr.get("street1", ""), addr.get("street2", ""),
             addr.get("city", ""), addr.get("stateOrCountry", ""),
             addr.get("zipCode", "")]
    return ", ".join(p for p in parts if p)


def _format_edgar(data: dict, entity_name: str) -> dict:
    cik = str(data.get("cik", "")).zfill(10)
    name = data.get("name", "")
    entity_type = data.get("entityType", "")
    sic = data.get("sic", "")
    sic_desc = data.get("sicDescription", "")
    ein = data.get("ein", "")
    state_inc = data.get("stateOfIncorporation", "")
    fiscal_year = data.get("fiscalYearEnd", "")
    category = data.get("category", "")
    phone = data.get("phone", "")
    tickers = data.get("tickers") or None
    exchanges = data.get("exchanges") or None

    addresses = data.get("addresses") or {}
    mailing = _format_addr(addresses.get("mailing") or {})
    business = _format_addr(addresses.get("business") or {})

    former_names = [
        {"name": fn.get("name", ""), "from": fn.get("from", ""), "to": fn.get("to", "")}
        for fn in (data.get("formerNames") or [])
    ]

    recent = (data.get("filings") or {}).get("recent") or {}
    filing_count = len(recent.get("accessionNumber", [])) if recent else 0

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": ein or None,
        "headquarters": business or mailing or None,
        "industry": sic_desc or None,
        "is_listed": bool(tickers),
        "stock_code": (tickers[0] if tickers else None),
        # US-specific extras
        "cik": cik,
        "ein": ein or None,
        "sic_code": sic or None,
        "sic_description": sic_desc or None,
        "entity_type": entity_type or None,
        "state_of_incorporation": state_inc or None,
        "tickers": tickers,
        "exchanges": exchanges,
        "category": category or None,
        "fiscal_year_end": fiscal_year or None,
        "phone": phone or None,
        "mailing_address": mailing or None,
        "business_address": business or None,
        "former_names": former_names or None,
        "total_filings": filing_count,
        "status": "ACTIVE" if filing_count > 0 else "REGISTERED",
        "summary": (
            f"{name or entity_name} — CIK {cik} — "
            + (f"tickers: {','.join(tickers)} ({','.join(exchanges or [])})" if tickers else "no tickers")
            + (f" — {sic_desc}" if sic_desc else "")
        ),
    }


def _parse_us_cik(raw: dict, entity_name: str, ids: dict) -> dict:
    if raw.get("status") == 404:
        return {"found": False, "note": f"CIK {ids.get('cik')} not found in EDGAR"}
    data = raw.get("json")
    if not isinstance(data, dict) or not data.get("cik"):
        return {"found": False}
    return _format_edgar(data, entity_name)


def _parse_us_name(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    hits = ((data.get("hits") or {}).get("hits")) or []
    if not hits:
        return {"found": False,
                "note": "No SEC filings found. EDGAR covers SEC-registered entities only."}

    seen = {}
    for h in hits:
        src = h.get("_source") or {}
        names = src.get("display_names") or []
        for i, cik in enumerate(src.get("ciks") or []):
            if cik not in seen:
                seen[cik] = names[i] if i < len(names) else ""

    if not seen:
        return {"found": False}

    best_cik = next(iter(seen))
    detail = _fetch_cik(best_cik, entity_name)
    if not detail.get("found"):
        return detail

    if len(seen) > 1:
        detail["alternatives"] = [
            {"cik": cik, "display_name": nm} for cik, nm in list(seen.items())[1:5]
        ]

    return detail


def _fetch_cik(cik: str, entity_name: str) -> dict:
    cik_padded = cik.zfill(10)
    r = cffi_requests.get(
        f"{_SUBMISSIONS}/CIK{cik_padded}.json",
        headers={"User-Agent": _UA}, impersonate="chrome", timeout=15,
    )
    if r.status_code == 404:
        return {"found": False, "note": f"CIK {cik_padded} not found in EDGAR"}
    try:
        data = r.json()
    except Exception:
        return {"found": False, "error": "edgar_json_parse_failed"}
    return _format_edgar(data, entity_name)


def _cik_from_ticker(ticker: str) -> str:
    try:
        r = cffi_requests.get(_TICKERS, headers={"User-Agent": _UA}, impersonate="chrome", timeout=15)
        r.raise_for_status()
        data = r.json()
        ticker = ticker.upper()
        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker:
                return str(entry["cik_str"]).zfill(10)
    except Exception as e:
        log.warning("SEC ticker lookup failed: %s", e)
    return ""


US_CIK_CONFIG = eng.CountryConfig(
    country_code="US",
    source_name="SEC EDGAR — U.S. Securities and Exchange Commission",
    transport=eng.T_MLX_HTTP,
    primary_url=_SUBMISSIONS + "/CIK{cik}.json",
    parser=_parse_us_cik,
    timeout=15,
    headers={"User-Agent": _UA},
    how_to_reproduce_template=(
        "Visit https://www.sec.gov/cgi-bin/browse-edgar → CIK {entity}"
    ),
)

US_NAME_CONFIG = eng.CountryConfig(
    country_code="US",
    source_name="SEC EDGAR — U.S. Securities and Exchange Commission",
    transport=eng.T_MLX_HTTP,
    primary_url=_SEARCH + "?q=%22{q}%22&forms=10-K,10-Q,20-F,8-K&dateRange=custom&startdt=2024-01-01&enddt=2026-12-31",
    parser=_parse_us_name,
    timeout=15,
    headers={"User-Agent": _UA},
    how_to_reproduce_template="Visit https://efts.sec.gov → search '{entity}'",
)


def edgar_verify(entity_name: str, cik: str = "", ticker: str = "") -> dict:
    """US verify entry point — backward compat with main.py routing."""
    if cik:
        clean = cik.strip().zfill(10)
        return eng.run(US_CIK_CONFIG, entity_name or clean, {"cik": clean})

    if ticker:
        resolved = _cik_from_ticker(ticker.strip())
        if resolved:
            return eng.run(US_CIK_CONFIG, entity_name or ticker.upper(), {"cik": resolved})
        return {
            "entity_name": entity_name, "country_code": "US",
            "ticker": ticker, "found": False, "verified": False,
            "note": f"Ticker '{ticker}' not found in SEC EDGAR tickers file",
            "source": "SEC EDGAR, United States",
        }

    return eng.run(US_NAME_CONFIG, entity_name, {})
