"""Argentina — GLEIF shim onto source_gleif. AFIP/ARCA REST is dead, GLEIF is the bank-grade alternative."""

import source_gleif

init = source_gleif.init

_COVERAGE_NOTE = (
    "AR entity verification via GLEIF only. AFIP/ARCA constancia returns 403 "
    "from non-AR datacenter IPs and AFIP SOAP requires a digital certificate. "
    "GLEIF covers AR banks, listed companies and large corporates with LEIs; "
    "smaller AR entities are not covered."
)


def afip_verify(entity_name: str, cuit: str = "") -> dict:
    return source_gleif.gleif_verify(
        "AR", entity_name=entity_name, reg_number=cuit,
        coverage_note=_COVERAGE_NOTE,
    )
