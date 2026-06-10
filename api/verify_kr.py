"""
South Korea verification — Naver business search (primary, via Multilogin) +
DART (FSS) enrichment for listed companies.

Why this shape:
- DART only indexes listed companies + large public filers. Private JVs
  (e.g. Lotte INEOS Chemical) are invisible to DART by design.
- Naver business search covers ALL Korean entities — private, JV, listed —
  and returns structured business info (KR+EN name, HQ, CEO, founding year,
  industry, ownership). Naver is JS-rendered + anti-bot, so we use Multilogin
  with a KR exit IP.
- DART is kept as ENRICHMENT for the subset that is publicly listed
  (adds stock code, BRN, fiscal year detail, filing history).
"""

import logging
import re
import time
import urllib.parse

import mlx_http

log = logging.getLogger("verify-gateway")

from curl_cffi import requests as cffi_requests
from proxy_cfg import get_dc_proxy

_DART_BASE = "https://opendart.fss.or.kr/api"
_DART_KEY = None
_DART_PROXY = None
_NAVER_BASE = "https://search.naver.com/search.naver"


def init(get_secret):
    global _DART_KEY, _DART_PROXY
    _DART_KEY = get_secret("dart-api-key")
    _DART_PROXY = get_dc_proxy()
    if _DART_KEY:
        log.info("KR verify ready — Naver (Multilogin, primary) + DART (enrichment)")
    else:
        log.warning("KR verify ready — Naver only (DART key not configured)")


def kr_verify(entity_name: str, corp_code: str = "", brn: str = "") -> dict:
    """
    Verify a Korean entity.

    Primary: Naver business search via Multilogin (KR proxy) — covers private + public.
    Enrichment: DART (FSS) for listed companies if corp_code provided OR if entity is in DART.
    """
    if not entity_name and not corp_code:
        return {"found": False, "error": "entity_name or corp_code required"}

    naver = _naver_search(entity_name) if entity_name else {}

    dart = {}
    if _DART_KEY and (corp_code or naver.get("found")):
        dart = _dart_enrich(entity_name=entity_name, corp_code=corp_code)

    return _merge(entity_name, naver, dart)


def _naver_search(entity_name: str) -> dict:
    try:
        q = urllib.parse.quote(entity_name)
        url = f"{_NAVER_BASE}?query={q}"
        r = mlx_http.mlx_navigate(url=url, wait_s=5, country_code="KR", timeout=60)
        html = r.get("html") or ""
        body = r.get("body") or ""

        if not html and not body:
            return {"found": False, "note": "Naver returned empty"}

        result = _parse_naver(html, body, entity_name)
        result["naver_search_url"] = url
        return result
    except Exception as e:
        log.warning("Naver KR search failed for %s: %s", entity_name, e)
        return {"found": False, "error": f"naver_unreachable: {str(e)[:120]}"}


def _parse_naver(html: str, body: str, entity_name: str) -> dict:
    src = body + "\n\n" + html

    eng_match = re.search(r"영어\s*[:\-]\s*([A-Za-z][^,<\n)]{2,80})", src)
    english_name = eng_match.group(1).strip().rstrip(".,") if eng_match else None

    year_match = re.search(r"(19[5-9]\d|20[0-2]\d)\s*년", src)
    founded = year_match.group(1) if year_match else None

    hq_match = re.search(
        r"본사[는은]?\s*"
        r"([가-힣]+(?:특별시|광역시|특별자치시|특별자치도|도)"
        r"\s*[가-힣]+(?:시|구|군)"
        r"\s*[가-힣0-9\-\s]{0,40}?\d+[\- ]?\d*)",
        src,
    )
    if not hq_match:
        hq_match = re.search(
            r"([가-힣]+(?:특별시|광역시|특별자치시|특별자치도)"
            r"\s*[가-힣]+(?:구|군)"
            r"\s*[가-힣]+(?:동|로|길|읍|면)"
            r"\s*[0-9\-]+)",
            src,
        )
    headquarters = hq_match.group(1).strip().rstrip(".") if hq_match else None

    ceo = None
    # "대표이사 NAME" or "대표이사 NAME 대표" — capture 2-4 char Korean name
    for pattern in (
        r"대표이사\s+([가-힣]{2,4})(?![가-힣])",
        r"CEO\s+([가-힣]{2,4})(?![가-힣])",
        r"([가-힣]{2,4})\s+(?:롯데이네오스화학\s+)?대표(?:이사)?(?![가-힣])",
    ):
        m = re.search(pattern, src)
        if m:
            cand = m.group(1)
            if cand not in ("이사", "대표", "회장", "사장"):
                ceo = cand
                break

    jv_match = re.search(r"(\d+\s*대\s*\d+)\s*(?:로\s*)?합작", src)
    ownership = f"JV {jv_match.group(1)}" if jv_match else None

    industry = None
    for kw, label in [("화학", "Chemicals"), ("제약", "Pharmaceuticals"),
                      ("반도체", "Semiconductors"), ("금융", "Financial Services"),
                      ("건설", "Construction"), ("자동차", "Automotive"),
                      ("식품", "Food"), ("전자", "Electronics"),
                      ("에너지", "Energy"), ("물류", "Logistics")]:
        if kw in src[:5000]:
            industry = label
            break

    found = (entity_name in src) or (entity_name.replace(" ", "") in src.replace(" ", ""))

    return {
        "found": found,
        "legal_name_kr": entity_name if found else None,
        "legal_name_en": english_name,
        "ceo": ceo,
        "headquarters": headquarters,
        "founded_year": founded,
        "industry": industry,
        "ownership_structure": ownership,
        "source": "Naver business search (Korea)",
    }


def _dart_enrich(entity_name: str, corp_code: str = "") -> dict:
    try:
        if corp_code:
            return _dart_company_profile(corp_code)
        cc = _dart_find_corp_code(entity_name)
        if cc:
            return _dart_company_profile(cc)
        return {}
    except Exception as e:
        log.debug("DART enrichment failed: %s", e)
        return {}


def _dart_find_corp_code(entity_name: str) -> str:
    session = cffi_requests.Session(impersonate="chrome")
    session.get("https://dart.fss.or.kr/", proxy=_DART_PROXY, timeout=15)
    resp = session.post(
        "https://dart.fss.or.kr/dsab001/search.ax",
        data={"textCrpNm": entity_name, "currentPage": "1", "maxResults": "5", "textCrpCik": ""},
        proxy=_DART_PROXY, timeout=15,
    )
    if resp.status_code != 200:
        return ""
    m = re.findall(r"openCorpInfoNew\('(\d+)'", resp.text)
    return m[0] if m else ""


def _dart_company_profile(corp_code: str) -> dict:
    resp = cffi_requests.get(
        f"{_DART_BASE}/company.json",
        params={"crtfc_key": _DART_KEY, "corp_code": corp_code},
        impersonate="chrome", proxy=_DART_PROXY, timeout=15,
    )
    if resp.status_code != 200:
        return {}
    d = resp.json()
    if d.get("status") != "000":
        return {}
    return {
        "dart_corp_code": corp_code,
        "dart_stock_code": (d.get("stock_code") or "").strip() or None,
        "dart_brn": d.get("bizr_no") or None,
        "dart_ceo": d.get("ceo_nm") or None,
        "dart_address": d.get("adres") or None,
        "dart_market": d.get("corp_cls"),
        "dart_established_date": d.get("est_dt"),
        "dart_homepage": d.get("hm_url") or None,
        "dart_phone": d.get("phn_no") or None,
    }


def _merge(entity_name: str, naver: dict, dart: dict) -> dict:
    found = bool(naver.get("found")) or bool(dart.get("dart_corp_code"))
    return {
        "entity_name": entity_name,
        "country_code": "KR",
        "found": found,
        "verified": found,
        "legal_name": naver.get("legal_name_kr") or entity_name,
        "legal_name_en": naver.get("legal_name_en"),
        "ceo": dart.get("dart_ceo") or naver.get("ceo"),
        "headquarters": dart.get("dart_address") or naver.get("headquarters"),
        "business_registration_number": dart.get("dart_brn"),
        "stock_code": dart.get("dart_stock_code"),
        "founded_year": naver.get("founded_year"),
        "industry": naver.get("industry"),
        "ownership_structure": naver.get("ownership_structure"),
        "homepage": dart.get("dart_homepage"),
        "phone": dart.get("dart_phone"),
        "is_listed": bool(dart.get("dart_stock_code")),
        "source": "DART (FSS) + Naver business search" if dart else "Naver business search",
        "validation_source": {
            "primary": "Naver business search (Republic of Korea)",
            "primary_url": naver.get("naver_search_url"),
            "enrichment": "DART — Financial Supervisory Service" if dart else None,
            "enrichment_url": f"https://dart.fss.or.kr/dsae001/main.do?corp_code={dart['dart_corp_code']}" if dart.get("dart_corp_code") else None,
            "how_to_reproduce": (
                f"Visit https://search.naver.com/search.naver?query={urllib.parse.quote(entity_name)} → "
                f"View business panel. For listed entities cross-check via https://dart.fss.or.kr."
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "status": "ACTIVE" if found else "NOT_FOUND",
        "summary": (
            f"'{entity_name}' verified via Naver (KR) — "
            f"{'listed (DART confirmed)' if dart.get('dart_stock_code') else 'private/unlisted'}"
        ) if found else f"'{entity_name}' not found in Naver KR or DART",
    }


# Backward-compat: existing main.py calls dart_verify()
dart_verify = kr_verify
