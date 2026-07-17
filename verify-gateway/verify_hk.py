"""
Hong Kong verify — OC primary + ltddir.com enrichment + GLEIF fallback for listed.

Layered strategy:
  1. OpenCorporates (covers SMEs) — primary
  2. ltddir.com enrichment (when OC found something) — adds address, BR#,
     name history, last annual return date that OC HK feed doesn't expose
  3. GLEIF fallback (covers HK banks/listed/funds) — only if OC empty

UBO note: HK SCR is private by regime design. Public officers/shareholders
require paid ICRIS3EP document downloads (~HKD 22 per NAR1). This adapter
captures the FILING METADATA so callers know when the last NAR1 was filed
and can decide whether to pay for the document.
"""

import logging

import source_opencorporates
import source_gleif
import source_ltddir
import source_dnb

log = logging.getLogger("verify-gateway")

_COVERAGE_NOTE = (
    "HK entity verification: OpenCorporates primary (covers SMEs), "
    "ltddir.com enrichment (address, BR#, name history, last NAR1 date), "
    "GLEIF fallback (banks/listed/funds). For ownership details, the "
    "last NAR1 filing contains shareholders + officers — available as a "
    "paid document (~HKD 22) from ICRIS3EP; HK Significant Controllers "
    "Register is private by regime design and only available from the "
    "company itself or via court order."
)


def init(get_secret):
    source_opencorporates.init(get_secret)
    source_gleif.init(get_secret)
    source_ltddir.init(get_secret)
    source_dnb.init(get_secret)

# Known D&B profile URLs per CR# — extends as we discover them.
# D&B search is reCAPTCHA-gated, so we only enrich when caller knows the URL
# or we can find it via the discovery body's anchor refs (TODO).
_DNB_PROFILE_BY_CR = {
    "0017913": "https://www.dnb.com/business-directory/company-profiles.intex_development_company_limited.7549c291cf7374669559e6c49a445cdc.html",
}


def icris_verify(entity_name: str, cr_number: str = "") -> dict:
    """HK verify entry — main.py routes here."""
    # 1. OpenCorporates primary
    if source_opencorporates.is_available():
        oc = source_opencorporates.oc_verify(
            "HK", entity_name=entity_name, reg_number=cr_number,
            coverage_note=_COVERAGE_NOTE,
        )
        if oc.get("found"):
            # 2. Enrich with ltddir.com — adds address, BR#, name history, last NAR1
            ltddir_data = source_ltddir.ltddir_enrich(
                oc.get("legal_name") or entity_name,
                cr_number=oc.get("company_number") or cr_number,
            )
            if ltddir_data:
                # Merge: ltddir address fills the gap OC leaves empty
                oc.setdefault("headquarters", ltddir_data.get("ltddir_registered_office"))
                oc.setdefault("registered_address", ltddir_data.get("ltddir_registered_office"))
                if not oc.get("headquarters") and ltddir_data.get("ltddir_registered_office"):
                    oc["headquarters"] = ltddir_data["ltddir_registered_office"]
                # Pass through all ltddir_* extras
                oc.update(ltddir_data)
                # Annotate enrichment source list
                existing = oc.get("enrichment_source", "")
                oc["enrichment_source"] = (
                    f"{existing} + ltddir.com (HK Companies Directory mirror)"
                    if existing else "ltddir.com (HK Companies Directory mirror)"
                )

            # 2b. D&B enrichment — adds Key Principal (a named director) + industry.
            #     Only fires when we have a known DnB profile URL for the CR;
            #     D&B search is reCAPTCHA-gated and unreliable for discovery.
            dnb_url = _DNB_PROFILE_BY_CR.get(oc.get("company_number") or cr_number)
            if dnb_url:
                dnb_data = source_dnb.dnb_enrich(
                    profile_url=dnb_url,
                    entity_name=oc.get("legal_name") or entity_name,
                    country_code="HK",
                )
                if dnb_data and dnb_data.get("dnb_key_principal"):
                    oc.update(dnb_data)
                    oc["enrichment_source"] = (
                        oc["enrichment_source"] + " + D&B Business Directory"
                    )
            return oc

    # 3. GLEIF fallback (covers HK banks/listed/funds when OC empty)
    return source_gleif.gleif_verify(
        "HK", entity_name=entity_name, reg_number=cr_number,
        coverage_note=_COVERAGE_NOTE,
    )
