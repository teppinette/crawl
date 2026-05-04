# Crawl Research Gateway — Build Specification
## Handoff to Global Compliance App Team
### Document: GC-CRG-API-001 | Version 3.0 | 2026-04-19

---

## 1. PURPOSE

This API (Crawl Research Gateway v3.0) provides a scenario-based OSINT research gateway. It supports multiple research scenarios — each with its own payload, routing strategy, and output format. The GC app submits research requests via a generic endpoint, specifying which scenario to run.

**Current Scenarios:**
| Scenario | Purpose | Routing |
|----------|---------|---------|
| `cir` | Counterparty Intelligence Report (DD research) + auto dark-web enrichment | Single region + dark-web VM (auto) |
| `product-intel` | Product market intelligence (pricing, sourcing, competitors) | Fan-out to all regions covering target markets |
| `dark-web` | Standalone dark web OSINT (16 sources via Tor) | Direct to dark-web VM (Netherlands) |

**The GC app is responsible for:**
- Collecting the seed data (Section 3 for CIR, Section 3B for Product Intel)
- Calling the API to submit a research job (Section 4)
- Polling for completion (Section 5)
- Reading the result from blob storage (Section 6)
- Presenting findings to the analyst for review

**The Gateway is responsible for:**
- **Data sanitization** — stripping ALL internal/identifying fields before dispatch
- Routing to the correct regional research agent(s)
- Executing the research via OpenClaw agents
- Producing structured JSON reports
- Uploading to Azure blob storage

**CRITICAL:** The gateway sanitizes ALL payloads before dispatch. Internal fields
(`copap_relationship`, `copap_products`, `source_report`, etc.) are stripped
automatically. The research agents NEVER see who requested the research or why.

---

## 2. ARCHITECTURE OVERVIEW

```
┌──────────────────────┐
│  GC / Production App │
│  (Global Compliance)  │
└──────────┬───────────┘
           │ POST /api/v1/jobs {scenario, payload}
           │ POST /api/v1/research (backward compat)
           ▼
┌──────────────────────┐
│  Crawl Research      │
│  Gateway v3.0        │
│  Port 8400           │
│  DATA SANITIZATION   │
└──────────┬───────────┘
           │ Routes by entity_country
           ▼
┌─────────────────────────────────────────────┐
│          Regional Crawl VMs                  │
│  ┌──────────┐ ┌────────┐ ┌──────┐          │
│  │ Americas │ │ Europe │ │ Gulf │          │
│  └──────────┘ └────────┘ └──────┘          │
│  ┌──────────┐ ┌────────┐                    │
│  │  China   │ │ India  │                    │
│  └──────────┘ └────────┘                    │
└──────────┬──────────────────────────────────┘
           │ Research complete
           ▼
┌──────────────────────┐
│  Azure Blob Storage  │
│  stcrawlosint /      │
│  osint-staging/      │
│  <region>/<file>.json│
└──────────┬───────────┘
           │ GC app reads result
           ▼
┌──────────────────────┐
│  GC Onboarding App   │
│  Analyst Review      │
└──────────────────────┘
```

---

## 3. SEED DATA — WHAT GC MUST CAPTURE

The GC onboarding form must collect the following fields before triggering a CIR. Fields marked **REQUIRED** must be populated; the rest improve research quality.

### 3.1 Entity Fields

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `entity_legal_name` | string | **YES** | Full legal name as registered | "C.J. Shah and Co." |
| `entity_country` | string(2) | **YES** | ISO 3166-1 alpha-2 country code | "IN" |
| `entity_trade_names` | string | No | DBAs, trade names, brand names (comma-separated) | "CJ Shah Group, CJ Shah & Co." |
| `entity_jurisdiction` | string | No | State/province/region | "Maharashtra" |
| `entity_address` | string | No | Registered or HQ address | "18 Akruti Apartment, Kandivali West, Mumbai 400067" |
| `entity_website` | string | No | Company URL | "www.cjshahgroup.in" |
| `entity_type` | string | No | Legal form | "Partnership Firm", "LLC", "A.S.", "GmbH", "Ltd" |
| `entity_industry` | string | No | Sector description | "Specialty Chemicals Distribution" |
| `entity_tax_id` | string | No | Tax ID, registration number, GSTIN, EIN, etc. | "27AAAFC5600N2ZI" |
| `copap_relationship` | string | No | "Customer", "Supplier", or "Both" | "Supplier" |
| `copap_products` | string | No | What products are being traded | "Toluene, Specialty Chemicals" |
| `copap_incoterms` | string | No | Trade terms if known | "CFR", "CIF", "FOB" |
| `source_report` | string | No | Reference to ForenBiz or other source report | "ForenBiz File #2024-0891" |
| `priority` | string | No | "immediate", "high", or "standard" (default: "standard") | "immediate" |
| `workstreams` | string[] | No | Specific workstreams to run. Null = all. | ["1A", "2A", "3A", "8A"] |

### 3.2 Key Individuals

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `key_individuals` | array | No | Directors, UBOs, officers | See below |
| `key_individuals[].name` | string | **YES** (per entry) | Full name | "Apurva Mahesh Shah" |
| `key_individuals[].title` | string | No | Title/Role | "Managing Partner" |

### 3.3 Known Affiliates

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `known_affiliates` | array | No | Subsidiaries, parent entities, related parties | See below |
| `known_affiliates[].entity_name` | string | **YES** (per entry) | Legal name of affiliate | "CJS Specialty Chemicals Pvt Ltd" |
| `known_affiliates[].country` | string | **YES** (per entry) | ISO country code | "IN" |
| `known_affiliates[].relationship` | string | No | Subsidiary, Parent, Affiliate, JV, etc. | "Subsidiary" |

### 3.4 Known Suppliers

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `known_suppliers` | array | No | Known upstream suppliers | See below |
| `known_suppliers[].entity_name` | string | **YES** (per entry) | Legal name | "ALSEERAH TRADING LLC" |
| `known_suppliers[].country` | string | No | ISO country code | "AE" |

### 3.5 Data Quality Guidance for GC Team

**The more seed data provided, the better the research.** Specifically:

- **`entity_tax_id`** — If available, dramatically improves registry lookups (GSTIN for India, EIN for US, EORI for EU, etc.)
- **`key_individuals`** — Enables individual sanctions screening, PEP checks, and adverse media on directors/UBOs
- **`known_affiliates`** — Enables OFAC 50% Rule analysis across the corporate group
- **`entity_trade_names`** — Critical for entities that operate under different names than their legal registration
- **`known_suppliers`** — Enables supply chain sanctions screening

**Minimum viable submission:** `entity_legal_name` + `entity_country` only. Research will still run but may miss affiliates, individuals, and ownership chain details that aren't discoverable from public sources alone.

---

## 4. API INTEGRATION — SUBMITTING A RESEARCH JOB

### 4.1 Connection Details

| Parameter | Value |
|-----------|-------|
| **Base URL** | `http://20.94.45.219:8400` |
| **Protocol** | HTTP (internal network only — not exposed to internet) |
| **Authentication** | API key via `X-API-Key` header |
| **Production API Key** | `cpk_cir_2026Q2_a7f3e9d1b4c8` |
| **Content-Type** | `application/json` |
| **OpenAPI Spec** | `GET /openapi.json` or interactive docs at `GET /docs` |

### 4.2 Submit Research

```
POST /api/v1/research
```

**Headers:**
```
Content-Type: application/json
X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8
```

**Request Body — Full Example:**
```json
{
  "entity_legal_name": "C.J. Shah and Co.",
  "entity_trade_names": "CJ Shah Group, CJ Shah & Co.",
  "entity_country": "IN",
  "entity_jurisdiction": "Maharashtra",
  "entity_address": "18 Akruti Apartment, Kandivali West, Mumbai 400067",
  "entity_website": "www.cjshahgroup.in",
  "entity_type": "Partnership Firm (Private)",
  "entity_industry": "Specialty Chemicals Distribution",
  "entity_tax_id": "27AAAFC5600N2ZI",
  "copap_relationship": "Supplier",
  "copap_products": "Toluene, Specialty Chemicals",
  "copap_incoterms": "CFR",
  "key_individuals": [
    {"name": "Apurva Mahesh Shah", "title": "Managing Partner"},
    {"name": "Ashish Prakash Shah", "title": "Director, CJS Specialty Chemicals"},
    {"name": "Ashit Mahesh Shah", "title": "Director, CJS Specialty Chemicals"}
  ],
  "known_affiliates": [
    {"entity_name": "CJS Specialty Chemicals Pvt Ltd", "country": "IN", "relationship": "Subsidiary"},
    {"entity_name": "Shah CJ World LLP", "country": "IN", "relationship": "Affiliate"}
  ],
  "known_suppliers": [
    {"entity_name": "ALSEERAH TRADING LLC", "country": "AE"}
  ],
  "source_report": "ForenBiz File #2024-0891, dated 2026-03-15",
  "priority": "immediate"
}
```

**Request Body — Minimum Example:**
```json
{
  "entity_legal_name": "Acme Chemical Trading LLC",
  "entity_country": "AE"
}
```

**Response (202 Accepted):**
```json
{
  "job_id": "cab91c35-0719-4ba2-8256-61771f6d5476",
  "status": "queued",
  "region": "india",
  "entity_name": "C.J. Shah and Co.",
  "country": "IN",
  "created_at": "2026-04-13T14:34:43.145789+00:00",
  "updated_at": "2026-04-13T14:34:43.145789+00:00",
  "blob_path": null,
  "report_summary": null,
  "error": null
}
```

**Key fields in response:**
- `job_id` — Use this to poll for status
- `status` — "queued" → "running" → "completed" or "failed"
- `region` — Which regional agent is handling the research
- `blob_path` — Populated when complete; path to the JSON result in Azure blob

### 4.3 Error Responses

| Status Code | Meaning | Example |
|-------------|---------|---------|
| 403 | Invalid API key | `{"detail": "Invalid API key"}` |
| 422 | Validation error (missing required fields) | `{"detail": [{"loc": ["body", "entity_legal_name"], "msg": "Field required"}]}` |
| 404 | Job not found | `{"detail": "Job <id> not found"}` |

---

## 5. POLLING FOR COMPLETION

### 5.1 Check Job Status

```
GET /api/v1/research/{job_id}
X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8
```

**Response when running:**
```json
{
  "job_id": "cab91c35-...",
  "status": "running",
  "region": "india",
  "entity_name": "C.J. Shah and Co.",
  "country": "IN",
  "created_at": "2026-04-13T14:34:43Z",
  "updated_at": "2026-04-13T14:35:12Z",
  "blob_path": null,
  "report_summary": null,
  "error": null
}
```

**Response when completed:**
```json
{
  "job_id": "cab91c35-...",
  "status": "completed",
  "region": "india",
  "entity_name": "C.J. Shah and Co.",
  "country": "IN",
  "created_at": "2026-04-13T14:34:43Z",
  "updated_at": "2026-04-13T14:40:16Z",
  "blob_path": "osint-staging/india/cj_shah_and_co_in_20260413.json",
  "report_summary": "DONE: C.J. Shah and Co., India — Risk: 5 BLOCK — OFAC SDN #55874...",
  "error": null
}
```

### 5.2 Recommended Polling Strategy

```
Poll every 30 seconds for up to 20 minutes.
Typical research completes in 5-15 minutes.

if status == "completed" → read blob_path
if status == "failed"    → check error field, alert analyst
if status == "running" after 20 min → alert analyst (timeout)
```

### 5.3 List All Jobs

```
GET /api/v1/research?limit=50
X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8
```

Returns array of job objects, sorted most recent first.

---

## 6. READING RESULTS FROM BLOB STORAGE

### 6.1 Blob Storage Details

| Parameter | Value |
|-----------|-------|
| **Storage Account** | `stcrawlosint` |
| **Container** | `osint-staging` |
| **Blob Path Pattern** | `<region>/<entity_snake_case>_<country>_<YYYYMMDD>.json` |
| **Auth** | Azure AD (Managed Identity or Service Principal) |
| **Azure Subscription** | COPAPCrawl (6184b4c7-7866-4d10-ab6d-427b698b3345) |

### 6.2 How to Read the Blob

The `blob_path` returned by the status endpoint gives you the exact path. Example:

```
osint-staging/india/cj_shah_and_co_in_20260413.json
```

**Using Azure SDK (C# — likely for GC app):**
```csharp
var blobClient = new BlobClient(
    new Uri($"https://stcrawlosint.blob.core.windows.net/osint-staging/{blobPath}"),
    new DefaultAzureCredential()
);
var response = await blobClient.DownloadContentAsync();
var report = JsonSerializer.Deserialize<CirReport>(response.Value.Content);
```

**Using Azure SDK (Python):**
```python
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential

blob_service = BlobServiceClient(
    account_url="https://stcrawlosint.blob.core.windows.net",
    credential=DefaultAzureCredential()
)
blob_client = blob_service.get_blob_client("osint-staging", blob_path)
report = json.loads(blob_client.download_blob().readall())
```

### 6.3 Result JSON Schema

The blob contains a structured JSON report. Key fields:

```json
{
  "counterparty_name": "C.J. Shah and Co.",
  "country": "India",
  "country_code": "IN",
  "research_date": "2026-04-13",
  "research_region": "india",

  "corporate_registry": {
    "registration_number": "...",
    "status": "Active | Dissolved | Struck Off",
    "incorporation_date": "1961-03-26",
    "registered_address": "...",
    "legal_form": "Partnership Firm",
    "directors": [
      {"name": "...", "role": "...", "id_number": "DIN/TIN", "appointment_date": "..."}
    ],
    "shareholders": [
      {"name": "...", "ownership_pct": 92.83, "type": "individual|corporate"}
    ],
    "source_url": "https://...",
    "source_date": "2026-04-13"
  },

  "beneficial_ownership": {
    "ubo_chain": [
      {"level": 1, "entity": "...", "ownership_pct": 0, "country": "IN", "type": "individual"}
    ],
    "opaque_structures": true,
    "nominee_detected": false
  },

  "sanctions_screening": {
    "direct_hits": [
      {"list": "OFAC SDN", "matched_name": "C.J. SHAH AND CO.", "entry_id": "55874", "designation_date": "2025-10-09"}
    ],
    "director_hits": [],
    "ubo_hits": [],
    "fifty_pct_rule_flags": [
      {"entity": "CJS Specialty Chemicals Pvt Ltd", "probable_ownership": "92.83%", "status": "FLAG"}
    ]
  },

  "adverse_media": [
    {"headline": "...", "source": "...", "date": "...", "url": "...", "category": "SANCTIONS", "relevance": "HIGH"}
  ],

  "litigation": [
    {"court": "CESTAT Mumbai", "case_number": "86216/2022", "type": "Customs", "status": "Decided", "summary": "..."}
  ],

  "pep_connections": [],

  "trade_risk": {
    "sanctioned_country_exposure": ["Iran"],
    "transshipment_flags": ["UAE front entities (ALSEERAH TRADING LLC, Kriscon DMCC)"],
    "dual_use_indicators": ["Toluene (HS 2902.30) — CWC Schedule 2 precursor"]
  },

  "risk_assessment": "BLOCK\n\nDARK WEB RISK (CRITICAL): 20 findings detected. Immediate review recommended.",
  "risk_score": 5,
  "risk_rationale": "Direct OFAC SDN hit under EO 13846...",

  "dark_web_screening": {
    "risk_level": "CRITICAL",
    "total_findings": 20,
    "sources_searched": 32,
    "sources_with_hits": 3,
    "key_findings": [
      "CREDENTIAL COMPROMISE: company-domain.com",
      "LEAKED DOCUMENT: PAK/PAKISTAN/SOUTH ASIA",
      "LEAKED DOCUMENT: [TACTICAL] ammonium nitrate"
    ],
    "breakdown": {
      "credential_compromise": 1,
      "ransomware_victim": 0,
      "dark_web_mentions": 0,
      "sanctions_pep_hits": 0,
      "occrp_hits": 0,
      "offshore_entities": 0,
      "leaked_documents": 9,
      "paste_dumps": 0,
      "adverse_media": 0,
      "web_mentions": 10
    },
    "sources_checked": [
      "Ahmia (.onion search)", "Torch (.onion search)",
      "DuckDuckGo via Tor", "DuckDuckGo adverse keywords",
      "IntelligenceX (leak archive)", "Psbdmp (paste dumps)",
      "LeakIX (exposed services)", "HudsonRock (infostealer DB)",
      "Ransomlook (ransomware victims)", "OCCRP Aleph",
      "ICIJ Offshore Leaks", "OpenSanctions",
      "WikiLeaks", "Telegram channels",
      "Web Archive", "Court records (Tor-routed)"
    ]
  },

  "dark_web_intelligence": {
    "status": "completed",
    "sources_searched": 32,
    "total_findings": 20,
    "findings": [
      {"source": "hudsonrock_cavalier", "type": "infostealer_exposure", "domain": "company.com", "total_stealers": 3},
      {"source": "wikileaks", "type": "leaked_document", "title": "Cable reference...", "url": "..."},
      {"source": "duckduckgo_tor", "type": "web_mention", "title": "Company profile...", "url": "..."}
    ]
  },

  "sources": [
    {"name": "OFAC SDN", "url": "https://sanctionssearch.ofac.treas.gov/...", "data_quality": "HIGH"}
  ]
}
```

### 6.4 Risk Score Interpretation

| Score | Label | GC App Action |
|-------|-------|--------------|
| **5** | **BLOCK** | Auto-reject onboarding. Flag for compliance officer review. Do not proceed. |
| **4** | **REVIEW** | Escalate to senior compliance. Enhanced due diligence required before approval. |
| **3** | **MONITOR** | Approve with conditions. Set up periodic re-screening (quarterly). |
| **2** | **CLEAR** | Standard onboarding. Annual re-screening. |
| **1** | **CLEAR** | Low-risk. Standard onboarding. Annual re-screening. |

---

## 7. REGION ROUTING REFERENCE

The API automatically routes to the correct regional agent based on `entity_country`. The GC app does NOT need to specify the region.

| Region | Countries | Capabilities |
|--------|-----------|-------------|
| **americas** | US, CA, CO, BR, MX, CL, PE, AR | SEC EDGAR, PACER, state registries, OFAC primary |
| **europe** | TR, RU, BY, RS, NG, UA, BG, DE, NL, GB, FR, IT, ES, CH, SE, NO | MERSIS, Companies House, Handelsregister, EGRUL |
| **gulf** | AE, EG, PK, IQ, SA, QA, BH, KW, OM, JO | DED, JAFZA, DMCC, free zone registries, Iran evasion patterns |
| **china** | CN, HK, VN, MM, TW, KR, JP, SG, TH, MY, PH, ID | Qichacha, Tianyancha, NECIPS, VIE analysis, UFLPA |
| **india** | IN | MCA21, ROC, GST, SEBI, IndianKanoon courts, Sarvam AI (Hindi/Marathi) |

**Fallback:** Countries not in the map default to **americas**.

**To add a new country:** Request update to the jurisdiction map. No app changes needed — only the API config changes.

---

## 8. WORKSTREAM REFERENCE

If the GC app wants to run specific workstreams instead of all, pass the `workstreams` array:

| Code | Workstream | What it does |
|------|-----------|-------------|
| `1A` | Entity Verification | Corporate registry lookup, directors, shareholders |
| `1B` | Domestic Affiliates | Verify affiliates in same country |
| `1C` | International Subsidiaries | Verify affiliates in other countries |
| `2A` | UBO & Key Individuals | Individual screening, PEP, adverse media per person |
| `2B` | Ownership Chain | Full UBO chain mapping, 50% Rule analysis |
| `3A` | Sanctions Screening | OFAC, UN, EU, UK, Canada, BIS, World Bank |
| `3B` | OFAC 50% Rule | Indirect sanctions exposure through ownership |
| `4A` | Litigation & Enforcement | Court records, regulatory actions |
| `4B` | Regulatory Compliance | Licenses, certifications, industry memberships |
| `5A` | Adverse Media | News screening in English + local language |
| `5B` | High-Risk Trade Nexus | Trade partner investigation in sanctioned jurisdictions |
| `6A` | Trade Pattern Analysis | Import/export data, HS codes, trade partners |
| `6B` | Financial Standing | Credit ratings, bank exposure, financial health |
| `7A` | Supplier/Customer Verification | Key trade partner profiles |
| `8A` | Risk Assessment | Consolidated risk scoring and onboarding recommendation |

**Default:** All workstreams run if `workstreams` is null or omitted.

**Fast screening:** `["3A", "5A", "8A"]` — sanctions + media + risk score only (~3 min).

**Full DD:** `null` (all workstreams) — ~10-15 min.

---

## 9. TIMING EXPECTATIONS

| Priority | Typical Duration | Use Case |
|----------|-----------------|----------|
| `immediate` | 5-10 min | Known high-risk counterparty, urgent onboarding |
| `high` | 10-15 min | Standard new counterparty DD |
| `standard` | 10-15 min | Periodic re-screening, batch processing |

**Note:** Duration depends on data availability in the counterparty's jurisdiction. India and China entities with court records may take longer due to registry crawling depth.

---

## 10. GC APP INTEGRATION CHECKLIST

### Before Go-Live

- [ ] **API Key** — Obtain production API key from infrastructure team
- [ ] **Network Access** — Confirm GC app server can reach dev VM on port 8400 (internal network)
- [ ] **Azure Blob Access** — GC app service principal must have `Storage Blob Data Reader` role on `stcrawlosint` storage account in COPAPCrawl subscription
- [ ] **Onboarding Form** — Add seed data fields per Section 3 to the GC onboarding form
- [ ] **Polling Logic** — Implement 30-second polling with 20-minute timeout per Section 5.2
- [ ] **Result Parser** — Parse JSON from blob per schema in Section 6.3
- [ ] **Risk Display** — Map risk_score (1-5) to UI indicators per Section 6.4
- [ ] **Error Handling** — Handle `failed` status, display `error` field to analyst
- [ ] **Audit Trail** — Log job_id, entity_name, submission time, completion time, risk_score

### GC-ONB-001 Document Checklist Integration

When `risk_assessment` = "BLOCK" or "REVIEW", the GC app should auto-populate the GC-ONB-001 checklist flags:

| JSON Field | GC-ONB-001 Requirement |
|------------|----------------------|
| `sanctions_screening.direct_hits` not empty | Block onboarding immediately |
| `sanctions_screening.fifty_pct_rule_flags` not empty | Escalate for OFAC 50% Rule review |
| `beneficial_ownership.opaque_structures` = true | Request UBO Declaration from counterparty |
| `pep_connections` not empty | Request PEP disclosure and EDD |
| `trade_risk.sanctioned_country_exposure` not empty | Request sanctions compliance attestation |
| `dark_web_screening.risk_level` = "CRITICAL" or "HIGH" | Escalate to compliance officer. Display dark web alert banner. |
| `dark_web_screening.breakdown.credential_compromise` > 0 | Flag: company credentials found in infostealer databases |
| `dark_web_screening.breakdown.ransomware_victim` > 0 | Flag: entity appeared on ransomware group victim list |
| `dark_web_screening.breakdown.dark_web_mentions` > 0 | Flag: entity found on dark web (.onion) sites |
| `dark_web_screening.breakdown.offshore_entities` > 0 | Flag: entity found in ICIJ Offshore Leaks (Panama/Pandora Papers) |
| `dark_web_screening.breakdown.occrp_hits` > 0 | Flag: entity found in OCCRP organized crime database |
| `risk_score` >= 4 | Require senior compliance officer sign-off |

### Dark Web Screening — Display Guidance

Every CIR blob now includes a `dark_web_screening` section (auto-enriched via Tor from 16 sources). The GC onboarding UI should display this prominently:

**API Response (polling `GET /api/v1/jobs/{job_id}`):**
```json
{
  "dark_web": {
    "alert": "CRITICAL",
    "findings_count": 20,
    "sources_searched": 32,
    "status": "completed",
    "note": "Dark web scan: 20 findings across 32 sources."
  }
}
```

**Blob JSON (3 locations to read from):**

1. **`dark_web_screening`** — Structured summary for UI display:
   - `risk_level`: CLEAN / LOW / MEDIUM / HIGH / CRITICAL
   - `key_findings[]`: Human-readable list of top hits (show first 5)
   - `breakdown`: Counts by category (show non-zero counts as badges)
   - `sources_checked[]`: All 16 source names (show as "16 sources checked")

2. **`executive_summary`** — Contains dark web risk line appended (if dict, check `dark_web_screening` and `dark_web_risk` keys)

3. **`dark_web_intelligence`** — Raw findings array for analyst drill-down

**Recommended UI rendering:**

```
┌─────────────────────────────────────────────────┐
│ DARK WEB SCREENING: CRITICAL                    │
│ 20 findings from 3/32 sources via Tor           │
│                                                  │
│ ● CREDENTIAL COMPROMISE: topsungroup.pk         │
│ ● LEAKED DOCUMENT: [TACTICAL] ammonium nitrate  │
│ ● LEAKED DOCUMENT: PAK/PAKISTAN cables (x9)     │
│                                                  │
│ [View all 20 findings]                          │
└─────────────────────────────────────────────────┘
```

**Dark web risk level colors:**
| Level | Color | Action |
|-------|-------|--------|
| CLEAN | Green | No action |
| LOW | Blue | Note in file |
| MEDIUM | Yellow | Review recommended |
| HIGH | Orange | Escalate to compliance |
| CRITICAL | Red | Immediate compliance review, block if combined with other flags |

**The `report_summary` field** in the API response also includes a markdown-formatted dark web section at the end (after `---`), suitable for rendering in a markdown viewer.

---

## 11. SECURITY & ISOLATION

| Concern | Mitigation |
|---------|-----------|
| **No production data flows to crawl VMs** | API sends only entity name + country + public identifiers. No EntityIDs, financials, or internal data. |
| **Research results are staged** | All results land in `osint-staging` blob. Human analyst review required before any data enters production ComplianceEntity. |
| **API authentication** | API key required on all endpoints. Keys rotated quarterly. |
| **Network isolation** | CIR API on internal network only. Not internet-facing. NSG restricts to GC app server IP. |
| **Crawl VM isolation** | No VNet peering to production. SSH locked to COPAP VPN. Auto-shutdown at 23:00 UTC. |
| **No credential leakage** | Crawl VMs have no production DB credentials. `seed_entities.py` runs only on dev VM. |

---

## 12. SUPPORT & ESCALATION

| Issue | Contact | Resolution |
|-------|---------|-----------|
| API unreachable | Infrastructure team | Check dev VM status, systemd service |
| Job stuck in "running" > 20 min | Infrastructure team | SSH to regional VM, check OpenClaw gateway |
| Blob not readable | Infrastructure team | Verify `az login` on crawl VM, check blob permissions |
| Research quality issue | Compliance team | Review seed data quality, check if workstreams need tuning |
| New country needed | Infrastructure team | Add to jurisdiction map in API config |
| API key rotation | Infrastructure team | Update key in API env and GC app config |

---

## APPENDIX A: OPENAPI SPECIFICATION

Full OpenAPI 3.1 spec available at:
- **Interactive docs:** `http://20.94.45.219:8400/docs`
- **JSON spec:** `http://20.94.45.219:8400/openapi.json`
- **Local copy:** `/home/copapadmin/crawl/api/openapi.json`

---

## APPENDIX B: EXAMPLE — COMPLETE FLOW

### Step 1: GC Onboarding Analyst enters counterparty data

Analyst fills in: "C.J. Shah and Co.", India, address in Mumbai, GSTIN, 3 directors, 2 affiliates.

### Step 2: GC App submits to API

```bash
curl -X POST http://20.94.45.219:8400/api/v1/research \
  -H "Content-Type: application/json" \
  -H "X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8" \
  -d '{
    "entity_legal_name": "C.J. Shah and Co.",
    "entity_country": "IN",
    "entity_address": "18 Akruti Apartment, Kandivali West, Mumbai 400067",
    "entity_tax_id": "27AAAFC5600N2ZI",
    "key_individuals": [
      {"name": "Apurva Mahesh Shah", "title": "Managing Partner"}
    ]
  }'
```

**Response:** `{"job_id": "cab91c35-...", "status": "queued", "region": "india"}`

### Step 3: GC App polls for status

```bash
curl http://20.94.45.219:8400/api/v1/research/cab91c35-... -H "X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8"
```

After ~6 minutes: `{"status": "completed", "blob_path": "osint-staging/india/cj_shah_and_co_in_20260413.json"}`

### Step 4: GC App reads blob

```python
report = read_blob("osint-staging/india/cj_shah_and_co_in_20260413.json")
```

### Step 5: GC App displays to analyst

```
COUNTERPARTY: C.J. Shah and Co. (India)
RISK SCORE: 5 / 5 — BLOCK

⛔ SANCTIONS HIT: OFAC SDN #55874 (Iran/EO13846)
   Designated 2025-10-09 for $44M in Iranian petrochemical imports

⛔ 50% RULE FLAG: CJS Specialty Chemicals Pvt Ltd (92.83% promoter-held)

⚠ LITIGATION: CESTAT Mumbai — prior Iranian toluene misdeclaration (2020)

🔴 DARK WEB SCREENING: CRITICAL (20 findings / 32 sources)
   ● CREDENTIAL COMPROMISE: cjshah.com (infostealer database)
   ● LEAKED DOCUMENTS: 9 WikiLeaks cable references
   ● WEB MENTIONS: 10 Tor-routed search results

RECOMMENDATION: DO NOT ONBOARD. Compliance officer review required.
```

### Step 6: Analyst reviews and takes action

Analyst confirms BLOCK, adds to COPAP sanctions watchlist, documents in GC-ONB-001.

---

---

## 13. SCENARIO: PRODUCT INTELLIGENCE

### 13.1 Purpose

The `product-intel` scenario allows the GC app (or any production app) to request
market intelligence on specific products across target markets. Unlike CIR (which
routes to a single region), product-intel **fans out** to ALL regions that cover the
requested markets simultaneously.

### 13.2 Submit via Generic Endpoint

```
POST /api/v1/jobs
```

**Headers:**
```
Content-Type: application/json
X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8
```

**Request Body — Full Example (new contract):**
```json
{
  "scenario": "product-intel",
  "payload": {
    "request_id": "pi-2026-0419-lab-gulf",
    "product": {
      "generic_name": "Linear Alkyl Benzene",
      "grade_code": "C10-C13",
      "commodity_family": "surfactants"
    },
    "region_hint": "gulf",
    "target_markets": ["AE", "IN", "TR"],
    "lookback_days": 30,
    "signal_types": ["news", "price_index", "freight", "supply_disruption", "geopolitical"],
    "known_producers": ["Sasol", "CEPSA", "Indian Oil"],
    "specific_questions": [
      "Current CFR pricing for LAB in Jebel Ali",
      "New LAB capacity coming online in India 2026-2027"
    ]
  }
}
```

**Request Body — Minimum Example:**
```json
{
  "scenario": "product-intel",
  "payload": {
    "product": {
      "generic_name": "Caustic Soda Flakes"
    },
    "target_markets": ["IN", "AE"]
  }
}
```

**Request Body — Using region_hint instead of target_markets:**
```json
{
  "scenario": "product-intel",
  "payload": {
    "product": {
      "generic_name": "Methanol",
      "commodity_family": "alcohols"
    },
    "region_hint": "gulf",
    "lookback_days": 90,
    "signal_types": ["price_index", "freight"]
  }
}
```

### 13.3 Product Intel Payload Fields

| Field | Type | Required | Description | Example |
|-------|------|----------|-------------|---------|
| `request_id` | string | No | Idempotency key — resubmit returns existing job | "pi-2026-0419-lab-gulf" |
| `product.generic_name` | string | **YES** | Product common name | "Linear Alkyl Benzene" |
| `product.grade_code` | string | No | Grade/spec code | "C10-C13", "96%" |
| `product.commodity_family` | string | No | Commodity family | "surfactants", "aromatics" |
| `target_markets` | string[] | **YES*** | ISO 2-letter country codes | ["AE", "IN", "TR"] |
| `region_hint` | string | **YES*** | Alternative to target_markets | "gulf", "asia", "europe" |
| `lookback_days` | int | No | Lookback period in days (default: 30) | 30, 90, 365 |
| `signal_types` | string[] | No | Signal types to collect (default: all) | ["news", "price_index"] |
| `known_producers` | string[] | No | Known producers to track | ["Sasol", "CEPSA"] |
| `specific_questions` | string[] | No | Specific research questions | ["CFR pricing in Jebel Ali"] |
| `priority` | string | No | INTERNAL: stripped before dispatch | "standard" |

*Either `target_markets` OR `region_hint` is required (not both needed).

**Valid region_hint values:** gulf, asia, europe, americas, india, china, mena

**Valid signal_types:** news, price_index, freight, supply_disruption, geopolitical

### 13.4 Response

**Response (202 Accepted):**
```json
{
  "job_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "scenario": "product-intel",
  "status": "queued",
  "region": null,
  "regions": ["gulf", "india", "europe"],
  "entity_name": null,
  "country": null,
  "created_at": "2026-04-19T10:30:00Z",
  "updated_at": "2026-04-19T10:30:00Z",
  "blob_path": null,
  "blob_paths": [],
  "report_summary": null,
  "error": null
}
```

**Key differences from CIR response:**
- `scenario` field is `"product-intel"` (CIR jobs say `"cir"`)
- `regions` (plural) — list of regions being researched simultaneously
- `blob_paths` (plural) — one blob per region when complete
- `region` / `blob_path` (singular) are null for fan-out jobs

**Completed response:**
```json
{
  "job_id": "f47ac10b-...",
  "scenario": "product-intel",
  "status": "completed",
  "regions": ["gulf", "india", "europe"],
  "blob_paths": [
    "osint-staging/product-intel/gulf/linear_alkyl_benzene_20260419.json",
    "osint-staging/product-intel/india/linear_alkyl_benzene_20260419.json",
    "osint-staging/product-intel/europe/linear_alkyl_benzene_20260419.json"
  ],
  "report_summary": "DONE: Linear Alkyl Benzene, AE/IN/TR — CFR Jebel Ali $1,180-1,220/MT..."
}
```

### 13.5 Polling

Same as CIR (Section 5). Use `GET /api/v1/jobs/{job_id}` to poll.

```
GET /api/v1/jobs/{job_id}
X-API-Key: cpk_cir_2026Q2_a7f3e9d1b4c8
```

**Timing:** Product intel typically completes in 5-10 minutes per region.
Fan-out runs in parallel so total time ~= slowest region.

### 13.6 Product Intel Result JSON Schema (Signals Array)

Each region produces a separate blob. The blob contains a **signals array** — one entry
per data point found, typed by signal_type. Plus a **coverage_score** indicating what
percentage of requested signal types returned data.

```json
{
  "product_name": "Linear Alkyl Benzene",
  "grade_code": "C10-C13",
  "commodity_family": "surfactants",
  "target_markets": ["AE"],
  "research_date": "2026-04-19",
  "research_region": "gulf",
  "coverage_score": 80,

  "signals": [
    {
      "type": "news",
      "headline": "LAB prices firm in Gulf on tight supply",
      "sentiment": "positive",
      "source": "ICIS News",
      "date": "2026-04-17",
      "url": "https://www.icis.com/...",
      "summary": "Spot LAB prices rose $10/MT WoW in Jebel Ali..."
    },
    {
      "type": "news",
      "headline": "India considers export duty on LAB to protect domestic detergent makers",
      "sentiment": "negative",
      "source": "Business Standard",
      "date": "2026-04-14",
      "url": "https://www.business-standard.com/...",
      "summary": "Government committee reviewing 5% export levy proposal..."
    },
    {
      "type": "price_index",
      "market": "AE",
      "price_low": 1180,
      "price_high": 1220,
      "currency": "USD",
      "unit": "MT",
      "basis": "CFR",
      "port": "Jebel Ali",
      "date": "2026-04-15",
      "source": "ICIS"
    },
    {
      "type": "price_index",
      "market": "AE",
      "price_low": 1150,
      "price_high": 1190,
      "currency": "USD",
      "unit": "MT",
      "basis": "CFR",
      "port": "Jebel Ali",
      "date": "2026-03-15",
      "source": "ICIS"
    },
    {
      "type": "freight",
      "route": "India-UAE (Nhava Sheva to Jebel Ali)",
      "rate": "$45/MT",
      "mode": "container",
      "transit_days": 7,
      "date": "2026-04-18",
      "source": "Freightos"
    },
    {
      "type": "supply_disruption",
      "producer": "HPCL",
      "country": "IN",
      "event": "Planned turnaround at Vizag refinery LAB unit",
      "duration": "45 days (May-Jun 2026)",
      "capacity_impact": "120,000 MT/yr offline",
      "date": "2026-04-10"
    },
    {
      "type": "geopolitical",
      "country": "IN",
      "policy": "Proposed 5% export duty on LAB (HS 3817.00)",
      "effective_date": "Under review — expected decision Q3 2026",
      "impact": "Would raise India-origin CFR Gulf by ~$60/MT",
      "source": "Ministry of Commerce draft circular"
    }
  ],

  "sources": [
    {
      "name": "ICIS Pricing",
      "url": "https://www.icis.com/...",
      "accessed_date": "2026-04-19",
      "data_quality": "HIGH"
    }
  ],
  "research_notes": ""
}
```

**Coverage Score Calculation:**
```
coverage_score = (signal_types_with_data / total_requested_signal_types) * 100
```
Example: Requested `["news", "price_index", "freight", "supply_disruption", "geopolitical"]`
(5 types). Got data for 4 of them. `coverage_score = 80`.

**Productintel-side caching:** The productintel app caches results for 6 hours
on the key `(grade_code, region, lookback_days)`. The gateway itself does not cache —
each call dispatches fresh research. Use `request_id` for idempotency instead.

### 13.7 Merging Multi-Region Results

For fan-out jobs, the GC app receives multiple blobs (one per region). To present
a unified view:

```python
# Pseudocode for merging product-intel results
blobs = [read_blob(path) for path in job["blob_paths"]]

merged = {
    "product_name": blobs[0]["product_name"],
    "all_markets": [],
    "pricing": {"spot_prices": []},
    "sourcing": {"producers": [], "new_capacity": []},
    "competitors": [],
    "regulatory": {"import_duties": []},
}

for blob in blobs:
    merged["all_markets"].extend(blob["target_markets"])
    merged["pricing"]["spot_prices"].extend(blob["pricing"]["spot_prices"])
    merged["sourcing"]["producers"].extend(blob["sourcing"]["producers"])
    merged["sourcing"]["new_capacity"].extend(blob["sourcing"]["new_capacity"])
    merged["competitors"].extend(blob["competitors"])
    merged["regulatory"]["import_duties"].extend(blob["regulatory"]["import_duties"])
```

### 13.8 Intel Type Reference

| intel_type | What it researches | Typical Duration |
|------------|-------------------|-----------------|
| `pricing` | Spot/contract prices, trends, freight rates | 3-5 min |
| `sourcing` | Producers, capacity, trade flows, lead times | 5-8 min |
| `competitors` | Traders, distributors, market structure | 5-8 min |
| `regulatory` | Duties, restrictions, registrations, anti-dumping | 3-5 min |
| `all` | All of the above | 8-15 min |

### 13.9 CIR via Generic Endpoint

CIR can also be submitted via the generic endpoint (same payload as `/api/v1/research`):

```json
{
  "scenario": "cir",
  "payload": {
    "entity_legal_name": "Acme Chemical Trading LLC",
    "entity_country": "AE",
    "entity_industry": "Chemical Distribution",
    "key_individuals": [
      {"name": "John Smith", "title": "Director"}
    ]
  }
}
```

The old `POST /api/v1/research` endpoint continues to work unchanged.

---

## 14. AVAILABLE SCENARIOS ENDPOINT

```
GET /api/v1/scenarios
```

Returns the list of available scenarios and their configurations:

```json
{
  "cir": {
    "name": "Counterparty Intelligence Report",
    "description": "Due diligence research on a counterparty entity",
    "routing": "single"
  },
  "product-intel": {
    "name": "Product Market Intelligence",
    "description": "Product pricing, sourcing, competitor, and regulatory intelligence",
    "routing": "fanout"
  }
}
```

New scenarios will be added to this list as they're developed.

---

---

## 12. ENTITY VERIFICATION JOB (NEW — 2026-05-03)

### 12.1 Overview

A new async verification endpoint that runs **4 parallel checks** against a
counterparty and its declared directors/UBOs. This is designed for the
**onboarding flow** — trigger it as soon as entity name + country are entered,
and poll for results while the user continues filling in the form.

| Check | What It Does | Time |
|-------|-------------|------|
| Government Registry | Confirms entity in SECP (PK) / MCA (IN) | 8-12s |
| LinkedIn Company | Finds & scrapes entity's LinkedIn page | 5-15s |
| LinkedIn Persons | Validates each director/UBO on LinkedIn — yes/no per person | 1-3 min |
| Dark Web | Scans 37 sources for breaches, adverse mentions | 30-60s |

**Total time: 1-5 minutes.** Results appear progressively as each check completes.

### 12.2 Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/verify-job` | POST | Submit verification job (returns immediately) |
| `/api/v1/verify-job/{job_id}` | GET | Poll for progressive results |
| `/api/v1/verify-jobs` | GET | List recent verify jobs |

Same `X-API-Key` auth as all other endpoints.

### 12.3 Request

```json
POST /api/v1/verify-job
{
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "ntn": "4334750-9",
    "persons": ["Muhammad Ali Khan", "Rashid Ahmed"],
    "domain": "agrochina.pk"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `entity_name` | **Yes** | Legal name of entity |
| `country_code` | **Yes** | ISO 2-letter: PK, IN, AE, TR, CN |
| `persons` | No | Director/UBO names to validate on LinkedIn |
| `domain` | No | Company domain (improves dark web + LinkedIn search) |
| `ntn` | No | Pakistan NTN for tax check |
| `cin` | No | India CIN for direct MCA lookup |
| `linkedin_url` | No | Company LinkedIn URL if known |

### 12.4 Response (Poll)

```json
GET /api/v1/verify-job/{job_id}

{
    "job_id": "...",
    "status": "completed",
    "checks": {
        "registry": {
            "status": "completed",
            "verified": true,
            "legal_name": "AGROCHINA PAKISTAN (PVT.) LIMITED",
            "registration_number": "0089758",
            "entity_status": "Incorporated",
            "source": "SECP eServices (direct gov query)"
        },
        "linkedin_company": {
            "status": "completed",
            "found": true,
            "name": "AgroChina Pakistan",
            "industry": "International Trade",
            "company_size": "51-200 employees",
            "employees_on_linkedin": 47
        },
        "linkedin_persons": {
            "status": "completed",
            "confirmed_count": 1,
            "total_searched": 2,
            "persons_verification": [
                {"name": "Muhammad Ali Khan", "works_at_entity": true, "title": "CEO"},
                {"name": "Rashid Ahmed", "works_at_entity": false, "note": "Not found at Agro China Pakistan on LinkedIn"}
            ]
        },
        "dark_web": {
            "status": "completed",
            "risk_level": "LOW",
            "total_findings": 3,
            "sources_searched": 37
        }
    }
}
```

### 12.5 Person Verification — Yes/No

The `persons_verification` array gives a simple answer per person:

| Field | Description |
|-------|-------------|
| `name` | The name you submitted |
| `works_at_entity` | **true** = confirmed on LinkedIn at this company. **false** = not confirmed |
| `title` | Their LinkedIn title (if confirmed) |
| `linkedin_url` | Profile link (if confirmed) |
| `note` | Explanation (if not confirmed) |

**Logic:** We search LinkedIn for the person + company. If we find a profile
that actually lists this entity in their employment → `true`. If the profile
is a different person at a different company (common names) → `false`.
We never show random matches from other companies.

### 12.6 Risk Levels (Dark Web)

| Level | Meaning | Action |
|-------|---------|--------|
| `CLEAN` | 0 findings | Proceed |
| `LOW` | 1-5 findings | Review, likely OK |
| `MEDIUM` | 6-15 findings | Manual review required |
| `HIGH` | 16+ findings | Escalate |
| `CRITICAL` | Breach/infostealer/sanctions hit | Block until reviewed |

### 12.7 Integration Guidance for GC/Onboarding

**When to trigger:**
- As soon as entity_name + country_code are entered (don't wait for full form)
- Add persons/domain/NTN as they become available (submit a new job if needed)

**How to show results:**
- Registry: green checkmark if verified=true
- LinkedIn persons: per-person checkmarks (true) or warning icons (false)
- Dark web: color-coded badge (green/yellow/orange/red)

**What happens if a check fails:**
- Job status becomes `partial_success`
- Other completed checks still have valid results — use them
- Failed check shows `"status": "failed"` with error message

**Full spec with code examples:** See `VERIFY_JOB_SPEC.md` in the same directory.

---

*Document updated: 2026-05-03*
*API Version: 3.1.0*
*Contact: Infrastructure team for API access and keys*
