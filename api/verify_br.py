"""
Brazil verify — runs on the generic engine.

Source: BrasilAPI (wrapping Receita Federal) — primary.
Fallback: ReceitaWS (when BrasilAPI rate-limits or 404s).

Both via Bright Data BR residential proxy.

Note: BrasilAPI/ReceitaWS don't support name search — CNPJ is required.
"""

import logging
import re
import time

from curl_cffi import requests as cffi_requests
from proxy_cfg import get_proxy, get_dc_proxy

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_BRASIL_API   = "https://brasilapi.com.br/api/cnpj/v1"
_RECEITAWS    = "https://receitaws.com.br/v1/cnpj"
_PROXY = None


def init(get_secret):
    global _PROXY
    try:
        cffi_requests.get(
            "https://lumtest.com/myip.json",
            proxy=get_proxy("br"), impersonate="chrome", timeout=10,
        )
        _PROXY = get_proxy("br")
        log.info("BR verify ready (engine) — BrasilAPI primary, ReceitaWS fallback, BR residential proxy")
    except Exception:
        _PROXY = get_dc_proxy()
        log.info("BR verify ready (engine) — fallback proxy (BR residential unavailable)")


def _fmt_cnpj(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def _try_receitaws(cnpj: str) -> dict:
    """Best-effort fallback when BrasilAPI doesn't yield data."""
    try:
        r = cffi_requests.get(
            f"{_RECEITAWS}/{cnpj}", impersonate="chrome", proxy=_PROXY, timeout=20,
        )
        if r.status_code != 200:
            return {}
        data = r.json() or {}
        if data.get("status") == "ERROR":
            return {}
        addr_parts = [
            data.get("logradouro", ""), data.get("numero", ""),
            data.get("complemento", ""), data.get("bairro", ""),
            data.get("municipio", ""), data.get("uf", ""), data.get("cep", ""),
        ]
        partners = [
            {"name": q.get("nome", ""), "role": q.get("qual", "")}
            for q in (data.get("qsa") or [])
        ]
        atv = (data.get("atividade_principal") or [{}])[0]
        return {
            "legal_name": data.get("nome", ""),
            "trade_name": data.get("fantasia", "") or None,
            "status": (data.get("situacao", "") or "").upper() or "UNKNOWN",
            "date_opened": data.get("abertura", "") or None,
            "legal_nature": data.get("natureza_juridica", "") or None,
            "registered_address": ", ".join(p for p in addr_parts if p) or None,
            "cnae_code": str(atv.get("code", "")) or None,
            "cnae_description": atv.get("text", "") or None,
            "capital_social": data.get("capital_social", "") or None,
            "partners": partners or None,
            "fallback_used": "ReceitaWS",
        }
    except Exception as e:
        log.debug("ReceitaWS fallback failed for %s: %s", cnpj, str(e)[:120])
        return {}


def _parse_br(raw: dict, entity_name: str, ids: dict) -> dict:
    cnpj = ids.get("cnpj", "")

    # If primary returned 404 or rate-limited, try fallback
    data = raw.get("json")
    status_code = raw.get("status", 200)

    if status_code == 404 or not data or not isinstance(data, dict) or not data.get("razao_social"):
        fb = _try_receitaws(cnpj)
        if fb:
            fb.update({
                "found": True,
                "business_registration_number": _fmt_cnpj(cnpj),
                "cnpj": _fmt_cnpj(cnpj),
                "is_listed": False,
                "summary": f"{fb.get('legal_name') or entity_name} — CNPJ {_fmt_cnpj(cnpj)} — {fb.get('status')}",
            })
            return fb
        if status_code == 404:
            return {"found": False, "error": "not_found_in_cnpj_registry"}
        if status_code == 429:
            return {"found": False, "error": "BrasilAPI and ReceitaWS both rate-limited"}
        return {"found": False}

    # BrasilAPI happy path
    razao = data.get("razao_social", "")
    fantasia = data.get("nome_fantasia", "")
    situacao = data.get("descricao_situacao_cadastral", "")
    addr_parts = [
        data.get("logradouro", ""), data.get("numero", ""),
        data.get("complemento", ""), data.get("bairro", ""),
        data.get("municipio", ""), data.get("uf", ""), data.get("cep", ""),
    ]
    address = ", ".join(p for p in addr_parts if p) or None
    partners = [
        {
            "name": q.get("nome_socio", ""),
            "role": q.get("qualificacao_socio", ""),
            "country": q.get("pais", "") or None,
        }
        for q in (data.get("qsa") or [])
    ]
    cnae_principal = data.get("cnae_fiscal_descricao", "") or None
    cnae_code = str(data.get("cnae_fiscal", "")) or None
    date_opened = data.get("data_inicio_atividade", "") or None
    founded_year = date_opened[:4] if date_opened and len(date_opened) >= 4 else None

    return {
        "found": True,
        "legal_name": razao or entity_name,
        "business_registration_number": _fmt_cnpj(cnpj),
        "headquarters": address,
        "founded_year": founded_year,
        "industry": cnae_principal,
        "directors": partners or None,
        "is_listed": False,
        # BR-specific extras
        "cnpj": _fmt_cnpj(cnpj),
        "trade_name": fantasia or None,
        "date_opened": date_opened,
        "legal_nature": data.get("natureza_juridica", "") or None,
        "registered_address": address,
        "cnae_code": cnae_code,
        "cnae_description": cnae_principal,
        "capital_social": data.get("capital_social", 0),
        "partners": partners or None,
        "status": (situacao or "UNKNOWN").upper(),
        "summary": (
            f"{razao or entity_name} — CNPJ {_fmt_cnpj(cnpj)} — {situacao or 'UNKNOWN'}"
        ),
    }


def _br_proxy() -> dict:
    return _PROXY


BR_CONFIG = eng.CountryConfig(
    country_code="BR",
    source_name="Receita Federal do Brasil (via BrasilAPI)",
    transport=eng.T_PROXY_API,
    primary_url=_BRASIL_API + "/{cnpj}",
    parser=_parse_br,
    timeout=20,
    proxy_provider=_br_proxy,
    how_to_reproduce_template=(
        "Visit https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/ → "
        "enter CNPJ {entity} → solve CAPTCHA → view registration"
    ),
)


def cnpj_verify(entity_name: str, cnpj: str = "") -> dict:
    """BR verify entry point — backward compat with main.py routing."""
    if not cnpj:
        return eng.run(
            BR_CONFIG, entity_name or "[no CNPJ]",
            {"cnpj": "", "_error": "CNPJ required — BrasilAPI does not support name search"},
        )
    clean = re.sub(r"[^0-9]", "", cnpj)
    if len(clean) != 14:
        return eng.run(BR_CONFIG, entity_name or cnpj, {"cnpj": clean, "_error": "CNPJ must be 14 digits"})
    return eng.run(BR_CONFIG, entity_name or _fmt_cnpj(clean), {"cnpj": clean})
