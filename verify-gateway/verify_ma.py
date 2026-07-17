"""Morocco — GLEIF-only verification.

OMPIC (Office Marocain de la Propriété Industrielle et Commerciale) has no
public REST API for company verification. GLEIF is the only reliable free
source for MA entities. Coverage limited to MA banks, listed companies, and
large corporates with LEIs (~250 entities). Smaller entities return
NOT_FOUND with explanatory note.

If an opencorporates-token secret is configured, OpenCorporates is attempted
as a secondary fallback.

Mirrors the verify_eg.py pattern.
"""

import logging

from curl_cffi import requests as cffi_requests

import source_gleif

log = logging.getLogger("verify-gateway")

_OC_URL = "https://api.opencorporates.com/v0.4/companies/search"
_OC_TOKEN = ""

_COVERAGE_NOTE = (
    "MA entity verification via GLEIF only (OMPIC has no public REST API). "
    "GLEIF covers MA banks, listed companies, large corporates with LEIs "
    "(~250 entities). Smaller MA entities are not in GLEIF; if an "
    "opencorporates-token secret is configured, OpenCorporates is attempted "
    "as a secondary fallback."
)


def init(get_secret):
    global _OC_TOKEN
    _OC_TOKEN = get_secret("opencorporates-token") or ""
    source_gleif.init(get_secret)
    if _OC_TOKEN:
        log.info("MA verify ready: GLEIF (primary) + OpenCorporates (secondary, token configured)")
    else:
        log.info("MA verify ready: GLEIF (primary). OpenCorporates not configured "
                 "(set opencorporates-token in Key Vault for broader coverage)")


def _try_oc(entity_name: str, ompic_number: str) -> dict:
    """Best-effort OpenCorporates fallback (GLEIF returned no match)."""
    if not _OC_TOKEN:
        return {}
    try:
        r = cffi_requests.get(
            _OC_URL,
            params={
                "q": ompic_number or entity_name,
                "jurisdiction_code": "ma",
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
            "legal_name": best.get("name"),
            "registration_number": best.get("company_number"),
            "status": best.get("current_status"),
            "incorporation_date": best.get("incorporation_date"),
            "jurisdiction": "ma",
            "validation_source": {
                "primary": "OpenCorporates (paid API, MA jurisdiction)",
                "primary_url": (best.get("opencorporates_url")
                                or "https://opencorporates.com/companies/ma"),
                "how_to_reproduce": (
                    f"Visit opencorporates.com → jurisdiction MA → search "
                    f"'{ompic_number or entity_name}'"
                ),
            },
            "summary": f"{best.get('name','')} — {best.get('company_number','')} — OpenCorporates (MA)",
        }
    except Exception as e:
        log.warning("OC fallback for MA failed: %s", e)
        return {}


def verify(entity_name: str = "", ompic_number: str = "") -> dict:
    """main.py calls this — GLEIF primary, OC secondary."""
    r = source_gleif.gleif_verify(
        "MA", entity_name=entity_name, reg_number=ompic_number,
    )
    if r.get("verified") or r.get("found"):
        return r
    oc = _try_oc(entity_name, ompic_number)
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
                                 f"?filter[entity.legalName]={entity_name} → no match"),
        },
    }
