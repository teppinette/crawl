"""
Peru verify — runs on the generic engine.

Source: Decolecta API (SUNAT RUC lookup).
Free tier 1K req/mo, Bearer token. Direct HTTP with PE residential proxy.
RUC required (Decolecta does not support name search).
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_API_URL = "https://api.decolecta.com/v1/sunat/ruc"
_API_TOKEN = ""
_PROXY = None

_RUC_RE = re.compile(r"^(10|20)\d{9}$")


def init(get_secret):
    global _API_TOKEN, _PROXY
    _API_TOKEN = get_secret("peru-apis-token") or ""
    try:
        from proxy_cfg import get_proxy
        _PROXY = get_proxy("pe")
    except Exception:
        _PROXY = None
    if _API_TOKEN:
        log.info("PE verify ready (engine) — Decolecta SUNAT RUC")
    else:
        log.warning("PE verify token missing — set peru-apis-token")


def _pe_proxy() -> dict:
    return _PROXY


def _parse_pe(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json")
    if not isinstance(data, dict) or raw.get("status") != 200:
        if raw.get("status") == 404:
            return {"found": False, "note": "RUC not found in SUNAT"}
        return {"found": False, "error": f"Decolecta status {raw.get('status')}"}

    razon = data.get("razon_social", "")
    if not razon:
        return {"found": False}

    ruc = data.get("numero_documento") or ids.get("ruc") or ""
    estado = data.get("estado", "")
    condicion = data.get("condicion", "")
    direccion = data.get("direccion", "")
    departamento = data.get("departamento", "")
    provincia = data.get("provincia", "")
    distrito = data.get("distrito", "")

    full_address_parts = [direccion, distrito, provincia, departamento]
    headquarters = ", ".join(p for p in full_address_parts if p and p != "-") or None

    # Status mapping
    status_map = {
        "ACTIVO": "ACTIVE", "BAJA DEFINITIVA": "DISSOLVED",
        "BAJA PROVISIONAL": "INACTIVE", "SUSPENSION TEMPORAL": "SUSPENDED",
    }
    status = status_map.get((estado or "").upper(), (estado or "UNKNOWN").upper())

    return {
        "found": True,
        "legal_name": razon,
        "business_registration_number": ruc or None,
        "headquarters": headquarters,
        "is_listed": False,
        # PE-specific extras
        "ruc": ruc or None,
        "estado": estado or None,
        "condicion": condicion or None,
        "direccion": direccion or None,
        "departamento": departamento or None,
        "provincia": provincia or None,
        "distrito": distrito or None,
        "is_retention_agent": data.get("es_agente_retencion") if "es_agente_retencion" in data else None,
        "status": status,
        "summary": (
            f"{razon} — RUC {ruc} — {estado or 'unknown'}"
            + (f" / {condicion}" if condicion and condicion != estado else "")
        ),
    }


PE_CONFIG = eng.CountryConfig(
    country_code="PE",
    source_name="SUNAT RUC (via Decolecta), Peru",
    transport=eng.T_PROXY_API,
    primary_url=_API_URL + "?numero={ruc}",
    parser=_parse_pe,
    timeout=20,
    headers={"Content-Type": "application/json"},  # Authorization added at runtime below
    proxy_provider=_pe_proxy,
    how_to_reproduce_template=(
        "Visit SUNAT consulta RUC → enter RUC {entity}"
    ),
)


def sunat_ruc_verify(entity_name: str, ruc: str = "") -> dict:
    """PE verify entry point — backward compat with main.py routing."""
    if not _API_TOKEN:
        return {
            "entity_name": entity_name, "country_code": "PE",
            "ruc": ruc or None, "found": False, "verified": False,
            "error": "Peru SUNAT API token not configured — set peru-apis-token in Key Vault",
        }
    if not ruc:
        return {
            "entity_name": entity_name, "country_code": "PE",
            "found": False, "verified": False,
            "note": "RUC required — Decolecta API does not support name search",
        }
    clean = re.sub(r"[.\s-]", "", ruc.strip())
    if not _RUC_RE.match(clean):
        return {
            "entity_name": entity_name, "country_code": "PE",
            "ruc": ruc, "found": False, "verified": False,
            "error": "RUC must be 11 digits starting with 10 or 20",
        }
    # Inject auth header for this call only — engine respects config.headers
    PE_CONFIG.headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_API_TOKEN}",
    }
    return eng.run(PE_CONFIG, entity_name or clean, {"ruc": clean})
