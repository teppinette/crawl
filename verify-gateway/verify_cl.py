"""Chile — GLEIF shim onto source_gleif. SII RUT public lookup deprecated (HTTP 500)."""

import source_gleif

init = source_gleif.init

_COVERAGE_NOTE = (
    "CL entity verification via GLEIF only. The legacy SII RUT public lookup "
    "was deprecated in 2024 and now returns HTTP 500; modern SII endpoints "
    "require Clave Tributaria auth. GLEIF covers CL banks, AFP, listed firms "
    "and large corporates with LEIs."
)


def sii_rut_verify(entity_name: str, rut: str = "") -> dict:
    return source_gleif.gleif_verify(
        "CL", entity_name=entity_name, reg_number=rut,
        coverage_note=_COVERAGE_NOTE,
    )
