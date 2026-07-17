"""Belgium — VIES shim onto source_vies."""

import source_vies

init = source_vies.init


def kbo_verify(entity_name: str, cbe_number: str = "") -> dict:
    """main.py calls this name. CBE number is BE's VAT ID format."""
    return source_vies.vies_verify("BE", entity_name, cbe_number)
