"""
Australia verify — runs on the generic engine.

Source: ABR (Australian Business Register) JSONP API. Free, public GUID auth.
Multilogin HTTP with AU exit IP.

Two configs sharing parser:
  AU_ABN_CONFIG  — exact ABN lookup
  AU_NAME_CONFIG — name search
"""

import json
import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_ABN_URL  = "https://abr.business.gov.au/json/AbnDetails.aspx"
_NAME_URL = "https://abr.business.gov.au/json/MatchingNames.aspx"
_GUID = "3e7189e6-4743-4090-a8d1-348e58b498d6"

_STATE_MAP = {
    "NSW": "New South Wales", "VIC": "Victoria", "QLD": "Queensland",
    "WA": "Western Australia", "SA": "South Australia",
    "TAS": "Tasmania", "ACT": "Australian Capital Territory",
    "NT": "Northern Territory",
}


def init(get_secret=None):
    log.info("AU verify ready (engine) — ABR JSONP via Multilogin")


def _unwrap_jsonp(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"callback\((.*)\)$", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    return json.loads(text)


def _parse_au_abn(raw: dict, entity_name: str, ids: dict) -> dict:
    body = raw.get("body") or ""
    try:
        data = _unwrap_jsonp(body)
    except Exception as e:
        return {"found": False, "error": f"jsonp_parse: {str(e)[:120]}"}

    if data.get("Message"):
        return {"found": False, "note": data.get("Message")}

    name = data.get("EntityName", "")
    abn = data.get("Abn", "")
    if not name and not abn:
        return {"found": False}

    abn_status = data.get("AbnStatus", "")
    entity_type = data.get("EntityTypeName", "")
    state = data.get("AddressState", "")
    postcode = data.get("AddressPostcode", "")
    gst = data.get("Gst", "")
    acn = data.get("Acn", "")

    business_names = []
    bn = data.get("BusinessName")
    if isinstance(bn, list):
        business_names = [b for b in bn if b]
    elif isinstance(bn, str) and bn:
        business_names = [bn]

    return {
        "found": True,
        "legal_name": name,
        "business_registration_number": abn or None,
        "headquarters": f"{_STATE_MAP.get(state, state)} {postcode}".strip() or None,
        "industry": entity_type or None,
        "is_listed": False,
        # AU-specific extras
        "abn": abn or None,
        "acn": acn or None,
        "entity_type": entity_type or None,
        "abn_status": abn_status or None,
        "state": _STATE_MAP.get(state, state) or None,
        "state_code": state or None,
        "postcode": postcode or None,
        "gst_registered": bool(gst) if gst != "" else None,
        "business_names": business_names or None,
        "status": "ACTIVE" if abn_status == "Active" else (abn_status.upper() if abn_status else "UNKNOWN"),
        "summary": (
            f"{name} — ABN {abn} — {abn_status or 'unknown'}"
            + (f" — {entity_type}" if entity_type else "")
            + (f" ({_STATE_MAP.get(state, state)})" if state else "")
        ),
    }


def _parse_au_name(raw: dict, entity_name: str, ids: dict) -> dict:
    body = raw.get("body") or ""
    try:
        data = _unwrap_jsonp(body)
    except Exception as e:
        return {"found": False, "error": f"jsonp_parse: {str(e)[:120]}"}

    names = data.get("Names") or []
    if not names:
        return {"found": False}

    best = names[0]
    abn = best.get("Abn", "")

    matches = [
        {
            "name": n.get("Name", ""),
            "abn": n.get("Abn", ""),
            "abn_status": n.get("AbnStatus", ""),
            "state": _STATE_MAP.get(n.get("State", ""), n.get("State", "")),
            "postcode": n.get("Postcode", ""),
            "is_current": n.get("IsCurrent", ""),
            "score": n.get("Score", ""),
        }
        for n in names[:5]
    ]

    # Now fetch full detail for the best ABN match
    if abn:
        # Trigger a second engine call by mimicking the ABN parser on a direct fetch
        from mlx_http import mlx_get
        try:
            r = mlx_get(
                _ABN_URL,
                params={"abn": abn, "callback": "callback", "guid": _GUID},
                country_code="au", timeout=30,
            )
            if r.get("ok"):
                detail = _parse_au_abn({"body": r.get("body", "")}, entity_name, ids)
                if detail.get("found"):
                    detail["total_matches"] = len(names)
                    detail["alternative_matches"] = matches[1:] or None
                    return detail
        except Exception as e:
            log.debug("AU detail fetch failed: %s", e)

    # Fallback: return search-only data
    return {
        "found": True,
        "legal_name": best.get("Name", "") or entity_name,
        "business_registration_number": abn or None,
        "is_listed": False,
        "abn": abn or None,
        "abn_status": best.get("AbnStatus", ""),
        "state": _STATE_MAP.get(best.get("State", ""), best.get("State", "")) or None,
        "postcode": best.get("Postcode", "") or None,
        "status": best.get("AbnStatus", "").upper() or "UNKNOWN",
        "total_matches": len(names),
        "alternative_matches": matches[1:] or None,
        "summary": f"{best.get('Name','')} — ABN {abn or 'N/A'} — {best.get('AbnStatus','unknown')}",
    }


AU_ABN_CONFIG = eng.CountryConfig(
    country_code="AU",
    source_name="Australian Business Register (ABR)",
    transport=eng.T_MLX_HTTP,
    primary_url=_ABN_URL + "?abn={q}&callback=callback&guid=" + _GUID,
    parser=_parse_au_abn,
    timeout=30,
    how_to_reproduce_template="Visit https://abr.business.gov.au → lookup ABN {entity}",
)

AU_NAME_CONFIG = eng.CountryConfig(
    country_code="AU",
    source_name="Australian Business Register (ABR)",
    transport=eng.T_MLX_HTTP,
    primary_url=_NAME_URL + "?name={q}&callback=callback&guid=" + _GUID,
    parser=_parse_au_name,
    timeout=30,
    how_to_reproduce_template="Visit https://abr.business.gov.au → search '{entity}'",
)


def abr_verify(entity_name: str, abn: str = "") -> dict:
    """AU verify entry point — backward compat with main.py routing."""
    if abn:
        clean = re.sub(r"[\s\-]", "", abn.strip())
        if re.match(r"^\d{11}$", clean):
            return eng.run(AU_ABN_CONFIG, clean, {"abn": clean})
    return eng.run(AU_NAME_CONFIG, entity_name, {})
