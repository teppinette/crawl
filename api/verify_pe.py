"""
Peru SUNAT RUC verification via Decolecta API (formerly apis.net.pe).

Source: https://api.decolecta.com/v1/sunat/ruc
Free tier: 1000 requests/month, Bearer token auth.

Input: 11-digit RUC number
Returns: company name (razon social), status, condition,
         address, branch locations, retention agent flag.
"""

import logging
import re
import time

from curl_cffi import requests as cffi_requests
from proxy_cfg import get_proxy

log = logging.getLogger("verify-gateway")

_API_URL = "https://api.decolecta.com/v1/sunat/ruc"
_API_TOKEN = ""
_PROXY = None

# RUC: 11 digits starting with 10 (persona natural) or 20 (empresa)
_RUC_RE = re.compile(r"^(10|20)\d{9}$")


def init(get_secret):
    global _API_TOKEN, _PROXY
    _API_TOKEN = get_secret("peru-apis-token") or ""
    _PROXY = get_proxy("pe")
    if _API_TOKEN:
        log.info("PE SUNAT RUC ready (Decolecta API token configured)")
    else:
        log.warning("PE SUNAT RUC not configured — set peru-apis-token in Key Vault")


def sunat_ruc_verify(entity_name: str, ruc: str = "") -> dict:
    """
    Verify a Peruvian company via SUNAT RUC lookup.

    RUC: 11 digits (20XXXXXXXXX for companies, 10XXXXXXXXX for individuals).
    """
    if not ruc and not entity_name:
        return {"found": False, "error": "ruc or entity_name required"}

    if not ruc:
        return {
            "entity_name": entity_name,
            "found": False,
            "note": "RUC number is required for Peru verification. "
                    "SUNAT does not support name search via this API — only RUC lookup.",
        }

    clean = re.sub(r"[.\s-]", "", ruc.strip())
    if not _RUC_RE.match(clean):
        return {"ruc": ruc, "found": False, "error": "RUC must be 11 digits starting with 10 or 20"}

    try:
        if _API_TOKEN:
            return _try_decolecta(clean, entity_name)
        return {
            "ruc": clean, "found": False,
            "error": "Peru SUNAT API token not configured — register at decolecta.com for free token",
        }
    except Exception as e:
        log.error("PE SUNAT error for RUC %s: %s", ruc, e)
        return {"ruc": clean, "found": False, "error": str(e)[:300]}


def _try_decolecta(ruc: str, entity_name: str) -> dict:
    """Lookup via Decolecta API (formerly apis.net.pe)."""
    resp = cffi_requests.get(
        _API_URL,
        params={"numero": ruc},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_API_TOKEN}",
        },
        impersonate="chrome",
        proxy=_PROXY,
        timeout=15,
    )

    if resp.status_code == 404 or resp.status_code == 422:
        return {
            "ruc": ruc, "found": False,
            "status": "NOT_FOUND",
            "source": "SUNAT (via Decolecta), Peru",
        }
    if resp.status_code == 401:
        return {"ruc": ruc, "found": False, "error": "Decolecta API token invalid or expired"}
    if resp.status_code == 429:
        return {"ruc": ruc, "found": False, "error": "Decolecta API rate limit exceeded"}

    resp.raise_for_status()
    data = resp.json()

    if not data or data.get("message"):
        return {
            "ruc": ruc, "found": False,
            "status": "NOT_FOUND",
            "note": data.get("message", "No data returned"),
            "source": "SUNAT (via Decolecta), Peru",
        }

    return _format_result(data, ruc)


def _format_result(data: dict, ruc: str) -> dict:
    """Format Decolecta API response (snake_case fields)."""
    name = data.get("razon_social", data.get("nombre", ""))
    status = data.get("estado", "unknown")
    condition = data.get("condicion", "")

    # Address
    addr_parts = [
        data.get("direccion", ""),
        data.get("distrito", ""),
        data.get("provincia", ""),
        data.get("departamento", ""),
    ]
    address = ", ".join(p for p in addr_parts if p)

    # Branch locations
    branches = []
    for loc in (data.get("locales_anexos") or []):
        branches.append({
            "address": loc.get("direccion", ""),
            "district": loc.get("distrito", ""),
            "province": loc.get("provincia", ""),
            "department": loc.get("departamento", ""),
        })

    result = {
        "entity_name": name,
        "ruc": ruc,
        "found": True,
        "status": status.upper() if status else "UNKNOWN",
        "condition": condition.upper() if condition else None,
        "registered_address": address or None,
        "district": data.get("distrito", None),
        "province": data.get("provincia", None),
        "department": data.get("departamento", None),
        "is_retention_agent": data.get("es_agente_retencion", None),
        "is_good_taxpayer": data.get("es_buen_contribuyente", None),
        "branch_count": len(branches) if branches else 0,
        "branches": branches[:10] if branches else None,
        "source": "SUNAT (Superintendencia Nacional de Aduanas y de Administración Tributaria), Peru",
        "validation_source": {
            "registry": "SUNAT — Superintendencia Nacional de Aduanas y de Administración Tributaria, Peru",
            "url": "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/frameCriterioBusqueda.jsp",
            "record_id": ruc,
            "how_to_reproduce": (
                f"Visit e-consultaruc.sunat.gob.pe → "
                f"Enter RUC: {ruc} → Solve CAPTCHA → View registration"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    return result
