"""
UK Companies House verification via gov.uk web search.

Source: https://find-and-update.company-information.service.gov.uk
No API key needed. No CAPTCHA. No proxy needed.
Uses curl_cffi for browser TLS fingerprint.

Returns: company name, number, status, type, address, incorporation date, SIC codes.
"""

import logging
import re
import time

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

log = logging.getLogger("verify-gateway")

_BASE = "https://find-and-update.company-information.service.gov.uk"


def init(get_secret):
    log.info("UK Companies House ready (gov.uk web scrape, no auth)")


def companies_house_verify(entity_name: str, company_number: str = "") -> dict:
    """
    Verify a UK company via Companies House gov.uk website.
    If company_number provided, goes straight to detail page.
    Otherwise searches by name.
    """
    if not entity_name and not company_number:
        return {"found": False, "error": "entity_name or company_number required"}

    try:
        if company_number:
            return _lookup_by_number(company_number.strip().upper())
        return _search_by_name(entity_name.strip())
    except Exception as e:
        log.error("UK Companies House error: %s", e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_by_number(company_number: str) -> dict:
    """Direct company page lookup."""
    resp = cffi_requests.get(
        f"{_BASE}/company/{company_number}",
        impersonate="chrome",
        timeout=15,
    )
    if resp.status_code == 404:
        return {
            "company_number": company_number, "found": False,
            "status": "NOT_FOUND",
            "source": "Companies House, United Kingdom",
        }
    resp.raise_for_status()
    return _parse_company_page(resp.text, company_number)


def _search_by_name(entity_name: str) -> dict:
    """Search by company name, then fetch detail page for best match."""
    resp = cffi_requests.get(
        f"{_BASE}/search/companies",
        params={"q": entity_name},
        impersonate="chrome",
        timeout=15,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results_list = soup.find("ul", id="results")
    if not results_list:
        return {
            "entity_name": entity_name, "found": False,
            "status": "NOT_FOUND",
            "note": "No matching company found in Companies House",
            "source": "Companies House, United Kingdom",
        }

    items = results_list.find_all("li", class_="type-company")
    if not items:
        return {
            "entity_name": entity_name, "found": False,
            "status": "NOT_FOUND",
            "source": "Companies House, United Kingdom",
        }

    # Extract best match company number from link
    best_link = items[0].find("a", href=lambda h: h and "/company/" in h)
    if not best_link:
        return {"entity_name": entity_name, "found": False, "error": "Could not parse search results"}

    href = best_link["href"]
    best_number = href.rstrip("/").split("/")[-1]

    # Fetch detail page for full data
    result = _lookup_by_number(best_number)

    # Add alternatives from search
    if len(items) > 1:
        alts = []
        for item in items[1:5]:
            link = item.find("a", href=lambda h: h and "/company/" in h)
            if not link:
                continue
            alt_name = link.get_text(strip=True)
            alt_href = link["href"]
            alt_number = alt_href.rstrip("/").split("/")[-1]

            # Extract meta info
            meta = item.find("p", class_="meta")
            meta_text = meta.get_text(strip=True) if meta else ""

            # Extract address
            addr_p = item.find_all("p")
            alt_addr = ""
            for p in addr_p:
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
            result["alternatives"] = alts

    return result


def _parse_company_page(html: str, company_number: str) -> dict:
    """Parse company detail page HTML."""
    soup = BeautifulSoup(html, "html.parser")

    # Company name from h1
    h1 = soup.find("h1")
    company_name = h1.get_text(strip=True) if h1 else ""

    # Extract key-value pairs from dt/dd
    details = {}
    for dt in soup.find_all("dt"):
        key = dt.get_text(strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if dd:
            details[key] = dd.get_text(strip=True)

    status = details.get("company status", "unknown")
    company_type = details.get("company type", "")
    address = details.get("registered office address", "")
    inc_date = details.get("incorporated on", "")
    dissolved = details.get("dissolved on", "")

    # SIC codes
    sic_codes = []
    sic_heading = soup.find(string=lambda s: s and "SIC" in str(s))
    if sic_heading:
        sic_parent = sic_heading.find_parent()
        if sic_parent:
            sic_list = sic_parent.find_next("ul")
            if sic_list:
                for li in sic_list.find_all("li"):
                    sic_codes.append(li.get_text(strip=True))

    # Previous names
    prev_names = []
    prev_table = soup.find("table", id="previousNameTable")
    if prev_table:
        for row in prev_table.find_all("tr"):
            cells = row.find_all("td")
            if cells:
                prev_names.append(cells[0].get_text(strip=True))

    result = {
        "entity_name": company_name,
        "company_number": company_number,
        "found": True,
        "status": status.upper(),
        "company_type": company_type,
        "incorporated_on": inc_date,
        "dissolved_on": dissolved if dissolved else None,
        "registered_address": address,
        "sic_codes": sic_codes if sic_codes else None,
        "previous_names": prev_names if prev_names else None,
        "source": "Companies House, United Kingdom",
        "validation_source": {
            "registry": "Companies House, United Kingdom (Gov.UK)",
            "url": f"{_BASE}/company/{company_number}",
            "record_id": company_number,
            "how_to_reproduce": (
                f"Visit {_BASE} → "
                f"Search '{company_name}' or company number {company_number}"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }

    return result
