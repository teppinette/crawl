"""
Source: contact_verify — reusable Tier-1 verification of a counterparty's
CONTACT data pulled off its trade/KYC docs: PHONE (valid + country consistent)
and ADDRESS (geocodes to a country consistent with the stated country).

Companion to domain_trust (email/website). Country-consistency is the bar:
a "Taiwan" counterparty with a +86 China phone, or an address that geocodes to a
different country, is a REVIEW flag — the exact contact-data signal fraud review
(copap-ds fraud LLM) then scores and the risk assessment consumes as a line item.

Deterministic + offline for phone (libphonenumber); free geocode (OSM Nominatim,
no key) for address. Never raises.
"""

import logging
import re

import requests

log = logging.getLogger("crawl-gateway")

_UA = "Crawl-Research-Gateway/3.0 (+contact-verify; tom@copap.com)"
_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_GEO_TIMEOUT = 12

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat
    _HAS_PN = True
except Exception:  # pragma: no cover
    _HAS_PN = False


def check_phone(raw_phone, country_code):
    """Phone country-consistency. country_code = ISO-2 of the counterparty
    (e.g. 'TW'). VERIFIED when the number is valid AND its region matches the
    counterparty country; REVIEW on a country mismatch or an invalid number."""
    raw = (raw_phone or "").strip()
    cc = (country_code or "").strip().upper() or None
    out = {"phone": raw, "verdict": "REVIEW", "valid": False, "region": None,
           "e164": None, "reasons": []}
    if not raw:
        return None
    if not _HAS_PN:
        out["reasons"] = ["phone library unavailable"]
        return out
    try:
        num = phonenumbers.parse(raw, cc)
    except Exception:
        out["reasons"] = ["Could not parse the phone number"]
        return out
    valid = phonenumbers.is_valid_number(num)
    region = phonenumbers.region_code_for_number(num)
    out["valid"] = valid
    out["region"] = region
    try:
        out["e164"] = phonenumbers.format_number(num, PhoneNumberFormat.E164)
    except Exception:
        pass
    if not valid:
        out["reasons"] = ["Not a valid phone number"]
        out["verdict"] = "REVIEW"
    elif cc and region and region != cc:
        out["reasons"] = ["Phone country %s does not match counterparty country %s" % (region, cc)]
        out["verdict"] = "REVIEW"
    else:
        out["reasons"] = ["Valid; country %s consistent with the counterparty" % (region or cc or "?")]
        out["verdict"] = "VERIFIED"
    return out


def _geocode(address):
    """OSM Nominatim → {country_code, country, state, city, lat, lon} or None."""
    try:
        r = requests.get(
            _NOMINATIM,
            params={"q": address, "format": "jsonv2", "addressdetails": 1, "limit": 1},
            headers={"User-Agent": _UA}, timeout=_GEO_TIMEOUT)
        j = r.json()
    except Exception as e:
        log.info("geocode failed: %s", e)
        return None
    if not j:
        return None
    a = j[0].get("address") or {}
    return {
        "country_code": (a.get("country_code") or "").upper() or None,
        "country": a.get("country"),
        "state": a.get("state") or a.get("region"),
        "city": a.get("city") or a.get("town") or a.get("village") or a.get("county"),
        "lat": j[0].get("lat"), "lon": j[0].get("lon"),
        "display": j[0].get("display_name"),
    }


def check_address(raw_address, country_code):
    """Address country-consistency via geocoding. VERIFIED when the address
    geocodes to the counterparty's country; REVIEW when it geocodes elsewhere
    or cannot be resolved. (District plausibility is left to the fraud LLM.)"""
    raw = (raw_address or "").strip()
    cc = (country_code or "").strip().upper() or None
    if not raw or len(raw) < 6:
        return None
    out = {"address": raw[:200], "verdict": "REVIEW", "geo": None, "reasons": []}
    geo = _geocode(raw)
    out["geo"] = geo
    if not geo or not geo.get("country_code"):
        out["reasons"] = ["Address could not be geolocated"]
        return out
    gc = geo["country_code"]
    loc = ", ".join(x for x in (geo.get("city"), geo.get("state"), geo.get("country")) if x)
    if cc and gc != cc:
        out["reasons"] = ["Address geolocates to %s (%s), not the counterparty country %s" % (geo.get("country") or gc, loc, cc)]
        out["verdict"] = "REVIEW"
    else:
        out["reasons"] = ["Geolocates to %s — consistent with the counterparty" % (loc or gc)]
        out["verdict"] = "VERIFIED"
    return out


def _rank(v):
    return {"VERIFIED": 0, "REVIEW": 1}.get(v, 2)


def check(entity_name=None, country_code=None, phones=None, addresses=None):
    """Verify a counterparty's contact data. Returns per-item verdicts + an
    overall (worst) verdict. Reusable by onboarding, copap-ds fraud LLM, CIR."""
    phone_results = [r for r in (check_phone(p, country_code) for p in (phones or [])) if r]
    addr_results = [r for r in (check_address(a, country_code) for a in (addresses or [])) if r]
    all_v = [r["verdict"] for r in phone_results + addr_results]
    overall = min(all_v, key=_rank) if all_v else "NO_DATA"
    # worst wins for the headline (any REVIEW -> overall REVIEW)
    if any(v == "REVIEW" for v in all_v):
        overall = "REVIEW"
    elif all_v:
        overall = "VERIFIED"
    return {
        "source_id": "contact_verify",
        "source_url": "https://nominatim.openstreetmap.org",
        "country_code": (country_code or "").upper() or None,
        "verdict": overall,
        "phones": phone_results,
        "addresses": addr_results,
    }
