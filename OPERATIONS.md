# Crawl OSINT Platform — Operations Runbook

**Last updated:** 2026-06-28
**Audience:** Anyone running, deploying, or debugging the crawl platform.
**For project context + architecture:** see `CLAUDE.md`.

This document replaces the implicit knowledge that lived on `crawldevvm`
(which was deallocated 2026-06-28).

---

## 1 — Where to work from

**Ops console:** `copapdev_vm` (172.20.0.12, private only — no public IP).
Access via Azure Bastion, Tailscale, or your usual route into the COPAP AI
subscription.

Already set up on `copapdev_vm` under `copapadmin`:

- `~/crawl/` — git checkout of `https://github.com/teppinette/crawl`
- `~/.ssh/crawl_admin_key.pem` — SSH key for crawl-verify-new + crawl-darkweb-new
- `~/.ssh/crawl_known_hosts` — host fingerprints
- `psql`, `pg_dump`, `jq`, `git`, `az` (with system-assigned MI granting
  Contributor on COPAPAI_Resource_Group + Secrets Officer on `crawl-kv` +
  Cognitive Services User on Foundry + AcrPull on `copapcrawlacr`)

**Alternative:** Azure Cloud Shell. `az` works native; clone the repo
ad-hoc; nothing local to maintain.

---

## 2 — Service inventory

All in **COPAP AI Subscription** unless noted.

| Layer | Resource | Where |
|---|---|---|
| Gateway API | Container App `crawl-gateway-v2` | `copap-crawl-cae-vnet`, eastus2, public FQDN `crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io` |
| Container registry | `copapcrawlacr` | eastus2 |
| Verify VM | `crawl-verify-new` | vnet-eastus2 / snet-eastus2-20, private 172.20.0.26, public 172.177.71.40 |
| Dark-web VM | `crawl-darkweb-new` | vnet-westeurope, private 172.21.0.4, public 20.101.84.41 |
| PostgreSQL | `crawl-pg` flex server | vnet-eastus2 / snet-postgres (private 172.20.7.4) — 3 DBs: `crawlmonitor`, `crawl_reports`, `crawl_verification` |
| Key Vault | `crawl-kv` | RBAC mode, eastus2 |
| Blob storage | `stcrawlosintai` | eastus2, RA-GRS, container `osint-staging` |
| Foundry | `copapfoundry-resource` | 48 agents (42 collectors + extractor + 4 synthesizers + darkweb) |
| Container Apps Jobs | `crawl-health-check`, `crawl-usage-monitor`, `crawl-weekly-copap-scan` | same env as gateway |

---

## 3 — Deploy a code change

```bash
# On copapdev_vm or any host with the repo + az login
cd ~/crawl

# 1. Edit + commit + push (CI/CD is git; image build is manual)
git add api/main.py
git commit -m "fix: X"
git push

# 2. Build new image. Tag bump: v1.10 → v1.11
az acr build \
  --registry copapcrawlacr \
  --image crawl-gateway:v1.11 \
  --file deploy/Dockerfile .

# 3. Roll the Container App
az containerapp update \
  --name crawl-gateway-v2 \
  --resource-group COPAPAI_Resource_Group \
  --image copapcrawlacr.azurecr.io/crawl-gateway:v1.11 \
  --revision-suffix v11

# Optional: also roll the Jobs (only if the change affects them).
# Jobs share the same image; bump them when you ship script changes.
az containerapp job update --name crawl-health-check       --resource-group COPAPAI_Resource_Group --image copapcrawlacr.azurecr.io/crawl-gateway:v1.11
az containerapp job update --name crawl-usage-monitor      --resource-group COPAPAI_Resource_Group --image copapcrawlacr.azurecr.io/crawl-gateway:v1.11
az containerapp job update --name crawl-weekly-copap-scan  --resource-group COPAPAI_Resource_Group --image copapcrawlacr.azurecr.io/crawl-gateway:v1.11

# 4. Smoke
curl -fsS https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io/api/v1/health
```

Current image: **`crawl-gateway:v1.10`** (as of 2026-06-28). Tag history
in `azurecr.io` via `az acr repository show-tags --name copapcrawlacr --repository crawl-gateway`.

---

## 4 — View logs

### Container App
```bash
# Last 300 lines (max allowed by --tail)
az containerapp logs show \
  --name crawl-gateway-v2 \
  --resource-group COPAPAI_Resource_Group \
  --tail 300 --format text

# Filter for errors
az containerapp logs show --name crawl-gateway-v2 -g COPAPAI_Resource_Group --tail 300 --format text \
  | grep -E "500 Internal|Traceback|ERROR|FAILED"

# Stream live (preview command)
az containerapp logs show --name crawl-gateway-v2 -g COPAPAI_Resource_Group --follow
```

### Container Apps Job execution
```bash
# Latest execution of a Job
az containerapp job execution list -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan \
  --query "[].{name:name, status:properties.status, start:properties.startTime}" -o table

# Logs of a specific execution
az containerapp job logs show \
  --resource-group COPAPAI_Resource_Group \
  --name crawl-weekly-copap-scan \
  --execution <execution-name> \
  --container crawl-weekly-copap-scan \
  --tail 200 --format text
```

### VM service logs (crawl-verify-new + crawl-darkweb-new)
```bash
# crawl-verify-new (private IP, SSH from copapdev_vm — same subnet)
ssh -i ~/.ssh/crawl_admin_key.pem copapadmin@172.20.0.26 \
  "sudo journalctl --user-unit verify-gateway -n 100 --no-pager"

# crawl-darkweb-new (different VNet; use Azure Run Command instead of SSH)
az vm run-command invoke -g COPAPAI_Resource_Group -n crawl-darkweb-new \
  --command-id RunShellScript --scripts \
  "sudo journalctl -u darkweb-gateway -n 100 --no-pager"
```

---

## 5 — Manually trigger a Container Apps Job

```bash
# Fire job with its default spec (uses image + args + env stored on the Job)
az containerapp job start \
  -g COPAPAI_Resource_Group \
  -n crawl-weekly-copap-scan

# For weekly scanner, run a dry-run instead of a real fire
# (write goes through env flag because Azure CLI's --args list parsing is finicky):
az containerapp job update -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan \
  --set-env-vars WEEKLY_DRY_RUN=1
az containerapp job start  -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan
# When done testing:
az containerapp job update -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan \
  --remove-env-vars WEEKLY_DRY_RUN
```

### Job cron schedule reference

| Job | Cron | Purpose |
|---|---|---|
| `crawl-health-check` | `*/15 * * * *` | Probes verify-gateway + darkweb-gateway + gateway → writes `pipeline_events` row + Teams alert on failure |
| `crawl-usage-monitor` | `55 7 * * *` | Daily 07:55 UTC — collects API spend per provider → writes `api_usage_daily` + Teams |
| `crawl-weekly-copap-scan` | `0 22 * * 0` | Sunday 22:00 UTC — scans 7 COPAP entities (darkweb + screening + media + CIR) → PDF to blob + Teams card |

---

## 6 — Add or update a Key Vault secret

```bash
# Set (creates new or new version)
az keyvault secret set \
  --vault-name crawl-kv \
  --name my-new-secret \
  --value "the-value"

# Read
az keyvault secret show --vault-name crawl-kv --name cir-api-key --query value -o tsv

# List
az keyvault secret list --vault-name crawl-kv --query "[].name" -o tsv

# Delete (90-day soft delete recovery window — vault has purge protection ON)
az keyvault secret delete --vault-name crawl-kv --name my-old-secret
```

**Hard rule:** never bake a secret in source. The vault is the only
canonical store. `api/keyvault.py` reads from `crawl-kv` via the Container
App's user-assigned MI (`crawl-gateway-mi`).

For a new secret to flow into running services: it's read on each
`get_secret()` call BUT cached in memory until process restart.
Bump the Container App revision after adding a secret consumed at startup.

---

## 7 — Query the PostgreSQL databases

```bash
# Get password from KV (one-shot)
PGPASS=$(az keyvault secret show --vault-name crawl-kv --name db-password --query value -o tsv)
export PGPASSWORD="$PGPASS"

# crawlmonitor (ops + observability)
psql "host=crawl-pg.postgres.database.azure.com dbname=crawlmonitor user=crawladmin sslmode=require"

# crawl_reports (CIR + dark-web reports + evidence/claims/renders for /cir/run)
psql "host=crawl-pg.postgres.database.azure.com dbname=crawl_reports   user=crawladmin sslmode=require"

# crawl_verification (every /verify call)
psql "host=crawl-pg.postgres.database.azure.com dbname=crawl_verification user=crawladmin sslmode=require"
```

**Common queries** in `CLAUDE.md` § "Querying". A few extras:

```sql
-- Recent failed jobs (last hour)
SELECT job_id, scenario, region, error FROM job_events
WHERE status='failed' AND event_time > now() - interval '1 hour'
ORDER BY event_time DESC;

-- 5xx response rate over last 6h
SELECT date_trunc('hour', event_time) AS hr,
       count(*) FILTER (WHERE status_code >= 500) AS errors,
       count(*) AS total,
       round(100.0 * count(*) FILTER (WHERE status_code >= 500) / count(*), 1) AS pct
FROM api_access_log
WHERE event_time > now() - interval '6 hours'
GROUP BY hr ORDER BY hr;

-- Recent CIR runs from /cir/run
SELECT id, entity_name, country, status, evidence_count, claim_count, error
FROM cir_runs ORDER BY started_at DESC LIMIT 20;
```

---

## 8 — Blob storage operations

```bash
# Generate a short-lived write SAS for ad-hoc ops
KEY=$(az storage account keys list -g COPAPAI_Resource_Group -n stcrawlosintai --query "[0].value" -o tsv)
SAS=$(az storage container generate-sas --account-name stcrawlosintai --account-key "$KEY" \
  --name osint-staging --permissions rwl --expiry $(date -u -d '+2 hours' '+%Y-%m-%dT%H:%MZ') --https-only -o tsv)

# List
az storage blob list --account-name stcrawlosintai --account-key "$KEY" \
  --container-name osint-staging --prefix "dark-web/" --num-results 50 -o table

# Download
az storage blob download --account-name stcrawlosintai --account-key "$KEY" \
  --container-name osint-staging --name "dark-web/some_entity_20260627.json" \
  --file /tmp/out.json

# Upload
az storage blob upload --account-name stcrawlosintai --account-key "$KEY" \
  --container-name osint-staging --name "handoffs/my_doc.md" \
  --file ~/my_doc.md --overwrite
```

**Long-lived SAS tokens** are in `crawl-kv`:
- `blob-sas-token` — rwl, used by Container App + Jobs + VMs for writes
- `blob-sas-token-read` — rl, given to consumer apps (GC, Onboarding)
Both expire **2027-06-27**. Rotation: re-run the generate command with a
new expiry and `az keyvault secret set`.

---

## 9 — SSH into the regional VMs

```bash
# crawl-verify-new (eastus2, same vnet as copapdev_vm)
ssh -i ~/.ssh/crawl_admin_key.pem copapadmin@172.20.0.26

# crawl-darkweb-new — different vnet, no peering possible.
# Routine ops: use Azure Run Command (no SSH needed).
az vm run-command invoke -g COPAPAI_Resource_Group -n crawl-darkweb-new \
  --command-id RunShellScript --scripts "<your one-line script>"

# Interactive SSH to darkweb when truly needed: use Azure Bastion
# (the public-IP SSH path is intentionally narrowed by NSG).
```

NSG rules currently in place:

| VM NSG | Inbound SSH source |
|---|---|
| crawl-verify-newNSG | `172.20.0.0/24` (the eastus2 subnet) |
| crawl-darkweb-newNSG | _none from public internet_ — Azure Run Command only |

To add a new SSH source:
```bash
az network nsg rule update -g COPAPAI_Resource_Group --nsg-name crawl-verify-newNSG \
  --name default-allow-ssh --source-address-prefixes "172.20.0.0/24" "<new-ip>/32"
```

---

## 10 — Trigger a CIR manually (smoke)

```bash
API_KEY=$(az keyvault secret show --vault-name crawl-kv --name cir-api-key --query value -o tsv)
BASE="https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io"

# Fire
RESP=$(curl -sS -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"country_code":"US","entity_name":"Stripe Inc"}' \
  "$BASE/api/v1/cir/run")
RUN_ID=$(echo "$RESP" | jq -r .run_id)
echo "RUN_ID=$RUN_ID"

# Poll (complete typically ~90-180s)
until S=$(curl -sS -H "X-API-Key: $API_KEY" "$BASE/api/v1/evidence/runs/$RUN_ID" 2>/dev/null); \
      echo "$S" | jq -r '.status' | grep -qE 'complete|failed|error'; do sleep 10; done
echo "$S" | jq

# Fetch the markdown CIR
curl -sS -H "X-API-Key: $API_KEY" "$BASE/api/v1/evidence/runs/$RUN_ID/renders" \
  | jq '.renders[] | select(.render_type=="cir_markdown") | .payload.input_payload.markdown' -r
```

---

## 11 — Common debugging recipes

### Foundry agent failing or hanging
Check whether the agent is deployed:
```bash
grep -r "foundry_agent_id" ~/crawl/agents/ | grep -v "^#" | awk '{print $1, $NF}'
```
Logs of recent agent runs are in `crawlmonitor.tool_call_log` (records
every tool call with input/output/timing). Foundry-side run state needs
the AgentsClient (see `api/cir_orchestrator.py` for the auth pattern).

**A collector run ends `incomplete` / the CIR fails with `collector incomplete`.**
Two distinct causes (both fixed in image v1.9, commit `142f1e4`):
- `reason: content_filter` — Azure OpenAI's prompt-shield blocked the run
  BEFORE any tool call (looks like a 3s "hang"). Triggered by *assertive*
  instruction phrasing. Keep the `cir_orchestrator.py` phase instructions
  PLAIN (see §13). Inspect the reason with the AgentsClient:
  `run = client.runs.get(...); run.incomplete_details`.
- The orchestrator used to treat only COMPLETED/FAILED as terminal, so an
  `incomplete` run was polled until the 300s timeout. `_run_agent_sync` now
  recognises `incomplete` + retries the country collector 3×.

### Container App reports KV "secret not found"
Means either (a) secret really not in KV, or (b) MI lacks Secrets User
role. Check:
```bash
az role assignment list --scope $(az keyvault show -g COPAPAI_Resource_Group -n crawl-kv --query id -o tsv) \
  --query "[?roleDefinitionName=='Key Vault Secrets User'].principalId" -o tsv
# crawl-gateway-mi principalId: 88fb9bb9-4044-4278-9296-f2199fd3cecb
```

### Verify endpoint returning empty / wrong results
1. Test via Container App: `curl -X POST $BASE/api/v1/verify -H "X-API-Key:$API_KEY" -d '{"entity_name":"X","country_code":"PK"}'`
2. If that fails, hit crawl-verify-new directly: `ssh -i ~/.ssh/crawl_admin_key.pem copapadmin@172.20.0.26; curl 127.0.0.1:8460/health`
3. Logs: `journalctl -u verify-gateway -n 200 --no-pager` (system unit `/etc/systemd/system/verify-gateway.service`)
4. Multilogin daemon status: `sudo systemctl status mlx`
5. **Multilogin `"MLX launch failed: can't lock profile"`** (verify returns
   `found:false` in ~2s for CN/PK/IN/SG/TR/AE — the only Multilogin countries;
   the rest use free gov APIs): **stale pool locks** left after a VM
   move/restart that never released (no browser actually running). Fix on
   crawl-verify-new:
   ```bash
   cp ~/mlx/profiles.lock ~/mlx/profiles.lock.bak.$(date +%Y%m%d)
   sudo systemctl stop verify-gateway mlx
   : > ~/mlx/profiles.lock         # truncate the stale checkout list
   sudo systemctl start mlx; sleep 12; sudo systemctl start verify-gateway
   ```
   Confirm: a CN `/verify` now launches a profile and scrapes the gov registry
   (the log shows e.g. `Tianyancha (...) via CN residential proxy`).

### Dark-web scan timing out
1. Check the VM is up: `az vm get-instance-view -g COPAPAI_Resource_Group -n crawl-darkweb-new --query "instanceView.statuses[].displayStatus"`
2. Probe: `az vm run-command invoke -g COPAPAI_Resource_Group -n crawl-darkweb-new --command-id RunShellScript --scripts "curl -sS http://127.0.0.1:8450/health"`
3. Tor bootstrap can be slow on fresh boot (~30s). Restart sequence: `sudo systemctl restart tor && sleep 15 && sudo systemctl restart darkweb-gateway`

### PostgreSQL write failures from Container App
1. Confirm KV `db-host` is `crawl-pg.postgres.database.azure.com` (NOT the old)
2. Confirm Container App can resolve + connect: hit `/api/v1/debug/pg-ping?host=crawl-pg.postgres.database.azure.com`
3. Check Postgres server status: `az postgres flexible-server show -g COPAPAI_Resource_Group -n crawl-pg --query state`

---

## 12 — Add a new COPAP entity to the weekly scanner

```bash
# Edit the entity list (lives in the repo, baked into the image)
cd ~/crawl
vi config/copap_weekly_entities.json
git add config/copap_weekly_entities.json
git commit -m "weekly scan: add NEW ENTITY NAME"
git push

# Rebuild + roll the scanner Job
az acr build --registry copapcrawlacr --image crawl-gateway:v1.11 --file deploy/Dockerfile .
az containerapp job update -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan \
  --image copapcrawlacr.azurecr.io/crawl-gateway:v1.11

# (Optional) Dry-run before Sunday:
az containerapp job update -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan \
  --set-env-vars WEEKLY_DRY_RUN=1
az containerapp job start  -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan
# Then remove flag:
az containerapp job update -g COPAPAI_Resource_Group -n crawl-weekly-copap-scan \
  --remove-env-vars WEEKLY_DRY_RUN
```

---

## 13 — Known wrinkles + workarounds

- **Keep agent instructions PLAIN — assertive phrasing trips the content
  filter.** The orchestrator's collector instruction once read *"Execute every
  step… ALL evidence_add and collector_complete calls REQUIRE run_id=… as the
  path parameter."* Azure OpenAI's prompt-shield flagged that as a
  jailbreak/injection → the run ended `incomplete (reason: content_filter)`
  in ~3s before any tool call (verified: the same agent runs fine with a plain
  instruction; the system prompt already enforces run_id). All three phase
  instructions in `cir_orchestrator.py` (collect / darkweb / extract) were
  de-risked (commit `142f1e4`, image `v1.9`). If you re-add detail to an
  instruction and CIRs start failing `incomplete` for one country, suspect
  this first.
- **`darkweb_collector` occasionally COMPLETES without firing `evidence_add`** —
  gpt-4.1-mini variability. Mitigated by orchestrator-side fallback in
  `cir_orchestrator._darkweb_fallback_persist` which detects the missing
  evidence row and calls `/sources/darkweb/scan` + persists via
  `evidence_db.add_evidence` (commit `199ccbe`).
- **`claim_extractor` occasionally passes a malformed UUID to `add_claim`** —
  same model variability. Gateway now validates UUID at the boundary and
  returns `422` with an actionable message (commit `c0064e6`).
- **`crawl-darkweb-new` is in vnet-westeurope** — peering to vnet-eastus2
  is permanently blocked by address overlap with copapfrance-vnet (which
  also claims 172.21.0.0/16). Container App reaches it via NSG-narrowed
  public IP (only `20.10.251.62/32` — the CAE static IP — allowed).
- **Container Apps Job `--args` list parsing is finicky** — use env-var
  flags (`WEEKLY_DRY_RUN=1`) for manual behaviour switches instead of
  trying to pass `--dry-run` as an arg via `az containerapp job start`.

---

## 14 — Emergency: roll back a Container App revision

```bash
# List revisions
az containerapp revision list --name crawl-gateway-v2 -g COPAPAI_Resource_Group \
  --query "[].{name:name, active:properties.active, image:properties.template.containers[0].image, created:properties.createdTime}" -o table

# Activate an older revision
az containerapp revision activate \
  --revision <revision-name> -n crawl-gateway-v2 -g COPAPAI_Resource_Group

# Or set traffic split
az containerapp ingress traffic set --name crawl-gateway-v2 -g COPAPAI_Resource_Group \
  --revision-weight <new-revision>=0 <old-revision>=100
```

---

## 15 — Decommissioned/retired

- `crawldevvm`, legacy `crawl-verify`, legacy `crawl-darkweb` — deallocated 2026-06-28
- 5 regional research VMs (americas/europe/gulf/india/china) — deallocated 2026-06-26
- `crawl-monitor-db` PostgreSQL (legacy in COPAPCrawl) — write-frozen 2026-06-26 23:19 UTC
- `crawlkeyvault` (legacy) — read-only fallback; `crawl-kv` is canonical
- `stcrawlosint` (legacy blob) — write-frozen 2026-06-27 11:30 UTC
- COPAPCrawl subscription — scheduled for delete ~2026-07-10

Recovery: any of the above can be reactivated/restarted within their soft-delete
windows (VM disks 90d, PG 7d post server delete, blob 30d, KV 90d).

---

## 16 — Where the source-of-truth lives

| What | Where |
|---|---|
| Code | https://github.com/teppinette/crawl |
| Image registry | `copapcrawlacr.azurecr.io/crawl-gateway:vX.Y` |
| Secrets | `crawl-kv` Key Vault |
| Data | `crawl-pg` (3 DBs) + `stcrawlosintai/osint-staging` |
| Architecture / context | `CLAUDE.md` in repo root |
| Operations | This file |
