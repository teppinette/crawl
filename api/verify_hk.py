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
            return oc

    # 3. GLEIF fallback (covers HK banks/listed/funds when OC empty)
    return source_gleif.gleif_verify(
        "HK", entity_name=entity_name, reg_number=cr_number,
        coverage_note=_COVERAGE_NOTE,
    )
