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


def _twilio_lookup(e164):
    """Paid enrichment: Twilio Lookup v2 line-type intelligence — mobile /
    landline / VOIP + carrier. VOIP for a company that should have a landline
    is a fraud tell. Key-gated (crawl-kv twilio-account-sid + twilio-auth-token);
    returns None when unconfigured. Never raises."""
    if not e164:
        return None
    try:
        from keyvault import get_secret
        sid = get_secret("twilio-account-sid")
        tok = get_secret("twilio-auth-token")
    except Exception:
        sid = tok = None
    if not sid or not tok:
        return None
    try:
        r = requests.get(
            "https://lookups.twilio.com/v2/PhoneNumbers/%s" % e164,
            params={"Fields": "line_type_intelligence"},
            auth=(sid, tok), headers={"User-Agent": _UA}, timeout=_GEO_TIMEOUT)
        if r.status_code >= 400:
            log.info("twilio lookup %s for %s", r.status_code, e164)
            return None
        j = r.json()
    except Exception as e:
        log.info("twilio lookup failed for %s: %s", e164, e)
        return None
    lti = j.get("line_type_intelligence") or {}
    return {"valid": j.get("valid"), "line_type": lti.get("type"),
            "carrier": lti.get("carrier_name"), "country": j.get("country_code")}


def check_phone(raw_phone, country_code):
    """Phone country-consistency. country_code = ISO-2 of the counterparty
    (e.g. 'TW'). VERIFIED when the number is valid AND its region matches the
    counterparty country; REVIEW on a country mismatch or an invalid number.
    When a Twilio key is configured, enriches with line-type/carrier + flags VOIP."""
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
    # Paid enrichment (Twilio Lookup): line type + carrier; flag VOIP.
    tw = _twilio_lookup(out.get("e164"))
    if tw:
        out["line_type"] = tw.get("line_type")
        out["carrier"] = tw.get("carrier")
        if (tw.get("line_type") or "").lower() == "voip":
            out["reasons"].append("Flag: VOIP line (higher fraud risk than a fixed line)")
    return out


def _parse_geo(hit):
    a = hit.get("address") or {}
    return {
        "country_code": (a.get("country_code") or "").upper() or None,
        "country": a.get("country"),
        "state": a.get("state") or a.get("region"),
        "city": a.get("city") or a.get("town") or a.get("village") or a.get("county"),
        "lat": hit.get("lat"), "lon": hit.get("lon"),
        "display": hit.get("display_name"),
    }


def _geocode(address):
    """Address -> {country_code, country, state, city, lat, lon}. Prefers the
    PAID LocationIQ geocoder (accurate + no rate-limit, needed for a book-wide
    scan) when crawl-kv 'locationiq-token' is set; falls back to the free OSM
    Nominatim. Both are OSM data; LocationIQ is the reliable/accurate tier."""
    # Paid tier: LocationIQ (OSM-based).
    try:
        from keyvault import get_secret
        liq = get_secret("locationiq-token")
    except Exception:
        liq = None
    if liq:
        try:
            r = requests.get(
                "https://us1.locationiq.com/v1/search",
                params={"key": liq, "q": address, "format": "json",
                        "addressdetails": 1, "limit": 1, "normalizeaddress": 1},
                headers={"User-Agent": _UA}, timeout=_GEO_TIMEOUT)
            if r.status_code < 400:
                j = r.json()
                if j:
                    return _parse_geo(j[0])
        except Exception as e:
            log.info("locationiq geocode failed: %s", e)
    # Free fallback: Nominatim.
    try:
        r = requests.get(
            _NOMINATIM,
            params={"q": address, "format": "jsonv2", "addressdetails": 1, "limit": 1},
            headers={"User-Agent": _UA}, timeout=_GEO_TIMEOUT)
        j = r.json()
    except Exception as e:
        log.info("geocode failed: %s", e)
        return None
    return _parse_geo(j[0]) if j else None


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


def check(entity_name=None, country_code=None, phones=None, addresses=None, emails=None):
    """Verify a counterparty's contact data — phone, address, email. Returns
    per-item verdicts + an overall (worst) verdict. Reusable by onboarding, the
    copap-ds fraud LLM, and CIR."""
    phone_results = [r for r in (check_phone(p, country_code) for p in (phones or [])) if r]
    addr_results = [r for r in (check_address(a, country_code) for a in (addresses or [])) if r]
    email_results = []
    try:
        from source_domain import verify_email
        email_results = [r for r in (verify_email(e) for e in (emails or [])) if r]
    except Exception as e:
        log.info("email verify unavailable: %s", e)
    all_v = [r["verdict"] for r in phone_results + addr_results + email_results]
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
        "emails": email_results,
    }
