"""Egypt — GLEIF shim onto source_gleif + optional OpenCorporates fallback.

GLEIF is the only reliable free source for EG (GAFI doesn't expose a public
REST API). OpenCorporates partially indexes EG via GCR scrape but needs a
paid token — invoked only when GLEIF returns no match AND the token is set.
"""

import logging

from curl_cffi import requests as cffi_requests

import source_gleif

log = logging.getLogger("verify-gateway")

_OC_URL = "https://api.opencorporates.com/v0.4/companies/search"
_OC_TOKEN = ""

_COVERAGE_NOTE = (
    "EG entity verification via GLEIF only (GAFI Commercial Register has no "
    "public REST API). GLEIF covers EG banks, listed companies, large "
    "corporates with LEIs (~322 entities). Smaller EG entities are not in "
    "GLEIF; if an opencorporates-token secret is configured, OpenCorporates "
    "is attempted as a secondary fallback."
)


def init(get_secret):
    global _OC_TOKEN
    _OC_TOKEN = get_secret("opencorporates-token") or ""
    source_gleif.init(get_secret)
    if _OC_TOKEN:
        log.info("EG verify ready: GLEIF (primary) + OpenCorporates (secondary, token configured)")
    else:
        log.info("EG verify ready: GLEIF (primary). OpenCorporates not configured "
                 "(set opencorporates-token in Key Vault for broader coverage)")


def _try_oc(entity_name: str, commercial_reg: str) -> dict:
    """Best-effort OpenCorporates fallback (GLEIF returned no match)."""
    if not _OC_TOKEN:
        return {}
    try:
        r = cffi_requests.get(
            _OC_URL,
            params={
                "q": commercial_reg or entity_name,
                "jurisdiction_code": "eg",
                "api_token": _OC_TOKEN,
                "per_page": 5,
            },
            impersonate="chrome", timeout=15,
        )
        if r.status_code != 200:
            return {}
        data = r.json() or {}
        results = (data.get("results") or {}).get("companies") or []
        if not results:
            return {}
        best = (results[0] or {}).get("company") or {}
        return {
            "found": True,
            "verified": True,
            "entity_name": best.get("name") or entity_name,
            "country_code": "EG",
            "legal_name": best.get("name"),
            "business_registration_number": best.get("company_number"),
            "commercial_reg": best.get("company_number"),
            "status": (best.get("current_status") or "UNKNOWN").upper(),
            "is_listed": False,
            "source": "OpenCorporates (Egypt)",
            "validation_source": {
                "primary": "OpenCorporates (paid API, EG jurisdiction)",
                "primary_url": (best.get("opencorporates_url")
                                or "https://opencorporates.com/companies/eg"),
                "how_to_reproduce": (
                    f"Visit opencorporates.com → jurisdiction EG → search "
                    f"'{commercial_reg or entity_name}'"
                ),
            },
            "summary": f"{best.get('name','')} — {best.get('company_number','')} — OpenCorporates (EG)",
        }
    except Exception as e:
        log.debug("EG OpenCorporates fallback failed: %s", e)
        return {}


def gafi_verify(entity_name: str, commercial_reg: str = "") -> dict:
    """main.py calls this — GLEIF primary, OC secondary."""
    r = source_gleif.gleif_verify(
        "EG", entity_name=entity_name, reg_number=commercial_reg,
        coverage_note=_COVERAGE_NOTE,
    )
    if r.get("verified"):
        return r
    # GLEIF empty — try OpenCorporates if available
    oc = _try_oc(entity_name, commercial_reg)
    return oc or r
