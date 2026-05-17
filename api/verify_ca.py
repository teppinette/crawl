"""
Canada company verification via BC OrgBook API (v4).

Source: https://orgbook.gov.bc.ca/api/v4/search/topic
Free API — no auth, no rate limit observed, JSON response.
Covers all Canadian corporations registered in BC or extra-provincially
(federal + all provinces that register in BC). ~1.5M entities.

Input: entity_name (search by name) or business_number (9-digit BN)
Returns: legal_name, status, entity_type, registration_date, home_jurisdiction,
         business_number, source_id (BC Registry number)
"""

import logging
import time

import requests

log = logging.getLogger("verify-gateway")

_API_URL = "https://orgbook.gov.bc.ca/api/v4/search/topic"
_PROXY = None


def init(get_secret):
    # OrgBook is a free public API — no auth needed
    # Direct access (no proxy) — gov.bc.ca is a .gov domain
    log.info("CA OrgBook ready (BC Registries API, no auth required, direct access)")


def orgbook_verify(entity_name: str, business_number: str = "") -> dict:
    """
    Verify a Canadian company via BC OrgBook.

    Searches by company name or 9-digit business number.
    """
    if not entity_name and not business_number:
        return {"found": False, "error": "entity_name or business_number required"}

    try:
        query = business_number.strip() if business_number else entity_name.strip()
        records = _search(query)

        if not records:
            return {
                "entity_name": entity_name,
                "business_number": business_number or None,
                "found": False,
                "status": "NOT_FOUND",
                "source": "BC OrgBook (BC Registries), Canada",
                "validation_source": _validation_source(query),
            }

        best = records[0]
        return _format_result(best, entity_name, business_number, len(records), records[:5])

    except Exception as e:
        log.error("CA OrgBook error for %s: %s", entity_name or business_number, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _search(query: str) -> list:
    """Search OrgBook by name or business number."""
    resp = requests.get(
        _API_URL,
        params={
            "q": query,
            "page_size": 10,
        },
        headers={"Accept": "application/json"},
        timeout=20,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


def _format_result(record: dict, query_name: str, business_number: str,
                   total_matches: int, top_matches: list) -> dict:
    """Format OrgBook record into standard verification response."""
    # Extract names
    names_list = record.get("names", [])
    names = {}
    for n in names_list:
        names[n.get("type", "")] = n.get("text", "")

    entity_name_val = names.get("entity_name", "")
    bn = names.get("business_number", "")

    # Extract attributes
    attrs_list = record.get("attributes", [])
    attrs = {}
    for a in attrs_list:
        attrs[a.get("type", "")] = a.get("value", "")

    status_raw = attrs.get("entity_status", "")
    entity_type = attrs.get("entity_type", "")
    reg_date = attrs.get("registration_date", "")
    home_jurisdiction = attrs.get("home_jurisdiction", "")
    source_id = record.get("source_id", "")
    inactive = record.get("inactive", False)
    revoked = record.get("revoked", False)

    # Status mapping
    status_map = {
        "ACT": "ACTIVE",
        "HIS": "HISTORICAL",
    }
    status = status_map.get(status_raw, status_raw.upper() if status_raw else "UNKNOWN")
    if inactive or revoked:
        status = "INACTIVE"

    # Entity type mapping
    type_map = {
        "A": "Extra-Provincial",
        "B": "Extra-Provincial",
        "BC": "BC Company",
        "BEN": "Benefit Company",
        "C": "Continuation In",
        "CC": "BC Community Contribution Company",
        "CCC": "BC Community Contribution Company",
        "CP": "Cooperative",
        "CS": "Community Service Cooperative",
        "CUL": "BC Unlimited Liability Company",
        "FI": "Financial Institution",
        "FOR": "Foreign Entity",
        "GP": "General Partnership",
        "LL": "Limited Liability Partnership",
        "LLC": "Limited Liability Company",
        "LP": "Limited Partnership",
        "PA": "Private Act",
        "QA": "Extra-Provincial (Fed)",
        "QB": "Extra-Provincial (Fed)",
        "REG": "Extra-Provincial",
        "S": "Society",
        "SP": "Sole Proprietorship",
        "ULC": "Unlimited Liability Company",
        "XCP": "Extra-Provincial Cooperative",
        "XL": "Extra-Provincial LL Partnership",
        "XP": "Extra-Provincial Limited Partnership",
        "XS": "Extra-Provincial Society",
    }
    entity_type_display = type_map.get(entity_type, entity_type)

    # Jurisdiction mapping
    jurisdiction_map = {
        "BC": "British Columbia",
        "AB": "Alberta",
        "SK": "Saskatchewan",
        "MB": "Manitoba",
        "ON": "Ontario",
        "QC": "Quebec",
        "NB": "New Brunswick",
        "NS": "Nova Scotia",
        "PE": "Prince Edward Island",
        "NL": "Newfoundland and Labrador",
        "YT": "Yukon",
        "NT": "Northwest Territories",
        "NU": "Nunavut",
        "FD": "Federal",
    }
    home_display = jurisdiction_map.get(home_jurisdiction, home_jurisdiction)

    # Format registration date
    reg_date_clean = reg_date[:10] if reg_date else None

    # Other matches
    other_matches = []
    for m in top_matches[1:]:
        m_names = {n.get("type", ""): n.get("text", "") for n in m.get("names", [])}
        m_attrs = {a.get("type", ""): a.get("value", "") for a in m.get("attributes", [])}
        other_matches.append({
            "name": m_names.get("entity_name", ""),
            "business_number": m_names.get("business_number", ""),
            "source_id": m.get("source_id", ""),
            "status": m_attrs.get("entity_status", ""),
            "entity_type": m_attrs.get("entity_type", ""),
            "home_jurisdiction": m_attrs.get("home_jurisdiction", ""),
        })

    return {
        "entity_name": entity_name_val,
        "query_name": query_name,
        "found": True,
        "status": status,
        "business_number": bn or None,
        "source_id": source_id or None,
        "entity_type": entity_type_display or None,
        "entity_type_code": entity_type or None,
        "registration_date": reg_date_clean,
        "home_jurisdiction": home_display or None,
        "home_jurisdiction_code": home_jurisdiction or None,
        "registered_jurisdiction": "British Columbia",
        "inactive": inactive,
        "revoked": revoked,
        "total_matches": total_matches,
        "other_matches": other_matches if other_matches else None,
        "source": "BC OrgBook (BC Registries & Online Services), Canada",
        "validation_source": _validation_source(query_name or bn),
    }


def _validation_source(query: str) -> dict:
    return {
        "registry": "BC Registries & Online Services, Government of British Columbia",
        "url": "https://www.orgbook.gov.bc.ca/search",
        "api": "https://orgbook.gov.bc.ca/api/v4/search/topic",
        "how_to_reproduce": (
            f"Visit orgbook.gov.bc.ca → "
            f"Search: {query} → View entity details"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
