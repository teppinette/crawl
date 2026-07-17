"""
Finland verify — runs on the generic engine.

Source: PRH Avoindata (Patentti- ja rekisterihallitus / Finnish Patent and
Registration Office). Free public JSON API, no auth.
Direct HTTP (avoindata.prh.fi, no proxy needed).

Single config (FI uses one endpoint for both name and Business ID search).
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BASE = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies"


def init(get_secret=None):
    log.info("FI verify ready (engine) — PRH Avoindata direct JSON")


def _parse_fi(raw: dict, entity_name: str, ids: dict) -> dict:
    if raw.get("status") == 404:
        return {"found": False, "note": "Business ID not found in PRH"}
    data = raw.get("json") or {}
    companies = data.get("companies") or []
    if not companies:
        return {"found": False}

    best = companies[0]

    # Names — type 1 = current primary, 2 = parallel, 3 = aux
    names = best.get("names") or []
    primary_name = next((n["name"] for n in names if n.get("type") == "1"), "")
    parallel_names = [n["name"] for n in names if n.get("type") == "2"]
    aux_names = [n["name"] for n in names if n.get("type") == "3"]

    bid = (best.get("businessId") or {}).get("value", "")
    bid_reg_date = (best.get("businessId") or {}).get("registrationDate", "")
    founded_year = bid_reg_date[:4] if bid_reg_date and len(bid_reg_date) >= 4 else None

    eu_id = (best.get("euId") or {}).get("value", "")

    # Main business line
    biz_line = best.get("mainBusinessLine") or {}
    industry_code = biz_line.get("type", "")
    descriptions = biz_line.get("descriptions") or []
    industry = next(
        (d.get("description", "") for d in descriptions if d.get("languageCode") == "1"),
        next((d.get("description", "") for d in descriptions), None),
    ) if descriptions else None

    # Status — "endDate" present and in past = dissolved
    end_date = best.get("endDate", "")
    is_dissolved = bool(end_date)
    status = "DISSOLVED" if is_dissolved else "ACTIVE"

    # Address
    addresses = best.get("addresses") or []
    addr_obj = next(
        (a for a in addresses if a.get("type") == "1"),  # visiting address
        addresses[0] if addresses else None,
    )
    if addr_obj:
        addr_parts = [
            addr_obj.get("street", ""),
            addr_obj.get("buildingNumber", ""),
            addr_obj.get("postCode", ""),
            addr_obj.get("postOffices", [{}])[0].get("city", "") if addr_obj.get("postOffices") else "",
        ]
        address = ", ".join(p for p in addr_parts if p) or None
    else:
        address = None

    # Company forms — current type
    company_forms = best.get("companyForms") or []
    legal_form = ""
    if company_forms:
        cf = company_forms[0]
        cf_descs = cf.get("descriptions") or []
        legal_form = next(
            (d.get("description", "") for d in cf_descs if d.get("languageCode") == "1"),
            next((d.get("description", "") for d in cf_descs), ""),
        )

    others = [
        {
            "businessId": (c.get("businessId") or {}).get("value", ""),
            "name": next((n["name"] for n in (c.get("names") or []) if n.get("type") == "1"), ""),
        }
        for c in companies[1:5]
    ]

    return {
        "found": True,
        "legal_name": primary_name or entity_name,
        "business_registration_number": bid or None,
        "headquarters": address,
        "founded_year": founded_year,
        "industry": industry,
        "is_listed": False,
        # FI-specific extras
        "business_id": bid or None,
        "eu_id": eu_id or None,
        "legal_form": legal_form or None,
        "industry_code": industry_code or None,
        "registration_date": bid_reg_date or None,
        "end_date": end_date or None,
        "parallel_names": parallel_names or None,
        "auxiliary_names": aux_names or None,
        "total_matches": data.get("totalResults", len(companies)),
        "other_matches": others or None,
        "status": status,
        "summary": (
            f"{primary_name} — BID {bid} — {status}"
            + (f" — {legal_form}" if legal_form else "")
        ),
    }


FI_BID_CONFIG = eng.CountryConfig(
    country_code="FI",
    source_name="PRH Avoindata (Patentti- ja rekisterihallitus), Finland",
    transport=eng.T_MLX_HTTP,
    primary_url=_BASE + "?businessId={bid}",
    parser=_parse_fi,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://tietopalvelu.ytj.fi → enter Business ID {entity}"
    ),
)

FI_NAME_CONFIG = eng.CountryConfig(
    country_code="FI",
    source_name="PRH Avoindata (Patentti- ja rekisterihallitus), Finland",
    transport=eng.T_MLX_HTTP,
    primary_url=_BASE + "?name={q}",
    parser=_parse_fi,
    timeout=15,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://tietopalvelu.ytj.fi → search '{entity}'"
    ),
)


def prh_verify(entity_name: str, business_id: str = "") -> dict:
    """FI verify entry point — backward compat with main.py routing."""
    bid = (business_id or "").strip()
    # FI Business ID format: 7 digits + "-" + check digit, e.g. 1927400-1
    if re.match(r"^\d{6,7}-\d$", bid):
        return eng.run(FI_BID_CONFIG, bid, {"bid": bid})
    return eng.run(FI_NAME_CONFIG, entity_name, {})
