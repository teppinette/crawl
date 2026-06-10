"""Hong Kong — GLEIF shim onto source_gleif. ICRIS migrated to paywalled SPA in 2026."""

import source_gleif

init = source_gleif.init

_COVERAGE_NOTE = (
    "HK entity verification via GLEIF only. The ICRIS Cyber Search Centre was "
    "migrated to a Vue-based paywalled SPA (ICRIS3EP) in 2026 and is no longer "
    "programmatically accessible without a Companies Registry account. GLEIF "
    "covers HK licensed banks, HKEX-listed companies, insurers, regulated "
    "funds, and large corporates with derivatives reporting obligations."
)


def icris_verify(entity_name: str, cr_number: str = "") -> dict:
    return source_gleif.gleif_verify(
        "HK", entity_name=entity_name, reg_number=cr_number,
        coverage_note=_COVERAGE_NOTE,
    )
