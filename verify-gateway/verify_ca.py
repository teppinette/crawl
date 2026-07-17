"""
Canada verify — runs on the generic engine.

Source: BC OrgBook (BC Registries & Online Services).
Free public REST API, no auth. Covers all Canadian corporations
registered in BC or extra-provincially — federal + every province.
~1.5M entities.
"""

import logging

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_ENTITY_TYPE_MAP = {
    "A": "Extra-Provincial", "B": "Extra-Provincial",
    "BC": "BC Company", "BEN": "Benefit Company",
    "C": "Continuation In",
    "CC": "BC Community Contribution Company",
    "CCC": "BC Community Contribution Company",
    "CP": "Cooperative", "CS": "Community Service Cooperative",
    "CUL": "BC Unlimited Liability Company",
    "FI": "Financial Institution", "FOR": "Foreign Entity",
    "GP": "General Partnership",
    "LL": "Limited Liability Partnership",
    "LLC": "Limited Liability Company",
    "LP": "Limited Partnership",
    "PA": "Private Act",
    "QA": "Extra-Provincial (Fed)", "QB": "Extra-Provincial (Fed)",
    "REG": "Extra-Provincial",
    "S": "Society", "SP": "Sole Proprietorship",
    "ULC": "Unlimited Liability Company",
    "XCP": "Extra-Provincial Cooperative",
    "XL": "Extra-Provincial LL Partnership",
    "XP": "Extra-Provincial Limited Partnership",
    "XS": "Extra-Provincial Society",
}

_JURISDICTION_MAP = {
    "BC": "British Columbia", "AB": "Alberta", "SK": "Saskatchewan",
    "MB": "Manitoba", "ON": "Ontario", "QC": "Quebec",
    "NB": "New Brunswick", "NS": "Nova Scotia",
    "PE": "Prince Edward Island",
    "NL": "Newfoundland and Labrador",
    "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut",
    "FD": "Federal",
}

_STATUS_MAP = {"ACT": "ACTIVE", "HIS": "HISTORICAL"}


def init(get_secret):
    log.info("CA verify ready (engine) — BC OrgBook (free JSON API)")


def _parse_ca(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json") or {}
    results = data.get("results") or []
    if not results:
        return {"found": False}

    best = results[0]

    names = {n.get("type", ""): n.get("text", "") for n in best.get("names", [])}
    attrs = {a.get("type", ""): a.get("value", "") for a in best.get("attributes", [])}

    legal_name = names.get("entity_name", "")
    business_number = names.get("business_number", "")
    source_id = best.get("source_id", "")
    status_raw = attrs.get("entity_status", "")
    entity_type = attrs.get("entity_type", "")
    reg_date = attrs.get("registration_date", "")
    home_jurisdiction = attrs.get("home_jurisdiction", "")
    inactive = best.get("inactive", False)
    revoked = best.get("revoked", False)

    status = _STATUS_MAP.get(status_raw, status_raw.upper() if status_raw else "UNKNOWN")
    if inactive or revoked:
        status = "INACTIVE"
    is_active = status == "ACTIVE"

    other_matches = []
    for m in results[1:5]:
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

    founded_year = reg_date[:4] if reg_date and len(reg_date) >= 4 else None

    return {
        "found": True,
        "legal_name": legal_name or entity_name,
        "business_registration_number": business_number or None,
        "founded_year": founded_year,
        "registration_date": reg_date[:10] if reg_date else None,
        "is_listed": False,
        # Country-specific extras pass through via engine extras
        "business_number": business_number or None,
        "source_id": source_id or None,
        "entity_type": _ENTITY_TYPE_MAP.get(entity_type, entity_type) or None,
        "entity_type_code": entity_type or None,
        "home_jurisdiction": _JURISDICTION_MAP.get(home_jurisdiction, home_jurisdiction) or None,
        "home_jurisdiction_code": home_jurisdiction or None,
        "registered_jurisdiction": "British Columbia",
        "inactive": inactive,
        "revoked": revoked,
        "total_matches": len(results),
        "other_matches": other_matches or None,
        "summary": (
            f"{legal_name or entity_name} — BN {business_number or 'N/A'} — "
            f"{status} — {_JURISDICTION_MAP.get(home_jurisdiction, home_jurisdiction or 'unknown')}"
        ),
    }


CA_CONFIG = eng.CountryConfig(
    country_code="CA",
    source_name="BC OrgBook (BC Registries & Online Services), Canada",
    transport=eng.T_MLX_HTTP,
    primary_url="https://orgbook.gov.bc.ca/api/v4/search/topic?q={q}&page_size=10",
    parser=_parse_ca,
    timeout=20,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://www.orgbook.gov.bc.ca/search → search '{entity}' → "
        "view entity details"
    ),
)


def orgbook_verify(entity_name: str, business_number: str = "") -> dict:
    """CA verify entry point — backward compat with main.py routing."""
    # If a BN was supplied, search by it (more precise than name)
    query = business_number.strip() or entity_name
    return eng.run(CA_CONFIG, query, {"business_number": business_number})
