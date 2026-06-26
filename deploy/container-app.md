# crawldevvm gateway — Azure Container App deploy plan

Status as of 2026-06-26: Dockerfile + requirements.txt drafted. Image not yet built. Container App Environment not yet provisioned. This doc captures what's needed to ship.

## Pre-deploy: one-time Azure plumbing

All click-through in portal (under COPAP AI Subscription):

1. **Azure Container Registry** — `copapacr` (or whatever name). Standard tier ($20/mo). For pushing the image.
2. **Container App Environment** — `crawl-cae` in East US 2, VNet-integrated into `vnet-eastus2` so the Container App can use private DNS / managed identity in the same VNet as Foundry.
3. **Azure Files share** — `crawl-jobs` on storage account (~10 GB). Mount target: `/app/api/jobs` inside the container. Replaces the in-VM filesystem for bulk verify job state (cross-replica safety).
4. **User-assigned Managed Identity** — `crawl-gateway-mi`. Assign to:
   - `crawlkeyvault` (KV Secrets User) — for `api/keyvault.py` to read all 41 secrets via DefaultAzureCredential.
   - `crawl-monitor-db` (postgresql admin, or just add to firewall by Container App egress IP — TBD).
   - `stcrawlosint` (Blob Data Contributor) — for SAS-less blob writes during transition.
   - `copapfoundry-resource` (Cognitive Services User) — so the orchestrator can dispatch agents.

## Connectivity (post-deploy)

The Container App reaches its dependencies over their public endpoints + TLS + API keys / MI. We're NOT VNet-peering crawldevvm-vnet to COPAP AI today because `DevCrawl_VN` and `copapfrance-vnet` both claim 180.20.0.0/16 — Azure rejects the peering. Re-IP'ing DevCrawl_VN is a planned-but-separate workstream. Path C from the 2026-06-26 plan.

| Destination | How | Notes |
|---|---|---|
| crawl-verify | `https://20.110.193.6:8460` (public IP + self-signed cert + API key) | Add Container App egress IP to crawl-verify NSG inbound 8460 rules. Self-signed cert means `verify=False` or pin cert hash. |
| Foundry agent endpoint | `https://copapfoundry-resource.services.ai.azure.com/api/projects/copapfoundry` | Already public. MI auth. |
| crawl-monitor-db (PostgreSQL) | `crawl-monitor-db.postgres.database.azure.com:5432` | Add Container App egress IP to PG firewall. Or use Private Endpoint later. |
| crawlkeyvault | `https://crawlkeyvault.vault.azure.net/` | MI auth, public endpoint with managed-identity scope. |
| stcrawlosint blob | `https://stcrawlosint.blob.core.windows.net/` | MI auth. |
| Bright Data | `brd.superproxy.io:33335` (residential), `api.brightdata.com` (Deep Lookup) | Their allowlist is source-IP based. Need to add Container App egress IPs to BD allowlist. |
| Multilogin | `gate.multilogin.com:8080`, `api.multilogin.com` | Same allowlist concern. |
| Foundry tool URLs in `agents/tools/*.openapi.yaml` | Currently hardcoded to `http://20.94.45.219:8400` (crawldevvm public IP) | When Container App goes live, rewrite to the Container App's ingress URL + redeploy all 48 agents. |

## Secrets — Container App env wiring

The Container App spec references these as secrets pulled from Key Vault (Container Apps has native KV-secret-reference support). Don't put values in env directly.

| Secret name | KV reference |
|---|---|
| `CIR_API_KEY` (for `verify_api_key` middleware) | `secretref:crawlkeyvault/cir-api-key` |
| All others | api/keyvault.py reads at runtime via MI — no Container App env needed |

## State that needs Azure Files mount

| Path in container | Why | Storage |
|---|---|---|
| `/app/api/jobs/` | Legacy CIR job state (.json per job_id) | Azure Files `crawl-jobs` |
| `/app/api/jobs/bulk/` | bulk verify cross-replica file-backed storage (CRITICAL — without this `/api/v1/verify/bulk` polls 404 on different replica) | same mount, subdir |

`api/jobs_archive/` and `output/` and `raw_responses/` should move to blob (stcrawlosint), not Azure Files.

## Deploy sequence

1. Build image locally: `docker build -f deploy/Dockerfile -t crawl-gateway:v0.1 .` (~3-5 min)
2. Push to ACR: `az acr login --name copapacr && docker tag crawl-gateway:v0.1 copapacr.azurecr.io/crawl-gateway:v0.1 && docker push copapacr.azurecr.io/crawl-gateway:v0.1`
3. Create Container App with image ref + MI + KV secret refs + Azure Files mount + ingress on 8400
4. Container App is "test-mode" — separate ingress URL, doesn't take live traffic
5. Run smoke tests against the Container App URL — verify_bulk, /verify, /cir/run
6. Side-by-side: route 10% of GC App traffic to Container App, watch for divergence
7. Cut over 100% when validated
8. Deallocate crawldevvm VM (reversible)
9. Decommission after 1 week of clean Container App ops

## Open

- Container App's egress IPs aren't deterministic by default. Either:
  - Configure NAT Gateway on the Container App Environment subnet → single egress IP → easy to allowlist
  - OR enable Azure Front Door / Application Gateway and use static frontend
  Pick NAT Gateway — cheapest, simplest.

- Self-signed cert on crawl-verify (:8460) won't validate from a fresh container. Options:
  - Pass `--no-check-certificate` equivalent at the HTTP client level (`verify=False` in `requests`)
  - OR generate a proper cert for crawl-verify via Let's Encrypt + DNS challenge
  Decide before deploy.

- `agents/tools/*.openapi.yaml` server URLs — rewrite + Foundry agent redeploy is part of the cutover step. Need a script.
