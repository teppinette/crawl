"""
Japan company verification via Houjin Bangou (Corporate Number) API.

Source: https://www.houjin-bangou.nta.go.jp/webapi/
Free API — requires application ID (registered).
Corporate number is 13 digits.

Input: entity_name (search by name) or corp_number (13 digits)
Returns: legal_name, corp_number, status, address, kind
"""

import logging
import re
import time
import xml.etree.ElementTree as ET

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_API_BASE = "https://api.houjin-bangou.nta.go.jp/4"
_APP_ID = ""

_KIND_MAP = {
    "101": "株式会社 (Kabushiki Kaisha / Corp.)",
    "201": "有限会社 (Yugen Kaisha / Ltd.)",
    "301": "合名会社 (Gomei Kaisha / General Partnership)",
    "302": "合資会社 (Goshi Kaisha / Limited Partnership)",
    "303": "合同会社 (Godo Kaisha / LLC)",
    "399": "その他の設立登記法人 (Other registered entity)",
    "401": "外国会社等 (Foreign company)",
    "499": "その他 (Other)",
    "601": "国の機関 (National gov agency)",
    "602": "地方公共団体 (Local gov)",
    "603": "株式会社以外の法人 (Non-corporate entity)",
    "701": "NPO法人 (NPO)",
}

_PROCESS_MAP = {
    "01": "NEW", "11": "TRADE_NAME_CHANGE", "12": "DOMESTIC_ADDRESS_CHANGE",
    "13": "FOREIGN_ADDRESS_CHANGE", "21": "REGISTRATION_CHANGE",
    "22": "MERGER", "71": "DISSOLVED", "72": "DISSOLVED_MERGER",
    "81": "REVOCATION", "99": "DELETED",
}


def init(get_secret):
    global _APP_ID
    _APP_ID = get_secret("houjin-bangou-app-id") or ""
    if _APP_ID:
        log.info("JP Houjin Bangou ready (NTA Corporate Number API)")
    else:
        log.warning("JP Houjin Bangou — no app ID configured (set houjin-bangou-app-id)")


def houjin_verify(entity_name: str, corp_number: str = "") -> dict:
    if not entity_name and not corp_number:
        return {"found": False, "error": "entity_name or corp_number required"}

    if not _APP_ID:
        return {
            "entity_name": entity_name, "found": False,
            "error": "Japan NTA API app ID not configured — register at houjin-bangou.nta.go.jp",
        }

    try:
        if corp_number:
            clean = re.sub(r"[\s\-]", "", corp_number.strip())
            if not re.match(r"^\d{13}$", clean):
                return {"corp_number": corp_number, "found": False,
                        "error": "Corporate number must be 13 digits"}
            return _lookup_number(clean, entity_name)
        return _search_name(entity_name)
    except Exception as e:
        log.error("JP Houjin error for %s: %s", entity_name or corp_number, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_number(corp_number: str, entity_name: str) -> dict:
    result = mlx_get(
        f"{_API_BASE}/num",
        params={"id": _APP_ID, "number": corp_number, "type": 12, "history": 0},
        timeout=30, country_code="jp",
    )
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")

    # Try JSON first, fall back to XML
    data = result.get("json")
    if data and isinstance(data, dict):
        corps = data.get("corporations", [])
        if not corps:
            return _not_found(entity_name, corp_number)
        return _format(corps[0], entity_name, 1, [])

    # XML fallback
    return _parse_xml(result.get("body", ""), entity_name, corp_number)


def _search_name(entity_name: str) -> dict:
    result = mlx_get(
        f"{_API_BASE}/name",
        params={"id": _APP_ID, "name": entity_name, "type": 12, "maxCount": 10, "mode": 2},
        timeout=30, country_code="jp",
    )
    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")

    # Try JSON first, fall back to XML
    data = result.get("json")
    if data and isinstance(data, dict):
        corps = data.get("corporations", [])
        if not corps:
            return _not_found(entity_name, "")
        best = corps[0]
        others = [_summary(c) for c in corps[1:5]]
        return _format(best, entity_name, len(corps), others)

    return _parse_xml(result.get("body", ""), entity_name, "")


def _parse_xml(xml_text: str, entity_name: str, corp_number: str) -> dict:
    """Parse NTA XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _not_found(entity_name, corp_number)

    corps = root.findall(".//corporation")
    if not corps:
        return _not_found(entity_name, corp_number)

    best = corps[0]
    others = []
    for c in corps[1:5]:
        others.append({
            "name": _xml_text(c, "name"),
            "corp_number": _xml_text(c, "corporateNumber"),
            "address": _xml_text(c, "prefectureName") + " " + _xml_text(c, "cityName"),
        })

    return _format_xml(best, entity_name, len(corps), others)


def _xml_text(el, tag: str) -> str:
    child = el.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def _format_xml(corp, entity_name: str, total: int, others: list) -> dict:
    name = _xml_text(corp, "name")
    cn = _xml_text(corp, "corporateNumber")
    kind = _xml_text(corp, "kind")
    process = _xml_text(corp, "process")
    prefecture = _xml_text(corp, "prefectureName")
    city = _xml_text(corp, "cityName")
    street = _xml_text(corp, "streetNumber")
    change_date = _xml_text(corp, "changeDate")
    assignment_date = _xml_text(corp, "assignmentDate")
    furigana = _xml_text(corp, "furigana")
    en_name = _xml_text(corp, "enName")

    address = " ".join(p for p in [prefecture, city, street] if p).strip()
    status = _PROCESS_MAP.get(process, process.upper() if process else "ACTIVE")
    kind_display = _KIND_MAP.get(kind, kind)

    return {
        "entity_name": en_name or name,
        "legal_name_ja": name,
        "legal_name_kana": furigana or None,
        "legal_name_en": en_name or None,
        "query_name": entity_name,
        "country_code": "JP",
        "found": True,
        "corp_number": cn,
        "kind": kind_display,
        "kind_code": kind,
        "status": status,
        "process_code": process,
        "registered_address": address or None,
        "prefecture": prefecture or None,
        "city": city or None,
        "assignment_date": assignment_date or None,
        "change_date": change_date or None,
        "total_matches": total,
        "other_matches": others or None,
        "source": "Houjin Bangou (National Tax Agency Corporate Number System), Japan",
        "validation_source": _source(entity_name or cn),
    }


def _format(corp: dict, entity_name: str, total: int, others: list) -> dict:
    name = corp.get("name", "")
    cn = corp.get("corporateNumber", "")
    kind = corp.get("kind", "")
    process = corp.get("process", "")
    prefecture = corp.get("prefectureName", "")
    city = corp.get("cityName", "")
    street = corp.get("streetNumber", "")
    furigana = corp.get("furigana", "")
    en_name = corp.get("enName", "")

    address = " ".join(p for p in [prefecture, city, street] if p).strip()
    status = _PROCESS_MAP.get(process, process.upper() if process else "ACTIVE")
    kind_display = _KIND_MAP.get(kind, kind)

    return {
        "entity_name": en_name or name,
        "legal_name_ja": name,
        "legal_name_kana": furigana or None,
        "legal_name_en": en_name or None,
        "query_name": entity_name,
        "country_code": "JP",
        "found": True,
        "corp_number": cn,
        "kind": kind_display,
        "kind_code": kind,
        "status": status,
        "process_code": process,
        "registered_address": address or None,
        "prefecture": prefecture or None,
        "city": city or None,
        "assignment_date": corp.get("assignmentDate", ""),
        "change_date": corp.get("changeDate", ""),
        "total_matches": total,
        "other_matches": others or None,
        "source": "Houjin Bangou (National Tax Agency Corporate Number System), Japan",
        "validation_source": _source(entity_name or cn),
    }


def _summary(corp: dict) -> dict:
    return {
        "name": corp.get("name", ""),
        "corp_number": corp.get("corporateNumber", ""),
        "address": (corp.get("prefectureName", "") + " " + corp.get("cityName", "")).strip(),
    }


def _not_found(entity_name: str, corp_number: str) -> dict:
    return {
        "entity_name": entity_name, "corp_number": corp_number or None,
        "country_code": "JP",
        "found": False, "status": "NOT_FOUND",
        "validation_source": _source(entity_name or corp_number),
    }


def _source(query: str) -> dict:
    return {
        "registry": "Houjin Bangou — National Tax Agency, Japan",
        "url": "https://www.houjin-bangou.nta.go.jp/",
        "api": f"{_API_BASE}/name",
        "how_to_reproduce": f"Visit houjin-bangou.nta.go.jp → Search: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
