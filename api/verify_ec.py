"""
Ecuador company verification via SRI (Servicio de Rentas Internas).

Source: https://srienlinea.sri.gob.ec/sri-en-linea/SriRucWeb/ConsultaRuc/
RUC lookup — 13 digits. Free, no auth.

Input: entity_name + ruc (13 digits)
Returns: legal_name, ruc, status, economic_activity, address
"""

import logging
import re
import time

from mlx_http import mlx_post, mlx_get

log = logging.getLogger("verify-gateway")

_SRI_URL = "https://srienlinea.sri.gob.ec/sri-catastro-sujeto-servicio-internet/rest/ConsolidadoContribuyente/obtenerPorNumerosRuc"
_SUPERCIAS_URL = "https://appscvs2.supercias.gob.ec/portaldeinformacion/consul_cia_param"
_RUC_RE = re.compile(r"^\d{13}$")


def init(get_secret=None):
    log.info("EC SRI/Supercias ready (Ecuador tax + company registry)")


def supercias_verify(entity_name: str, ruc: str = "") -> dict:
    if not entity_name and not ruc:
        return {"found": False, "error": "entity_name or ruc required"}

    if ruc:
        clean = re.sub(r"[\s\-]", "", ruc.strip())
        if not _RUC_RE.match(clean):
            return {"ruc": ruc, "found": False, "error": "RUC must be 13 digits"}
        try:
            return _lookup_sri(clean, entity_name)
        except Exception as e:
            log.warning("EC SRI lookup failed for %s, trying Supercias: %s", ruc, e)

    # Try Supercias name search
    try:
        return _search_supercias(entity_name, ruc)
    except Exception as e:
        log.error("EC Supercias error for %s: %s", entity_name, e)
        return {"entity_name": entity_name, "found": False, "error": str(e)[:300]}


def _lookup_sri(ruc: str, entity_name: str) -> dict:
    """Lookup by RUC via SRI tax authority."""
    result = mlx_post(
        _SRI_URL,
        json_body=[ruc],
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=60, country_code="ec",
    )

    if result.get("status_code") == 404:
        return {
            "entity_name": entity_name, "ruc": ruc,
            "country_code": "EC",
            "found": False, "status": "NOT_FOUND",
            "validation_source": _source(ruc),
        }

    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
    data = result.get("json") or {}

    if not data or (isinstance(data, list) and len(data) == 0):
        return {
            "entity_name": entity_name, "ruc": ruc,
            "country_code": "EC",
            "found": False, "status": "NOT_FOUND",
            "validation_source": _source(ruc),
        }

    record = data[0] if isinstance(data, list) else data

    name = record.get("nombreComercial") or record.get("razonSocial", "")
    status = record.get("estadoContribuyente", "")
    obligation = record.get("obligado", "")

    activity = record.get("actividadEconomicaPrincipal", "")
    addr_parts = [
        record.get("direccionCorta", ""),
        record.get("canton", ""),
        record.get("provincia", ""),
    ]
    address = ", ".join(p for p in addr_parts if p)

    return {
        "entity_name": name,
        "query_name": entity_name,
        "country_code": "EC",
        "found": True,
        "ruc": ruc,
        "razon_social": record.get("razonSocial", ""),
        "trade_name": record.get("nombreComercial", ""),
        "status": status.upper() if status else "UNKNOWN",
        "taxpayer_type": record.get("tipoContribuyente", ""),
        "economic_activity": activity or None,
        "registered_address": address or None,
        "province": record.get("provincia", ""),
        "canton": record.get("canton", ""),
        "forced_accounting": obligation or None,
        "source": "SRI (Servicio de Rentas Internas), Ecuador",
        "validation_source": _source(ruc),
    }


def _search_supercias(entity_name: str, ruc: str = "") -> dict:
    """Search Supercias company registry by name."""
    result = mlx_get(
        _SUPERCIAS_URL,
        params={"nombre": entity_name},
        headers={"Accept": "application/json"},
        timeout=60, country_code="ec",
    )

    if result.get("status_code") in (404, 204):
        return {
            "entity_name": entity_name, "ruc": ruc or None,
            "country_code": "EC",
            "found": False, "status": "NOT_FOUND",
            "validation_source": _source(entity_name),
        }

    if not result.get("ok"):
        raise RuntimeError(f"HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")

    data = result.get("json")
    if data is None:
        return {
            "entity_name": entity_name,
            "country_code": "EC",
            "found": False, "status": "NOT_FOUND",
            "note": "Supercias returned non-JSON response",
            "validation_source": _source(entity_name),
        }

    if not data:
        return {
            "entity_name": entity_name,
            "country_code": "EC",
            "found": False, "status": "NOT_FOUND",
            "validation_source": _source(entity_name),
        }

    records = data if isinstance(data, list) else [data]
    best = records[0]

    others = []
    for r in records[1:5]:
        others.append({
            "name": r.get("nombreCompania", r.get("razonSocial", "")),
            "ruc": r.get("expediente", r.get("ruc", "")),
            "status": r.get("situacionLegal", ""),
        })

    name = best.get("nombreCompania", best.get("razonSocial", ""))
    exp = best.get("expediente", best.get("ruc", ""))

    return {
        "entity_name": name,
        "query_name": entity_name,
        "country_code": "EC",
        "found": True,
        "ruc": exp or ruc or None,
        "status": best.get("situacionLegal", "UNKNOWN").upper(),
        "legal_form": best.get("tipoCompania", ""),
        "registered_address": best.get("direccion", ""),
        "province": best.get("provincia", ""),
        "city": best.get("ciudad", ""),
        "total_matches": len(records),
        "other_matches": others or None,
        "source": "Superintendencia de Compañías, Ecuador",
        "validation_source": _source(entity_name),
    }


def _source(query: str) -> dict:
    return {
        "registry": "SRI / Superintendencia de Compañías, Ecuador",
        "url": "https://srienlinea.sri.gob.ec/",
        "how_to_reproduce": f"Visit srienlinea.sri.gob.ec → Consulta RUC: {query}",
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
