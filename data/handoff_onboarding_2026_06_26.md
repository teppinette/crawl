# Onboarding team — crawl gateway endpoint moved (2026-06-26)

**From:** crawl platform (teppinette@copap.com)
**Context:** COPAPCrawl → COPAP AI subscription consolidation, completed today. This affects your /verify testing.

---

## Summary

You're actively using `/api/v1/verify` and `/api/v1/verify/bulk` from your failure-CSV testing. The endpoints work identically, just at a new URL. **Drop-in replacement — no code changes needed.**

## New URL

```
https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io
```

> **Coming soon (target):** `https://crawl.copap.com` as a custom domain. DNS setup pending — see "Custom domain" section at the bottom. Once live, point your config at that and you'll never need to update again.

## Action required

Update your `CRAWL_GATEWAY_URL` (or however you've configured it) on your side. **No code changes** — same API key, same payloads, same responses.

## Endpoints you use — all identical contracts

- `POST /api/v1/verify` — single entity registry check
- `POST /api/v1/verify/bulk` — batch (shipped this week, recommend you switch to this for the 160-entity list — 1 submit + poll instead of 160 sequential calls)
- `GET /api/v1/verify/bulk/{bulk_job_id}` — poll bulk results
- `POST /api/v1/lookup` — id-keyed deterministic lookup

## What's improved on the new platform

- Real Microsoft-issued TLS cert (no more self-signed cert warnings in your client)
- VNet-private path to crawl-verify under the hood — fewer latency variance / network blips
- Auto-scaling on the gateway (was a single VM with 4 workers — now scales 1–N replicas on load)
- API key (`cir-api-key`) and response shapes — unchanged

## Recently shipped that might help your retest

- `/verify/bulk` — see endpoints list above. Big efficiency win for your 160-entity batch.
- `tried_variants` field on PK verify responses (PK SECP rebuild last week)
- IN `name_mismatch` block on CIN-path (kills the BOGUS_CONSTANT TWS-SYSTEMS bug from your earlier failure CSV)
- Honest Tier-2 escalation hints with accurate `delivers` field
- Deep Lookup fallback wired for AE/MA/EG/TR — when GLEIF / OC misses, you get founding_year + HQ + industry via Bright Data preview

## Quick smoke test

```bash
curl -k -H "X-API-Key: <your-key>" \
  https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/health
```

Should return:
```json
{"status":"ok","service":"crawl-research-gateway","version":"3.0.0","scenarios":[...],"regions":[...]}
```

End-to-end PK SECP smoke (proves the full path Container App → crawl-verify → SECP):
```bash
curl -k -X POST \
  -H "X-API-Key: <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"country_code":"PK","entity_name":"PACKAGES LIMITED","nocache":true}' \
  https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/verify
```

Expected: `verified=true, founding_year=1956, registration_number=0000792, status=Incorporated`.

## Timeline

- **Now:** Old URL (`crawldevvm:8443` / `20.94.45.219`) **still working** — side-by-side
- **+1 week clean on new platform:** Deallocate old crawldevvm + old crawl-verify
- **Hard cutover deadline:** TBD — confirm with us when you've updated your env

## Bulk verify quick reference

If you haven't switched from sequential `/verify` calls to `/verify/bulk` yet, here's the contract:

```bash
# Submit up to 500 entities at once
curl -X POST -H "X-API-Key: <your-key>" -H "Content-Type: application/json" \
  -d '{
    "entities": [
      {"correlation_id":"COPAP-4762","country_code":"PK","entity_name":"BIN ARIF INDUSTRIES PVT LTD"},
      {"correlation_id":"COPAP-5235","country_code":"PK","entity_name":"NUCHEM (PVT.) LTD"},
      ...
    ],
    "options": {"concurrency": 10}
  }' \
  https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/verify/bulk

# Returns: {"bulk_job_id": "...", "status": "pending", "total": N, "next_steps": {...}}

# Poll
curl -H "X-API-Key: <your-key>" \
  https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/verify/bulk/<bulk_job_id>

# Returns: {"status": "pending|partial|complete", "completed": N/total, "results": [...]}
```

Each result preserves your `correlation_id` so you can join back to your source list.

## Custom domain — pending DNS setup

Once one DNS record is created at your DNS provider for `copap.com`, we'll bind a managed cert and you can use `https://crawl.copap.com` permanently.

**DNS records to create:**

| Type | Host | Value |
|---|---|---|
| `CNAME` | `crawl` (→ `crawl.copap.com`) | `crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io` |
| `TXT` | `asuid.crawl` | `262E199387C0C630D6C05A0575FB0ED4423A4154FDBB6568CD1EE1E469352A32` |

The TXT record proves Azure that we own the subdomain. After both records propagate (~5–30 min), we bind the custom domain to the Container App and Azure auto-issues a managed cert (Let's Encrypt). Total downtime: zero (the existing Azure URL keeps working in parallel).

## Contact

Reply to teppinette@copap.com or DM on Teams.
