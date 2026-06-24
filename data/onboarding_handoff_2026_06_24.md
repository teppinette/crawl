# Crawl /verify API — what's shipped for Onboarding (2026-06-24)

End-of-day handoff covering the verify-API feedback list, with each item triaged to
what's live, what's queued, and how to use the new fields.

---

## Use this checklist when scoring Onboarding's 239-row gap

| Block | Action |
|---|---|
| **The 4 held CN USCCs** | Pass `reg_number=<USCC>` (or `uscc=<USCC>`) to `/api/v1/verify?country_code=CN`. Deterministic exact-record lookup — bypasses the English→Chinese name mismatch. Returns founding date, capital, legal rep, address (now structured). |
| **The 4 held IN GSTINs** | Pass `gstin=<GSTIN>` to `/api/v1/verify?country_code=IN`. Goes straight to `sandbox_india.verify_gstin` paid API. Full record. |
| **The 1 held AR CUIT** | Pass `cuit=<value>` (or `reg_number=<value>`) to `country_code=AR`. |
| **HK (2 entries with BRNs available)** | `/api/v1/verify/officers?country_code=HK` with the BRN. Costs HKD 22 per uncached BRN. **Now returns** `incorporation_date`, `cr_number`, `status`, `company_type`, `dissolution_date` in addition to officers (added today). |
| **Everything else, name-only** | `/api/v1/verify` with `entity_name` + `country_code`. CN goes through Baidu fallback today (see "what's broken" below). PK/AE/EG/TR need adapter improvements — pending. |

---

## What landed today

### Verify response fields

`/api/v1/verify` (CN especially) now returns:

```jsonc
{
  "verified": true,
  "legal_name": "比亚迪股份有限公司",
  "uscc": "91440300192317458F",
  "registration_number": "91440300192317458F",  // generic alias for tier engines
  "legal_representative": "王传福",
  "status": "存续",                                // 存续/在业/注销/吊销/迁出/停业
  "registered_capital": "911719.7565万人民币",
  "registered_capital_parsed": {                  // NEW — structured
    "raw": "911719.7565万人民币",
    "value": 911719.7565,
    "unit": "万",            // 万 = 10k, 亿 = 100M
    "unit_multiplier": 10000,
    "currency": "CNY"
  },
  "established_date": "1995-02-10",
  "incorporation_date": "1995-02-10",             // generic alias
  "address": "深圳市大鹏新区葵涌街道延安路一号",
  "address_parts": {                              // NEW — structured
    "province": null,                              // direct-administered muni: null
    "city": "深圳",
    "district": "大鹏",                            // 大鹏新区 → 大鹏 + 新区
    "street": "葵涌街道延安路一号",
    "notes": null                                  // anything in (parens) goes here
  },
  "business_scope": "...",                         // 经营范围
  "industry": "...",                               // 行业
  "adverse_flags": {                              // dedicated booleans
    "operation_anomaly":   {"flagged": true},      // 经营异常
    "severe_violation":    {"flagged": true},      // 严重违法
    "judgment_debtor":     {"flagged": true},      // 失信被执行人
    "enforcement_target":  {"flagged": true},      // 被执行人
    "license_revoked":     {"flagged": true},      // 吊销
    "admin_penalty":       {"flagged": true},      // 行政处罚
    "court_case":          {"flagged": true}       // 司法案件
  },
  "name_match_score": 1.0,                        // 0.0-1.0
  "candidate": null,                              // populated only on name-mismatch reject
  "validation_source": {
    "registry": "...",
    "url": "...",
    "record_id": "...",
    "how_to_reproduce": "...",
    "verified_at": "2026-06-24T...",
    "confidence": "high" | "medium",
    "fallback_path": false                         // true when Baidu fallback fired
  },
  "_cache_hit": false,                             // NEW — true if served from cache
  "_cache_age_seconds": null                       // NEW — age in seconds when cached
}
```

### B-priority contract fixes

- **B1 honest provenance.** Baidu fallback responses now flag `validation_source.fallback_path: true` and `confidence: "medium"`. The tier engine can weight accordingly. No more "SAMR (via Baidu)" pretending to be a primary-tier hit.
- **B2 hard no-match.** When Tianyancha returns a candidate whose brand prefix doesn't match the queried name (Alibaba→Ant Group, Ningbo Zhuoli→Zhejiang Jiehong, CNPC→Sinopec, ICBC→ABC), the response is `verified: false` with a `candidate` field showing what was returned + `name_match_score`. No more false positives.
- **B3 USCC dispatch alias.** `reg_number=<USCC>` and `uscc=<USCC>` both work for CN deterministic lookup. The gateway forwards both forms.
- **B4 24-hour response cache.** Same-entity re-query is now deterministic — 5ms instead of 3 seconds, with `_cache_hit: true` + `_cache_age_seconds` markers for observability. Pass `nocache: true` in the body to force a fresh lookup.

### Other notes

- **`/api/v2/enrich` is NOT 404.** It works — 140s p95 latency (returns 200 with `providers.CRUNCHBASE` + `providers.DEEP_LOOKUP` + `profile`). Most client default timeouts are too short. Set client timeout ≥ 180s.
- **`/api/v1/verify/officers` HK** (paid HKD 22 / uncached BRN) now also returns `incorporation_date`, `cr_number`, `status`, `company_type`, `dissolution_date` in addition to the directors list — same single paid lookup, more data.

---

## What's NOT done

| Item | Why | When |
|---|---|---|
| A2 shareholders + ownership %, A3 full officers roster, A6 former names (CN) | Tianyancha detail-page enrichment code is written and deployed, but the CN residential proxy market is dry today (Multilogin + Bright Data both giving non-CN IPs). Code lights up automatically when CN proxy recovers. | when CN proxy market recovers |
| A7 branch/parent relationships | not started — separate design | TBD |
| `/api/v1/lookup` | ✅ **shipped 2026-06-24 evening** — deterministic id-keyed wrapper |
| `/api/v1/raw` | not started — every adapter would need to surface verbatim upstream response | TBD |
| **MA (Morocco) adapter** | ✅ **shipped 2026-06-24 evening** — GLEIF primary + OpenCorporates secondary. Banque Centrale Populaire returned `verified=true`, LEI 54930083GPEJRQSDDR70, ACTIVE. |
| **Cross-country generic-alias normalization** | ✅ shipped — every /verify response now carries `incorporation_date`, `founding_year`, `registration_number`, `directors` as aliases for the country-specific field names. Tier engine can key on consistent names across all 41+ countries. |
| PK 17, AE 11, EG 8, TR 7, IL 5, MA 5 adapter enhancements | each ~30–60 min of work; prioritized by Onboarding's 160-row CSV | needs CSV from Onboarding |
| Onboarding's `/verify/officers` HK regex tuning | regex patterns I added are based on standard HK Companies Registry format; may need real-data tuning if HK ICRIS3EP renders differently | when first paid HK call comes back |

---

## How to test what's live

```bash
# Use established Tesco PLC as a clean GB control
curl -X POST https://crawldevvm:8443/api/v1/verify \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"entity_name":"TESCO PLC","country_code":"GB"}'

# A CN entity with held USCC
curl -X POST https://crawldevvm:8443/api/v1/verify \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"entity_name":"<any>","country_code":"CN","reg_number":"<USCC>"}'

# Force fresh (bypass cache)
curl -X POST https://crawldevvm:8443/api/v1/verify \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"entity_name":"...","country_code":"...","nocache":true}'

# /enrich — set client timeout to 180s
curl --max-time 180 -X POST https://crawldevvm:8443/api/v2/enrich \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"entity_name":"TESCO PLC","country_code":"GB"}'

# HK officers + incorporation date (HKD 22 per uncached BRN)
curl -X POST https://crawldevvm:8443/api/v1/verify/officers \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"country_code":"HK","brn":"<BRN>"}'

# /lookup — deterministic id-keyed lookup (recommended over /verify when
# you have a registry ID: USCC, CIN, CIK, BRN, CNPJ, KvK, SIREN, etc.)
curl -X POST https://crawldevvm:8443/api/v1/lookup \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"country_code":"CN","registration_id":"91440300192317458F"}'
# returns same shape as /verify with incorporation_date + founding_year + all fields

# /cir/run — single-call full CIR pipeline (collector → extractor → synthesizer).
# Returns run_id IMMEDIATELY (~100ms); pipeline runs in background ~2-3 min.
# Poll status via /evidence/runs/{run_id}; fetch rendered CIR via /evidence/runs/{run_id}/renders.
curl -X POST https://crawldevvm:8443/api/v1/cir/run \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"country_code":"GB","entity_name":"TESCO PLC"}'
# response: {run_id, next_steps:{poll_status, fetch_renders, expected_completion_seconds:180}}
# Then poll:
curl -sS -H "X-API-Key: $CIR_API_KEY" \
  https://crawldevvm:8443/api/v1/evidence/runs/<RUN_ID>
# When status="complete", fetch the rendered CIR:
curl -sS -H "X-API-Key: $CIR_API_KEY" \
  https://crawldevvm:8443/api/v1/evidence/runs/<RUN_ID>/renders
# render.payload.markdown contains the full banker-cited CIR
```

## Field-name guide (after cross-country normalization)

Every /verify and /lookup response now carries these CROSS-COUNTRY generic aliases:

| Generic field | Country-specific source |
|---|---|
| `incorporation_date` | aliased from registration_date, established_date, incorporated_on, founding_date, date_of_incorporation, date_opened, issue_date, activity_start_date |
| `founding_year` (int) | 4-digit year extracted from incorporation_date |
| `registration_number` | aliased from cin, uscc, company_number, uen, vkn, trn, corp_code, cr_number, rut, nit, ruc, cnpj, cik, kvk, ico, abn, siren, krs |
| `directors` | aliased from officers, partners, owners, managers, representatives |

**Recommendation for your tier engine:** key on `incorporation_date` / `founding_year` / `registration_number` / `directors` consistently. The country-specific field names (cin, uscc, etc.) are still present for backward compat.
