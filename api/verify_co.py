"""Colombia — GLEIF shim onto source_gleif. RUES API needs Confecámaras auth."""

import source_gleif

init = source_gleif.init

_COVERAGE_NOTE = (
    "CO entity verification via GLEIF only. RUES (ruesapi.rues.org.co) returns "
    "HTTP 401 without a Confecámaras-issued token, which is restricted to "
    "Colombian chambers of commerce. GLEIF covers CO banks, listed firms, "
    "and large corporates with LEIs."
)


def rues_verify(entity_name: str, nit: str = "") -> dict:
    return source_gleif.gleif_verify(
        "CO", entity_name=entity_name, reg_number=nit,
        coverage_note=_COVERAGE_NOTE,
    )
