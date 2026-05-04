# Entity Verification — GC Build Guide

**From:** Infrastructure / Crawl Platform
**To:** GC App Development Team
**Date:** 2026-05-03
**Priority:** Ready to build — API is live

---

## What This Is

A new verification service that automatically checks counterparties during
onboarding. When a user enters an entity + directors, the system:

1. Checks government registries (is this company real and active?)
2. Checks LinkedIn (does this company exist? do these directors actually work there?)
3. Checks dark web (any breaches, leaks, sanctions hits, adverse mentions?)

Results come back in 1-5 minutes. GC shows them progressively as they arrive.

---

## Connection Details

```
Host:    http://20.94.45.219:8400   (or https://20.94.45.219:8443 via TLS)
Auth:    X-API-Key header           (same CIR_API_KEY you already have)
Timeout: 30s for submit, 10s for poll
```

No new credentials needed. Same key as CIR jobs.

---

## What GC Needs to Build

### 1. Trigger on Onboarding

When the user has entered **entity name + country** (minimum), fire the verify job.
Don't wait for the full form to be complete.

```python
import requests

CRAWL_API = "http://20.94.45.219:8400"
API_KEY = os.environ["CIR_API_KEY"]

def submit_verification(entity_name, country_code, persons=None, domain=None, ntn=None, cin=None):
    """Call this as soon as entity + country are confirmed in onboarding."""
    resp = requests.post(
        f"{CRAWL_API}/api/v1/verify-job",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={
            "entity_name": entity_name,
            "country_code": country_code,    # PK, IN, AE, TR, CN
            "persons": persons or [],         # director/UBO names
            "domain": domain or "",           # company website domain
            "ntn": ntn or "",                 # Pakistan NTN (optional)
            "cin": cin or "",                 # India CIN (optional)
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["job_id"]
```

### 2. Poll for Results

Poll every 10 seconds. Show results as each check completes.

```python
import time

def poll_verification(job_id):
    """Poll until done. Returns final result dict."""
    while True:
        resp = requests.get(
            f"{CRAWL_API}/api/v1/verify-job/{job_id}",
            headers={"X-API-Key": API_KEY},
            timeout=10,
        )
        data = resp.json()

        if data["status"] in ("completed", "partial_success", "failed"):
            return data

        time.sleep(10)
```

### 3. Display Results

Each check in `data["checks"]` has its own status. Show them independently.

---

## UI Specification

### Where to Show

Add a **"Verification Status"** panel on the onboarding/entity detail page.
It should appear as soon as a verify job is submitted and update live.

### What to Show

```
+--------------------------------------------------+
|  VERIFICATION STATUS                    [1:23 ago]|
|                                                   |
|  Registry (SECP)          [green checkmark] Active|
|    AGROCHINA PAKISTAN (PVT.) LIMITED               |
|    SECP #0089758 | CRO Lahore | Inc: 2014-09-05  |
|                                                   |
|  LinkedIn Company         [yellow warning] Not found|
|                                                   |
|  Directors on LinkedIn                            |
|    Muhammad Ali Khan      [red X] Not confirmed   |
|    Rashid Ahmed           [green check] Confirmed |
|      → Director of Operations at AgroChina Pakistan|
|                                                   |
|  Dark Web                 [orange badge] MEDIUM   |
|    8 findings from 37 sources                     |
|    2 breach records, 6 web mentions               |
|                                                   |
+--------------------------------------------------+
```

### Status Colors

| Check | Green | Yellow | Red |
|-------|-------|--------|-----|
| Registry | `verified: true` + Active | `verified: true` + other status | `verified: false` |
| LinkedIn Company | `found: true` | — | `found: false` |
| Each Person | `works_at_entity: true` | — | `works_at_entity: false` |
| Dark Web | CLEAN | LOW or MEDIUM | HIGH or CRITICAL |

### Loading States

While a check is `"status": "running"`, show a spinner. Each check resolves
independently — show results as they arrive, don't wait for all 4.

```
Registry          [spinner] Checking SECP...
LinkedIn Company  [checkmark] Found — 47 employees
Directors         [spinner] Verifying 2/3...
Dark Web          [spinner] Scanning 37 sources...
```

---

## API Response Reference

### Submit Response

```json
POST /api/v1/verify-job

Response:
{
    "job_id": "a3f7c9e1-1234-5678-abcd-ef0123456789",
    "status": "queued",
    "poll_url": "/api/v1/verify-job/a3f7c9e1-1234-5678-abcd-ef0123456789",
    "estimated_time": "1-5 minutes"
}
```

Store `job_id` and start polling.

### Poll Response — Registry Check

```json
"registry": {
    "status": "completed",
    "verified": true,
    "legal_name": "AGROCHINA PAKISTAN (PVT.) LIMITED",
    "registration_number": "0089758",
    "entity_status": "Incorporated",
    "company_type": "Private Limited Company",
    "cro": "CRO Lahore",
    "registration_date": "05-09-2014 10:03:11",
    "fbr": {"ntn": "4334750-9", "status": "ACTIVE"},
    "source": "SECP eServices (direct gov query)"
}
```

**Display:** Legal name, reg number, status, registration date.
**Decision:** If `verified: false` → flag for manual review.

### Poll Response — LinkedIn Company

```json
"linkedin_company": {
    "status": "completed",
    "found": true,
    "name": "AgroChina Pakistan",
    "linkedin_url": "https://www.linkedin.com/company/agrochina-pakistan",
    "industry": "International Trade and Development",
    "company_size": "51-200 employees",
    "employees_on_linkedin": 47,
    "organization_type": "Privately Held",
    "website": "https://agrochina.pk",
    "source": "LinkedIn via Bright Data Web Scraper API"
}
```

**Display:** Company name, industry, size, employee count.
**Decision:** If `found: false` → not necessarily bad (many small companies aren't on LinkedIn), but note it.

### Poll Response — LinkedIn Persons (KEY CHECK)

```json
"linkedin_persons": {
    "status": "completed",
    "confirmed_count": 1,
    "total_searched": 2,
    "persons_verification": [
        {
            "name": "Muhammad Ali Khan",
            "works_at_entity": false,
            "note": "Not found at Agro China Pakistan on LinkedIn"
        },
        {
            "name": "Rashid Ahmed",
            "works_at_entity": true,
            "title": "Director of Operations",
            "linkedin_url": "https://linkedin.com/in/rashidahmed-trade",
            "city": "Karachi, Pakistan"
        }
    ],
    "note": "1/2 persons confirmed at entity on LinkedIn",
    "source": "LinkedIn via Bright Data Web Scraper API"
}
```

**Display:** Per-person checkmark or X.
**Decision:**
- `works_at_entity: true` → person confirmed at this company
- `works_at_entity: false` → person NOT confirmed — doesn't mean fraud, but flag it
- If 0 out of N confirmed → higher concern, may need manual verification

### Poll Response — Dark Web

```json
"dark_web": {
    "status": "completed",
    "risk_level": "MEDIUM",
    "total_findings": 8,
    "sources_searched": 37,
    "sources_with_results": 3,
    "by_type": {"web_mention": 6, "breach_record": 2},
    "key_findings": [
        {"source": "Dehashed", "type": "breach_record", "snippet": "Email in LinkedIn 2023 breach..."},
        {"source": "DuckDuckGo-adverse", "type": "web_mention", "snippet": "Customs dispute filing..."}
    ],
    "source": "Crawl Dark Web Gateway (37 sources via Tor)"
}
```

**Display:** Risk badge + finding count + top findings.
**Decision:**
- CLEAN/LOW → proceed normally
- MEDIUM → show to analyst, continue onboarding
- HIGH/CRITICAL → block onboarding, require analyst review before proceeding

---

## Overall Job Status

| Value | Meaning | UI Action |
|-------|---------|-----------|
| `queued` | Just submitted | Show "Starting verification..." |
| `running` | Checks in progress | Show spinners, display completed checks |
| `completed` | All checks done | Show final results |
| `partial_success` | Some checks failed | Show what worked, note failures |
| `failed` | All checks failed | Show error, offer retry button |

---

## Onboarding Flow Integration

```
Step 1: User enters entity name + country
        → GC calls POST /api/v1/verify-job
        → Store job_id in session/entity record

Step 2: User continues filling form (directors, domain, etc.)
        → GC polls every 10s in background
        → As results come in, update the verification panel

Step 3: User enters directors
        → OPTION A: Submit a NEW verify-job with persons included
        → OPTION B: If first job already completed, submit new job with persons
        (Keep both job_ids — show latest results)

Step 4: User finishes form
        → Verification panel shows final status
        → Analyst reviews before proceeding

Step 5: If HIGH/CRITICAL risk
        → Block "Approve" button until analyst acknowledges
        → Optionally trigger full CIR job for deep research
```

---

## When to Submit a New Job vs. Wait

| Situation | Action |
|-----------|--------|
| Entity + country entered | Submit immediately (no persons yet) |
| Directors added later | Submit a new job with persons included |
| Domain added later | Submit a new job (improves dark web accuracy) |
| User changes entity name | Cancel old job display, submit new one |

Each verify-job is independent. You can submit multiple — show the latest one's results.

---

## Error Handling

| HTTP Code | Meaning | GC Action |
|-----------|---------|-----------|
| 200 | Success | Process response |
| 422 | Missing fields | Check entity_name + country_code are set |
| 403 | Bad API key | Check CIR_API_KEY env var |
| 503 | Server busy | Retry in 30 seconds |
| 404 | Job not found | Job expired (30 day retention) |

---

## Input Fields Mapping

Map from GC onboarding form fields to verify-job API fields:

| GC Form Field | API Field | Notes |
|---------------|-----------|-------|
| Entity Legal Name | `entity_name` | Required |
| Country | `country_code` | Required, ISO 2-letter |
| Directors / Key Individuals | `persons` | Array of "First Last" strings |
| Website | `domain` | Strip protocol (e.g., "agrochina.pk" not "https://agrochina.pk") |
| Tax ID (Pakistan NTN) | `ntn` | Only for PK entities |
| CIN (India) | `cin` | Only for IN entities |
| LinkedIn URL | `linkedin_url` | If user entered it during onboarding |

---

## Cost & Rate Limits

- **Cost per job:** < $0.01 (covered by platform)
- **Rate limit:** 30 requests/minute per IP
- **Max concurrent jobs:** 20 across all scenarios
- **Job retention:** 30 days

---

## Supported Countries

| Country | Registry Check | LinkedIn | Dark Web |
|---------|---------------|----------|----------|
| Pakistan (PK) | SECP (direct gov) | Yes | Yes |
| India (IN) | MCA21 via Tofler | Yes | Yes |
| Turkey (TR) | Guidance only | Yes | Yes |
| UAE (AE) | Guidance only | Yes | Yes |
| China (CN) | Guidance only | Yes | Yes |

For TR/AE/CN: registry check returns available registries and notes that
browser-based verification isn't available yet. LinkedIn and dark web still run.

---

## Quick Start — Minimum Viable Integration

If you want to get something working fast:

```python
# 1. Submit when entity is entered
job_id = submit_verification("Agro China Pakistan", "PK")

# 2. Poll in background (setInterval in frontend, or background task)
result = poll_verification(job_id)

# 3. Show simple traffic light
registry_ok = result["checks"]["registry"].get("verified", False)
dark_web_ok = result["checks"]["dark_web"].get("risk_level") in ("CLEAN", "LOW")

if registry_ok and dark_web_ok:
    show_green_badge("Verification passed")
elif not registry_ok:
    show_red_badge("Entity not found in registry")
else:
    show_yellow_badge(f"Dark web: {result['checks']['dark_web']['risk_level']}")
```

Add the LinkedIn person checks and full UI later — the above gives you
immediate value with minimal build effort.

---

## Questions?

The API is live at `http://20.94.45.219:8400`. You can test right now:

```bash
curl -X POST http://20.94.45.219:8400/api/v1/verify-job \
  -H "X-API-Key: $CIR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"entity_name": "Test Company", "country_code": "PK"}'
```

Full technical spec: `VERIFY_JOB_SPEC.md`
Master API doc: `BUILD_SPEC_GC_HANDOFF.md` (Section 12)
