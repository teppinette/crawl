# GC team — crawl gateway endpoint moved (2026-06-26)

**From:** crawl platform (teppinette@copap.com)
**Context:** COPAPCrawl → COPAP AI subscription consolidation, completed today.

---

## Read this first — you're still using the old gateway

Per our access logs from `104.209.146.16` (your App Service), **your app made 40,623 requests to the OLD gateway in the last 24 hours**:

| Method | Endpoint | Hits / 24h |
|---|---|---:|
| `POST` | `/api/v2/screening` | **38,233** |
| `POST` | `/tools/adverse_media` | **2,409** |

This is ~28 requests/minute non-stop. You probably have a background scan, scheduled job, or screening-on-onboard pipeline running these. **When we deallocate the old gateway, this traffic will hard-fail with connection timeouts** unless you've updated the URL on your side.

The earlier "we're off /api/v1/research" reply was correct as far as that one endpoint goes — but **/api/v2/screening and /tools/adverse_media are different endpoints that you didn't mention and we missed initially.** They're still very much in active use.

This message is the explicit ping to update those two specific endpoints' URLs before deallocation.

## New URL

```
https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io
```

> **Coming soon:** `https://crawl.copap.com` as a custom domain — DNS setup pending. If you'd rather wait for that to avoid updating twice, fine — but tell us so we don't deallocate prematurely.

## What's the same

- Endpoints, payloads, response shapes — **identical** to the old gateway. Drop-in URL replacement, no code changes.
- API key (`cir-api-key`) — same value, still valid
- Auth header (`X-API-Key`) — unchanged

## What's different

- The URL itself
- Underlying compute is Azure Container Apps in COPAP AI subscription (was a VM in COPAPCrawl)
- TLS — real Microsoft-issued cert (no more self-signed cert warnings)
- `/api/v1/research` returns 410 Gone after deallocation (you're already off this)
- `osint-staging` blob in COPAPCrawl will stop receiving new CIRs (you confirmed this is fine)

## What we need from you

1. **Find every place in your code/config that references the old endpoint** (`crawldevvm`, `crawldevvm:8443`, `20.94.45.219`) — specifically the ones hitting `/api/v2/screening` and `/tools/adverse_media`
2. Update to the new URL
3. Confirm with us when done so we can set the deallocation window
4. If you'd rather wait for `crawl.copap.com` instead of updating twice, **say so** — we won't deallocate until you give the go-ahead

## Quick verification

```bash
# Health check (no auth required)
curl https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/health

# Your screening call — should return same shape as old gateway
curl -X POST \
  -H "X-API-Key: <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"entity_name":"Test Corp","country_code":"US"}' \
  https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v2/screening
```

## Timeline

- **Now:** Old gateway (`crawldevvm:8443`) and new (Container App) both running
- **+1 week clean traffic on new + you confirm cutover** → deallocate old crawldevvm + old crawl-verify
- **+further:** retire COPAPCrawl subscription entirely

**Important:** the deallocation timer doesn't start until *you* confirm you've cut over the screening + adverse media calls. We're not going to break your production by accident.

## Custom domain — pending DNS setup

If you'd rather migrate once to `crawl.copap.com` instead of twice (once to the Azure subdomain, again to the custom one), you can wait. Two DNS records needed at your provider:

| Type | Host | Value |
|---|---|---|
| `CNAME` | `crawl` (→ `crawl.copap.com`) | `crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io` |
| `TXT` | `asuid.crawl` | `262E199387C0C630D6C05A0575FB0ED4423A4154FDBB6568CD1EE1E469352A32` |

After both propagate (5–30 min), we bind the custom domain and Azure auto-issues a managed cert. Zero downtime — the Azure subdomain keeps working in parallel.

## Contact

Reply to teppinette@copap.com or DM on Teams.
