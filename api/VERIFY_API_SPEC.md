# Entity Verification API — Global Compliance Integration Guide

**Endpoint:** `POST /api/v1/verify`
**Host:** `crawldevvm:8400` (20.94.45.219:8400)
**Auth:** `X-API-Key: <COPAP_CIR_API_KEY>`
**Response time:** 5–15 seconds
**Last updated:** 2026-05-02

---

## Overview

Real-time entity verification against government corporate registries.
Returns structured data with full source attribution (registry name, URL,
method, and timestamp) for compliance audit trail.

No AI synthesis — data comes directly from government registry queries.

---

## Supported Countries & Registries

| Country | Code | Registry | Source Type | Data Returned |
|---------|------|----------|-------------|---------------|
| Pakistan | `PK` | SECP (eservices.secp.gov.pk) | Direct gov query | Legal name, SECP reg #, status, company type, CRO, reg date, Form A/B filing date |
| Pakistan | `PK` | FBR (e.fbr.gov.pk) | Via PK residential proxy | NTN tax status (when FBR is reachable) |
| India | `IN` | MCA21 via Tofler.in | Via Bright Data proxy | CIN, legal name, status, company type, incorporation date, address, directors, capital |
| Turkey | `TR` | — | — | Not yet available for real-time verify. Use CIR job. |
| UAE | `AE` | — | — | Not yet available for real-time verify. Use CIR job. |
| China | `CN` | — | — | Not yet available for real-time verify. Use CIR job. |

**Note:** TR, AE, CN registries require browser rendering for search.
Real-time verification for these countries will be added when browser-based
proxy access is available. In the meantime, submit a CIR job via
`POST /api/v1/jobs` for full research.

---

## Request Format

```json
POST /api/v1/verify
Content-Type: application/json
X-API-Key: <your-api-key>

{
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "ntn": "4334750-9",
    "cin": ""
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `entity_name` | string | Yes | Company name to verify |
| `country_code` | string | Yes | ISO 2-letter country code (PK, IN, TR, AE, CN) |
| `ntn` | string | No | Pakistan NTN number for FBR tax verification (PK only) |
| `cin` | string | No | India CIN for direct MCA lookup (IN only, speeds up search) |

---

## Response Format — Pakistan (PK)

```json
{
    "entity_name": "Agro China Pakistan",
    "country_code": "PK",
    "verified": true,
    "legal_name": "AGROCHINA PAKISTAN (PVT.) LIMITED",
    "registration_number": "0089758",
    "status": "Incorporated",
    "company_type": "Private Limited Company",
    "cro": "CRO Lahore",
    "registration_date": "05-09-2014 10:03:11",
    "form_ab_filed_upto": "28/10/2023",
    "fbr": {
        "ntn": "4334750-9",
        "status": "UNAVAILABLE",
        "note": "FBR portal is down — manual verification required"
    },
    "all_matches": null,
    "validation_source": {
        "registry": "Securities and Exchange Commission of Pakistan (SECP)",
        "url": "https://eservices.secp.gov.pk/eServices/NameSearch.jsp",
        "method": "Direct POST to SECP ControllerServlet via Gulf VM (SSH)",
        "verified_at": "2026-05-02T17:43:38.762635+00:00"
    },
    "timestamp": "2026-05-02T17:43:38.762635+00:00",
    "summary": "AGROCHINA PAKISTAN (PVT.) LIMITED — SECP #0089758 — Private Limited Company — CRO Lahore — Reg: 05-09-2014"
}
```

### PK Response Fields

| Field | Description |
|-------|-------------|
| `verified` | `true` if entity found in SECP registry |
| `legal_name` | Registered legal name (as per SECP) |
| `registration_number` | SECP registration number (7 digits) |
| `status` | Registration status: Incorporated, Dissolved, Struck Off |
| `company_type` | Private Limited Company, Public Limited, LLP, etc. |
| `cro` | Company Registration Office (e.g., CRO Lahore) |
| `registration_date` | Date of incorporation |
| `form_ab_filed_upto` | Last Form A/B annual filing date |
| `fbr` | FBR tax status (if NTN provided): ACTIVE, INACTIVE, UNAVAILABLE |
| `all_matches` | Array of all matching companies (if multiple matches) |
| `validation_source` | Audit trail: registry, URL, method, timestamp |

---

## Response Format — India (IN)

```json
{
    "entity_name": "CJS Specialty Chemicals",
    "country_code": "IN",
    "verified": true,
    "legal_name": "C J S SPECIALTY CHEMICALS PRIVATE LIMITED",
    "cin": "U24110MH2008PTC186710",
    "status": "Active",
    "company_type": "Private Limited Company",
    "incorporation_date": "12 September, 2008",
    "registered_address": "OFFICE NO. 31 A, 3RD FLOOR, BAJAJ BHAWAN, JAMNALAL BAJAJ MARG, 226, NARIMAN POINT, Mumbai City, MUMBAI, Maharashtra, India, 400021",
    "directors": [
        "Dharmesh Kishan Mange",
        "Ashit Mahesh Shah",
        "Ashish Prakash Shah"
    ],
    "authorized_capital": "5.00 lac",
    "paidup_capital": "1.00 lac",
    "validation_source": {
        "registry": "Ministry of Corporate Affairs (MCA21) — via Tofler.in",
        "url": "https://www.tofler.in/cjs-specialty-chemicals/company/U24110MH2008PTC186710",
        "method": "Bright Data residential proxy (IN) → Tofler.in (aggregates MCA21 data)",
        "verified_at": "2026-05-02T12:12:48.802688+00:00"
    },
    "timestamp": "2026-05-02T12:12:48.802688+00:00",
    "summary": "C J S SPECIALTY CHEMICALS PRIVATE LIMITED — CIN U24110MH2008PTC186710 — Active — Private Limited Company — Inc: 12 September, 2008"
}
```

### IN Response Fields

| Field | Description |
|-------|-------------|
| `verified` | `true` if entity found with valid CIN |
| `legal_name` | Registered legal name (as per MCA21) |
| `cin` | Corporate Identification Number (21 characters) |
| `status` | Active, Struck Off, Dormant |
| `company_type` | Private Limited, Public Limited, LLP |
| `incorporation_date` | Date of incorporation |
| `registered_address` | Registered office address (with PIN code) |
| `directors` | Array of current director names |
| `authorized_capital` | Authorized share capital (INR) |
| `paidup_capital` | Paid-up share capital (INR) |
| `validation_source` | Audit trail: registry, URL, method, timestamp |

---

## Response Format — TR / AE / CN (guidance only)

```json
{
    "entity_name": "Example Company",
    "country_code": "TR",
    "verified": false,
    "available_registries": [
        "MERSIS (mersis.gtb.gov.tr)",
        "GIB (gib.gov.tr)",
        "E-Devlet (turkiye.gov.tr)"
    ],
    "note": "Turkish registries require browser rendering. Use /api/v1/jobs with scenario=cir for full research.",
    "timestamp": "2026-05-02T17:45:00.000000+00:00",
    "summary": "Real-time verify not available for TR. Submit CIR job for full research."
}
```

---

## Error Responses

### Missing fields (422)
```json
{"detail": "entity_name and country_code required"}
```

### Unsupported country (422)
```json
{"detail": "Verify not yet supported for XX. Supported: AE, CN, IN, PK, TR."}
```

### Invalid API key (403)
```json
{"detail": "Invalid API key"}
```

### Entity not found (200, verified=false)
```json
{
    "entity_name": "Nonexistent Corp",
    "country_code": "PK",
    "verified": false,
    "summary": "No SECP registration found for 'Nonexistent Corp'"
}
```

---

## Integration Example (Python)

```python
import requests

API_URL = "http://20.94.45.219:8400/api/v1/verify"
API_KEY = "your-api-key-here"

def verify_entity(entity_name, country_code, ntn=None, cin=None):
    payload = {
        "entity_name": entity_name,
        "country_code": country_code,
    }
    if ntn:
        payload["ntn"] = ntn
    if cin:
        payload["cin"] = cin

    resp = requests.post(
        API_URL,
        json=payload,
        headers={"X-API-Key": API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

# Pakistan — verify against SECP
result = verify_entity("Agro China Pakistan", "PK", ntn="4334750-9")
print(result["verified"])           # True
print(result["legal_name"])         # AGROCHINA PAKISTAN (PVT.) LIMITED
print(result["validation_source"])  # Full audit trail

# India — verify against MCA21
result = verify_entity("CJS Specialty Chemicals", "IN", cin="U24110MH2008PTC186710")
print(result["verified"])           # True
print(result["directors"])          # ['Dharmesh Kishan Mange', ...]
```

---

## Validation Source Attribution

Every successful verification includes a `validation_source` object for
compliance audit trail:

```json
{
    "registry": "Securities and Exchange Commission of Pakistan (SECP)",
    "url": "https://eservices.secp.gov.pk/eServices/NameSearch.jsp",
    "method": "Direct POST to SECP ControllerServlet via Gulf VM (SSH)",
    "verified_at": "2026-05-02T17:43:38.762635+00:00"
}
```

| Field | Description |
|-------|-------------|
| `registry` | Full name of government registry that provided the data |
| `url` | URL of the registry portal accessed |
| `method` | Technical method used to access the registry |
| `verified_at` | ISO 8601 timestamp of when verification was performed |

This allows compliance teams to document in audit files:
> "Entity registration confirmed by SECP (eservices.secp.gov.pk) on 2026-05-02 at 17:43 UTC via direct registry query."

---

## Relationship to CIR (Full Research)

The `/api/v1/verify` endpoint is for **quick registration checks** during
onboarding. It confirms an entity exists in a government registry.

For **full due diligence** (ownership chain, sanctions, adverse media, courts,
dark web, trade risk), submit a CIR job:

```
POST /api/v1/jobs
{"scenario": "cir", "payload": {"entity_legal_name": "...", "entity_country": "PK"}}
```

CIR jobs take 5–10 minutes and produce a comprehensive research report
uploaded to blob storage.

---

## Notes

- PK verification queries SECP directly (no proxy, no intermediary)
- IN verification uses Tofler.in which aggregates MCA21 government data
- FBR (Pakistan tax) geo-blocks non-Pakistani IPs; accessed via residential proxy when available
- All timestamps are UTC (ISO 8601)
- Response time: PK ~8 seconds, IN ~12 seconds
- Rate limit: shared with CIR dispatch thread pool (5 concurrent)
