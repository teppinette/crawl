"""Argentina — GLEIF primary + OpenCorporates secondary.

AFIP/ARCA constancia returns 403 from non-AR datacenter IPs and AFIP SOAP
requires a digital certificate, so direct gov-registry access isn't viable
from crawl-verify. GLEIF covers AR banks, listed companies and large
corporates with LEIs; smaller AR entities (industrial SMEs etc.) are not
in GLEIF — those resolve via OpenCorporates AR jurisdiction when its
token is configured.
"""

import logging

from curl_cffi import requests as cffi_requests

import source_gleif

log = logging.getLogger("verify-gateway")

_OC_URL = "https://api.opencorporates.com/v0.4/companies/search"
_OC_TOKEN = ""

_COVERAGE_NOTE = (
    "AR entity verification via GLEIF (primary) + OpenCorporates (secondary). "
    "AFIP/ARCA REST and SOAP not callable from non-AR + non-cert origins. "
    "GLEIF covers banks/listed/large corporates with LEIs; OC fills the SME tail."
)


def init(get_secret):
    global _OC_TOKEN
    _OC_TOKEN = get_secret("opencorporates-token") or ""
    source_gleif.init(get_secret)
    if _OC_TOKEN:
        log.info("AR verify ready: GLEIF (primary) + OpenCorporates (secondary, token configured)")
    else:
        log.info("AR verify ready: GLEIF (primary). OpenCorporates not configured "
                 "(set opencorporates-token in Key Vault for SME coverage)")


def _try_oc(entity_name: str, cuit: str) -> dict:
    if not _OC_TOKEN:
        return {}
    try:
        r = cffi_requests.get(
            _OC_URL,
            params={
                "q": cuit or entity_name,
                "jurisdiction_code": "ar",
                "api_token": _OC_TOKEN,
                "per_page": 5,
            },
            timeout=20, impersonate="chrome120",
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        results = (((data.get("results") or {}).get("companies")) or [])
        if not results:
            return {}
        best = (results[0] or {}).get("company") or {}
        return {
            "found": True,
            "verified": True,
            "legal_name": best.get("name"),
            "entity_name": best.get("name"),
            "cuit": best.get("company_number"),
            "registration_number": best.get("company_number"),
            "status": best.get("current_status"),
            "incorporation_date": best.get("incorporation_date"),
            "registered_address": (best.get("registered_address_in_full")
                                   or (best.get("registered_address") or {}).get("locality")),
            "company_type": best.get("company_type"),
            "jurisdiction": "AR",
            "validation_source": {
                "primary": "OpenCorporates (paid API, AR jurisdiction)",
                "primary_url": (best.get("opencorporates_url")
                                or "https://opencorporates.com/companies/ar"),
                "confidence": "medium",
                "tier": "COMMERCIAL_AGGREGATOR",
                "how_to_reproduce": (f"Visit opencorporates.com → jurisdiction AR → "
                                     f"search '{cuit or entity_name}'"),
            },
            "summary": f"{best.get('name','')} — CUIT {best.get('company_number','')} — OpenCorporates (AR)",
        }
    except Exception as e:
        log.warning("OC fallback for AR failed: %s", e)
        return {}


def afip_verify(entity_name: str, cuit: str = "") -> dict:
    """main.py calls this — GLEIF primary, OC secondary."""
    r = source_gleif.gleif_verify(
        "AR", entity_name=entity_name, reg_number=cuit,
        coverage_note=_COVERAGE_NOTE,
    )
    if r.get("verified") or r.get("found"):
        return r
    oc = _try_oc(entity_name, cuit)
    if oc:
        return oc
    return {
        "found": False,
        "verified": False,
        "note": _COVERAGE_NOTE,
        "validation_source": {
            "primary": "GLEIF (api.gleif.org)",
            "primary_url": "https://api.gleif.org/api/v1/lei-records",
            "how_to_reproduce": (f"GET https://api.gleif.org/api/v1/lei-records"
                                 f"?filter[entity.legalName]={entity_name} → no match; "
                                 f"OpenCorporates AR also returned no match"),
        },
    }
