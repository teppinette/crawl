# Crawl Data Gateway — API v2.0 Specification
## For: Global Compliance, Onboarding, SalesTracker, iPhone App
### Document: CRG-DATA-V2-001 | Version 2.2 | 2026-05-09

---

## 1. WHAT IS THIS

A unified data API for structured lookups — government registry verification,
corporate hierarchy mapping, adverse media screening, and company enrichment.
Every response includes `validation_source` or `citations` so compliance teams
can independently verify the data.

No AI agents. No dark web. No proxy knowledge required. Just structured data
from authoritative sources, delivered as JSON.

### What v2 covers

| Endpoint | What it does | Sources | Response time |
|----------|-------------|---------|---------------|
| `POST /api/v2/verify` | Gov registry verification (10 countries) | SECP, FBR, MCA, ACRA, GIB, FTA, Tianyancha, Companies House, Receita Federal, SEC EDGAR, DART | 2-15s |
| `POST /api/v2/verify/lei` | GLEIF LEI corporate hierarchy | GLEIF API | 2-5s |
| `POST /api/v2/media` | Adverse media / news screening | GDELT (65 languages), Bright Data SERP (Google News), Bright Data Discover (AI-ranked), crt.sh, Wayback | 10-25s |
| `POST /api/v2/enrich` | Company enrichment (revenue, employees, leadership, funding) | Bright Data Deep Lookup (1000+ sources), Crunchbase | 30-60s |
| `POST /api/v2/screening` | Sanctions & watchlist screening (6 sources) | CSL/OFAC, UK FCDO, UN SC, FBI, INTERPOL, EU (limited) | 3-8s |
| `POST /api/v2/lookup` | One-shot fan-out (all above in parallel) | All above | 30-60s |
| `GET /api/v2/health` | Per-source health status | All above | <1s |

### What v2 does NOT cover (covered elsewhere)

| Capability | Where it lives | Why |
|-----------|---------------|-----|
| Sanctions / PEP screening (Bridger) | LexisNexis Bridger (GC) | Bridger is the primary paid screening engine — PEPs, sub-second, commercial grade. v2/screening is a free cross-check layer. |
| Offshore leaks (Panama/Paradise/Pandora) | ICIJ Neo4j mirror (.11) | Already local, no network call needed |
| Market data (SEC/GLEIF/Yahoo/OpenFIGI) | GC direct calls | Sub-second, working adapters, no proxy needed |
| Trade data (Volza/Panjiva) | GC deepdive.py | Custom risk-relevant parsing too tightly integrated to extract |
| AI-powered research (CIR) | v1 API /api/v1/jobs | Separate concern — uses OpenClaw agents |
| Dark web / breach scanning | v1 API, crawl-darkweb VM | Separate concern — Tor network |

### What stays on v1 (unchanged)

| v1 Endpoint | Purpose |
|-------------|---------|
| `POST /api/v1/jobs` | Submit AI research jobs (CIR, product-intel) |
| `GET /api/v1/jobs/{id}` | Poll AI research jobs |
| `POST /api/v1/verify` | v1 verify (still works, v2 is the same logic) |
| `POST /tools/adverse_media` | v1 adverse media (v2/media wraps this) |

---

## 2. CONNECTION DETAILS

```
Base URL:   https://crawldevvm:8443/api/v2
            http://20.94.45.219:8400/api/v2    (plain HTTP, internal)
Auth:       X-API-Key header (or Authorization: Bearer <key>)
Content:    application/json (POST body + response)
Timeout:    30s for verify/lei, 30s for media, 75s for enrich, 75s for lookup
Rate limit: 30 requests / 60 seconds per API key
```

GC app connects from 104.209.146.16 — already allowed in NSG (rules 205/206).

### API Key

```
X-API-Key: <key>
```

Same key for all endpoints. Contact crawl-infra for provisioning.

---

## 3. ENDPOINTS

### 3.1 Registry Verification

```
POST /api/v2/verify
```

Verifies an entity against the official government registry for its country.
Returns the legal name, status, and a `validation_source` block that a banker
can use to independently verify.

**Request:**
```json
{
    "entity_name": "Tesla Inc",
    "country_code": "US",
    "ticker": "TSLA"
}
```

Optional fields by country: `ntn` (PK), `cin`/`iec` (IN), `uen` (SG), `vkn` (TR),
`trn` (AE), `uscc` (CN), `company_number` (GB), `cnpj` (BR), `cik`/`ticker` (US),
`corp_code`/`brn` (KR).

**Response:**
```json
{
    "entity_name": "Tesla Inc",
    "country_code": "US",
    "verified": true,
    "legal_name": "Tesla, Inc.",
    "cik": "0001318605",
    "status": "ACTIVE",
    "sic_description": "Motor Vehicles & Passenger Car Bodies",
    "tickers": ["TSLA"],
    "exchanges": ["Nasdaq"],
    "validation_source": {
        "registry": "U.S. Securities and Exchange Commission (SEC) — EDGAR",
        "url": "https://www.sec.gov/cgi-bin/browse-edgar?...",
        "record_id": "0001318605",
        "how_to_reproduce": "Visit SEC EDGAR → Search CIK '0001318605'",
        "verified_at": "2026-05-09T17:45:51Z"
    },
    "summary": "Tesla, Inc. — CIK 0001318605 — ACTIVE — Motor Vehicles & Passenger Car Bodies"
}
```

**Supported countries:** PK, IN, SG, TR, AE, CN, GB, BR, US, KR (expanding).

Response fields vary by country. Every response always includes:
`verified`, `legal_name`, `status`, `validation_source`, `timestamp`, `summary`.

---

### 3.2 LEI Corporate Hierarchy

```
POST /api/v2/verify/lei
```

Looks up the Legal Entity Identifier and returns the full corporate hierarchy
(parent → ultimate parent) from GLEIF.

**Request:**
```json
{
    "entity_name": "HSBC Holdings",
    "lei": "",
    "country_code": "GB"
}
```

**Response:**
```json
{
    "lei": "MLU0ZO3ML4LN2LL2TL39",
    "entity_name": "HSBC HOLDINGS PLC",
    "found": true,
    "status": "ISSUED",
    "jurisdiction": "GB",
    "legal_address": "8 Canada Square, London E14 5HQ, GB",
    "parent": { "lei": "...", "name": "...", "country": "..." },
    "ultimate_parent": { "lei": "...", "name": "...", "country": "..." },
    "validation_source": {
        "registry": "GLEIF — Global Legal Entity Identifier Foundation",
        "url": "https://search.gleif.org/#/record/MLU0ZO3ML4LN2LL2TL39",
        "verified_at": "2026-05-09T05:00:00Z"
    }
}
```

---

### 3.3 Adverse Media

```
POST /api/v2/media
```

Searches mainstream news sources for adverse coverage. NOT dark web.
Three article providers run in parallel; results are deduplicated by URL.

**Request:**
```json
{
    "entity_name": "Wirecard AG",
    "country_code": "DE",
    "domain": "wirecard.com",
    "languages": ["en", "de"],
    "days_back": 30,
    "max_results": 20
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `entity_name` | Yes | — | Company or person name |
| `country_code` | No | "XX" | ISO 2-letter, auto-selects languages |
| `domain` | No | — | Enables crt.sh + Wayback shell signals |
| `languages` | No | Auto from country | ISO 639-1 codes |
| `days_back` | No | 7 | Max 90 (GDELT caps at ~84 days) |
| `max_results` | No | 20 | Max articles returned |

**Response:**
```json
{
    "status": "complete",
    "duration_ms": 12918,
    "providers": {
        "GDELT": {"status": "ok", "count": 15, "latency_ms": 8200},
        "BD_SERP": {"status": "ok", "count": 8, "latency_ms": 3200},
        "BD_DISCOVER": {"status": "ok", "count": 5, "latency_ms": 6100},
        "CRT_SH": {"status": "ok", "count": 3},
        "WAYBACK": {"status": "ok", "count": 12}
    },
    "articles": [
        {
            "title": "Wirecard trial shows the risks of slow legal process",
            "url": "https://www.irishtimes.com/...",
            "source": "irishtimes.com",
            "published_at": "2026-04-10T05:00:00Z",
            "language": "en",
            "source_provider": "GDELT",
            "relevance_score": null
        }
    ],
    "shell_signals": {
        "cert_count": 3,
        "earliest_cert_date": "2015-03-12",
        "wayback_first_capture": "2006-02-14",
        "wayback_total_captures": 12,
        "domain_age_days": 7400
    }
}
```

**Article providers:**
- **GDELT** — 65 languages, negative-tone filter, free (rate-limited 1 req/5s)
- **BD_SERP** — Google News via Bright Data SERP API (paid per request)
- **BD_DISCOVER** — AI-ranked results with `relevance_score` 0-1 (paid per request)

**Shell signal providers** (only if `domain` provided):
- **CRT_SH** — SSL certificate transparency (new domain = shell company risk)
- **WAYBACK** — Wayback Machine captures (no web history = shell company risk)

**Status values:** `complete` (all providers ok), `partial` (some failed, some data),
`error` (all article providers failed).

---

### 3.4 Company Enrichment

```
POST /api/v2/enrich
```

AI-powered company enrichment from 1000+ public sources. Returns structured
profile with citations that can be independently verified.

**Request:**
```json
{
    "entity_name": "Samsung Electronics",
    "country_code": "KR",
    "domain": "samsung.com"
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `entity_name` | Yes | Company name |
| `country_code` | No | Improves Deep Lookup accuracy |
| `domain` | No | Improves Crunchbase slug matching |

**Response:**
```json
{
    "status": "complete",
    "duration_ms": 51248,
    "entity_name": "Samsung Electronics",
    "providers": {
        "CRUNCHBASE": {"status": "ok", "latency_ms": 17447},
        "DEEP_LOOKUP": {"status": "ok", "latency_ms": 51597}
    },
    "profile": {
        "name": "Samsung Electronics",
        "domain": "samsung.com",
        "revenue": "$234.62 billion USD",
        "employee_count": "262,647 employees",
        "headquarters": "Suwon-si, Gyeonggi-do, South Korea",
        "ceo": "TM Roh and Young Hyun Jun",
        "founded": "1969",
        "industry": "Consumer Electronics",
        "industries": ["Consumer Electronics", "Electronics", "Manufacturing"],
        "website": "https://www.samsung.com",
        "operating_status": "active",
        "contact_email": "...",
        "contact_phone": "...",
        "social_media": ["https://linkedin.com/company/..."],
        "funding": {
            "total_raised": "...",
            "last_round_type": "...",
            "last_round_date": "...",
            "num_rounds": 3
        },
        "leadership": [
            {"name": "TM Roh", "title": "President & Head of MX", "linkedin": "..."}
        ]
    },
    "citations": [
        {
            "field": "revenue",
            "url": "https://www.macrotrends.net/stocks/charts/SSNLF/samsung/revenue",
            "title": "Samsung Electronics Revenue 2012-2026",
            "excerpt": "Samsung annual revenue for 2025 was $234.62B"
        }
    ],
    "timestamp": "2026-05-09T19:10:00Z"
}
```

**Enrichment providers:**
- **CRUNCHBASE** — Structured company data (funding, leadership, financials).
  Best for public / VC-backed companies.
- **DEEP_LOOKUP** — AI-powered search across 1000+ public sources with citations via Bright Data.
  Works for private companies too. We use the free preview only (10 samples, no charge).

**Typical response time:** 30-60 seconds.

---

### 3.5 Sanctions & Watchlist Screening

```
POST /api/v2/screening
```

Free cross-check layer screening against 6 government sanctions/watchlist sources
in parallel. Supplements Bridger (which remains the primary paid screening engine
on GC). No per-call API costs — all sources are free public APIs or cached XML lists.
Platform infrastructure costs (Bright Data proxy, Multilogin, Azure VMs) apply — see migration guide.

**Request:**
```json
{
    "entity_name": "Gazprom",
    "country": "RU",
    "entity_type": "company"
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `entity_name` | Yes | — | Company or person name |
| `country` | No | — | ISO 2-letter code (used by CSL for filtering) |
| `entity_type` | No | `"both"` | `"company"`, `"person"`, or `"both"` |

**Response:**
```json
{
    "status": "hit",
    "risk_level": "HIGH",
    "total_hits": 4,
    "duration_ms": 6637,
    "entity_name": "Gazprom",
    "country": "RU",
    "entity_type": "company",
    "sources": {
        "CSL": {
            "source": "CSL",
            "status": "hit",
            "risk_level": "HIGH",
            "hits": [
                {
                    "name": "Gazprom, OAO",
                    "source_list": "Entity List (EL) - Bureau of Industry and Security",
                    "type": "Entity",
                    "programs": ["EAR99"],
                    "addresses": [{"country": "Russia"}]
                }
            ],
            "hit_count": 2,
            "latency_ms": 1200
        },
        "UK_FCDO": {
            "source": "UK_FCDO",
            "status": "hit",
            "risk_level": "HIGH",
            "hits": [
                {"name": "GAZPROM NEFT", "list_type": "entity", "detail": "UK_FCDO (entity): GAZPROM NEFT"}
            ],
            "hit_count": 2,
            "latency_ms": 223
        },
        "EU": {"source": "EU", "status": "unavailable", "error": "List unavailable"},
        "UN_SC": {"source": "UN_SC", "status": "clear", "hit_count": 0, "latency_ms": 11},
        "FBI": {"source": "FBI", "status": "clear", "hit_count": 0, "latency_ms": 11},
        "INTERPOL": {"source": "INTERPOL", "status": "clear", "hit_count": 0, "latency_ms": 343}
    },
    "timestamp": "2026-05-09T22:00:04Z"
}
```

**Screening sources (all free):**

| Source | What it covers | Type | Notes |
|--------|---------------|------|-------|
| **CSL** | OFAC SDN + 10 US lists (BIS Entity/Denied/Unverified, SSI, FSE, CMIC, MEU) | Real-time API | Subscription key in Key Vault |
| **UK_FCDO** | UK Financial Sanctions | XML, cached 12h | ~2000 entries |
| **EU** | EU Consolidated Sanctions | XML, cached 12h | Currently unavailable (webgate 403 from Azure) |
| **UN_SC** | UN Security Council Consolidated List | XML, cached 12h | ~800 entries |
| **FBI** | FBI Most Wanted | JSON API, cached 12h | Persons only |
| **INTERPOL** | INTERPOL Red Notices | REST API, real-time | Persons only |

**Risk levels:** `CLEAR` (no hits), `MEDIUM` (non-SDN list match), `HIGH` (BIS/SSI/FCDO/UN match), `CRITICAL` (SDN/FBI/INTERPOL match).

**Name matching:** Fuzzy matching via SequenceMatcher (threshold 0.82 for companies, 0.90 for persons) + token overlap. Filters out spurious Elasticsearch fuzzy matches.

**Typical response time:** 3-8 seconds (first call loads XML caches ~5s, subsequent calls <1s from cache).

---

### 3.6 One-Shot Lookup

```
POST /api/v2/lookup
```

Runs verify + LEI + media + enrich + screening in parallel. Returns combined result.
Designed for iPhone app / quick lookups before a meeting.

**Request:**
```json
{
    "entity_name": "Samsung Electronics",
    "country_code": "KR",
    "domain": "samsung.com"
}
```

**Response:**
```json
{
    "entity_name": "Samsung Electronics",
    "country_code": "KR",
    "lookup_time_ms": 52000,
    "registry": {
        "verified": true,
        "legal_name": "삼성전자",
        "status": "ACTIVE",
        "validation_source": { "..." }
    },
    "lei": {
        "found": true,
        "lei": "9884007ER46L6N7EI764",
        "entity_name": "SAMSUNG ELECTRONICS CO., LTD.",
        "parent": { "name": "Samsung C&T Corporation", "country": "KR" },
        "ultimate_parent": { "name": "Samsung C&T Corporation", "country": "KR" }
    },
    "media": {
        "total_articles": 5,
        "risk_level": "LOW",
        "top_article": "Samsung reports record Q1 chip revenue — reuters.com",
        "providers": {"GDELT": "ok", "BD_SERP": "ok", "BD_DISCOVER": "ok"}
    },
    "enrichment": {
        "status": "complete",
        "name": "Samsung Electronics",
        "industry": ["Consumer Electronics", "Electronics"],
        "employee_count": "262,647",
        "headquarters": "Suwon-si, South Korea",
        "website": "https://www.samsung.com",
        "revenue": "$234.62 billion USD"
    },
    "screening": {
        "status": "clear",
        "risk_level": "CLEAR",
        "total_hits": 0,
        "sources": {"CSL": "clear", "UK_FCDO": "clear", "UN_SC": "clear", "FBI": "clear", "INTERPOL": "clear"}
    },
    "timestamp": "2026-05-09T19:15:00Z"
}
```

**Typical response time:** 30-60 seconds (bounded by enrichment + media).

---

### 3.7 Health

```
GET /api/v2/health
```

No auth required. Shows per-source status. Use for monitoring probes.

**Response:**
```json
{
    "status": "ok",
    "service": "crawl-data-gateway",
    "api_version": "2.0.0",
    "sources": {
        "gateway": { "status": "up", "version": "3.0.0" },
        "verify_vm": { "status": "up", "version": "1.3.0", "countries": ["PK","IN","SG","TR","AE","CN","GB","BR","US","KR"] },
        "adverse_media": {
            "GDELT": "up",
            "BD_SERP": "up",
            "BD_DISCOVER": "up",
            "CRT_SH": "up",
            "WAYBACK": "up"
        },
        "enrichment": {
            "CRUNCHBASE": "up",
            "DEEP_LOOKUP": "up"
        },
        "screening": {
            "CSL": "up",
            "UK_FCDO": "up",
            "EU": "limited",
            "UN_SC": "up",
            "FBI": "up",
            "INTERPOL": "up"
        },
        "verify_countries": ["PK","IN","SG","TR","AE","CN","GB","BR","US","KR"]
    }
}
```

---

## 4. ERROR HANDLING

| Code | Meaning |
|------|---------|
| 200 | Success (check `verified`/`found`/`status` fields — 200 with `verified: false` means not found, not an error) |
| 400 | Blocked terms detected (data sanitization) |
| 403 | Missing or invalid API key |
| 422 | Validation error (missing required fields, unsupported country) |
| 429 | Rate limited (30 req / 60s) |
| 502 | Upstream source unavailable |

**Partial success:** Media and enrich endpoints return `status: "partial"` when
some providers fail but others returned data. Check the `providers` block to see
which sources contributed.

---

## 5. VALIDATION SOURCE

Every verify/LEI response includes:

```json
{
    "validation_source": {
        "registry": "Securities and Exchange Commission of Pakistan (SECP)",
        "url": "https://eservices.secp.gov.pk/eServices/NameSearch.jsp",
        "record_id": "0012345",
        "how_to_reproduce": "Visit SECP eServices → Name Search → Enter 'Acme Pakistan'",
        "verified_at": "2026-05-09T05:00:00Z"
    }
}
```

For compliance audit trail. A banker can follow `how_to_reproduce` to verify independently.

Every enrich response includes `citations[]` — URL + title + excerpt for each data point.

---

## 6. DATA SANITIZATION

- **NEVER** include COPAP name, customer names, supplier names, or internal identifiers
- Gateway HARD FAILS (HTTP 400) on blocked terms — does not silently redact
- Callers must sanitize entity names before sending (no internal references)

---

## 7. SAMPLE CLIENT CODE

```python
import requests, os

CRAWL_API = "https://crawldevvm:8443/api/v2"
API_KEY = os.environ["CRAWL_API_KEY"]
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def crawl_verify(entity_name, country_code, **kwargs):
    """Registry verification — 2-15s."""
    resp = requests.post(f"{CRAWL_API}/verify",
        json={"entity_name": entity_name, "country_code": country_code, **kwargs},
        headers=HEADERS, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()


def crawl_media(entity_name, country_code="XX", domain=None, days_back=30):
    """Adverse media screening — 10-25s."""
    resp = requests.post(f"{CRAWL_API}/media",
        json={"entity_name": entity_name, "country_code": country_code,
              "domain": domain, "days_back": days_back},
        headers=HEADERS, timeout=35, verify=False)
    resp.raise_for_status()
    return resp.json()


def crawl_enrich(entity_name, country_code="", domain=None):
    """Company enrichment — 30-60s."""
    resp = requests.post(f"{CRAWL_API}/enrich",
        json={"entity_name": entity_name, "country_code": country_code,
              "domain": domain},
        headers=HEADERS, timeout=90, verify=False)
    resp.raise_for_status()
    return resp.json()


def crawl_screening(entity_name, country="", entity_type="both"):
    """Sanctions & watchlist screening — 3-8s."""
    resp = requests.post(f"{CRAWL_API}/screening",
        json={"entity_name": entity_name, "country": country,
              "entity_type": entity_type},
        headers=HEADERS, timeout=15, verify=False)
    resp.raise_for_status()
    return resp.json()


def crawl_lookup(entity_name, country_code, **kwargs):
    """One-shot: verify + LEI + media + enrich + screening — 30-60s."""
    resp = requests.post(f"{CRAWL_API}/lookup",
        json={"entity_name": entity_name, "country_code": country_code, **kwargs},
        headers=HEADERS, timeout=90, verify=False)
    resp.raise_for_status()
    return resp.json()
```

### Integration Notes for GC / Onboarding

1. **Replace internal registry calls** with `/api/v2/verify` — one endpoint,
   10 countries, consistent `validation_source` format. No more per-country
   adapter maintenance on the GC side.

2. **Replace adverse media gaps** with `/api/v2/media` — the screening
   completeness watcher can call this directly. Returns structured articles
   with `source_provider` so you know where each result came from.

3. **New: company enrichment** via `/api/v2/enrich` — revenue, employees,
   leadership, funding, headquarters. Useful for onboarding risk scoring
   and compliance profiles. Citations included for audit trail.

4. **For quick lookups** (iPhone app, pre-meeting checks), use `/api/v2/lookup`
   to get everything in one call.

5. **Timeouts:** Set 30s for verify/media, 90s for enrich/lookup. The gateway
   handles per-provider timeouts internally — a slow provider won't block
   the whole response.

6. **Partial results:** Media and enrich can return `status: "partial"` if some
   providers fail. Always check the `providers` block to see what contributed.

---

## 8. SERVER ROLES (no duplication)

| Server | Role | What it owns |
|--------|------|-------------|
| **crawl-verify** (180.20.0.4) | Gov registry verification | All country registry adapters. One source of truth. |
| **crawl-darkweb** (20.86.161.6) | Dark web / breach / leak | 37 Tor sources. Network-isolated. |
| **5x OpenClaw VMs** | Deep AI research | CIR narratives, product intel, financial analysis |
| **crawldevvm** (20.94.45.219) | Gateway + data APIs | Routes all requests. Adverse media, enrichment, Bright Data APIs. |
| **GC app** (.11) | Compliance decision engine | Bridger, ICIJ Neo4j, market data, trade data, AI synthesis |

Nothing is duplicated. Each server has one job.

---

## 9. MIGRATION PLAN

### Phase 1 (now) — Adopt new endpoints
- GC/Onboarding start calling `/api/v2/media` for adverse media screening
- GC/Onboarding start calling `/api/v2/enrich` for company enrichment
- Both systems continue running existing registry adapters

### Phase 2 — Registry consolidation
- Port GC's mature country adapters (GB, IN, US, SG) into crawl-verify
- GC/Onboarding switch to `/api/v2/verify` for all registry checks
- Retire per-country adapters on GC side

### Phase 3 — Full v2 adoption
- All structured lookups go through v2 API
- GC keeps: Bridger, ICIJ, market data, trade data, AI synthesis
- iPhone app / SalesTracker use `/api/v2/lookup` for one-shot lookups
