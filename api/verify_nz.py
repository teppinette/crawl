"""New Zealand — GLEIF shim onto source_gleif. NZ Companies Office public search migrated to a JS-only SPA in 2026."""

import source_gleif

init = source_gleif.init

_COVERAGE_NOTE = (
    "NZ entity verification via GLEIF only. The NZ Companies Office public "
    "search (app.companiesoffice.govt.nz) was migrated to a Vue/JS SPA at "
    "companies-register.companiesoffice.govt.nz and no longer returns JSON "
    "to direct queries. The NZBN service API (api.business.govt.nz) requires "
    "a subscription key. GLEIF covers NZ banks, listed companies, and large "
    "co-operatives (Fonterra, Auckland Airport, Spark, etc.) with LEIs."
)


def companies_office_verify(entity_name: str, nzbn: str = "") -> dict:
    return source_gleif.gleif_verify(
        "NZ", entity_name=entity_name, reg_number=nzbn,
        coverage_note=_COVERAGE_NOTE,
    )
