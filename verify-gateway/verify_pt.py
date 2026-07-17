"""Portugal — VIES shim onto source_vies."""

import source_vies

init = source_vies.init


def mj_verify(entity_name: str, nipc: str = "") -> dict:
    """main.py calls this name. NIPC is PT's VAT ID format."""
    return source_vies.vies_verify("PT", entity_name, nipc)
