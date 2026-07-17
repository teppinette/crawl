"""Italy — VIES shim onto source_vies."""

import source_vies

init = source_vies.init


def registroimprese_verify(entity_name: str, partita_iva: str = "") -> dict:
    """main.py calls this name. Partita IVA is IT's VAT ID format."""
    return source_vies.vies_verify("IT", entity_name, partita_iva)
