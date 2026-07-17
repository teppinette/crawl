"""
Ecuador verify — runs on the generic engine.

SRI (Servicio de Rentas Internas) primary — RUC-based lookup, POST endpoint.
Supercias (Superintendencia de Compañías) fallback — name-based search.

Two configs sharing the country code, called sequentially by the entry function.
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_SRI_URL = (
    "https://srienlinea.sri.gob.ec/sri-catastro-sujeto-servicio-internet/"
    "rest/ConsolidadoContribuyente/obtenerPorNumerosRuc"
)
_SUPERCIAS_URL = "https://appscvs2.supercias.gob.ec/portaldeinformacion/consul_cia_param"
_RUC_RE = re.compile(r"^\d{13}$")


def init(get_secret=None):
    log.info("EC verify ready (engine) — SRI primary (RUC), Supercias fallback (name)")


def _sri_body(entity_name: str, ids: dict) -> list:
    return [ids["ruc"]]


def _parse_sri(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json")
    if not data or (isinstance(data, list) and not data):
        return {"found": False}
    record = data[0] if isinstance(data, list) else data
    if not record.get("razonSocial") and not record.get("nombreComercial"):
        return {"found": False}

    name = record.get("nombreComercial") or record.get("razonSocial", "")
    status_raw = (record.get("estadoContribuyente", "") or "").upper()
    activity = record.get("actividadEconomicaPrincipal", "")
    addr = ", ".join(
        p for p in (
            record.get("direccionCorta", ""),
            record.get("canton", ""),
            record.get("provincia", ""),
        ) if p
    ) or None

    return {
        "found": True,
        "legal_name": name,
        "business_registration_number": ids.get("ruc"),
        "headquarters": addr,
        "industry": activity or None,
        "is_listed": False,
        # EC-specific extras
        "ruc": ids.get("ruc"),
        "razon_social": record.get("razonSocial", ""),
        "trade_name": record.get("nombreComercial", "") or None,
        "taxpayer_type": record.get("tipoContribuyente", "") or None,
        "economic_activity": activity or None,
        "province": record.get("provincia", "") or None,
        "canton": record.get("canton", "") or None,
        "forced_accounting": record.get("obligado", "") or None,
        "status": status_raw or "UNKNOWN",
        "summary": f"{name} — RUC {ids.get('ruc')} — {status_raw or 'unknown'}",
    }


def _parse_supercias(raw: dict, entity_name: str, ids: dict) -> dict:
    data = raw.get("json")
    if data is None:
        return {"found": False, "note": "Supercias returned non-JSON"}
    if not data:
        return {"found": False}
    records = data if isinstance(data, list) else [data]
    best = records[0]
    name = best.get("nombreCompania", best.get("razonSocial", ""))
    exp = best.get("expediente", best.get("ruc", ""))
    others = [
        {
            "name": r.get("nombreCompania", r.get("razonSocial", "")),
            "ruc": r.get("expediente", r.get("ruc", "")),
            "status": r.get("situacionLegal", ""),
        }
        for r in records[1:5]
    ]
    return {
        "found": True,
        "legal_name": name,
        "business_registration_number": exp or None,
        "headquarters": best.get("direccion", "") or None,
        "is_listed": False,
        "ruc": exp or None,
        "legal_form": best.get("tipoCompania", "") or None,
        "province": best.get("provincia", "") or None,
        "city": best.get("ciudad", "") or None,
        "total_matches": len(records),
        "other_matches": others or None,
        "status": (best.get("situacionLegal", "") or "UNKNOWN").upper(),
        "summary": (
            f"{name} — {exp or 'no-ruc'} — "
            f"{(best.get('situacionLegal') or 'UNKNOWN').upper()}"
        ),
    }


SRI_CONFIG = eng.CountryConfig(
    country_code="EC",
    source_name="SRI (Servicio de Rentas Internas), Ecuador",
    transport=eng.T_MLX_HTTP,
    method="POST",
    body_builder=_sri_body,
    primary_url=_SRI_URL,
    parser=_parse_sri,
    timeout=60,
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://srienlinea.sri.gob.ec/ → Consulta RUC: {entity}"
    ),
)

SUPERCIAS_CONFIG = eng.CountryConfig(
    country_code="EC",
    source_name="Superintendencia de Compañías, Ecuador",
    transport=eng.T_MLX_HTTP,
    primary_url=_SUPERCIAS_URL + "?nombre={q}",
    parser=_parse_supercias,
    timeout=60,
    headers={"Accept": "application/json"},
    how_to_reproduce_template=(
        "Visit https://appscvs2.supercias.gob.ec → search '{entity}'"
    ),
)


def supercias_verify(entity_name: str, ruc: str = "") -> dict:
    """EC verify entry point — backward compat with main.py routing."""
    if ruc:
        clean = re.sub(r"[\s\-]", "", ruc.strip())
        if not _RUC_RE.match(clean):
            return {
                "entity_name": entity_name, "country_code": "EC",
                "ruc": ruc, "found": False, "verified": False,
                "error": "RUC must be 13 digits",
            }
        r = eng.run(SRI_CONFIG, entity_name or clean, {"ruc": clean})
        if r.get("verified"):
            return r
        # SRI didn't find it — fall through to Supercias name search
        if entity_name:
            return eng.run(SUPERCIAS_CONFIG, entity_name, {"ruc": clean})
        return r
    if entity_name:
        return eng.run(SUPERCIAS_CONFIG, entity_name, {})
    return {
        "entity_name": entity_name, "country_code": "EC",
        "found": False, "verified": False,
        "error": "entity_name or ruc required",
    }
