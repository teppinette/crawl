# Crawl Data Gateway — API v2.0 Specification
## For: Global Compliance, Onboarding, SalesTracker, iPhone App
### Document: CRG-DATA-V2-001 | Version 2.0 | 2026-05-09

---

## 1. WHAT IS THIS

A unified data API for structured lookups — government registry verification,
corporate hierarchy mapping, and adverse media screening. Every response
includes `validation_source` so compliance teams can independently verify
the data.

No AI. No dark web. No proxy knowledge required. Just structured data from
authoritative sources.

### What v2 covers

| Endpoint | What it does | Sources |
|----------|-------------|---------|
| `POST /api/v2/verify` | Gov registry verification (10+ countries) | SECP, FBR, MCA, ACRA, GIB, FTA, Tianyancha, Companies House, Receita Federal, SEC EDGAR, DART |
| `POST /api/v2/verify/lei` | GLEIF LEI corporate hierarchy | GLEIF API |
| `POST /api/v2/media` | Adverse media / news screening | GDELT (65 languages), Bing News*, SerpAPI*, crt.sh, Wayback |
| `POST /api/v2/lookup` | One-shot fan-out (all above in parallel) | All above |
| `GET /api/v2/health` | Per-source health status | All above |

*Bing and SerpAPI pending API key provisioning.

### What v2 does NOT cover (covered elsewhere)

| Capability | Where it lives | Why |
|-----------|---------------|-----|
| Sanctions / PEP / watchlist screening | LexisNexis Bridger (GC) | Bridger covers OFAC, EU, UK, UN, Interpol, PEPs — comprehensive, paid, sub-second |
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
| `POST /api/v1/verify` | v1 verify (still works, v2 is the same endpoint) |
| `POST /tools/adverse_media` | v1 adverse media (v2/media wraps this) |

---

## 2. CONNECTION DETAILS

```
Base URL:   https://crawldevvm:8443/api/v2
            http://20.94.45.219:8400/api/v2    (plain HTTP, internal)
Auth:       X-API-Key header
Content:    application/json (POST body + response)
Timeout:    30s for verify/lei/media, 60s for lookup
Rate limit: 30 requests / 60 seconds per API key
```

GC app connects from 104.209.146.16 — already allowed in NSG (rules 205/206).

---

## 3. ENDPOINTS

### 3.1 Registry Verification

```
POST /api/v2/verify
```

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

**Response:**
```json
{
    "status": "complete",
    "duration_ms": 12918,
    "providers": {
        "GDELT": {"status": "ok", "count": 15, "latency_ms": 8200},
        "BING": {"status": "disabled", "count": 0},
        "SERPAPI": {"status": "disabled", "count": 0},
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
            "source_provider": "GDELT"
        }
    ],
    "shell_signals": { ... }
}
```

---

### 3.4 One-Shot Lookup

```
POST /api/v2/lookup
```

Runs verify + LEI + media in parallel. Returns combined result.
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
    "lookup_time_ms": 15200,
    "registry": {
        "verified": true,
        "legal_name": "삼성전자",
        "status": "ACTIVE",
        "validation_source": { ... }
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
        "top_article": "Samsung reports record Q1 chip revenue — reuters.com"
    },
    "timestamp": "2026-05-09T17:46:19Z"
}
```

**Typical response time:** 10-30 seconds.

---

### 3.5 Health

```
GET /api/v2/health
```

No auth required. Shows per-source status.

**Response:**
```json
{
    "status": "ok",
    "service": "crawl-data-gateway",
    "api_version": "2.0.0",
    "sources": {
        "gateway": { "status": "up", "version": "3.0.0" },
        "verify_vm": { "status": "up", "version": "1.3.0", "countries": [...] },
        "adverse_media": {
            "GDELT": "up",
            "BING": "disabled",
            "CRT_SH": "up",
            "WAYBACK": "up"
        },
        "verify_countries": ["PK","IN","SG","TR","AE","CN","GB","BR","US","KR"]
    }
}
```

---

## 4. ERROR HANDLING

| Code | Meaning |
|------|---------|
| 200 | Success (check `verified`/`found` fields — 200 with `false` means not found, not an error) |
| 400 | Blocked terms detected (data sanitization) |
| 401 | Missing or invalid API key |
| 422 | Validation error (missing required fields, unsupported country) |
| 429 | Rate limited (30 req / 60s) |
| 502 | Upstream source unavailable |

---

## 5. VALIDATION SOURCE

Every response includes:

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

---

## 6. DATA SANITIZATION

- **NEVER** include COPAP name, customer names, supplier names, or internal identifiers
- Gateway HARD FAILS (HTTP 400) on blocked terms — does not silently redact

---

## 7. SAMPLE CLIENT CODE

```python
import requests, os

CRAWL_API = "https://crawldevvm:8443/api/v2"
API_KEY = os.environ["CRAWL_API_KEY"]

def crawl_verify(entity_name, country_code, **kwargs):
    resp = requests.post(f"{CRAWL_API}/verify",
        json={"entity_name": entity_name, "country_code": country_code, **kwargs},
        headers={"X-API-Key": API_KEY}, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()

def crawl_lookup(entity_name, country_code, **kwargs):
    resp = requests.post(f"{CRAWL_API}/lookup",
        json={"entity_name": entity_name, "country_code": country_code, **kwargs},
        headers={"X-API-Key": API_KEY}, timeout=60, verify=False)
    resp.raise_for_status()
    return resp.json()
```

---

## 8. SERVER ROLES (no duplication)

| Server | Role | What it owns |
|--------|------|-------------|
| **crawl-verify** (180.20.0.4) | Gov registry verification | All country registry adapters. One source of truth. |
| **crawl-darkweb** (20.86.161.6) | Dark web / breach / leak | 37 Tor sources. Network-isolated. |
| **5x OpenClaw VMs** | Deep AI research | CIR narratives, product intel, financial analysis |
| **crawldevvm** (20.94.45.219) | Gateway + adverse media | Routes all requests. Runs GDELT/Bing/SerpAPI locally. |
| **GC app** (.11) | Compliance decision engine | Bridger, ICIJ Neo4j, market data, trade data, AI synthesis |

Nothing is duplicated. Each server has one job.
