# Crawl v2 API — Migration Guide for GC & Onboarding
### Document: CRG-MIGRATE-001 | Version 1.1 | 2026-05-19

**v1.1 changes** — see Section 10 (Changelog). Six country adapters changed
behavior on 2026-05-19; the field shape on the wire is unchanged but the
data source behind CL / CO / HK switched from broken gov scrapes to GLEIF
LEI fallback, and SA / TW / PE became newly functional.

---

## 1. OVERVIEW

This guide walks GC and Onboarding through migrating to the Crawl v2 API.
All 7 blockers identified in the v2 spec review are now resolved.

| Blocker | Status | Detail |
|---------|--------|--------|
| NSG open | DONE | 172.20.0.11 -> crawldevvm:8400 (rule 200) + :8443 (rule 201) |
| Latency SLOs | DONE | `GET /api/v2/metrics` — real p50/p95/p99 from PostgreSQL |
| Per-source health | DONE | `GET /api/v2/health` — each upstream source individually |
| Schema versioning | DONE | `X-API-Version` + `X-Schema-Version` response headers |
| Raw response retention | DONE | 90-day store, `GET /api/v2/raw/{id}` for audit replay |
| Pricing model | DONE | Per-provider breakdown in `/api/v2/metrics` (see Section 6) |
| Real SSL cert | DONE | Let's Encrypt on `crawldevvm.eastus2.cloudapp.azure.com` |

---

## 2. CONNECTION DETAILS

```
Base URL (HTTP):   http://20.94.45.219:8400
Base URL (HTTPS):  https://crawldevvm.eastus2.cloudapp.azure.com:8443
Auth header:       X-API-Key: <CIR_API_KEY>
```

**Env vars on GC / Onboarding (.11):**

```bash
# Already set (v1 CIR integration)
CIR_API_KEY=cpk_cir_2026Q2_a7f3e9d1b4c8
CIR_API_URL=http://20.94.45.219:8400

# NEW — adverse media tool (set these if not already done)
ADVERSE_MEDIA_TOOL_URL=https://crawldevvm.eastus2.cloudapp.azure.com:8443/tools/adverse_media
ADVERSE_MEDIA_TOKEN=<value from: az keyvault secret show --vault-name crawlkeyvault --name internal-api-token>
```

**SSL note:** The HTTPS endpoint uses a real Let's Encrypt cert — no `verify=False` needed.
HTTP on port 8400 still works for VNet-internal calls.

---

## 3. MIGRATION ORDER (recommended)

### Phase 1: Screening (lowest risk, highest value)

**What it replaces:** Your direct CSL, OpenSanctions, and ICIJ integrations.

**Endpoint:** `POST /api/v2/screening`

```python
import requests

resp = requests.post(
    "https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/screening",
    headers={"X-API-Key": CIR_API_KEY, "Content-Type": "application/json"},
    json={
        "entity_name": "NIS A.D. NOVI SAD",
        "country": "RS",              # optional ISO-2
        "entity_type": "company"      # company | person | both (default: both)
    },
    timeout=15,
)
result = resp.json()

# result.status     = "hit" or "clear"
# result.risk_level = "CLEAR" | "MEDIUM" | "HIGH" | "CRITICAL"
# result.total_hits = 4
# result.sources.CSL.status       = "hit" | "clear" | "error" | "disabled"
# result.sources.CSL.hits[0].name = "matched entity name"
# result.sources.UK_FCDO.status   = "hit"
# result.sources.UN_SC.status     = "clear"
# etc.
```

**Sources (6 free, parallel):**

| Source | What it covers | Cost |
|--------|---------------|------|
| CSL | OFAC SDN + 10 BIS/Treasury lists (US) | Free |
| UK_FCDO | UK Financial Sanctions | Free |
| UN_SC | UN Security Council Consolidated List | Free |
| FBI | FBI Most Wanted (persons only) | Free |
| INTERPOL | Red Notices (persons only) | Free |
| EU | EU Consolidated Sanctions (limited — 403 from Azure, covered by Bridger) | Free |

**Migration steps:**

1. Add a new function `crawl_screening(entity_name, country)` that calls the endpoint above
2. Call it alongside Bridger — v2 screening is a free cross-check, not a replacement
3. Merge results: Bridger for PEPs/paid lists, v2 for CSL/FCDO/UN/FBI/INTERPOL
4. If any source returns `status: "hit"`, flag the entity for review
5. Store the `response_id` from the raw store for audit trail:
   - `GET /api/v2/raw?source=CSL&entity_name=NIS` to find stored responses
   - `GET /api/v2/raw/{response_id}` to retrieve the original upstream response

**Latency:** p50=5.1s, p95=6.3s (7 sources in parallel)

---

### Phase 2: Adverse Media

**What it replaces:** Your Bing/SerpAPI/NewsAPI adverse media scanning.

**Endpoint:** `POST /api/v2/media` (or `POST /tools/adverse_media` for direct access)

```python
resp = requests.post(
    "https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/media",
    headers={"X-API-Key": CIR_API_KEY, "Content-Type": "application/json"},
    json={
        "entity_name": "Gazprom",
        "country_code": "RU",
        "days_back": 30,           # optional, default 7
        "max_results": 20,         # optional, default 20
        "domain": "gazprom.ru",    # optional, improves crt.sh/Wayback
        "languages": ["en", "ru"], # optional, auto-detected from country
        "tier": "STANDARD",        # QUICK | STANDARD | DEEP
    },
    timeout=45,
)
result = resp.json()

# result.articles[]        — deduplicated, sorted by date
# result.articles[0].title
# result.articles[0].url
# result.articles[0].source
# result.articles[0].source_provider = "GDELT" | "BD_SERP" | "BD_DISCOVER"
# result.shell_signals     — crt.sh cert count, Wayback first capture
# result.providers.GDELT.status = "ok"
# result.providers.BD_SERP.status = "ok"
```

**For GC's `adverse_media_task.py`:** The endpoint is already wired via `ADVERSE_MEDIA_TOOL_URL`.
Set the env vars from Section 2 and the existing `_call_crawl_adverse_media()` function will work.

**Latency:** p50=25s, p95=38s (GDELT rate-limited at 6s stagger between queries)

---

### Phase 3: Verify (NEW countries only)

**What it replaces:** Nothing — this adds coverage you don't have.

**Do NOT migrate:** GB, IN, US, SG, BR, NO, CH, NZ, JP — keep your mature adapters.

**DO migrate:** Any new country where you'd otherwise need to build an adapter.

**Endpoint:** `POST /api/v2/verify`

```python
resp = requests.post(
    "https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/verify",
    headers={"X-API-Key": CIR_API_KEY, "Content-Type": "application/json"},
    json={
        "entity_name": "Heineken",
        "country_code": "NL",
        "domain": "heineken.com",   # optional, helps Deep Lookup fallback
    },
    timeout=90,
)
result = resp.json()

# result.verified           = true/false
# result.legal_name         = canonical name from registry
# result.officers[]         = extracted directors/officers
# result.validation_source  = { registry, url, verified_at }
# result.verify_note        = "Aggregator-sourced. KVK Handelsregister..."
# result.deep_lookup        = { name, industry, headquarters, ceo, revenue }  (if aggregator found no directors)
```

**Coverage: 77 countries**

- **10 gov registry** (authoritative, 2-15s): PK, IN, SG, TR, AE, CN, GB, BR, US, KR
- **67 aggregator** (best-effort via Firecrawl, 15-30s): AR, AT, AU, BD, BE, BG, BO, CA, CL, CO, CR, CY, CZ, DE, DO, DZ, EC, EG, ES, FI, FR, GR, GT, HK, HN, HU, ID, IL, IT, JO, JP, KE, LK, LT, LU, LV, MA, MT, MX, MY, NL, PA, PE, PL, PT, PY, RO, SE, SI, SV, TH, TW, UA, UY, VN, ZA + 10 Caribbean (VG, KY, BS, BM, BB, BZ, KN, JM, VI, TT) + MO

**Important:** Aggregator results include a `verify_note` telling you which authoritative source to check. They are NOT authoritative — treat them as leads, not proof.

**For Onboarding's `runners.py`:** The v1 verify endpoint is already wired via `CIR_API_URL`.
v2 is the same logic behind `/api/v2/verify`. No code change needed if using v1 path.

---

### Phase 4: Enrichment (optional)

**What it adds:** Revenue, employee count, leadership, funding data.

**Endpoint:** `POST /api/v2/enrich`

```python
resp = requests.post(
    "https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/enrich",
    headers={"X-API-Key": CIR_API_KEY, "Content-Type": "application/json"},
    json={
        "entity_name": "Tesla Inc",
        "country_code": "US",
        "domain": "tesla.com",
    },
    timeout=90,
)
result = resp.json()

# result.profile.name
# result.profile.revenue        = "$96.8 billion"
# result.profile.employee_count = "140,000"
# result.profile.headquarters   = "Austin, Texas"
# result.profile.ceo            = "Elon Musk"
# result.profile.industries     = ["Electric Vehicles", "Clean Energy"]
# result.profile.website
# result.profile.funding        = { total_raised, last_round_type, ... }
# result.profile.leadership[]   = [{ name, title, linkedin }, ...]
# result.citations[]            = [{ field, url, title, excerpt }, ...]
```

**Cost:** ~$0.01/call (Crunchbase scraper). Deep Lookup preview is free.

**Latency:** p50=65s, p95=75s (Deep Lookup polls up to 60s)

---

### Phase 5: One-shot Lookup (iPhone app / quick checks)

**Endpoint:** `POST /api/v2/lookup`

Runs verify + LEI + media + enrich + screening in parallel. Returns everything in one call.
Best for the iPhone app or ad-hoc lookups. NOT recommended for batch CIR flows (use individual endpoints for control).

---

## 4. AUDIT TRAIL (RAW RESPONSE RETENTION)

Every upstream HTTP call is stored for 90 days. Compliance can reproduce any data point.

```python
# List raw responses for an entity
resp = requests.get(
    "https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/raw",
    headers={"X-API-Key": CIR_API_KEY},
    params={"source": "CSL", "entity_name": "GAZPROM", "limit": 10},
)
# Returns: { count: 1, responses: [{ response_id, timestamp, source, status_code, body_bytes }] }

# Retrieve the full raw upstream response
resp = requests.get(
    f"https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/raw/{response_id}",
    headers={"X-API-Key": CIR_API_KEY},
)
# Returns: { response_id, timestamp, source, request: { method, url, params, headers },
#            response: { status_code, headers, body, body_truncated } }
```

**What's stored:** Method, URL, params, response status, headers, body (truncated at 500KB).
**What's redacted:** Authorization headers, API keys, cookies.
**Retention:** 90 days, daily cleanup at 03:05 UTC.

---

## 5. MONITORING

### Health check (no auth)
```
GET https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/health
```
Shows each upstream source individually: up/down/limited/disabled.

### Latency metrics + pricing
```
GET https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/metrics
```
Returns real p50/p95/p99 per endpoint, per-call cost breakdown, SLO targets, monthly projections.

### Schema versioning
Every v2 response includes:
- `X-API-Version: 2.2.0` — overall API version
- `X-Schema-Version: 1.0` — per-endpoint schema version

Pin to these in your client. When we change a response shape, we'll bump the schema version.
Your client can check `X-Schema-Version` and warn/fail if it sees an unexpected version.

---

## 6. COST ESTIMATE

### 6a. Variable API costs (per call)

| Endpoint | Cost/call | Source breakdown |
|----------|-----------|-----------------|
| /api/v2/screening | $0.00 | All 6 sources are free gov APIs |
| /api/v2/verify (gov) | $0.00 | Free gov registries |
| /api/v2/verify (aggregator) | ~$0.02 | ~5 Firecrawl searches |
| /api/v2/media | ~$0.02 | GDELT free + BD SERP $0.005 + BD Discover $0.01 |
| /api/v2/enrich | ~$0.01 | Crunchbase via Bright Data Web Scraper (Deep Lookup preview free) |
| /api/v2/lookup | ~$0.05 | All above combined |
| /api/v2/verify/lei | $0.00 | Free GLEIF API |

### 6b. Fixed platform costs (monthly)

| Item | Monthly | Vendor |
|------|---------|--------|
| Multilogin Business 300 (anti-detect browser, PK FBR + future gov sites) | $80 | Multilogin |
| Bright Data residential proxy (all outbound traffic) | included in API costs | Bright Data |
| Bright Data SERP API (adverse media) | ~$5/1K requests | Bright Data |
| Bright Data Discover API (adverse media) | ~$10/1K requests | Bright Data |
| Bright Data Web Scraper (Crunchbase enrichment) | ~$1.50/1K requests | Bright Data |
| Bright Data Deep Lookup (verify fallback) | free preview only | Bright Data |
| Dehashed (breach database, dark web) | $15 | Dehashed |
| 6 Azure VMs (5 regional + 1 dark web, auto-shutdown) | $190-260 | Azure |
| Azure Backup (8 VMs, daily, 30-day retention) | $80-120 | Azure |
| Azure Storage (RA-GRS, blob + raw responses) | $8-10 | Azure |
| Claude API (regional agents + CAPTCHA OCR) | $50-100 | Anthropic |
| DeepSeek API (China VM) | $15-30 | DeepSeek |
| Networking/egress | $10-20 | Azure |
| **Total fixed** | **$455-645** | |

### 6c. Loaded cost per entity (fixed + variable)

| Volume | Variable/mo | Fixed/mo | Total/mo | Per entity |
|--------|-------------|----------|----------|------------|
| 10 entities/day (300/mo) | $15 | ~$550 | ~$565 | ~$1.88 |
| 50 entities/day (1,500/mo) | $75 | ~$550 | ~$625 | ~$0.42 |
| 100 entities/day (3,000/mo) | $150 | ~$550 | ~$700 | ~$0.23 |

**Key vendors:** Bright Data (proxy, SERP, Discover, Web Scraper, Deep Lookup), Multilogin (anti-detect browser).

---

## 7. WHAT NOT TO MIGRATE

Keep these on GC — they work better there:

| Capability | Why keep on GC |
|-----------|----------------|
| Bridger (LexisNexis) | Primary PEP/sanctions, sub-second, commercial grade |
| ICIJ Neo4j | Already local on .11, zero latency |
| SEC EDGAR / GLEIF / Yahoo / OpenFIGI | Sub-second direct calls, gateway adds latency |
| Volza / Panjiva | Custom risk parsing in deepdive.py, too integrated |
| GB/NO/BR/CH/NZ/JP verify | Mature adapters with edge-case handling |

---

## 8. ROLLBACK

All v2 endpoints are additive. Your existing integrations continue to work unchanged.
If a v2 endpoint degrades:

1. Check `GET /api/v2/health` — identifies which source is down
2. Fall back to your existing integration for that source
3. Report to crawl team (Teams channel or SSH to crawldevvm)

No v1 endpoints were changed or removed. v2 is a parallel layer.

---

## 9. SUPPORT

- **Health dashboard:** `GET /api/v2/health` (no auth)
- **Latency/cost:** `GET /api/v2/metrics`
- **Logs:** `journalctl -u crawl-gateway -f` on crawldevvm
- **DB queries:** PostgreSQL `crawl-monitor-db` — tables `api_access_log`, `job_events`
- **Raw responses:** `GET /api/v2/raw?source=CSL&date=2026-05-10`

---

## 10. CHANGELOG — 2026-05-19 (v1.1)

Six country adapters changed behavior. The wire contract for `/api/v2/verify`
and `/api/v1/verify` is unchanged — same field names, same status codes — but
behind several adapters the data source moved, and three adapters that were
previously broken now return data. Onboarding QA should plan a pass on each
of the six.

### 10.1 Summary

| Country | Before 2026-05-19 | After 2026-05-19 | Onboarding QA action |
|---------|-------------------|------------------|----------------------|
| CL | Broken — SII endpoint deprecated, HTTP 500 | **GLEIF LEI fallback** (ISO 17442) | Re-test; confirm GLEIF tier on Verification tab; check NOT_FOUND handling for small entities without LEIs |
| CO | Broken — RUES API returns 401 (auth restricted to Colombian chambers of commerce) | **GLEIF LEI fallback** | Same as CL |
| HK | Broken — ICRIS migrated to a JS-only SPA (ICRIS3EP), bot-blocked | **GLEIF LEI fallback** | Same as CL/CO; +verify the parent-vs-subsidiary re-ranking returns the parent on common queries (HSBC HK, Cathay Pacific, Sun Hung Kai, Standard Chartered) |
| SA | Broken — MCI BotDetect CAPTCHA solver looping on cookie banner | **MCI live** via Multilogin + Sonnet 4.6 OCR | Confirm Arabic-script `legal_name` renders correctly in the UI; check fuzzy-match dedup against existing transliterated entries; add a normalization/transliteration step if needed |
| TW | Broken — GCIS dataset IDs renumbered; name-search filter missing required `Company_Status` clause | **Fixed + enriched** (App3 business items now populated) | Verify `paid_in_capital` and `business_scope` are surfaced where useful |
| PE | New adapter | **Decolecta SUNAT live** (1000 req/mo free tier) | Add Spanish-language status values to Onboarding's normalization map (see 10.4) |

### 10.2 GLEIF fallback (CL, CO, HK — and CH from 2026-05-15)

Three previously-broken adapters now use the GLEIF LEI Registry as a primary
source. This is the same pattern shipped earlier for Switzerland (`verify_ch.py`),
extended to CL / CO / HK after the local gov-scrape paths were ruled out for
the reasons in the table above.

**Field-shape differences vs gov-registry responses:**

- `validation_source.registry` now reads
  `"GLEIF — Global Legal Entity Identifier Foundation (ISO 17442)"` instead
  of the local gov registry name (`SII`, `RUES`, `ICRIS`). If your
  `consolidate_*` corroboration synth on the Verification tab keys off the
  registry string for tiering, audit the lookup table.
- New top-level field: `lei` — ISO 17442 20-character identifier (string).
- `legal_form` formatted per GLEIF conventions (ELF code or "other" string),
  not local-registry conventions (e.g. SII's "S.A.", RUES's "S.A.S.").
- `registered_address` joined from GLEIF's `addressLines` + city/region/postal/
  country — flat string, not the structured object some local registries
  returned.
- `source` field appended with the gov-registry deprecation reason —
  e.g. `"GLEIF LEI Registry (SII RUT lookup deprecated)"`.
- Smaller entities without LEIs return:
  ```json
  {
    "found": false,
    "status": "NOT_FOUND",
    "note": "GLEIF covers <COUNTRY> companies with Legal Entity Identifiers (banks, listed, large corporates)...",
    "validation_source": { ... }
  }
  ```
  — Onboarding's UI should render the `note` verbatim so analysts understand
  the limitation rather than treating it as "company doesn't exist."

**Coverage rule of thumb:**

GLEIF covers entities with regulatory derivatives-reporting or
financial-licensing obligations — banks, exchange-listed companies, insurers,
regulated funds, large corporates. The local gov registry would cover any
registered company, but for CL / CO / HK those gov paths aren't currently
reachable from a server (paywall, auth wall, or SPA bot-block respectively).

For high-volume Onboarding of smaller entities in CL / CO / HK, the gateway
will return `found: false` more often than it did before. This isn't a
regression in the data path — the data path is fixed and working — it's a
fundamental ceiling of free public sources for those jurisdictions.

### 10.3 SA Arabic-script `legal_name`

The MCI live response returns the legal name in Arabic UTF-8:

```json
{
  "verified": true,
  "country_code": "SA",
  "legal_name": "الشركة السعودية للصناعات الأساسيه سابك",
  "cr_number": "1010010813",
  "status": "نشط",
  "capital": "30000000000.0",
  ...
}
```

**Onboarding implications:**

- `Counterparty.LegalName` will receive Arabic-script content. The detail
  page renders this raw — confirm the UI uses a font stack that includes an
  Arabic-script face (e.g. Noto Sans Arabic, Segoe UI, Arial Unicode MS) so
  the glyphs don't fall back to tofu boxes on Windows clients.
- Fuzzy matching during merge/dedup needs to handle the case where one
  record has the Arabic name and another has a Latin transliteration ("SABIC"
  or "Saudi Basic Industries Corporation"). Options: (a) run an Arabic →
  Latin transliteration step on import and store both, (b) widen the fuzzy
  matcher to compare against the `cr_number` + jurisdiction first, falling
  back to name only on tie.
- Excel exports: ensure the export pipeline writes UTF-8 with BOM (or .xlsx
  rather than .csv) so Excel doesn't mojibake the Arabic.
- `status` value is also Arabic — `نشط` means "ACTIVE." Decision: either
  Onboarding maps Arabic statuses in its normalization layer, or we add a
  field-level English-status mapping in the SA adapter. Reach out and tell
  us which side you'd prefer.

### 10.4 PE Spanish status normalization

New PE adapter returns SUNAT's native Spanish status values:

| `status` value | English equivalent | Equivalent to Onboarding map |
|----------------|--------------------|------------------------------|
| `ACTIVO` | Active | `ACTIVE` |
| `SUSPENDIDO` | Suspended | `SUSPENDED` |
| `INACTIVO` | Inactive | `INACTIVE` |
| `BAJA DEFINITIVA` | Definitively closed | `DISSOLVED` |
| `BAJA PROVISIONAL` | Provisionally closed | `INACTIVE` |

The adapter also returns a separate `condition` field (SUNAT's "estado del
contribuyente" — taxpayer condition):

| `condition` value | English equivalent |
|-------------------|--------------------|
| `HABIDO` | Locatable (in good standing for tax notices) |
| `NO HABIDO` | Unlocatable (compliance risk) |
| `NO HALLADO` | Not found at registered address |

Suggest adding these to Onboarding's status normalization map. `NO HABIDO`
in particular is a useful adverse-flag signal that doesn't fit the standard
ACTIVE/INACTIVE binary.

### 10.5 TW field expansion

The fixed `verify_tw.py` adapter now populates two fields that were
empty / null before:

- `paid_in_capital`: e.g. `"TWD 259,325,245,210"` (TSMC) — paid-in capital
  alongside the existing `capital` (authorized capital). Useful for
  distinguishing shell companies (high authorized, near-zero paid-in) from
  operating businesses.
- `business_scope`: e.g. `"CC01080 電子零組件製造業; CC01090 電池製造業; ..."` —
  formatted as `"{code} {Chinese description}; ..."` joined from GCIS App3's
  `Cmp_Business` array, up to 20 items.

Both fields are nullable — older entries scraped before 2026-05-19 stayed at
`null` in the DB and won't repopulate retroactively.

### 10.6 Still blocked / awaiting credentials

| Country | Status | Action holder |
|---------|--------|---------------|
| EC | supercias.gob.ec unreachable from server (direct + proxy) — endpoint may have moved or be geo-fenced beyond what Bright Data residential pools cover | Crawl — periodic recheck; alternative SRI endpoint research |
| NL | zoeken.kvk.nl unreachable from server — KvK has a paid Handelsregister API as the realistic path | Open business decision |
| CH | GLEIF fallback live; awaiting Zefix Basic auth credentials (email `zefix@bj.admin.ch`) for the authoritative path | Crawl — credential request sent |
| AU | Awaiting free ABR Web Services GUID registration (`abr.business.gov.au`) | Crawl — pending |
| JP | Awaiting free houjin-bangou app ID registration (`houjin-bangou.nta.go.jp`) | Crawl — pending |

### 10.7 Adapter source commits (2026-05-19)

| Commit | Scope |
|--------|-------|
| `9d3c039` | Add CL/CO/PE verification + fix SA MCI CAPTCHA capture |
| `c7b6810` | Fix TW MOEA GCIS verification — refresh dataset IDs + name search filter |
| `90ee8d7` | Fix HK ICRIS verification — switch to GLEIF (ICRIS migrated to paywalled SPA) |
| `f9b98f0` | (earlier) Add GLEIF LEI fallback for CH verification |
| `0db7334` | (earlier) Add 16 expansion country verifiers + mlx_http proxy-only rewrite |
