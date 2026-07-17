"""Germany — VIES shim onto source_vies. See source_vies.py for the real logic."""

import source_vies

init = source_vies.init


def handelsregister_verify(entity_name: str, hrb: str = "", vat_id: str = "") -> dict:
    """main.py calls this name — preserved for backward compat."""
    if hrb and not vat_id:
        return {
            "entity_name": entity_name, "country_code": "DE",
            "hrb": hrb, "found": False, "verified": False,
            "note": ("HRB provided but VIES requires USt-IdNr. "
                     "Direct Handelsregister lookup not yet implemented."),
            "source": "VIES (EU VAT Information Exchange System)",
        }
    return source_vies.vies_verify("DE", entity_name, vat_id)
