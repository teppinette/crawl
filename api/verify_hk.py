"""
Hong Kong company verification via GLEIF LEI Registry.

Primary source: GLEIF LEI (https://api.gleif.org/api/v1/lei-records)
  - Free public REST API, no auth required (ISO 17442 standard)
  - Covers HK companies with LEIs (banks, HKEX-listed, regulated funds,
    insurers, large corporates with derivatives reporting obligations)
  - Returns: LEI, legal name, status, address, jurisdiction, CR number
    (in registeredAs)

Note on ICRIS: The legacy ICRIS Cyber Search Centre (icris.cr.gov.hk/csci)
was migrated to a Vue-based SPA (ICRIS3EP) in 2026. Every URL on the host
now returns the same JS-only shell; server-side bot detection prevents
extracting the backend API. Most CR functions also moved behind the paid
eRegistry. GLEIF is the bank-grade alternative for the universe banks and
counterparties actually care about.

Input: entity_name (search by name) or cr_number (7-digit company number)
Returns: legal_name, lei, cr_number (from registeredAs), status, address.
"""

import logging
import re
import time

from mlx_http import mlx_get

log = logging.getLogger("verify-gateway")

_GLEIF_URL = "https://api.gleif.org/api/v1/lei-records"


def init(get_secret=None):
    log.info("HK verification ready (GLEIF LEI — ICRIS migrated to paywalled SPA)")


def icris_verify(entity_name: str, cr_number: str = "") -> dict:
    if not entity_name and not cr_number:
        return {"found": False, "error": "entity_name or cr_number required"}

    try:
        return _gleif_search(entity_name, cr_number)
    except Exception as e:
        log.error("HK GLEIF error for %s: %s", entity_name or cr_number, e)
        return {"entity_name": entity_name, "cr_number": cr_number,
                "found": False, "error": str(e)[:300]}


_SUBSIDIARY_TERMS = (
    "scheme", "fund", "trust", "sub-fund", "subfund", "retirement",
    "provident", "pension", "mandatory provident",
)


def _gleif_search(entity_name: str, cr_number: str) -> dict:
    """Search GLEIF for HK entities by CR (registeredAs) or name.

    CR-specified queries do NOT fall back to name search — user explicitly
    asked for a specific registration number, so a wrong-CR-but-name-similar
    match would be incorrect.
    """
    base_params = {
        "filter[entity.legalAddress.country]": "HK",
        "page[size]": "20",
    }
    headers = {"Accept": "application/vnd.api+json"}

    # CR-first lookup — try raw and common zero-padded variants
    if cr_number:
        clean = re.sub(r"[\s\-]", "", cr_number.strip())
        candidates = [clean]
        if clean.isdigit():
            candidates.append(clean.zfill(8))
            candidates.append(clean.lstrip("0"))
        for cand in dict.fromkeys(candidates):
            params = dict(base_params)
            params["filter[entity.registeredAs]"] = cand
            result = mlx_get(_GLEIF_URL, params=params, headers=headers,
                             timeout=15, country_code="hk")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    return _format_gleif(records, entity_name, cr_number)
        # CR specified but not found — explicit NOT_FOUND for that CR
        return _not_found(entity_name, cr_number, by_cr=True)

    # Name search — try legalName then fulltext, then re-rank
    if entity_name:
        for filter_key in ("filter[entity.legalName]", "filter[fulltext]"):
            params = dict(base_params)
            params[filter_key] = entity_name
            result = mlx_get(_GLEIF_URL, params=params, headers=headers,
                             timeout=15, country_code="hk")
            if result.get("ok") and result.get("json"):
                records = result["json"].get("data", [])
                if records:
                    ranked = _rank_records(records, entity_name)
                    return _format_gleif(ranked, entity_name, cr_number)

    return _not_found(entity_name, cr_number)


def _rank_records(records: list, query: str) -> list:
    """Re-order GLEIF records to prefer parent companies over schemes/funds/trusts.

    Score (lower = better):
      0   : query is in legal name AND name has no subsidiary/scheme markers
      10  : query is in legal name (with subsidiary markers)
      20  : query not in legal name, no subsidiary markers
      30  : query not in legal name, has subsidiary markers
    Ties broken by shorter name length (parent names are usually shorter than fund names).
    """
    q = (query or "").lower().strip()

    def score(rec):
        name = rec["attributes"]["entity"]["legalName"]["name"].lower()
        q_match = q and q in name
        has_sub = any(t in name for t in _SUBSIDIARY_TERMS)
        # User-supplied query mentions a subsidiary term — don't demote those
        query_mentions_sub = any(t in q for t in _SUBSIDIARY_TERMS) if q else False
        sub_penalty = 0 if query_mentions_sub else (1 if has_sub else 0)
        if q_match and not sub_penalty:
            return (0, len(name))
        if q_match:
            return (10, len(name))
        if not sub_penalty:
            return (20, len(name))
        return (30, len(name))

    return sorted(records, key=score)


def _not_found(entity_name: str, cr_number: str, by_cr: bool = False) -> dict:
    if by_cr:
        note = (
            f"CR number {cr_number} not found in GLEIF (Hong Kong jurisdiction). "
            "GLEIF only covers HK companies with Legal Entity Identifiers. "
            "If the CR is correct and the entity exists, it may not have an LEI. "
            "ICRIS public lookup was migrated to a paywalled SPA in 2026 (ICRIS3EP)."
        )
    else:
        note = (
            "GLEIF covers Hong Kong companies with Legal Entity Identifiers "
            "(licensed banks, HKEX-listed companies, insurers, regulated funds, "
            "large corporates with derivatives reporting). Smaller entities "
            "without LEIs are not covered. ICRIS public lookup was migrated to "
            "a paywalled SPA in 2026 (ICRIS3EP) and is no longer programmatically "
            "accessible without a Companies Registry account."
        )
    return {
        "entity_name": entity_name,
        "country_code": "HK",
        "cr_number": cr_number or None,
        "found": False,
        "status": "NOT_FOUND",
        "note": note,
        "validation_source": _gleif_source(entity_name or cr_number),
    }


def _format_gleif(records: list, query_name: str, query_cr: str) -> dict:
    top = records[0]
    attrs = top["attributes"]
    entity = attrs["entity"]
    lei = attrs["lei"]

    legal_name = entity["legalName"]["name"]
    status = entity.get("status", "UNKNOWN")
    reg_as = entity.get("registeredAs", "") or ""

    addr = entity.get("legalAddress", {}) or {}
    addr_lines = addr.get("addressLines", []) or []
    full_addr = ", ".join(filter(None, addr_lines + [
        addr.get("city", ""), addr.get("region", ""),
        addr.get("postalCode", ""), addr.get("country", "HK"),
    ]))

    legal_form = entity.get("legalForm", {}) or {}
    legal_form_str = legal_form.get("other") or legal_form.get("id") or ""

    others = []
    for rec in records[1:5]:
        e = rec["attributes"]["entity"]
        others.append({
            "name": e["legalName"]["name"],
            "lei": rec["attributes"]["lei"],
            "cr_number": e.get("registeredAs"),
            "status": e.get("status", "UNKNOWN"),
        })

    return {
        "entity_name": legal_name,
        "query_name": query_name,
        "country_code": "HK",
        "found": True,
        "lei": lei,
        "cr_number": reg_as or query_cr or None,
        "status": status,
        "legal_form": legal_form_str or None,
        "registered_address": full_addr or None,
        "jurisdiction": entity.get("jurisdiction", "HK"),
        "total_matches": len(records),
        "other_matches": others if others else None,
        "source": "GLEIF LEI Registry (ICRIS migrated to paywalled SPA)",
        "validation_source": _gleif_source(query_name or query_cr, lei),
    }


def _gleif_source(query: str, lei: str = "") -> dict:
    return {
        "registry": "GLEIF — Global Legal Entity Identifier Foundation (ISO 17442)",
        "url": "https://search.gleif.org/",
        "api": _GLEIF_URL,
        "how_to_reproduce": (
            f"GLEIF record: https://search.gleif.org/#/record/{lei}"
            if lei else
            f"GLEIF search: {_GLEIF_URL}?filter[fulltext]={query}"
            f"&filter[entity.legalAddress.country]=HK"
        ),
        "limitations": (
            "GLEIF covers HK companies with Legal Entity Identifiers (banks, "
            "HKEX-listed companies, insurers, regulated funds, large corporates). "
            "Smaller entities without LEIs are not covered."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
