"""
US SEC EDGAR company verification.

Source: https://data.sec.gov/submissions/ (company profile)
        https://efts.sec.gov/LATEST/search-index (company search by name)
Free API, no auth key needed. Requires User-Agent header with contact email.

Returns: CIK, entity name, entity type, SIC code/description, EIN,
         state of incorporation, tickers, exchanges, addresses, phone,
         fiscal year end, category (filer size), former names.

Covers: all SEC-registered entities — public companies, investment companies,
        foreign private issuers, broker-dealers, etc.
"""

import logging
import re
import time

from curl_cffi import requests as cffi_requests

log = logging.getLogger("verify-gateway")

_SUBMISSIONS_URL = "https://data.sec.gov/submissions"
_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_UA = "CrawlVerifyPlatform admin@crawl.dev"

# SEC EDGAR is a US gov site but does NOT block proxies and explicitly
# provides a free API. Direct access is fine — no anti-bot, no CAPTCHA.
# However we route through proxy per platform policy.
_PROXY = None


def init(get_secret):
    global _PROXY
    # SEC EDGAR is .gov — Bright Data blocks gov sites by policy.
    # Direct access is required (same as UK Companies House).
    _PROXY = None
    log.info("US SEC EDGAR ready (free API, direct access — .gov blocked by Bright Data)")


def edgar_verify(entity_name: str, cik: str = "", ticker: str = "") -> dict:
    """
    Verify a US company via SEC EDGAR.

    entity_name: company name
    cik: SEC CIK number (10 digits) for direct lookup
    ticker: stock ticker for direct lookup
    """
    if not entity_name and not cik and not ticker:
        return {"found": False, "error": "entity_name, cik, or ticker required"}

    try:
        # Direct CIK lookup
        if cik:
            cik_padded = cik.strip().zfill(10)
            return _lookup_by_cik(cik_padded)

        # Ticker lookup — find CIK from tickers file
        if ticker:
            found_cik = _cik_from_ticker(ticker.strip().upper())
            if found_cik:
                return _lookup_by_cik(found_cik)
            return {
                "ticker": ticker, "found": False,
                "status": "NOT_FOUND",
                "note": f"Ticker '{ticker}' not found in SEC EDGAR",
                "source": "SEC EDGAR, United States",
            }

        # Name search
        return _search_by_name(entity_name.strip())

    except Exception as e:
        log.error("US EDGAR error: %s", e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_by_cik(cik: str) -> dict:
    """Direct company profile lookup by CIK."""
    resp = cffi_requests.get(
        f"{_SUBMISSIONS_URL}/CIK{cik}.json",
        headers={"User-Agent": _UA},
        impersonate="chrome",
        timeout=15,
    )

    if resp.status_code == 404:
        return {
            "cik": cik, "found": False,
            "status": "NOT_FOUND",
            "source": "SEC EDGAR, United States",
        }

    resp.raise_for_status()
    data = resp.json()
    return _format_company(data)


def _search_by_name(entity_name: str) -> dict:
    """Search EDGAR by company name via full-text search index."""
    resp = cffi_requests.get(
        _SEARCH_URL,
        params={
            "q": f'"{entity_name}"',
            "forms": "10-K,10-Q,20-F,8-K",
            "dateRange": "custom",
            "startdt": "2024-01-01",
            "enddt": "2026-12-31",
        },
        headers={"User-Agent": _UA},
        impersonate="chrome",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    hits = data.get("hits", {}).get("hits", [])
    if not hits:
        return {
            "entity_name": entity_name, "found": False,
            "status": "NOT_FOUND",
            "note": "No SEC filings found. EDGAR covers SEC-registered entities only "
                    "(public companies, investment companies, foreign private issuers).",
            "source": "SEC EDGAR, United States",
        }

    # Extract unique CIKs
    seen = {}
    for h in hits:
        src = h.get("_source", {})
        for i, cik in enumerate(src.get("ciks", [])):
            if cik not in seen:
                names = src.get("display_names", [])
                seen[cik] = names[i] if i < len(names) else ""

    if not seen:
        return {"entity_name": entity_name, "found": False, "status": "NOT_FOUND",
                "source": "SEC EDGAR, United States"}

    # Best match — first CIK found
    best_cik = list(seen.keys())[0]
    result = _lookup_by_cik(best_cik)

    # Add alternatives
    if len(seen) > 1:
        result["alternatives"] = [
            {"cik": cik, "display_name": name}
            for cik, name in list(seen.items())[1:5]
        ]

    return result


def _cik_from_ticker(ticker: str) -> str:
    """Look up CIK by stock ticker using SEC tickers file."""
    try:
        resp = cffi_requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": _UA},
            impersonate="chrome",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker:
                return str(entry["cik_str"]).zfill(10)

        return ""
    except Exception as e:
        log.warning("SEC ticker lookup failed: %s", e)
        return ""


def _format_company(data: dict) -> dict:
    """Format EDGAR submissions JSON into standard response."""
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

    tickers = data.get("tickers", [])
    exchanges = data.get("exchanges", [])

    # Addresses
    addresses = data.get("addresses", {})
    mailing = addresses.get("mailing", {})
    business = addresses.get("business", {})

    def _format_addr(addr):
        if not addr:
            return ""
        parts = [
            addr.get("street1", ""),
            addr.get("street2", ""),
            addr.get("city", ""),
            addr.get("stateOrCountry", ""),
            addr.get("zipCode", ""),
        ]
        return ", ".join(p for p in parts if p)

    # Former names
    former_names = []
    for fn in data.get("formerNames", []) or []:
        former_names.append({
            "name": fn.get("name", ""),
            "from": fn.get("from", ""),
            "to": fn.get("to", ""),
        })

    # Recent filings summary
    recent_filings = data.get("filings", {}).get("recent", {})
    filing_count = len(recent_filings.get("accessionNumber", [])) if recent_filings else 0

    return {
        "entity_name": name,
        "cik": cik,
        "found": True,
        "status": "ACTIVE" if filing_count > 0 else "REGISTERED",
        "entity_type": entity_type,
        "sic_code": sic,
        "sic_description": sic_desc,
        "ein": ein,
        "state_of_incorporation": state_inc,
        "tickers": tickers if tickers else None,
        "exchanges": exchanges if exchanges else None,
        "category": category,
        "fiscal_year_end": fiscal_year,
        "phone": phone,
        "mailing_address": _format_addr(mailing),
        "business_address": _format_addr(business),
        "former_names": former_names if former_names else None,
        "total_filings": filing_count,
        "source": "SEC EDGAR, United States",
        "validation_source": {
            "registry": "U.S. Securities and Exchange Commission (SEC) — EDGAR",
            "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40",
            "record_id": cik,
            "how_to_reproduce": (
                f"Visit https://www.sec.gov/cgi-bin/browse-edgar → "
                f"Search CIK '{cik}' or company name '{name}'"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
