"""
Taiwan MOEA company verification via GCIS Open Data API.

Source: https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8902-B7C3015AEB5F
Free JSON API — no auth, no rate limit observed.
Official MOEA (Ministry of Economic Affairs) open data.
Covers all ROC-registered companies with UBN (Unified Business Number).

Input: entity_name (search by name) or ubn (8-digit Unified Business Number)
Returns: legal_name, ubn, status, capital, registered_address, responsible_person,
         establishment_date, business_scope, country_code="TW"
"""

import logging
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

# GCIS Open Data API — current dataset IDs per Swagger
# (data.gcis.nat.gov.tw/resources/swagger/swagger.json, refreshed 2026-05-19)
# 公司登記基本資料-應用一 — full company record by UBN
_API_UBN  = "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
# 公司登記基本資料-應用三 — adds Company_Setup_Date + Cmp_Business (business items)
_API_UBN3 = "https://data.gcis.nat.gov.tw/od/data/api/236EE382-4942-41A9-BD03-CA0709025E7C"
# 公司登記關鍵字查詢 — name keyword search (requires Company_Status filter)
_API_NAME = "https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C"

# Status code for ACTIVE/approved companies (per GCIS spec, codes 01–33)
_STATUS_ACTIVE = "01"


def init(get_secret=None):
    log.info("TW MOEA GCIS ready (data.gcis.nat.gov.tw open data API, via Multilogin)")


def moea_verify(entity_name: str, ubn: str = "") -> dict:
    """
    Verify a Taiwanese company via MOEA GCIS open data API.

    If UBN provided: exact lookup by Unified Business Number (8 digits).
    If only name provided: substring search by company name.
    """
    if not entity_name and not ubn:
        return {"found": False, "error": "entity_name or ubn required"}

    clean_ubn = ubn.strip() if ubn else ""

    try:
        if clean_ubn:
            records = _search_by_ubn(clean_ubn)
        else:
            records = _search_by_name(entity_name.strip())

        if not records:
            return {
                "entity_name": entity_name,
                "country_code": "TW",
                "ubn": clean_ubn or None,
                "found": False,
                "status": "NOT_FOUND",
                "source": "GCIS (MOEA), Taiwan",
                "validation_source": _validation_source(entity_name, clean_ubn),
            }

        best = records[0]
        return _format_result(best, entity_name, clean_ubn, len(records), records[:5])

    except Exception as e:
        log.error("TW MOEA error for %s / UBN %s: %s", entity_name, ubn, e)
        return {"entity_name": entity_name, "country_code": "TW", "found": False, "error": str(e)[:300]}


def _search_by_ubn(ubn: str) -> list:
    """Exact lookup by 8-digit UBN via GCIS open data API."""
    # Spec requires Business_Accounting_NO to match [0-9]{8} exactly
    clean = "".join(c for c in ubn if c.isdigit())
    if len(clean) != 8:
        return []
    result = mlx_get(
        _API_UBN,
        params={
            "$format": "json",
            "$filter": f"Business_Accounting_NO eq {clean}",
        },
        timeout=60, country_code="tw",
    )
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or []
    if not isinstance(data, list):
        return []

    # Enrich with business items from App3 (best-effort, non-blocking on failure)
    if data:
        try:
            r3 = mlx_get(
                _API_UBN3,
                params={"$format": "json", "$filter": f"Business_Accounting_NO eq {clean}"},
                timeout=30, country_code="tw",
            )
            if r3.get("ok"):
                extra = r3.get("json") or []
                if isinstance(extra, list) and extra:
                    data[0]["Cmp_Business"] = extra[0].get("Cmp_Business")
                    if extra[0].get("Company_Setup_Date") and not data[0].get("Company_Setup_Date"):
                        data[0]["Company_Setup_Date"] = extra[0]["Company_Setup_Date"]
        except Exception as e:
            log.info("TW App3 enrichment failed for %s: %s", clean, str(e)[:80])
    return data


def _search_by_name(name: str) -> list:
    """Substring search by company name via GCIS keyword endpoint.

    Spec requires both Company_Name filter AND a Company_Status code (01–33).
    Default to 01 (ACTIVE) — most use cases want currently-registered companies.
    """
    # The keyword endpoint rejects whitespace inside the keyword
    keyword = name.strip().replace(" ", "")
    if not keyword:
        return []
    result = mlx_get(
        _API_NAME,
        params={
            "$format": "json",
            "$filter": f"Company_Name like {keyword} and Company_Status eq {_STATUS_ACTIVE}",
            "$top": "10",
        },
        timeout=60, country_code="tw",
    )
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or []
    if isinstance(data, list):
        return data
    return []


def _format_result(record: dict, query_name: str, query_ubn: str,
                   total_matches: int, top_matches: list) -> dict:
    """Format GCIS API record into standard verification response."""
    ubn         = (record.get("Business_Accounting_NO") or "").strip()
    legal_name  = (record.get("Company_Name") or "").strip()
    status_raw  = (record.get("Company_Status_Desc") or
                   record.get("Company_Status") or "").strip()
    capital_raw = record.get("Capital_Stock_Amount", "")
    paid_in_raw = record.get("Paid_In_Capital_Amount", "")
    address     = (record.get("Company_Location") or "").strip()
    responsible = (record.get("Responsible_Name") or "").strip()
    est_date    = str(record.get("Company_Setup_Date") or
                      record.get("Establishment_Approval_Date") or
                      record.get("Register_Organization_Date") or "").strip()
    org_type    = (record.get("Register_Organization_Desc") or
                   record.get("Organ_Belong") or
                   record.get("Company_Type") or "").strip()

    # business_scope from App3 Cmp_Business array, else legacy Business_Scope field
    biz_scope = ""
    cmp_business = record.get("Cmp_Business")
    if isinstance(cmp_business, list) and cmp_business:
        items = []
        for it in cmp_business:
            desc = (it.get("Business_Item_Desc") or "").strip()
            code = (it.get("Business_Item") or "").strip()
            if desc and not desc.startswith(("１", "２", "３")):  # skip free-text item descriptions
                items.append(f"{code} {desc}".strip() if code else desc)
        biz_scope = "; ".join(items[:20])
    if not biz_scope:
        biz_scope = (record.get("Business_Scope") or "").strip()

    # Normalise establishment date to YYYY-MM-DD (source is Taiwan calendar YYYMMDD or YYYY-MM-DD)
    est_date_clean = _parse_tw_date(est_date)

    # Capital: may be integer or string
    capital_display = None
    if capital_raw not in (None, "", "0", 0):
        try:
            capital_display = f"TWD {int(capital_raw):,}"
        except (ValueError, TypeError):
            capital_display = str(capital_raw)
    paid_in_display = None
    if paid_in_raw not in (None, "", "0", 0):
        try:
            paid_in_display = f"TWD {int(paid_in_raw):,}"
        except (ValueError, TypeError):
            paid_in_display = str(paid_in_raw)

    # Status normalisation
    status_map = {
        "核准設立": "ACTIVE",
        "撤銷": "REVOKED",
        "廢止": "DISSOLVED",
        "解散": "DISSOLVED",
        "停業": "SUSPENDED",
    }
    status = status_map.get(status_raw, status_raw.upper() if status_raw else "UNKNOWN")

    # Other matches
    other_matches = []
    for m in top_matches[1:]:
        other_matches.append({
            "ubn":   (m.get("Business_Accounting_NO") or "").strip(),
            "name":  (m.get("Company_Name") or "").strip(),
            "status": (m.get("Company_Status_Desc") or m.get("Company_Status") or "").strip(),
        })

    return {
        "entity_name":         legal_name or query_name,
        "query_name":          query_name,
        "country_code":        "TW",
        "found":               True,
        "ubn":                 ubn or query_ubn or None,
        "legal_name":          legal_name or None,
        "status":              status,
        "status_raw":          status_raw or None,
        "capital":             capital_display,
        "paid_in_capital":     paid_in_display,
        "registered_address":  address or None,
        "responsible_person":  responsible or None,
        "establishment_date":  est_date_clean or None,
        "organisation_type":   org_type or None,
        "business_scope":      biz_scope[:500] if biz_scope else None,
        "total_matches":       total_matches,
        "other_matches":       other_matches if other_matches else None,
        "source":              "GCIS Open Data (MOEA), Taiwan",
        "validation_source":   _validation_source(query_name, ubn or query_ubn),
    }


def _parse_tw_date(raw: str) -> str:
    """
    Convert Taiwan calendar dates to ISO 8601.

    Taiwan calendar year = Western year - 1911.
    Formats seen: YYYMMDD (7 digits), YYY/MM/DD, YYYY-MM-DD (already ISO).
    Returns ISO YYYY-MM-DD or the raw string if unparseable.
    """
    if not raw:
        return ""
    raw = raw.strip()
    # Already ISO
    if len(raw) == 10 and raw[4] == "-":
        return raw
    # YYY/MM/DD or YYY-MM-DD
    import re
    m = re.match(r"^(\d{3})[/\-](\d{2})[/\-](\d{2})$", raw)
    if m:
        year = int(m.group(1)) + 1911
        return f"{year}-{m.group(2)}-{m.group(3)}"
    # YYYMMDD (7 digits)
    m2 = re.match(r"^(\d{3})(\d{2})(\d{2})$", raw)
    if m2:
        year = int(m2.group(1)) + 1911
        return f"{year}-{m2.group(2)}-{m2.group(3)}"
    return raw


def _validation_source(query_name: str, ubn: str) -> dict:
    query_display = ubn if ubn else query_name
    return {
        "registry": "GCIS — Government Commercial Information Service, MOEA (Ministry of Economic Affairs), Taiwan (ROC)",
        "url": "https://findbiz.nat.gov.tw/fts/query/QueryBar/queryInit.do",
        "api": _API_UBN,
        "record_id": ubn or None,
        "how_to_reproduce": (
            f"Visit findbiz.nat.gov.tw → "
            f"Search: {query_display} → View company details"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
