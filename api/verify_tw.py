"""
Taiwan verify — runs on the generic engine.

Source: MOEA GCIS Open Data API (data.gcis.nat.gov.tw). Free JSON.
Multilogin HTTP with TW exit IP.

Two query modes:
  - UBN exact lookup: $filter=Business_Accounting_NO eq <ubn>
  - Name keyword search: $filter=Company_Name like <keyword> and Company_Status eq 01

Both feed _parse_tw_record() which extracts a uniform structure. UBN path
also fetches App3 dataset for business scope (best-effort, non-blocking).
"""

import logging
import re

from mlx_http import mlx_get

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_API_UBN  = "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
_API_UBN3 = "https://data.gcis.nat.gov.tw/od/data/api/236EE382-4942-41A9-BD03-CA0709025E7C"
_API_NAME = "https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C"

_STATUS_ACTIVE = "01"

_STATUS_MAP = {
    "核准設立": "ACTIVE", "撤銷": "REVOKED", "廢止": "DISSOLVED",
    "解散": "DISSOLVED", "停業": "SUSPENDED",
}


def init(get_secret=None):
    log.info("TW verify ready (engine) — MOEA GCIS Open Data via Multilogin")


def _parse_tw_date(raw: str) -> str:
    """Convert Taiwan calendar (year - 1911) to ISO 8601."""
    if not raw:
        return ""
    raw = str(raw).strip()
    if len(raw) == 10 and raw[4] == "-":
        return raw
    m = re.match(r"^(\d{3})[/\-](\d{2})[/\-](\d{2})$", raw)
    if m:
        return f"{int(m.group(1)) + 1911}-{m.group(2)}-{m.group(3)}"
    m2 = re.match(r"^(\d{3})(\d{2})(\d{2})$", raw)
    if m2:
        return f"{int(m2.group(1)) + 1911}-{m2.group(2)}-{m2.group(3)}"
    return raw


def _enrich_app3(ubn: str) -> dict:
    """Best-effort: fetch business scope + setup date from App3."""
    try:
        r = mlx_get(
            _API_UBN3,
            params={"$format": "json", "$filter": f"Business_Accounting_NO eq {ubn}"},
            timeout=30, country_code="tw",
        )
        if not r.get("ok"):
            return {}
        data = r.get("json") or []
        if not (isinstance(data, list) and data):
            return {}
        return data[0]
    except Exception as e:
        log.info("TW App3 enrichment failed for %s: %s", ubn, str(e)[:80])
        return {}


def _parse_tw_record(raw: dict, entity_name: str, ids: dict) -> dict:
    """Parse a GCIS company-record list (UBN exact or name search returns same shape)."""
    records = raw.get("json")
    if not isinstance(records, list) or not records:
        return {"found": False}

    best = dict(records[0])  # copy so enrichment doesn't mutate engine state

    ubn = (best.get("Business_Accounting_NO") or "").strip()
    legal_name = (best.get("Company_Name") or "").strip()

    # App3 enrichment for business scope (UBN lookup only — name search already covers this)
    if ubn and ids.get("by_ubn"):
        app3 = _enrich_app3(ubn)
        if app3:
            best.setdefault("Cmp_Business", app3.get("Cmp_Business"))
            if app3.get("Company_Setup_Date") and not best.get("Company_Setup_Date"):
                best["Company_Setup_Date"] = app3["Company_Setup_Date"]

    status_raw  = (best.get("Company_Status_Desc") or best.get("Company_Status") or "").strip()
    capital_raw = best.get("Capital_Stock_Amount", "")
    paid_in_raw = best.get("Paid_In_Capital_Amount", "")
    address     = (best.get("Company_Location") or "").strip()
    responsible = (best.get("Responsible_Name") or "").strip()
    est_date    = str(best.get("Company_Setup_Date") or
                      best.get("Establishment_Approval_Date") or
                      best.get("Register_Organization_Date") or "").strip()
    org_type    = (best.get("Register_Organization_Desc") or
                   best.get("Organ_Belong") or best.get("Company_Type") or "").strip()

    biz_scope = ""
    cmp_business = best.get("Cmp_Business")
    if isinstance(cmp_business, list) and cmp_business:
        items = []
        for it in cmp_business:
            desc = (it.get("Business_Item_Desc") or "").strip()
            code = (it.get("Business_Item") or "").strip()
            if desc and not desc.startswith(("１", "２", "３")):
                items.append(f"{code} {desc}".strip() if code else desc)
        biz_scope = "; ".join(items[:20])
    if not biz_scope:
        biz_scope = (best.get("Business_Scope") or "").strip()

    est_date_clean = _parse_tw_date(est_date)
    founded_year = est_date_clean[:4] if est_date_clean and len(est_date_clean) >= 4 and est_date_clean[:4].isdigit() else None

    def fmt_money(x):
        if x in (None, "", "0", 0):
            return None
        try:
            return f"TWD {int(x):,}"
        except (ValueError, TypeError):
            return str(x)

    status = _STATUS_MAP.get(status_raw, status_raw.upper() if status_raw else "UNKNOWN")

    others = [
        {
            "ubn":    (m.get("Business_Accounting_NO") or "").strip(),
            "name":   (m.get("Company_Name") or "").strip(),
            "status": (m.get("Company_Status_Desc") or m.get("Company_Status") or "").strip(),
        }
        for m in records[1:5]
    ]

    return {
        "found": True,
        "legal_name": legal_name or entity_name,
        "business_registration_number": ubn or None,
        "headquarters": address or None,
        "founded_year": founded_year,
        "ceo": responsible or None,
        "industry": (biz_scope[:200] + "...") if biz_scope and len(biz_scope) > 200 else (biz_scope or None),
        "is_listed": False,
        # TW-specific extras
        "ubn": ubn or None,
        "status_raw": status_raw or None,
        "capital": fmt_money(capital_raw),
        "paid_in_capital": fmt_money(paid_in_raw),
        "registered_address": address or None,
        "responsible_person": responsible or None,
        "establishment_date": est_date_clean or None,
        "organisation_type": org_type or None,
        "business_scope": biz_scope[:500] if biz_scope else None,
        "total_matches": len(records),
        "other_matches": others or None,
        "status": status,
        "summary": (
            f"{legal_name or entity_name} — UBN {ubn or 'N/A'} — {status}"
            + (f" — {org_type}" if org_type else "")
        ),
    }


TW_UBN_CONFIG = eng.CountryConfig(
    country_code="TW",
    source_name="GCIS Open Data (MOEA), Taiwan",
    transport=eng.T_MLX_HTTP,
    primary_url=_API_UBN + "?$format=json&$filter=Business_Accounting_NO+eq+{q}",
    parser=_parse_tw_record,
    timeout=60,
    how_to_reproduce_template=(
        "Visit https://findbiz.nat.gov.tw → enter UBN {entity}"
    ),
)

TW_NAME_CONFIG = eng.CountryConfig(
    country_code="TW",
    source_name="GCIS Open Data (MOEA), Taiwan",
    transport=eng.T_MLX_HTTP,
    primary_url=(
        _API_NAME +
        "?$format=json&$filter=Company_Name+like+{q}+and+Company_Status+eq+" + _STATUS_ACTIVE +
        "&$top=10"
    ),
    parser=_parse_tw_record,
    timeout=60,
    how_to_reproduce_template=(
        "Visit https://findbiz.nat.gov.tw → search '{entity}'"
    ),
)


def moea_verify(entity_name: str, ubn: str = "") -> dict:
    """TW verify entry point — backward compat with main.py routing."""
    clean_ubn = "".join(c for c in (ubn or "").strip() if c.isdigit())
    if clean_ubn and len(clean_ubn) == 8:
        return eng.run(TW_UBN_CONFIG, clean_ubn, {"by_ubn": True})
    # Name search — keyword endpoint rejects whitespace inside keyword
    keyword = entity_name.strip().replace(" ", "")
    return eng.run(TW_NAME_CONFIG, keyword, {"by_ubn": False})
