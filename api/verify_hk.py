"""
Hong Kong verify — OpenCorporates (primary, SME coverage) + GLEIF (fallback for listed).

ICRIS3EP was paywalled in 2026 — gov-direct programmatic access requires a paid
Companies Registry account ($). OpenCorporates aggregates the same gov data
(updated from CR scrapes) and covers SMEs that GLEIF misses entirely.

Strategy:
  1. OC name/CR# search — broad SME coverage
  2. If OC empty AND GLEIF might know (banks/listed/funds) → GLEIF fallback
  3. If both empty → explicit NOT_FOUND with the coverage note explaining
     the post-2026 HK gov-source paywall
"""

import logging

import source_opencorporates
import source_gleif

log = logging.getLogger("verify-gateway")

_COVERAGE_NOTE = (
    "HK entity verification: OpenCorporates primary (gov-data aggregator, "
    "covers SMEs), GLEIF fallback (covers HK banks, HKEX-listed, regulated "
    "funds, large corporates with derivatives reporting). ICRIS3EP direct "
    "would require a paid Companies Registry account (~HKD 22/search or "
    "HKD 380/mo subscription); not currently wired."
)


def init(get_secret):
    source_opencorporates.init(get_secret)
    source_gleif.init(get_secret)


def icris_verify(entity_name: str, cr_number: str = "") -> dict:
    """HK verify entry — keep the old function name for main.py backward compat."""
    # Try OpenCorporates first (covers SMEs)
    if source_opencorporates.is_available():
        oc = source_opencorporates.oc_verify(
            "HK", entity_name=entity_name, reg_number=cr_number,
            coverage_note=_COVERAGE_NOTE,
        )
        if oc.get("found"):
            return oc

    # OC empty or unavailable — try GLEIF for the listed/large subset
    gleif_result = source_gleif.gleif_verify(
        "HK", entity_name=entity_name, reg_number=cr_number,
        coverage_note=_COVERAGE_NOTE,
    )
    return gleif_result
