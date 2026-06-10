"""
Crawl /api/v1/verify shim for Onboarding scan runners.

Onboarding's per-country `run_*` runners (run_uk_companies_house,
run_australia_abr, run_bc_orgbook, run_norway_brreg, run_nz_companies,
run_gleif, ...) each call a different country's source directly. Per the
one-verify-server consolidation rule, those calls should route through
Crawl /api/v1/verify so verify logic lives in ONE place.

This helper:
  - Reads env var CRAWL_VERIFY_RUNNER_ALLOWLIST (comma-separated ISO
    codes, e.g. "GB,AU,CA,NO,NZ,GLEIF"). If a country is in the list
    AND CIR_API_KEY is set, the runner should call_crawl_verify().
  - Otherwise return None and let the legacy per-country runner body
    execute unchanged. Cutover is per-country, env-var gated, rollback
    is a redeploy with the country removed from the allowlist.

Reuses existing CIR_API_URL + CIR_API_KEY env vars (already set in
Onboarding for the verify-job runners further down runners.py — no new
secrets needed).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Optional

import requests

from .catalog import SourceResult

logger = logging.getLogger(__name__)

_VERIFY_API_URL = os.environ.get("CIR_API_URL", "http://20.94.45.219:8400")
_VERIFY_API_KEY = os.environ.get("CIR_API_KEY", "")

# Per-country reg-number → Crawl payload field. Crawl's /api/v1/verify accepts
# a country-specific identifier alongside entity_name + country_code.
_REG_NUMBER_FIELD = {
    "GB": "company_number",
    "CA": "business_number",
    "FR": "siren",
    "TW": "ubn",
    "BR": "cnpj",
    "US": "cik",
    "IL": "company_number",
    "PE": "ruc",
    "KR": "corp_code",
    "AU": "abn",
    "NO": "org_number",
    "NZ": "company_number",
}


def crawl_verify_enabled_for(country_code: str) -> bool:
    """Returns True if this country should route through Crawl /api/v1/verify.

    Empty allowlist or missing API key => returns False, legacy runner runs.
    """
    cc = (country_code or "").upper().strip()
    if not cc or not _VERIFY_API_KEY:
        return False
    allowlist = {
        x.strip().upper()
        for x in (os.environ.get("CRAWL_VERIFY_RUNNER_ALLOWLIST") or "").split(",")
        if x.strip()
    }
    return cc in allowlist


def call_crawl_verify(country_code: str, ctx) -> SourceResult:
    """Call Crawl /api/v1/verify and convert the response into a SourceResult.

    Designed to be called at the TOP of a per-country runner like so:

        def run_uk_companies_house(ctx: EntityContext) -> SourceResult:
            if crawl_verify_enabled_for("GB"):
                return call_crawl_verify("GB", ctx)
            # ... legacy body unchanged ...
    """
    cc = country_code.upper().strip()
    payload: dict = {
        "entity_name": (ctx.legal_name or "").strip(),
        "country_code": cc,
    }
    # Map reg_number into the country-specific input field
    if getattr(ctx, "reg_number", None):
        field = _REG_NUMBER_FIELD.get(cc, "reg_number")
        payload[field] = ctx.reg_number
    # GB also accepts a direct number via ctx.reg_number above; LEI lookups
    # go through the GLEIF allowlist key (handled separately in run_gleif).

    try:
        r = requests.post(
            f"{_VERIFY_API_URL}/api/v1/verify",
            headers={
                "X-API-Key": _VERIFY_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
            verify=False,  # crawldevvm uses self-signed cert today
        )
    except Exception as exc:
        logger.warning("crawl_verify call failed for %s: %s", cc, exc)
        return SourceResult(
            status="failed",
            error_code="CRAWL_VERIFY_UNREACHABLE",
            error_message=f"crawl_verify network error: {str(exc)[:200]}",
        )

    if r.status_code >= 400:
        return SourceResult(
            status="failed",
            error_code=f"CRAWL_VERIFY_HTTP_{r.status_code}",
            error_message=(r.text or "")[:300],
        )

    try:
        data = r.json()
    except ValueError:
        return SourceResult(
            status="failed",
            error_code="CRAWL_VERIFY_NON_JSON",
            error_message="non-JSON response from crawl_verify",
        )

    if not (data.get("verified") or data.get("found")):
        return SourceResult(
            status="empty",
            summary=(data.get("summary")
                     or f"no match in crawl_verify for {ctx.legal_name}"),
            raw_payload=data,
        )

    legal_name = data.get("legal_name") or data.get("entity_name") or ctx.legal_name
    reg_no = (data.get("business_registration_number")
              or data.get("company_number")
              or data.get("siren")
              or data.get("ubn")
              or data.get("cnpj")
              or data.get("cik")
              or data.get("ruc")
              or data.get("abn")
              or data.get("kvk_number")
              or "")
    status_str = data.get("status") or "UNKNOWN"

    summary = (
        data.get("summary")
        or f"{legal_name}"
           + (f" ({reg_no})" if reg_no else "")
           + f" — {status_str}"
    )

    return SourceResult(
        status="ok",
        found_data=True,
        summary=summary,
        raw_payload=data,
    )
