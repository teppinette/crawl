# Entity Verification Job API — GC & Onboarding Integration Guide

**Endpoint:** `POST /api/v1/verify-job`
**Host:** `crawldevvm:8400` (20.94.45.219:8400) | TLS: `crawldevvm:8443`
**Auth:** `X-API-Key: <CIR_API_KEY>` (same key used for CIR jobs)
**Response time:** Returns job_id immediately; full results in 1-5 minutes
**Last updated:** 2026-05-03

---

## Overview

The Verification Job is a comprehensive, async background check that runs
**four parallel verification channels** against a counterparty and its
declared directors/UBOs. Unlike the synchronous `/api/v1/verify` endpoint
(which only checks government registries), this job runs:

1. **Government Registry** — confirms entity exists in SECP (PK) / MCA (IN)
2. **LinkedIn Company** — finds and scrapes the company's LinkedIn page
3. **LinkedIn Persons** — validates each declared director/UBO on LinkedIn
4. **Dark Web Scan** — searches 37 sources for breach data, adverse mentions

Results build up progressively. GC/onboarding polls until all checks complete.

---

## When to Use This

**Trigger during onboarding** once the user has entered:
- Entity name + country
- At least one director/UBO name (optional but recommended)
- Company domain (optional but improves dark web + LinkedIn accuracy)

The job runs in the background while the user continues filling in onboarding
fields. By the time they finish the form, results are ready for review.

**Do NOT wait for this to complete before allowing the user to proceed.**
This is a background enrichment — the user sees results when they're ready.

---

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/verify-job` | POST | Submit verification job (returns immediately) |
| `/api/v1/verify-job/{job_id}` | GET | Poll for progressive results |
| `/api/v1/verify-jobs` | GET | List recent verify jobs (filter, paginate) |

---

## Request Format

```json
POST /api/v1/verify-job
Content-Type: application/json
X-API-Key: <your-api-key>

{
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "ntn": "4334750-9",
    "cin": "",
    "persons": ["Muhammad Ali Khan", "Rashid Ahmed", "Hassan Raza"],
    "domain": "agrochina.pk",
    "linkedin_url": ""
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_name` | string | **Yes** | Legal name of the entity to verify |
| `country_code` | string | **Yes** | ISO 2-letter code: PK, IN, AE, TR, CN |
| `ntn` | string | No | Pakistan NTN for FBR tax check |
| `cin` | string | No | India CIN for direct MCA lookup |
| `persons` | string[] | No | Director/UBO/shareholder names to validate on LinkedIn |
| `domain` | string | No | Company domain (improves dark web + LinkedIn search) |
| `linkedin_url` | string | No | Company LinkedIn URL (skips search if known) |

---

## Submit Response (immediate)

```json
{
    "job_id": "a3f7c9e1-1234-5678-abcd-ef0123456789",
    "status": "queued",
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "checks_planned": ["registry", "linkedin_company", "dark_web", "linkedin_persons"],
    "poll_url": "/api/v1/verify-job/a3f7c9e1-1234-5678-abcd-ef0123456789",
    "estimated_time": "1-5 minutes"
}
```

---

## Poll Response (progressive)

```json
GET /api/v1/verify-job/{job_id}

{
    "job_id": "a3f7c9e1-...",
    "status": "running",
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "created_at": "2026-05-03T14:00:00.000000+00:00",
    "updated_at": "2026-05-03T14:00:12.000000+00:00",
    "checks": {
        "registry": {
            "status": "completed",
            "completed_at": "2026-05-03T14:00:08.000000+00:00",
            "verified": true,
            "legal_name": "AGROCHINA PAKISTAN (PVT.) LIMITED",
            "registration_number": "0089758",
            "entity_status": "Incorporated",
            "company_type": "Private Limited Company",
            "cro": "CRO Lahore",
            "registration_date": "05-09-2014",
            "fbr": {"ntn": "4334750-9", "status": "ACTIVE"},
            "source": "SECP eServices (direct gov query)"
        },
        "linkedin_company": {
            "status": "completed",
            "completed_at": "2026-05-03T14:00:05.000000+00:00",
            "found": true,
            "name": "AgroChina Pakistan",
            "linkedin_url": "https://www.linkedin.com/company/agrochina-pakistan",
            "industry": "International Trade and Development",
            "company_size": "51-200 employees",
            "locations": ["Lahore, Pakistan"],
            "employees_on_linkedin": 47,
            "organization_type": "Privately Held",
            "website": "https://agrochina.pk",
            "notable_employees": [
                {"name": "Muhammad Ali Khan", "link": "https://linkedin.com/in/..."},
                {"name": "Rashid Ahmed", "link": "https://linkedin.com/in/..."}
            ],
            "source": "LinkedIn via Bright Data Web Scraper API"
        },
        "linkedin_persons": {
            "status": "running",
            "progress": "2/3 profiles found (searching 3/3)"
        },
        "dark_web": {
            "status": "completed",
            "completed_at": "2026-05-03T14:00:45.000000+00:00",
            "risk_level": "LOW",
            "total_findings": 3,
            "sources_searched": 37,
            "sources_with_results": 2,
            "by_type": {"web_mention": 2, "breach_record": 1},
            "key_findings": [
                {"source": "Dehashed", "type": "breach_record", "snippet": "Email found in LinkedIn 2023 breach..."},
                {"source": "DuckDuckGo-adverse", "type": "web_mention", "snippet": "...mentioned in customs dispute..."}
            ],
            "source": "Crawl Dark Web Gateway (37 sources via Tor)"
        }
    },
    "seed_data": {
        "entity_name": "Agro China Pakistan",
        "country_code": "PK",
        "persons": ["Muhammad Ali Khan", "Rashid Ahmed", "Hassan Raza"],
        "domain": "agrochina.pk",
        "ntn": "4334750-9",
        "cin": ""
    }
}
```

---

## Final Response (all checks complete)

When `status` is `"completed"` or `"partial_success"`, the `linkedin_persons`
check will include full profile data:

```json
"linkedin_persons": {
    "status": "completed",
    "completed_at": "2026-05-03T14:02:30.000000+00:00",
    "found_count": 2,
    "total_searched": 3,
    "profiles": [
        {
            "declared_name": "Muhammad Ali Khan",
            "linkedin_name": "Muhammad Ali Khan",
            "linkedin_url": "https://linkedin.com/in/muhammadalikhan-pk",
            "position": "CEO at AgroChina Pakistan",
            "current_company": "AgroChina Pakistan",
            "current_title": "Chief Executive Officer",
            "city": "Lahore, Pakistan",
            "country_code": "PK",
            "about": "Leading agricultural trade between China and Pakistan...",
            "match_entity": true
        },
        {
            "declared_name": "Rashid Ahmed",
            "linkedin_name": "Rashid Ahmed",
            "linkedin_url": "https://linkedin.com/in/rashidahmed-trade",
            "position": "Director of Operations at AgroChina Pakistan",
            "current_company": "AgroChina Pakistan",
            "current_title": "Director of Operations",
            "city": "Karachi, Pakistan",
            "country_code": "PK",
            "about": "Supply chain and logistics...",
            "match_entity": true
        }
    ],
    "source": "LinkedIn via Bright Data Web Scraper API"
}
```

### Key Fields for Compliance

| Field | What It Tells You |
|-------|-------------------|
| `match_entity` | Does LinkedIn confirm this person works at the entity? |
| `current_company` | Who they actually work for (cross-reference) |
| `current_title` | Does their role match what was declared? |
| `city` / `country_code` | Are they in the expected jurisdiction? |
| `dark_web.risk_level` | CLEAN / LOW / MEDIUM / HIGH / CRITICAL |

---

## Status Values

| Status | Meaning |
|--------|---------|
| `queued` | Job accepted, checks about to start |
| `running` | At least one check is in progress |
| `completed` | All checks finished successfully |
| `partial_success` | Some checks completed, some failed (results still useful) |
| `failed` | All checks failed |

### Per-Check Status

| Status | Meaning |
|--------|---------|
| `pending` | Not started yet |
| `running` | In progress (may include `progress` field) |
| `completed` | Finished with results |
| `failed` | Error occurred (see `error` field) |
| `skipped` | Not applicable (e.g., no persons provided) |

---

## Timing Expectations

| Check | Typical Time | What It Does |
|-------|-------------|--------------|
| Registry (PK/IN) | 8-12 seconds | Direct gov query via SSH |
| LinkedIn Company | 5-15 seconds | Tavily search + Bright Data scrape |
| Dark Web | 30-60 seconds | 37 sources via Tor |
| LinkedIn Persons | 1-3 minutes | Tavily search per person + Bright Data batch scrape |

**Total job time: 1-5 minutes** (LinkedIn persons is the bottleneck).

---

## Polling Strategy

Recommended polling interval: **5 seconds** for the first 30s, then **10 seconds**.

```python
import time
import requests

API_URL = "http://20.94.45.219:8400"
API_KEY = "your-api-key"
HEADERS = {"X-API-Key": API_KEY}

# Submit
resp = requests.post(f"{API_URL}/api/v1/verify-job", json={
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "persons": ["Muhammad Ali Khan", "Rashid Ahmed"],
    "domain": "agrochina.pk",
    "ntn": "4334750-9",
}, headers=HEADERS)
job_id = resp.json()["job_id"]

# Poll
while True:
    resp = requests.get(f"{API_URL}/api/v1/verify-job/{job_id}", headers=HEADERS)
    data = resp.json()
    
    # Show progressive results as they arrive
    for check_name, check_data in data["checks"].items():
        if check_data["status"] == "completed":
            print(f"  {check_name}: DONE")
        elif check_data["status"] == "running":
            print(f"  {check_name}: {check_data.get('progress', 'running...')}")
    
    if data["status"] in ("completed", "partial_success", "failed"):
        break
    
    time.sleep(10)

# Use results
registry = data["checks"]["registry"]
if registry.get("verified"):
    print(f"Entity confirmed: {registry['legal_name']}")

dark_web = data["checks"]["dark_web"]
if dark_web.get("risk_level") in ("HIGH", "CRITICAL"):
    print(f"ALERT: Dark web risk {dark_web['risk_level']}")

persons = data["checks"]["linkedin_persons"]
for p in persons.get("profiles", []):
    match = "CONFIRMED" if p["match_entity"] else "MISMATCH"
    print(f"  {p['declared_name']}: {p['current_title']} at {p['current_company']} [{match}]")
```

---

## Integration with GC Onboarding Flow

### Recommended UX Flow

```
1. User enters entity name + country
   → GC can submit verify-job immediately (before form is complete)

2. User enters directors, NTN, domain (optional fields)
   → GC can submit a NEW verify-job with additional data
   → Or wait until form is "complete enough" to submit once

3. User finishes form
   → GC shows verification results panel:
     ✓ Registry: Confirmed (SECP #0089758, Active)
     ✓ LinkedIn: Company found, 47 employees, 2/3 directors confirmed
     ⚠ Dark Web: LOW risk (3 findings — 1 breach record, 2 mentions)
     ⏳ LinkedIn Persons: 2/3 scraped...

4. Analyst reviews verification results
   → Decides whether to proceed, request CIR, or flag for manual review
```

### When to Submit

- **Minimum required:** entity_name + country_code
- **Best results:** add persons + domain + NTN/CIN
- **Trigger point:** when entity name + country are confirmed (don't wait for full form)

### What to Show Users

| Check | Green | Yellow | Red |
|-------|-------|--------|-----|
| Registry | verified=true + Active | verified=true + other status | verified=false |
| LinkedIn Company | found=true + matches | found=true + partial match | found=false |
| LinkedIn Persons | match_entity=true for all | some match, some not | none match |
| Dark Web | CLEAN | LOW or MEDIUM | HIGH or CRITICAL |

---

## Relationship to Other Endpoints

| Endpoint | Use Case | Time | Depth |
|----------|----------|------|-------|
| `POST /api/v1/verify` | Quick registry-only check (synchronous) | 8-15s | Shallow |
| **`POST /api/v1/verify-job`** | **Full async verification (registry + LinkedIn + dark web)** | **1-5 min** | **Medium** |
| `POST /api/v1/jobs` (scenario=cir) | Full due diligence research report | 5-15 min | Deep |

**Recommended progression:**
1. Onboarding triggers verify-job immediately
2. If entity looks clean → proceed with onboarding
3. If red flags → submit full CIR job for deep research
4. CIR results → analyst review → approve/reject

---

## Error Handling

### Validation errors (422)
```json
{"detail": "entity_name and country_code required"}
{"detail": "Unsupported country: XX. Supported: AE, CN, IN, PK, TR"}
```

### Server busy (503)
```json
{"detail": "Server busy: 20 jobs in-flight (max 20). Try again later."}
```

### Partial success
If some checks fail (e.g., Bright Data timeout, dark web VM unreachable),
the job status becomes `partial_success`. Completed checks still have valid
results — use what's available.

---

## Cost Per Job

| Check | Cost | Vendor |
|-------|------|--------|
| Registry (SECP/MCA) | Free (SSH to our VMs) | — |
| LinkedIn Company | ~$0.0015 (1 Bright Data Web Scraper request) | Bright Data |
| LinkedIn Persons (3 people) | ~$0.005 (3 Bright Data Web Scraper + 6 Tavily searches) | Bright Data + Tavily |
| Dark Web | Free (our own VM + Tor) | — |
| Multilogin (PK FBR only) | ~$0.40/lookup (amortized from $80/mo plan) | Multilogin |
| **Total per job** | **< $0.01 (excl. Multilogin)** | |

**Note:** Variable costs above exclude fixed platform costs (Bright Data proxy, Multilogin $80/mo, Azure VMs). See migration guide Section 6 for full loaded cost.

---

## Notes

- All checks run in parallel — the total time is bounded by the slowest check
- LinkedIn person search requires Tavily to find profile URLs first (LinkedIn blocks anonymous search)
- `match_entity` is a simple name containment check — not a guaranteed match
- Dark web scan includes breach databases, ransomware lists, leaked docs, sanctions, court records
- The `seed_data` field in the response echoes back what was submitted (for audit trail)
- Job files are retained for 30 days, then archived
- Same API key and auth as CIR/product-intel jobs
