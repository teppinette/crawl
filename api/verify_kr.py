"""
South Korea verify — runs on the generic engine.

Primary:   Naver business search via Multilogin (KR exit IP)
Enrichment: DART (FSS) OpenAPI for the listed subset
"""

import logging
import re

import verify_engine as eng
from proxy_cfg import get_dc_proxy
from curl_cffi import requests as cffi_requests

log = logging.getLogger("verify-gateway")

_DART_KEY = None
_DART_PROXY = None
_DART_BASE = "https://opendart.fss.or.kr/api"


def init(get_secret):
    global _DART_KEY, _DART_PROXY
    _DART_KEY = get_secret("dart-api-key")
    _DART_PROXY = get_dc_proxy()
    log.info("KR verify ready (engine) — Naver primary, DART enrichment %s",
             "enabled" if _DART_KEY else "disabled (no key)")


def _parse_kr(raw: dict, entity_name: str, ids: dict) -> dict:
    """Extract structured fields from a Naver business-search page."""
    src = (raw.get("body") or "") + "\n\n" + (raw.get("html") or "")
    if not src.strip():
        return {"found": False, "error": "empty_response"}

    # English name — typical: "영어: Lotte INEOS Chemicals"
    eng = re.search(r"영어\s*[:\-]\s*([A-Za-z][^,<\n)]{2,80})", src)
    legal_name_en = eng.group(1).strip().rstrip(".,") if eng else None

    # Founding year
    year = re.search(r"(19[5-9]\d|20[0-2]\d)\s*년", src)
    founded_year = year.group(1) if year else None

    # Headquarters — Korean address
    hq = re.search(
        r"본사[는은]?\s*"
        r"([가-힣]+(?:특별시|광역시|특별자치시|특별자치도|도)"
        r"\s*[가-힣]+(?:시|구|군)"
        r"\s*[가-힣0-9\-\s]{0,40}?\d+[\- ]?\d*)",
        src,
    )
    if not hq:
        hq = re.search(
            r"([가-힣]+(?:특별시|광역시|특별자치시|특별자치도)"
            r"\s*[가-힣]+(?:구|군)"
            r"\s*[가-힣]+(?:동|로|길|읍|면)"
            r"\s*[0-9\-]+)",
            src,
        )
    headquarters = hq.group(1).strip().rstrip(".") if hq else None

    # CEO — avoid capturing the literal "이사" or "대표"
    ceo = None
    for pattern in (
        r"대표이사\s+([가-힣]{2,4})(?![가-힣])",
        r"CEO\s+([가-힣]{2,4})(?![가-힣])",
        r"([가-힣]{2,4})\s+대표(?:이사)?(?![가-힣])",
    ):
        m = re.search(pattern, src)
        if m:
            cand = m.group(1)
            if cand not in ("이사", "대표", "회장", "사장"):
                ceo = cand
                break

    # JV / ownership
    jv = re.search(r"(\d+\s*대\s*\d+)\s*(?:로\s*)?합작", src)
    ownership_structure = f"JV {jv.group(1)}" if jv else None

    # Industry from keyword presence in first 5K chars (search snippet area)
    industry = None
    for kw, label in [("화학", "Chemicals"), ("제약", "Pharmaceuticals"),
                      ("반도체", "Semiconductors"), ("금융", "Financial Services"),
                      ("건설", "Construction"), ("자동차", "Automotive"),
                      ("식품", "Food"), ("전자", "Electronics"),
                      ("에너지", "Energy"), ("물류", "Logistics")]:
        if kw in src[:5000]:
            industry = label
            break

    # Did Naver's page mention the entity? (loose match — entity name embedded somewhere)
    name_squashed = entity_name.replace(" ", "")
    src_squashed = src.replace(" ", "")
    found = (entity_name in src) or (name_squashed in src_squashed)

    return {
        "found": found,
        "legal_name": entity_name if found else None,
        "legal_name_en": legal_name_en,
        "ceo": ceo,
        "headquarters": headquarters,
        "founded_year": founded_year,
        "industry": industry,
        "ownership_structure": ownership_structure,
    }


def _dart_enrich(entity_name: str, ids: dict, primary: dict) -> dict:
    """Optional DART (FSS) enrichment for listed companies."""
    if not _DART_KEY:
        return {}
    corp_code = (ids.get("corp_code") or "").strip()
    try:
        if not corp_code:
            corp_code = _dart_find_corp_code(entity_name)
        if not corp_code:
            return {}
        profile = _dart_company_profile(corp_code)
        if not profile:
            return {}
        out = {
            "stock_code": profile.get("stock_code"),
            "business_registration_number": profile.get("bizr_no"),
            "homepage": profile.get("hm_url"),
            "phone": profile.get("phn_no"),
            "is_listed": bool(profile.get("stock_code")),
            "enrichment_source": "DART (Financial Supervisory Service, Republic of Korea)",
            "enrichment_url": f"https://dart.fss.or.kr/dsae001/main.do?corp_code={corp_code}",
        }
        # Prefer DART CEO if Naver missed it
        if not primary.get("ceo") and profile.get("ceo_nm"):
            out["ceo"] = profile["ceo_nm"]
        return out
    except Exception as e:
        log.debug("DART enrichment skipped: %s", e)
        return {}


def _dart_find_corp_code(entity_name: str) -> str:
    s = cffi_requests.Session(impersonate="chrome")
    s.get("https://dart.fss.or.kr/", proxy=_DART_PROXY, timeout=15)
    r = s.post(
        "https://dart.fss.or.kr/dsab001/search.ax",
        data={"textCrpNm": entity_name, "currentPage": "1", "maxResults": "5", "textCrpCik": ""},
        proxy=_DART_PROXY, timeout=15,
    )
    if r.status_code != 200:
        return ""
    m = re.findall(r"openCorpInfoNew\('(\d+)'", r.text)
    return m[0] if m else ""


def _dart_company_profile(corp_code: str) -> dict:
    r = cffi_requests.get(
        f"{_DART_BASE}/company.json",
        params={"crtfc_key": _DART_KEY, "corp_code": corp_code},
        impersonate="chrome", proxy=_DART_PROXY, timeout=15,
    )
    if r.status_code != 200:
        return {}
    d = r.json()
    if d.get("status") != "000":
        return {}
    return d


KR_CONFIG = eng.CountryConfig(
    country_code="KR",
    source_name="Naver business search (Republic of Korea)",
    transport=eng.T_MLX_NAVIGATE,
    primary_url="https://search.naver.com/search.naver?query={q}",
    wait_s=5,
    timeout=60,
    parser=_parse_kr,
    enrichment=_dart_enrich,
    how_to_reproduce_template=(
        "Visit {url} → view the entity business panel. "
        "For listed entities cross-check via https://dart.fss.or.kr."
    ),
)


def kr_verify(entity_name: str, corp_code: str = "", brn: str = "") -> dict:
    """KR verify entry point — backward compat with main.py routing."""
    return eng.run(KR_CONFIG, entity_name, {"corp_code": corp_code, "brn": brn})


# Backward-compat alias (existing main.py calls dart_verify)
dart_verify = kr_verify
