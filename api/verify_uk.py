"""
UK verify — runs on the generic engine.

Source: Companies House public web (find-and-update.company-information.service.gov.uk).
Direct HTTP — gov.uk blocks all Bright Data proxies by policy AND is a clean
public registry with no anti-bot. No API key, no CAPTCHA.

Two configs:
  - UK_SEARCH_CONFIG: name search → top result → fetch detail page
  - UK_DETAIL_CONFIG: direct lookup by company_number
Both feed the same _parse_detail_html() once a detail page is in hand.
"""

import logging
import time

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BASE = "https://find-and-update.company-information.service.gov.uk"


def init(get_secret):
    log.info("UK verify ready (engine) — Companies House direct (gov.uk)")


def _parse_detail_html(html: str, company_number: str) -> dict:
    """Parse a /company/{number} detail page."""
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    company_name = h1.get_text(strip=True) if h1 else ""

    details = {}
    for dt in soup.find_all("dt"):
        key = dt.get_text(strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if dd:
            details[key] = dd.get_text(strip=True)

    status = (details.get("company status") or "").upper() or "UNKNOWN"
    company_type = details.get("company type", "")
    address = details.get("registered office address", "")
    inc_date = details.get("incorporated on", "")
    dissolved = details.get("dissolved on", "")

    sic_codes = []
    sic_heading = soup.find(string=lambda s: s and "SIC" in str(s))
    if sic_heading:
        sic_parent = sic_heading.find_parent()
        if sic_parent:
            sic_list = sic_parent.find_next("ul")
            if sic_list:
                for li in sic_list.find_all("li"):
                    sic_codes.append(li.get_text(strip=True))

    prev_names = []
    prev_table = soup.find("table", id="previousNameTable")
    if prev_table:
        for row in prev_table.find_all("tr"):
            cells = row.find_all("td")
            if cells:
                prev_names.append(cells[0].get_text(strip=True))

    # Founded year from incorporation date — format e.g. "4 October 1971"
    founded_year = None
    for token in inc_date.split():
        if token.isdigit() and 1700 <= int(token) <= 2100:
            founded_year = token
            break

    return {
        "found": True,
        "legal_name": company_name,
        "headquarters": address or None,
        "founded_year": founded_year,
        "is_listed": False,  # Companies House doesn't expose listing status
        # UK-specific extras
        "company_number": company_number,
        "company_type": company_type or None,
        "incorporated_on": inc_date or None,
        "dissolved_on": dissolved if dissolved else None,
        "registered_address": address or None,
        "sic_codes": sic_codes or None,
        "previous_names": prev_names or None,
        "status": status,
        "summary": (
            f"{company_name} — {company_number} — {status}"
            + (f" — dissolved {dissolved}" if dissolved else "")
        ),
    }


def _fetch_detail(company_number: str) -> dict | None:
    resp = cffi_requests.get(
        f"{_BASE}/company/{company_number}",
        impersonate="chrome",
        timeout=15,
    )
    if resp.status_code == 404:
        return {"found": False}
    if resp.status_code != 200:
        return None
    return _parse_detail_html(resp.text, company_number)


def _parse_uk_search(raw: dict, entity_name: str, ids: dict) -> dict:
    """Parse search results, then fetch the top-result detail page."""
    html = raw.get("body") or ""
    soup = BeautifulSoup(html, "html.parser")
    items = soup.find_all("li", class_="type-company")
    if not items:
        return {"found": False}

    best_link = items[0].find("a", href=lambda h: h and "/company/" in h)
    if not best_link:
        return {"found": False, "error": "could not parse search results"}

    best_number = best_link["href"].rstrip("/").split("/")[-1]

    detail = _fetch_detail(best_number)
    if not detail or not detail.get("found"):
        return {"found": False, "error": "detail page fetch failed"}

    # Add up to 4 alternative search matches
    alts = []
    for item in items[1:5]:
        link = item.find("a", href=lambda h: h and "/company/" in h)
        if not link:
            continue
        href = link["href"]
        alt_number = href.rstrip("/").split("/")[-1]
        alt_name = link.get_text(strip=True)
        meta = item.find("p", class_="meta")
        meta_text = meta.get_text(strip=True) if meta else ""
        alt_addr = ""
        for p in item.find_all("p"):
            if "meta" not in (p.get("class") or []):
                alt_addr = p.get_text(strip=True)
                break
        alts.append({
            "company_name": alt_name,
            "company_number": alt_number,
            "meta": meta_text[:100],
            "address": alt_addr[:150],
        })
    if alts:
        detail["alternatives"] = alts

    return detail


def _parse_uk_detail(raw: dict, entity_name: str, ids: dict) -> dict:
    """Direct lookup — entity_name here is actually the company_number."""
    company_number = ids.get("company_number") or entity_name
    body = raw.get("body") or ""
    if not body:
        return {"found": False}
    return _parse_detail_html(body, company_number)


UK_SEARCH_CONFIG = eng.CountryConfig(
    country_code="GB",
    source_name="Companies House, United Kingdom (Gov.UK)",
    transport=eng.T_DIRECT_API,
    primary_url=f"{_BASE}/search/companies?q={{q}}",
    parser=_parse_uk_search,
    timeout=20,
    how_to_reproduce_template=(
        f"Visit {_BASE} → search '{{entity}}' → view company detail page"
    ),
)

UK_DETAIL_CONFIG = eng.CountryConfig(
    country_code="GB",
    source_name="Companies House, United Kingdom (Gov.UK)",
    transport=eng.T_DIRECT_API,
    primary_url=f"{_BASE}/company/{{company_number}}",
    parser=_parse_uk_detail,
    timeout=20,
    how_to_reproduce_template=(
        f"Visit {_BASE}/company/{{entity}} for direct lookup"
    ),
)


def companies_house_verify(entity_name: str, company_number: str = "") -> dict:
    """UK verify entry point — backward compat with main.py routing."""
    company_number = (company_number or "").strip().upper()
    if company_number:
        return eng.run(UK_DETAIL_CONFIG, company_number, {"company_number": company_number})
    return eng.run(UK_SEARCH_CONFIG, entity_name, {})
