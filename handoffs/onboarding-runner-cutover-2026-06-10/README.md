# Onboarding scan-runner cutover to Crawl /api/v1/verify

**Generated:** 2026-06-10 by Crawl team
**Target repo:** `onboarding` (Onboarding App)
**Owner:** Onboarding owner

## Why

Onboarding's `app/counterparties/scan/runners.py` has six per-country
runners (`run_uk_companies_house`, `run_australia_abr`, `run_bc_orgbook`,
`run_norway_brreg`, `run_nz_companies`, `run_gleif`) that each call a
different country's source directly — duplicating logic Crawl /api/v1/verify
already provides. Per the **one-verify-server consolidation rule**, these
should route through Crawl so verify logic lives in ONE place across all
COPAP apps.

Onboarding already calls Crawl for the async verify-job (`/api/v1/verify-job`,
the 4-check one). This handoff extends the same pattern to the per-country
runners.

## What changes

1. **NEW** `app/counterparties/scan/_crawl_verify_runner.py` — shared helper
   with `call_crawl_verify(country_code, ctx)` and
   `crawl_verify_enabled_for(country_code)`. Reuses existing `CIR_API_URL` +
   `CIR_API_KEY` env vars (no new secrets needed).
2. **PATCH** `app/counterparties/scan/runners.py` — at the top of each of
   the 6 runners, add:
   ```python
   if crawl_verify_enabled_for("GB"):
       return call_crawl_verify("GB", ctx)
   ```
   When the country is in the allowlist env var, the runner routes through
   Crawl. Otherwise the existing legacy body runs unchanged — zero risk.

## Rollout plan

1. Apply this patch.
2. Set ONE env var in Onboarding's Azure App Service config (and on the
   Celery worker at 172.20.0.11):
   ```
   CRAWL_VERIFY_RUNNER_ALLOWLIST=GB,AU,CA,NO,NZ,GLEIF
   ```
   (`CIR_API_URL` and `CIR_API_KEY` are already set in Onboarding env per
   the existing verify-job runners — reuse them.)
3. Deploy. Watch logs — every cutover call returns a SourceResult with
   `raw_payload` from Crawl. Source label stays as `UK_Companies_House`,
   `AU_ABR`, etc. so downstream catalog/scan code doesn't change.
4. Soak 1 week. Confirm parity with prior runs (compare SourceResult
   summaries / found_data flags).
5. After parity confirmed, optionally remove the legacy runner bodies
   (everything after the cutover check) — but they're harmless to leave
   in for emergency rollback.

## Rollback

Pure env-var rollback. Remove a country from `CRAWL_VERIFY_RUNNER_ALLOWLIST`
and redeploy. The runner's legacy body resumes automatically — no code
change required.

## Per-runner allowlist gating

The single env var `CRAWL_VERIFY_RUNNER_ALLOWLIST` gates all 6 runners.
Start narrow if you want — e.g. `CRAWL_VERIFY_RUNNER_ALLOWLIST=GB` to
cut over Companies House only — then widen as confidence builds.

| Runner | Allowlist code | Crawl-side path |
|---|---|---|
| `run_uk_companies_house` | `GB` | `/api/v1/verify` country_code=GB (UK Companies House live-verified against Barclays/Tesco) |
| `run_australia_abr` | `AU` | `/api/v1/verify` country_code=AU (NB: hardcoded ABR GUID currently revoked — applies equally to Onboarding's existing call) |
| `run_bc_orgbook` | `CA` | `/api/v1/verify` country_code=CA (BC OrgBook live-verified against Shopify) |
| `run_norway_brreg` | `NO` | Currently not yet wired into Crawl `/api/v1/verify`. If allowlisted, Crawl returns NOT_FOUND with note. Either: (a) wait until Crawl adds NO, or (b) leave NO out of the allowlist. |
| `run_nz_companies` | `NZ` | Same as NO — not yet wired into Crawl. Leave out of allowlist for now. |
| `run_gleif` | `GLEIF` | Maps to Crawl's GLEIF source (already used by AR/CL/CO/HK/EG). For LEI-by-LEI lookup, populate `ctx.reg_number = ctx.lei` before the call. |

**Practical starter allowlist for first deploy:** `GB,CA` — both are
live-verified end-to-end on Crawl today.

## Files in this handoff

| File | Action |
|---|---|
| `_crawl_verify_runner.py` | NEW → `app/counterparties/scan/_crawl_verify_runner.py` |
| `runners.patch` | PATCH → `app/counterparties/scan/runners.py` |
| `README.md` | this file |

Apply order (from inside the Onboarding repo):
```
cp /path/to/handoff/_crawl_verify_runner.py app/counterparties/scan/_crawl_verify_runner.py
git apply /path/to/handoff/runners.patch
```

Tested: against `/opt/copap-readonly/onboarding` mirror, patch applies
clean and both files parse as valid Python.
