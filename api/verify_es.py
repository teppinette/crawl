"""Spain — VIES shim onto source_vies."""

import source_vies

init = source_vies.init


def borme_verify(entity_name: str, cif: str = "") -> dict:
    """main.py calls this name. CIF is ES's VAT ID format."""
    return source_vies.vies_verify("ES", entity_name, cif)
