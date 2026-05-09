"""
South Korea DART (FSS) company verification.

DART (Data Analysis, Retrieval and Transfer System) is the electronic
disclosure system of Korea's Financial Supervisory Service (FSS).

API: https://opendart.fss.or.kr/api/
Free API key required (registered at opendart.fss.or.kr).
IP whitelisted — uses Bright Data static KR IP.

Returns: company name (KR + EN), stock code, CEO, corp type,
         business registration number, address, industry, establishment date,
         fiscal year end, homepage, phone/fax.

Also searches DART filing history to confirm entity is active and regulated.
"""

import logging
import re
import time

from curl_cffi import requests as cffi_requests
from proxy_cfg import get_dc_proxy

log = logging.getLogger("verify-gateway")

_BASE_URL = "https://opendart.fss.or.kr/api"
_DART_KEY = None
_PROXY = None


def init(get_secret):
    global _DART_KEY, _PROXY
    _DART_KEY = get_secret("dart-api-key")
    _PROXY = get_dc_proxy()  # Static KR IP (103.252.109.79) — registered with DART
    if _DART_KEY:
        log.info("KR DART ready (FSS OpenAPI, Bright Data static KR IP 103.252.109.79)")
    else:
        log.warning("KR DART: API key not configured (set DART_API_KEY in .env)")


def dart_verify(entity_name: str, corp_code: str = "", brn: str = "") -> dict:
    """
    Verify a Korean company via DART OpenAPI.

    entity_name: company name (Korean or English)
    corp_code: DART corp_code (8 digits) for direct lookup
    brn: Business Registration Number (10 digits, optional)
    """
    if not _DART_KEY:
        return {
            "entity_name": entity_name,
            "found": False,
            "error": "DART API key not configured. Register at https://opendart.fss.or.kr",
        }

    if not entity_name and not corp_code:
        return {"found": False, "error": "entity_name or corp_code required"}

    try:
        if corp_code:
            return _lookup_by_corp_code(corp_code.strip())

        return _search_by_name(entity_name.strip())

    except Exception as e:
        log.error("KR DART error: %s", e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_by_corp_code(corp_code: str) -> dict:
    """Direct company profile lookup by DART corp_code."""
    resp = cffi_requests.get(
        f"{_BASE_URL}/company.json",
        params={"crtfc_key": _DART_KEY, "corp_code": corp_code},
        impersonate="chrome",
        proxy=_PROXY,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        return {
            "corp_code": corp_code,
            "found": False,
            "status": "NOT_FOUND",
            "note": data.get("message", "Company not found in DART"),
            "source": "DART (FSS), Republic of Korea",
        }

    return _format_company(data)


def _search_by_name(entity_name: str) -> dict:
    """
    Search DART by company name.

    DART OpenAPI doesn't have a name search endpoint directly.
    We use the corpCode.xml download (contains all companies) or
    the DART web search to find corp_code, then look up the profile.
    """
    # Use DART web search to find corp_code
    corp_code = _find_corp_code_web(entity_name)

    if not corp_code:
        return {
            "entity_name": entity_name,
            "found": False,
            "status": "NOT_FOUND",
            "note": "Company not found in DART disclosure system. "
                    "DART covers listed companies and large unlisted entities "
                    "required to file with Korea's FSS.",
            "source": "DART (FSS), Republic of Korea",
        }

    return _lookup_by_corp_code(corp_code)


def _find_corp_code_web(entity_name: str) -> str:
    """Find corp_code by searching DART web filing list."""
    try:
        session = cffi_requests.Session(impersonate="chrome")

        # Hit main page for cookies
        session.get("https://dart.fss.or.kr/", proxy=_PROXY, timeout=15)

        # Search filings by company name
        resp = session.post(
            "https://dart.fss.or.kr/dsab001/search.ax",
            data={
                "textCrpNm": entity_name,
                "currentPage": "1",
                "maxResults": "5",
                "textCrpCik": "",
            },
            proxy=_PROXY,
            timeout=15,
        )

        if resp.status_code != 200:
            return ""

        # Extract corp_code from openCorpInfoNew('XXXXXXXX', ...) onclick
        matches = re.findall(r"openCorpInfoNew\('(\d+)'", resp.text)
        if matches:
            return matches[0]

        return ""

    except Exception as e:
        log.warning("DART web search failed: %s", e)
        return ""


def _format_company(data: dict) -> dict:
    """Format DART company.json response."""
    corp_code = data.get("corp_code", "")
    corp_name = data.get("corp_name", "")
    corp_name_eng = data.get("corp_name_eng", "")
    stock_code = data.get("stock_code", "").strip()
    ceo_nm = data.get("ceo_nm", "")
    corp_cls = data.get("corp_cls", "")  # Y=KOSPI, K=KOSDAQ, N=KONEX, E=etc

    # Map corp_cls to readable
    cls_map = {"Y": "KOSPI (listed)", "K": "KOSDAQ (listed)", "N": "KONEX (listed)", "E": "Unlisted"}
    corp_type = cls_map.get(corp_cls, corp_cls)

    # Business registration number
    bizr_no = data.get("bizr_no", "")

    # Address
    adres = data.get("adres", "")

    # Industry
    induty_code = data.get("induty_code", "")

    # Dates
    est_dt = data.get("est_dt", "")  # YYYYMMDD
    if est_dt and len(est_dt) == 8:
        est_dt = f"{est_dt[:4]}-{est_dt[4:6]}-{est_dt[6:]}"

    # Fiscal year
    acc_mt = data.get("acc_mt", "")  # Month number

    return {
        "entity_name": corp_name,
        "entity_name_eng": corp_name_eng,
        "corp_code": corp_code,
        "found": True,
        "status": "ACTIVE" if stock_code or corp_cls else "REGISTERED",
        "stock_code": stock_code if stock_code else None,
        "market": corp_type,
        "ceo": ceo_nm,
        "business_registration_number": bizr_no,
        "address": adres,
        "industry_code": induty_code,
        "established_date": est_dt,
        "fiscal_year_end_month": acc_mt,
        "homepage": data.get("hm_url", ""),
        "phone": data.get("phn_no", ""),
        "fax": data.get("fax_no", ""),
        "source": "DART (FSS), Republic of Korea",
        "validation_source": {
            "registry": "DART — Financial Supervisory Service (FSS), Republic of Korea",
            "url": f"https://dart.fss.or.kr/dsae001/main.do?corp_code={corp_code}",
            "record_id": corp_code,
            "how_to_reproduce": (
                f"Visit https://dart.fss.or.kr → Search '{corp_name}' → "
                f"View company profile (corp_code: {corp_code})"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
