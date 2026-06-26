# GC team — crawl gateway endpoint moved (2026-06-26)

**From:** crawl platform (teppinette@copap.com)
**Context:** COPAPCrawl → COPAP AI subscription consolidation, completed today.

---

## Summary

The crawl gateway is now an Azure Container App in the COPAP AI subscription. The underlying VM (`crawldevvm` / 20.94.45.219) and supporting VM (`crawl-verify`) are being deallocated after a side-by-side observation window.

## New URL

```
https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io
```

> **Coming soon (target):** `https://crawl.copap.com` as a custom domain. DNS setup pending — see "Custom domain" section below. Once live, you should migrate to that and never need to chase the random Azure subdomain again.

## What's the same

- All endpoints (`/api/v1/verify`, `/api/v1/cir/run`, `/api/v1/lookup`, `/api/v1/verify/bulk`, etc.) — **identical shapes, no contract changes**
- API key (`cir-api-key`) — same value, still valid
- Response payloads — no changes
- Authentication header (`X-API-Key`) — unchanged

## What's different

- The URL itself
- Underlying compute is Azure Container Apps in COPAP AI sub (was a VM in COPAPCrawl)
- TLS — now a real Microsoft-issued cert (no more self-signed cert warnings)
- `/api/v1/research` will return 410 Gone after we deallocate the old VM (you're already off this per your 2026-06-25 reply, just noting)
- `osint-staging` blob in COPAPCrawl will stop receiving new CIRs (you confirmed this is fine)

## What we need from you

- If anything on your side **still** references the old endpoint (`crawldevvm` / `20.94.45.219` / `crawldevvm:8443`), update it to the new URL
- If nothing on your side calls crawl at all anymore (per your last reply), no action needed — just acknowledge
- Ping us if anything breaks during the observation window

## Timeline

- **Now:** Both old (`crawldevvm`) and new (Container App) running side-by-side
- **+1 week clean:** Deallocate old crawldevvm + old crawl-verify in COPAPCrawl
- **+further confirmation:** Retire the COPAPCrawl subscription entirely

## Quick verification

```bash
curl -k -H "X-API-Key: <your-key>" \
  https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/health
```

Should return `{"status":"ok", "service":"crawl-research-gateway", ...}` with HTTP 200.

## Custom domain — pending DNS setup

Once you create one DNS record at your DNS provider, we'll bind a managed cert and you can use `https://crawl.copap.com` permanently.

**DNS records to create:**

| Type | Host | Value |
|---|---|---|
| `CNAME` | `crawl` (→ `crawl.copap.com`) | `crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io` |
| `TXT` | `asuid.crawl` | `262E199387C0C630D6C05A0575FB0ED4423A4154FDBB6568CD1EE1E469352A32` |

The TXT record proves Azure that we own the subdomain. After both records propagate (~5–30 min), we bind the custom domain to the Container App and Azure auto-issues a managed cert (Let's Encrypt). Total downtime: zero (the existing Azure URL keeps working).

## Contact

Reply to teppinette@copap.com or DM on Teams.
