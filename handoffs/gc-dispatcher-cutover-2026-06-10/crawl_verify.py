"""
Crawl Verify Gateway client.

Routes registry lookups to crawl-verify via the Crawl Research Gateway
(POST /api/v1/verify) so GC and Crawl don't maintain parallel per-country
adapters. One server, one source of truth — see consolidation rule.

Gated by Settings.crawl_verify_allowlist — countries outside the allowlist
fall through to the existing legacy adapters in registry_dispatcher.py
(no behavior change for non-allowlisted countries).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.integrations.registries import BaseRegistryClient, RegistryLookupResult

logger = logging.getLogger(__name__)


# Status normalization Crawl → GC RegistryLookupResult.company_status vocabulary.
# GC vocabulary: active | dissolved | liquidation | struck_off | winding_up | unknown
_STATUS_MAP = {
    "ACTIVE":   "active",
    "ACTIVO":   "active",
    "REGISTERED": "active",
    "DISSOLVED": "dissolved",
    "INACTIVE": "dissolved",
    "CEASED":   "dissolved",
    "CLOSED":   "dissolved",
    "HISTORICAL": "dissolved",
    "DISSOLVING": "liquidation",
    "IN_LIQUIDATION": "liquidation",
    "IN_LIQUIDATION_VOLUNTARILY": "liquidation",
    "SUSPENDED": "winding_up",
    "REVOKED":   "struck_off",
    "NOT_FOUND": "unknown",
}

# Per-country reg_number → Crawl payload field. Crawl's /api/v1/verify accepts
# entity_name + country_code + a country-specific identifier (company_number,
# siren, ubn, cnpj, cik, ...). Map GC's generic reg_number into the right key.
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
}


class CrawlVerifyClient(BaseRegistryClient):
    """Routes registry lookups for one country through Crawl /api/v1/verify."""

    def __init__(self, country_code: str, base_url: str, api_key: str):
        self.country_code = country_code.upper().strip()
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # verify=False until Crawl moves /api/v1/verify off the self-signed
        # cert at https://20.94.45.219:8443 to the Let's Encrypt host at
        # https://crawldevvm.eastus2.cloudapp.azure.com:8443. Confirm with
        # Crawl team and flip to verify=True once Lets-Encrypt host is wired.
        self._client = httpx.AsyncClient(verify=False, timeout=30.0)

    async def lookup_entity(
        self,
        entity_name: str,
        reg_number: Optional[str] = None,
    ) -> Optional[RegistryLookupResult]:
        payload: dict = {
            "entity_name": entity_name,
            "country_code": self.country_code,
        }
        if reg_number:
            field = _REG_NUMBER_FIELD.get(self.country_code, "reg_number")
            payload[field] = reg_number

        try:
            r = await self._client.post(
                f"{self.base_url}/api/v1/verify",
                json=payload,
                headers={"X-API-Key": self.api_key},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "crawl_verify HTTP error for %s/%s: %s",
                self.country_code, entity_name, exc,
            )
            return None

        if r.status_code >= 400:
            logger.warning(
                "crawl_verify HTTP %s for %s/%s",
                r.status_code, self.country_code, entity_name,
            )
            return None

        try:
            data = r.json()
        except ValueError:
            logger.warning(
                "crawl_verify non-JSON response for %s/%s",
                self.country_code, entity_name,
            )
            return None

        if not (data.get("verified") or data.get("found")):
            return None

        return self._to_result(data)

    def _to_result(self, data: dict) -> RegistryLookupResult:
        status_raw = (data.get("status") or "").upper().strip()
        company_status = _STATUS_MAP.get(status_raw, "unknown")

        # Address — Crawl returns a flat string; GC expects a dict.
        addr_str = (
            data.get("headquarters")
            or data.get("registered_address")
            or data.get("business_address")
            or ""
        )
        registered_address = {"line_1": addr_str} if addr_str else None

        # Officers — Crawl's directors list maps 1:1 with adjustments.
        officers = []
        for d in (data.get("directors") or []):
            if not isinstance(d, dict):
                continue
            officers.append({
                "name":                 d.get("name", ""),
                "role":                 d.get("role", ""),
                "appointed_on":         d.get("appointed_on"),
                "resigned_on":          d.get("resigned_on"),
                "nationality":          d.get("nationality"),
                "country_of_residence": d.get("country") or d.get("country_of_residence"),
                "occupation":           d.get("occupation"),
            })

        # Pull a registration_number from whichever country-specific key Crawl
        # surfaced. business_registration_number is the canonical field.
        registration_number = str(
            data.get("business_registration_number")
            or data.get("company_number")
            or data.get("siren")
            or data.get("ubn")
            or data.get("cnpj")
            or data.get("cik")
            or data.get("ruc")
            or data.get("kvk_number")
            or data.get("abn")
            or ""
        )

        return RegistryLookupResult(
            registry_source=f"crawl_verify_{self.country_code.lower()}",
            country_code=self.country_code,
            registration_number=registration_number,
            company_name=data.get("legal_name") or data.get("entity_name") or "",
            company_status=company_status,
            is_active=(company_status == "active"),
            company_type=data.get("company_type") or data.get("entity_type"),
            date_of_creation=(
                data.get("incorporated_on")
                or data.get("date_opened")
                or data.get("creation_date")
                or data.get("incorporation_date")
                or data.get("establishment_date")
            ),
            date_of_dissolution=data.get("dissolved_on"),
            registered_address=registered_address,
            officers=officers,
            sic_codes=data.get("sic_codes") or [],
            industry_description=data.get("industry") or data.get("economic_activity"),
            lei=data.get("lei"),
            full_response=data,
        )

    async def close(self) -> None:
        await self._client.aclose()
