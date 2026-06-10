# GC RegistryDispatcher — cutover to Crawl Verify Gateway

**Generated:** 2026-06-10 by Crawl team
**Target repo:** `globalcompliance` (GC App)
**Owner:** GC App owner (please review + merge per your process)

## Why

The [[one-verify-server]] consolidation rule says all registry lookups should
go through `crawl-verify` instead of being maintained in two parallel apps.
This patch routes a per-country allowlist of `lookup_sync()` calls to the
Crawl Research Gateway at `POST /api/v1/verify` and leaves everything else
on GC's existing adapters.

**Why per-country allowlist (not big-bang):** the cutover trigger per
country is "parity-passed" — we've live-verified the Crawl response against
a known entity. Today's allowlist (9 countries) covers the entities Crawl
has confirmed:

| Country | Crawl verified against | Crawl notes |
|---|---|---|
| KR | Lotte INEOS Chemical (private JV, missing from DART by design) | Naver + Multilogin → richer than GC's 23-line SerpAPI adapter |
| CA | Shopify Inc (BN 847871746, Ontario) | BC OrgBook direct API |
| GB | Barclays Bank PLC (01026167) + Tesco PLC (00445790) via name search | Companies House public scrape |
| FR | LVMH (SIREN 775670417) — 10 directors incl. Arnault family | api.gouv.fr (DINUM/INSEE) |
| TW | TSMC (UBN 22099131, CEO 魏哲家) | MOEA GCIS Open Data API |
| BR | Petrobras (CNPJ 33000167000101) | BrasilAPI + ReceitaWS fallback |
| US | Apple (CIK 0000320193, AAPL/Nasdaq) + Tesla via name search | SEC EDGAR |
| IL | Teva Tech Chemicals (#510178676, status `בפרוק מרצון`) | data.gov.il CKAN — captures Hebrew status (DD signal GC would miss) |
| PE | Petroperu (RUC 20100128218, ACTIVO/HABIDO) | Decolecta SUNAT |

All 9 above were live-verified end-to-end through the gateway between
2026-06-10 11:00–16:00 UTC.

## What changes

1. **NEW** `app/integrations/registries/crawl_verify.py` — `CrawlVerifyClient`
   that POSTs to `/api/v1/verify` and maps the response to GC's
   `RegistryLookupResult` dataclass.
2. **PATCH** `app/config.py` — add three Settings fields:
   `crawl_verify_base_url`, `crawl_verify_api_key`, `crawl_verify_allowlist`.
3. **PATCH** `app/integrations/registry_dispatcher.py` — in `_get_client()`,
   check allowlist BEFORE the existing per-country branches; if the country
   is in the allowlist AND the API key is configured, return a
   `CrawlVerifyClient`. Otherwise fall through to existing logic (so the
   change is risk-free: misconfigured allowlist = legacy behavior).

## Rollout plan

1. Apply this patch.
2. Set env vars in GC App's environment (Azure App Service config):
   ```
   CRAWL_VERIFY_BASE_URL=https://crawldevvm.eastus2.cloudapp.azure.com:8443
   CRAWL_VERIFY_API_KEY=<copy from Azure Key Vault crawlkeyvault/cir-api-key>
   CRAWL_VERIFY_ALLOWLIST=KR,CA,GB,FR,TW,BR,US,IL,PE
   ```
3. Deploy. Watch GC `dispatcher` logs — every allowlisted lookup logs as
   `Registry hit [XX] '<entity>' -> '<name>' (<reg_no>) status=<status>` with
   `registry_source=crawl_verify_<cc>` so it's easy to grep.
4. 1-week soak. If clean (no error rate increase, no parity complaints from
   compliance ops), proceed to step 5.
5. Delete the corresponding files from `app/integrations/registries/`:
   - `kr.py, ca.py, gb.py, fr.py, tw.py, br.py, us.py, il.py, pe.py`
6. Remove the corresponding imports from `registry_dispatcher.py`.
7. Repeat for the next batch as Crawl migrates more countries.

## Rollback

The allowlist is feature-flagged. To revert: remove a country code from
`CRAWL_VERIFY_ALLOWLIST` env var and redeploy. The `_get_client()` fall-
through will use the legacy adapter for that country. No code change needed.

## Files in this handoff

| File | Action |
|---|---|
| `crawl_verify.py` | NEW — `app/integrations/registries/crawl_verify.py` |
| `config.patch` | PATCH — `app/config.py` |
| `dispatcher.patch` | PATCH — `app/integrations/registry_dispatcher.py` |
| `README.md` | this file |

Apply order:
```
cd /path/to/globalcompliance
cp /path/to/this/handoff/crawl_verify.py app/integrations/registries/crawl_verify.py
git apply /path/to/this/handoff/config.patch
git apply /path/to/this/handoff/dispatcher.patch
```

## Field-mapping notes

Per-country reg-number field GC may pass:

| Country | GC `reg_number` → Crawl payload field |
|---|---|
| GB | `company_number` |
| CA | `business_number` |
| FR | `siren` |
| TW | `ubn` |
| BR | `cnpj` |
| US | `cik` |
| IL | `company_number` |
| PE | `ruc` |
| KR | `corp_code` |

Status string normalization (Crawl → GC's `company_status` vocabulary):

| Crawl says | → GC `company_status` |
|---|---|
| `ACTIVE`, `ACTIVO`, `REGISTERED` | `active` |
| `DISSOLVED`, `INACTIVE`, `CEASED`, `CLOSED`, `HISTORICAL` | `dissolved` |
| `DISSOLVING`, `IN_LIQUIDATION`, `IN_LIQUIDATION_VOLUNTARILY` | `liquidation` |
| `SUSPENDED` | `winding_up` |
| `REVOKED` | `struck_off` |
| anything else | `unknown` |

(Hebrew status from IL like `בפרוק מרצון` falls through to `unknown`
until the IL parser maps that specific spelling — non-blocking, captured
as a Crawl-side follow-up.)

## Open questions for GC owner

1. Does GC's `Settings` use pydantic-settings? (Patch assumes yes per
   `class Settings(BaseSettings)` at `app/config.py:12`.) If `BaseSettings`
   reads env vars automatically, no further wiring needed.
2. Does GC's HTTP client want `httpx.AsyncClient` (assumed) or something
   else? Easy to swap.
3. Should the dispatcher reject self-signed-cert risk? Patch passes
   `verify=False` because Crawl's TLS is a self-signed cert
   (CLAUDE.md notes "self-signed cert, IP SANs"). If GC requires a CA-
   trusted cert, Crawl can switch to its Let's Encrypt cert at
   `crawldevvm.eastus2.cloudapp.azure.com` — same host, different cert.
