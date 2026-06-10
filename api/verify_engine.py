"""
Generic country verification engine.

A country adapter becomes a CountryConfig (transport, URL template, parser,
optional enrichment) plus a thin verify() entry point. The engine handles:

- transport dispatch (Multilogin browser navigate / Multilogin HTTP /
  direct API / proxied API) — picks the right tool based on what each
  gov source actually needs
- uniform response shape (legal_name, legal_name_en, ceo, headquarters,
  founded_year, industry, is_listed, validation_source, summary, ...)
- error handling + logging
- validation_source citation block with reproducible URL

This collapses each country adapter from 150-400 lines of repeated
boilerplate to a config block + a focused parser function (~50-100 lines).
"""

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import mlx_http
from curl_cffi import requests as cffi_requests

log = logging.getLogger("verify-gateway.engine")

# Transport modes
T_MLX_NAVIGATE = "mlx_navigate"   # Multilogin browser, JS-rendered pages
T_MLX_HTTP     = "mlx_http_get"   # Multilogin HTTP (proxy + impersonation, no browser)
T_DIRECT_API   = "direct_api"     # Plain HTTP, no proxy — for clean public APIs
T_PROXY_API    = "proxy_api"      # HTTP via residential/datacenter proxy


@dataclass
class CountryConfig:
    """Per-country verification config. Drives the engine."""
    country_code: str                    # ISO 3166-1 alpha-2, uppercase
    source_name: str                     # Human-readable source name for citation
    transport: str                       # one of T_* constants above
    primary_url: str                     # URL template; {q} = url-encoded entity_name, {entity} = raw, **ids
    parser: Callable[[dict, str, dict], dict]  # parser(raw, entity_name, ids) -> extracted fields dict
    method: str = "GET"                  # "GET" or "POST" — POST sends body_builder(entity_name, ids) as JSON
    body_builder: Optional[Callable[[str, dict], dict]] = None  # POST body dict
    wait_s: int = 4                      # for mlx_navigate, post-load JS wait
    timeout: int = 60
    enrichment: Optional[Callable[[str, dict, dict], dict]] = None  # enrichment(entity_name, ids, primary_extracted) -> dict_to_merge
    how_to_reproduce_template: str = "Visit {url} and search for '{entity}'"
    headers: dict = field(default_factory=dict)
    proxy_provider: Optional[Callable[[], dict]] = None  # used for T_PROXY_API


def run(config: CountryConfig, entity_name: str, ids: dict | None = None) -> dict:
    """Run a verify lookup against the given country config. Always returns a dict."""
    ids = ids or {}
    if not entity_name:
        return _empty(config, entity_name, "entity_name required")

    encoded = urllib.parse.quote(entity_name)
    try:
        primary_url = config.primary_url.format(q=encoded, entity=entity_name, **ids)
    except KeyError as e:
        return _empty(config, entity_name, f"missing template key: {e}")

    try:
        raw = _fetch(config, primary_url, entity_name, ids)
    except Exception as e:
        log.warning("%s primary fetch failed: %s", config.country_code, e)
        return _empty(config, entity_name, f"primary_unreachable: {str(e)[:160]}", primary_url)

    raw["primary_url"] = primary_url

    try:
        extracted = config.parser(raw, entity_name, ids) or {}
    except Exception as e:
        log.warning("%s parser failed: %s", config.country_code, e)
        extracted = {"found": False, "error": f"parse_error: {str(e)[:160]}"}

    if config.enrichment and extracted.get("found"):
        try:
            enrich = config.enrichment(entity_name, ids, extracted) or {}
            for k, v in enrich.items():
                if v not in (None, "", []) and extracted.get(k) in (None, "", []):
                    extracted[k] = v
                elif k in ("enrichment_source", "enrichment_url"):
                    extracted[k] = v
        except Exception as e:
            log.debug("%s enrichment failed: %s", config.country_code, e)

    return _wrap(config, entity_name, extracted, primary_url)


def _fetch(config: CountryConfig, url: str, entity_name: str = "", ids: dict | None = None) -> dict:
    """Transport-specific fetch — returns {html, body, json, status}."""
    cc_lower = config.country_code.lower()
    ids = ids or {}
    body_data = config.body_builder(entity_name, ids) if config.body_builder else None

    if config.transport == T_MLX_NAVIGATE:
        r = mlx_http.mlx_navigate(url=url, wait_s=config.wait_s, country_code=cc_lower, timeout=config.timeout)
        return {"html": r.get("html", ""), "body": r.get("body", ""), "json": None, "status": 200}

    if config.transport == T_MLX_HTTP:
        if config.method == "POST":
            r = mlx_http.mlx_post(url=url, json_body=body_data,
                                  headers=config.headers or None,
                                  country_code=cc_lower, timeout=config.timeout)
        else:
            r = mlx_http.mlx_get(url=url, country_code=cc_lower, timeout=config.timeout, headers=config.headers or None)
        body = r.get("body", "")
        return {"html": "", "body": body, "json": _try_json(body), "status": r.get("status", 200)}

    if config.transport == T_DIRECT_API:
        if config.method == "POST":
            r = cffi_requests.post(url, json=body_data, headers=config.headers or None,
                                   impersonate="chrome", timeout=config.timeout)
        else:
            r = cffi_requests.get(url, headers=config.headers or None,
                                  impersonate="chrome", timeout=config.timeout)
        return {"html": "", "body": r.text, "json": _resp_json(r), "status": r.status_code}

    if config.transport == T_PROXY_API:
        proxy = config.proxy_provider() if config.proxy_provider else None
        if config.method == "POST":
            r = cffi_requests.post(url, json=body_data, headers=config.headers or None,
                                   proxy=proxy, impersonate="chrome", timeout=config.timeout)
        else:
            r = cffi_requests.get(url, headers=config.headers or None,
                                  proxy=proxy, impersonate="chrome", timeout=config.timeout)
        return {"html": "", "body": r.text, "json": _resp_json(r), "status": r.status_code}

    raise ValueError(f"unknown transport: {config.transport}")


def _try_json(body: str) -> Optional[Any]:
    if not body:
        return None
    s = body.lstrip()
    if not (s.startswith("{") or s.startswith("[")):
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _resp_json(r) -> Optional[Any]:
    try:
        return r.json()
    except Exception:
        return None


_RESERVED = {
    "found", "verified", "summary", "error",
    "enrichment_source", "enrichment_url",
}


def _wrap(config: CountryConfig, entity_name: str, extracted: dict, primary_url: str) -> dict:
    """Build the uniform verify response from extracted fields."""
    found = bool(extracted.get("found"))

    validation_source = {
        "primary": config.source_name,
        "primary_url": primary_url,
        "how_to_reproduce": config.how_to_reproduce_template.format(url=primary_url, entity=entity_name),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if extracted.get("enrichment_source"):
        validation_source["enrichment"] = extracted["enrichment_source"]
    if extracted.get("enrichment_url"):
        validation_source["enrichment_url"] = extracted["enrichment_url"]

    # Legacy contract: response `entity_name` is the legal name from the
    # registry when found (matches the pre-engine adapter shape that the
    # gateway projection in api/main.py expects). Original input preserved as
    # `query_name` for traceability — useful when caller searched by an ID.
    legal = extracted.get("legal_name")
    canonical_name = legal if (legal and found) else entity_name

    base = {
        "entity_name": canonical_name,
        "query_name": entity_name if entity_name != canonical_name else None,
        "country_code": config.country_code,
        "found": found,
        "verified": found,
        "legal_name": legal or (entity_name if found else None),
        "legal_name_en": extracted.get("legal_name_en"),
        "ceo": extracted.get("ceo"),
        "headquarters": extracted.get("headquarters") or extracted.get("address"),
        "business_registration_number": extracted.get("business_registration_number"),
        "stock_code": extracted.get("stock_code"),
        "founded_year": extracted.get("founded_year"),
        "industry": extracted.get("industry"),
        "ownership_structure": extracted.get("ownership_structure"),
        "is_listed": extracted.get("is_listed", False) if found else False,
        "homepage": extracted.get("homepage"),
        "phone": extracted.get("phone"),
        "directors": extracted.get("directors"),
        # Honor parser's status when set — captures DISSOLVED/INACTIVE/SUSPENDED/etc.
        # Only fall back to ACTIVE/NOT_FOUND when parser didn't surface a status.
        "status": extracted.get("status") or ("ACTIVE" if found else "NOT_FOUND"),
        "source": config.source_name,
        "validation_source": validation_source,
        "summary": extracted.get("summary") or _default_summary(entity_name, extracted),
        "error": extracted.get("error"),
    }

    # Pass through any country-specific extras the parser returned
    # (e.g. CA's home_jurisdiction, JP's corp_kind, FR's siren).
    # Parser-supplied keys never overwrite the engine's known shape — they only
    # extend it. Reserved keys (found/verified/summary/error/enrichment_*) are
    # handled above and never pass through verbatim.
    for k, v in extracted.items():
        if k in base or k in _RESERVED:
            continue
        if v in (None, "", []):
            continue
        base[k] = v

    return {k: v for k, v in base.items() if v is not None}


def _default_summary(entity_name: str, extracted: dict) -> str:
    if extracted.get("found"):
        en = f" ({extracted.get('legal_name_en')})" if extracted.get("legal_name_en") else ""
        listed = " — listed" if extracted.get("is_listed") else " — private/unlisted"
        return f"'{extracted.get('legal_name') or entity_name}'{en}{listed}"
    err = extracted.get("error")
    if err:
        return f"'{entity_name}' verify error: {err}"
    return f"'{entity_name}' not found"


def _empty(config: CountryConfig, entity_name: str, error: str, primary_url: str = "") -> dict:
    return _wrap(config, entity_name, {"found": False, "error": error}, primary_url)
