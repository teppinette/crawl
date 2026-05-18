"""
Hong Kong company verification via ICRIS (Companies Registry).

Source: https://www.icris.cr.gov.hk/csci/
Free public search — CR number or company name.

Input: entity_name (search by name) or cr_number (7-digit company number)
Returns: legal_name, cr_number, company_type, status, date_of_incorporation
"""

import logging
import re
import time

from mlx_http import mlx_navigate

log = logging.getLogger("verify-gateway")

_SEARCH_URL = "https://www.icris.cr.gov.hk/csci/cps_criteria.jsp"
_RESULT_URL = "https://www.icris.cr.gov.hk/csci/cps_result.jsp"
def init(get_secret=None):
    log.info("HK ICRIS ready (Companies Registry, free public search)")


def icris_verify(entity_name: str, cr_number: str = "") -> dict:
    if not entity_name and not cr_number:
        return {"found": False, "error": "entity_name or cr_number required"}

    try:
        if cr_number:
            clean = re.sub(r"[\s\-]", "", cr_number.strip())
            return _search_cr(clean, entity_name)
        return _search_name(entity_name)
    except Exception as e:
        log.error("HK ICRIS error for %s: %s", entity_name or cr_number, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _search_cr(cr_number: str, entity_name: str) -> dict:
    from urllib.parse import urlencode
    params = urlencode({
        "searchType": "C",
        "companyNumber": cr_number,
        "companyName": "",
        "nameType": "E",
    })
    # Navigate to the search page first to establish session, then to results
    # mlx_navigate uses a real browser — the JSP accepts GET params too
    mlx_navigate(_SEARCH_URL, wait_s=1, timeout=15, country_code="hk")
    result = mlx_navigate(f"{_RESULT_URL}?{params}", wait_s=3, timeout=60, country_code="hk")
    html = result.get("html", "")
    if not html:
        raise RuntimeError("ICRIS returned empty response")
    return _parse_results(html, entity_name, cr_number)


def _search_name(entity_name: str) -> dict:
    from urllib.parse import urlencode
    params = urlencode({
        "searchType": "N",
        "companyNumber": "",
        "companyName": entity_name,
        "nameType": "E",
    })
    mlx_navigate(_SEARCH_URL, wait_s=1, timeout=15, country_code="hk")
    result = mlx_navigate(f"{_RESULT_URL}?{params}", wait_s=3, timeout=60, country_code="hk")
    html = result.get("html", "")
    if not html:
        raise RuntimeError("ICRIS returned empty response")
    return _parse_results(html, entity_name, "")


def _parse_results(html: str, entity_name: str, cr_number: str) -> dict:
    """Parse ICRIS HTML results page."""
    # Check for no results
    if "No record found" in html or "no matching record" in html.lower():
        return {
            "entity_name": entity_name, "cr_number": cr_number or None,
            "country_code": "HK",
            "found": False, "status": "NOT_FOUND",
            "validation_source": _source(entity_name or cr_number),
        }

    # Extract table rows with company data
    # Pattern: CR number, company name, name type, status
    rows = re.findall(
        r'<td[^>]*>\s*(\d{5,8})\s*</td>\s*'
        r'<td[^>]*>\s*(.*?)\s*</td>\s*'
        r'<td[^>]*>\s*(.*?)\s*</td>\s*'
        r'<td[^>]*>\s*(.*?)\s*</td>',
        html, re.DOTALL | re.IGNORECASE,
    )

    if not rows:
        # Try simpler extraction — some pages have different layout
        names = re.findall(r'companyName["\s]*>\s*(.*?)\s*<', html)
        numbers = re.findall(r'companyNumber["\s]*>\s*(\d+)\s*<', html)
        statuses = re.findall(r'(?:status|Status)["\s]*>\s*(.*?)\s*<', html)

        if names:
            return {
                "entity_name": names[0].strip(),
                "query_name": entity_name,
                "country_code": "HK",
                "found": True,
                "cr_number": numbers[0] if numbers else cr_number or None,
                "status": statuses[0].strip().upper() if statuses else "UNKNOWN",
                "total_matches": len(names),
                "validation_source": _source(entity_name or cr_number),
            }

        # Last resort — check if we got ANY company-like content
        if cr_number and "Live" in html:
            return {
                "entity_name": entity_name,
                "country_code": "HK",
                "found": True,
                "cr_number": cr_number,
                "status": "LIVE",
                "note": "Company found but details could not be fully parsed",
                "validation_source": _source(cr_number),
            }

        return {
            "entity_name": entity_name, "cr_number": cr_number or None,
            "country_code": "HK",
            "found": False, "status": "NOT_FOUND",
            "note": "Search returned results but could not parse company data",
            "validation_source": _source(entity_name or cr_number),
        }

    best = rows[0]
    cr = best[0].strip()
    name = re.sub(r"<[^>]+>", "", best[1]).strip()
    name_type = re.sub(r"<[^>]+>", "", best[2]).strip()
    status_raw = re.sub(r"<[^>]+>", "", best[3]).strip()

    status_map = {
        "Live": "ACTIVE", "Dissolved": "DISSOLVED",
        "Winding Up": "WINDING_UP", "Deregistered": "DEREGISTERED",
        "Struck Off": "STRUCK_OFF",
    }
    status = status_map.get(status_raw, status_raw.upper() if status_raw else "UNKNOWN")

    other_matches = []
    for r in rows[1:5]:
        other_matches.append({
            "cr_number": r[0].strip(),
            "name": re.sub(r"<[^>]+>", "", r[1]).strip(),
            "status": re.sub(r"<[^>]+>", "", r[3]).strip(),
        })

    return {
        "entity_name": name,
        "query_name": entity_name,
        "country_code": "HK",
        "found": True,
        "cr_number": cr,
        "name_type": name_type or None,
        "status": status,
        "total_matches": len(rows),
        "other_matches": other_matches or None,
        "source": "ICRIS (Integrated Companies Registry Information System), Hong Kong",
        "validation_source": _source(entity_name or cr_number),
    }


def _source(query: str) -> dict:
    return {
        "registry": "ICRIS — Companies Registry, Hong Kong SAR Government",
        "url": "https://www.icris.cr.gov.hk/csci/",
        "how_to_reproduce": f"Visit icris.cr.gov.hk/csci → Search: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
