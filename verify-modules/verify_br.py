"""
Brazil CNPJ verification via BrasilAPI.

Endpoint: https://brasilapi.com.br/api/cnpj/v1/{cnpj}
Free, no auth, no CAPTCHA. Wraps Receita Federal data.

Returns: razao_social, fantasia, situacao_cadastral, endereco,
         cnae, capital_social, quadro_societario (partners).

Also tries ReceitaWS as fallback: https://receitaws.com.br/v1/cnpj/{cnpj}
"""

import logging
import re
import time

from curl_cffi import requests as cffi_requests

log = logging.getLogger("verify-gateway")

_BRASIL_API = "https://brasilapi.com.br/api/cnpj/v1"
_RECEITAWS_API = "https://receitaws.com.br/v1/cnpj"


def init(get_secret):
    """No secrets needed — BrasilAPI is free and open."""
    log.info("BR CNPJ ready (BrasilAPI + ReceitaWS fallback, no auth)")


def cnpj_verify(entity_name: str, cnpj: str = "") -> dict:
    """
    Verify a Brazilian company via CNPJ lookup.

    If cnpj is provided, does direct lookup.
    CNPJ format: 14 digits (e.g. 00.000.000/0001-00 or 00000000000100).
    """
    if not cnpj and not entity_name:
        return {"found": False, "error": "cnpj or entity_name required"}

    if not cnpj:
        return {
            "entity_name": entity_name,
            "found": False,
            "note": "CNPJ number is required for Brazil verification. "
                    "BrasilAPI does not support name search — only CNPJ lookup.",
        }

    # Clean CNPJ — digits only
    clean = re.sub(r"[^0-9]", "", cnpj)
    if len(clean) != 14:
        return {"cnpj": cnpj, "found": False, "error": "CNPJ must be 14 digits"}

    # Try BrasilAPI first
    result = _try_brasilapi(clean, entity_name)
    if result.get("found"):
        return result

    # Fallback to ReceitaWS
    log.info("BR BrasilAPI miss, trying ReceitaWS for CNPJ %s", clean[:8])
    return _try_receitaws(clean, entity_name)


def _try_brasilapi(cnpj: str, entity_name: str) -> dict:
    """Lookup via BrasilAPI."""
    try:
        resp = cffi_requests.get(
            f"{_BRASIL_API}/{cnpj}",
            impersonate="chrome",
            timeout=20,
        )

        if resp.status_code == 404:
            return {"cnpj": cnpj, "found": False, "status": "NOT_FOUND"}

        if resp.status_code == 429:
            log.warning("BR BrasilAPI rate limited")
            return {"cnpj": cnpj, "found": False, "status": "RATE_LIMITED"}

        resp.raise_for_status()
        data = resp.json()
        return _format_brasilapi(data, cnpj)

    except Exception as e:
        log.error("BR BrasilAPI error: %s", e)
        return {"cnpj": cnpj, "found": False, "error": f"BrasilAPI: {str(e)[:200]}"}


def _try_receitaws(cnpj: str, entity_name: str) -> dict:
    """Fallback lookup via ReceitaWS."""
    try:
        resp = cffi_requests.get(
            f"{_RECEITAWS_API}/{cnpj}",
            impersonate="chrome",
            timeout=20,
        )

        if resp.status_code == 429:
            return {
                "cnpj": cnpj, "found": False,
                "note": "Both BrasilAPI and ReceitaWS rate-limited. Try again in 60s.",
            }

        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "ERROR":
            return {"cnpj": cnpj, "found": False, "status": "NOT_FOUND",
                    "source": "ReceitaWS (Receita Federal)"}

        return _format_receitaws(data, cnpj)

    except Exception as e:
        log.error("BR ReceitaWS error: %s", e)
        return {"cnpj": cnpj, "found": False, "error": f"ReceitaWS: {str(e)[:200]}"}


def _format_cnpj(cnpj: str) -> str:
    """Format 14-digit CNPJ as XX.XXX.XXX/XXXX-XX."""
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def _format_brasilapi(data: dict, cnpj: str) -> dict:
    """Format BrasilAPI response."""
    formatted_cnpj = _format_cnpj(cnpj)
    razao = data.get("razao_social", "")
    fantasia = data.get("nome_fantasia", "")
    situacao = data.get("descricao_situacao_cadastral", "")

    # Build address
    addr_parts = [
        data.get("logradouro", ""),
        data.get("numero", ""),
        data.get("complemento", ""),
        data.get("bairro", ""),
        data.get("municipio", ""),
        data.get("uf", ""),
        data.get("cep", ""),
    ]
    address = ", ".join(p for p in addr_parts if p)

    # Partners / QSA
    partners = []
    for q in data.get("qsa", []):
        partners.append({
            "name": q.get("nome_socio", ""),
            "role": q.get("qualificacao_socio", ""),
            "country": q.get("pais", ""),
        })

    # CNAE
    cnae_principal = data.get("cnae_fiscal_descricao", "")
    cnae_code = str(data.get("cnae_fiscal", ""))

    return {
        "cnpj": formatted_cnpj,
        "entity_name": razao,
        "trade_name": fantasia,
        "found": True,
        "status": situacao.upper() if situacao else "UNKNOWN",
        "date_opened": data.get("data_inicio_atividade", ""),
        "legal_nature": data.get("natureza_juridica", ""),
        "registered_address": address,
        "cnae_code": cnae_code,
        "cnae_description": cnae_principal,
        "capital_social": data.get("capital_social", 0),
        "partners": partners,
        "source": "Receita Federal do Brasil (via BrasilAPI)",
        "validation_source": {
            "registry": "Receita Federal do Brasil — Cadastro Nacional da Pessoa Jurídica (CNPJ)",
            "url": f"https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/cnpjreva_solicitacao.asp",
            "record_id": formatted_cnpj,
            "how_to_reproduce": (
                f"Visit https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/ → "
                f"Enter CNPJ: {formatted_cnpj} → Solve CAPTCHA → View registration"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }


def _format_receitaws(data: dict, cnpj: str) -> dict:
    """Format ReceitaWS response (similar structure but different field names)."""
    formatted_cnpj = _format_cnpj(cnpj)
    situacao = data.get("situacao", "")

    addr_parts = [
        data.get("logradouro", ""),
        data.get("numero", ""),
        data.get("complemento", ""),
        data.get("bairro", ""),
        data.get("municipio", ""),
        data.get("uf", ""),
        data.get("cep", ""),
    ]
    address = ", ".join(p for p in addr_parts if p)

    partners = []
    for q in data.get("qsa", []):
        partners.append({
            "name": q.get("nome", ""),
            "role": q.get("qual", ""),
        })

    return {
        "cnpj": formatted_cnpj,
        "entity_name": data.get("nome", ""),
        "trade_name": data.get("fantasia", ""),
        "found": True,
        "status": situacao.upper() if situacao else "UNKNOWN",
        "date_opened": data.get("abertura", ""),
        "legal_nature": data.get("natureza_juridica", ""),
        "registered_address": address,
        "cnae_code": str(data.get("atividade_principal", [{}])[0].get("code", "")),
        "cnae_description": data.get("atividade_principal", [{}])[0].get("text", ""),
        "capital_social": data.get("capital_social", ""),
        "partners": partners,
        "source": "Receita Federal do Brasil (via ReceitaWS)",
        "validation_source": {
            "registry": "Receita Federal do Brasil — CNPJ",
            "url": "https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/cnpjreva_solicitacao.asp",
            "record_id": formatted_cnpj,
            "how_to_reproduce": (
                f"Visit Receita Federal CNPJ portal → "
                f"Enter CNPJ: {formatted_cnpj} → Solve CAPTCHA → View registration"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
