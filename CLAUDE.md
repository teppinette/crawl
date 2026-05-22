# Crawl OSINT Intelligence Platform

## READ THIS FIRST
This project deploys autonomous OSINT researchers via a scenario-based
gateway API. It runs on the **COPAPCrawl** Azure subscription --
completely isolated from the production COPAP AI subscription.

**THIS IS crawldevvm (20.94.45.219)** — the dev/control VM that hosts
the Crawl Research Gateway API and SSH-coordinates all regional crawl VMs.

**NAMING RULE:** Never use the word "openclaw" in any Azure resource name,
VM name, VNet, NSG, command alias, or directory on VMs. Use "crawl" only.
The underlying tool is installed via npm but all our naming is "crawl-*".

**HARD RULE — DATA SANITIZATION:**
The word "COPAP" and ANY customer/supplier names must NEVER appear in:
- Any prompt sent to OpenClaw
- Any skill file deployed to regional VMs
- Any data visible to the research agents
OpenClaw must never know who is requesting the research or why.
The API enforces this with a sanitization layer that HARD FAILS on violations.

**CRITICAL ISOLATION RULES:**
1. NO production credentials ever go in this repo or on any crawl VM
2. NO VNet peering between COPAPCrawl and production COPAP AI subscription
3. The ONLY bridge to production is the `osint-staging` blob container (human review required)
4. Seed data (entity names + countries) is plain text only -- no EntityIDs, no financial data
5. API keys for LLM providers are separate from production keys
6. NEVER send customer/supplier names or any COPAP identifier to OpenClaw
7. Regional VMs are reachable ONLY from crawldevvm (NSG enforced)

---

## Architecture

```
GC App (172.20.0.11) / Phone (Tailscale 100.68.236.16)
  |  POST /api/v1/jobs {scenario: "cir"|"product-intel"|"dark-web", payload: {...}}
  |  POST /api/v1/research (backward compat, scenario=cir)
  |  GET  /api/v1/jobs/{job_id} (poll — includes dark_web alert block)
  |  Read blob from osint-staging (OSINT_BLOB_KEY)
  v
crawldevvm (20.94.45.219) — Crawl Research Gateway v3.0 (port 8400 / 8443 TLS)
  |  Tailscale: 100.68.236.16 (phone access via tailnet)
  |  systemd: crawl-gateway.service (4 uvicorn workers, auto-restart)
  |  nginx TLS reverse proxy on port 8443 (self-signed cert)
  |  Secrets: Azure Key Vault (crawlkeyvault) via managed identity
  |  DATA SANITIZATION: strips all COPAP/customer/supplier refs before dispatch
  |  Scenario routing: single-region (CIR), fan-out (product-intel), or darkweb
  |  ThreadPoolExecutor(10) — non-blocking SSH dispatch, backpressure at 20 jobs
  |  Auto-retry on failure (1 retry, 10s backoff)
  |  SFTP report back + blob upload via SAS token
  |  CIR auto-enrichment: dark-web scan runs after CIR completes, findings
  |    injected into blob (dark_web_screening, executive_summary, risk_assessment)
  |
  | SSH + paramiko (key: ~/.ssh/crawldevvm_key.pem)
  | NSG: ONLY crawldevvm can reach regional VMs
  |
  +-- crawl-americas  (172.206.2.41)   — US/CA/CO/BR/MX/CL/PE/AR
  |     SwarmClaw control plane (port 3456)
  +-- crawl-europe    (172.189.56.218) — TR/RU/BY/RS/EU/NG/UA
  +-- crawl-gulf      (20.233.46.58)  — AE/EG/PK/IQ/SA/QA/BH/KW/OM/JO  [user: copadmin]
  +-- crawl-china     (10.0.0.4)       — CN/HK/VN/MM/TW/KR/JP/SG/TH/MY/PH/ID
  +-- crawl-india     (20.193.150.43)  — IN
  +-- crawl-darkweb   (20.86.161.6)    — Tor research (Netherlands) [no OpenClaw]
  |     darkweb-gateway.service (port 8450, 37 sources via Tor)
  |     Free: Ahmia, Torch, DDG-Tor, DDG-adverse, HudsonRock, LeakIX,
  |           LeakCheck, BreachDirectory, Ransomlook, OCCRP Aleph,
  |           ICIJ Offshore Leaks, OpenSanctions, WikiLeaks, Telegram,
  |           Psbdmp, Web Archive, court records, PulseDive, FullHunt,
  |           Greynoise, IntelX (free tier)
  |     Paid: Dehashed ($15/mo — breach records with passwords/emails)

Each regional VM:
  OpenClaw Gateway (port 18789)
  Skills: counterparty_research, product_intel + region sources
  Model: anthropic/claude-sonnet-4-6 (china: deepseek-chat)
  Output -> ~/crawl/output/
  NEVER sees: COPAP name, customer/supplier names, internal refs

crawl-darkweb VM (NO OpenClaw):
  Tor daemon (SOCKS5 127.0.0.1:9050) + privoxy (HTTP 127.0.0.1:8118)
  darkweb_gateway.py (FastAPI, port 8450)
  API key: dwk_crawl_2026Q2_f8a3b7e1d9c4
  Receives sanitized queries from crawldevvm via SSH+curl
  Returns structured findings JSON with risk classification
  NSG: SSH only from crawldevvm, all other inbound denied

Production Bridge (human review):
  osint-staging blob -> analyst review (POST /review) -> approved -> ComplianceEntity
```

## Crawl Research Gateway v3.0 (crawldevvm)

**Location:** `/home/copapadmin/crawl/api/main.py`
**Port:** 8400 | **Service:** `crawl-gateway.service` (systemd, 4 workers)
**Auth:** `X-API-Key` header (from Azure Key Vault `cir-api-key`)
**TLS:** nginx reverse proxy on port 8443 (self-signed cert, IP SANs)

### Scenarios

| Scenario | Routing | Description |
|----------|---------|-------------|
| `cir` | Single region + dark-web auto-enrichment | Counterparty Intelligence Report (DD research). After CIR completes, gateway parses the blob for ALL discovered directors/UBOs/affiliates, then runs dark-web scan on all of them. Findings injected into blob (`dark_web_screening`, `dark_web_intelligence`, `executive_summary`, `risk_assessment`). API response includes `dark_web` alert block. |
| `product-intel` | Fan-out (multi-region) | Product pricing, sourcing, competitors, regulatory |
| `dark-web` | Direct to crawl-darkweb VM | Standalone dark web OSINT: 37 sources (36 free + Dehashed $15/mo). Searches entity + owners + domain across Tor search engines, breach databases, investigative databases, leaked documents, threat intel, certificate transparency, Interpol, World Bank debarment, URLScan. Supports entity + owners + domain. |

### Endpoints — Generic (new)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/jobs` | POST | Submit job for ANY scenario |
| `/api/v1/jobs/{job_id}` | GET | Check job status / get results |
| `/api/v1/jobs` | GET | List recent jobs (filter by ?scenario=) |
| `/api/v1/scenarios` | GET | List available scenarios |

### Endpoints — CIR (backward compatible)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/research` | POST | Submit counterparty for DD research |
| `/api/v1/research/{job_id}` | GET | Check job status / get results |
| `/api/v1/research` | GET | List recent jobs |
| `/api/v1/research/{job_id}/review` | POST | Submit analyst review (score 1-5) |
| `/api/v1/reviews` | GET | Review dashboard (per-region averages) |

### Endpoints — Entity Verification (NEW 2026-05-03, refreshed 2026-05-19)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/verify` | POST | Synchronous registry check (34 countries) — 2-15s |
| `/api/v1/verify/lei` | POST | GLEIF LEI corporate hierarchy lookup (cross-country) — 2-5s |
| `/api/v1/verify-job` | POST | Async full verification (registry + LinkedIn + dark web) — 1-5 min |
| `/api/v1/verify-job/{job_id}` | GET | Poll verify job (progressive results) |
| `/api/v1/verify-jobs` | GET | List recent verify jobs |

**GLEIF-LEI fallback primary source for CL, CO, HK, CH** (gov registries
all paywalled / auth-walled / SPA-blocked respectively — see §10.2 of
`api/MIGRATION_GUIDE.md`). Coverage limited to entities with LEIs (banks,
listed, large corporates); smaller entities return `found: false` with
explicit `note`. SA legal_name returned in Arabic; `status` normalized to
English vocab on the producer side; `status_raw` preserves the original
Arabic for audit. PE via Decolecta SUNAT (1K/mo free tier). TW via GCIS
open data — dataset IDs refreshed via Swagger when they renumber.
**Migration guide:** `api/MIGRATION_GUIDE.md` v1.2 documents per-country
behavior, field shapes, and Onboarding-side QA decisions.

### Endpoints — System

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/health` | GET | Health check (scenarios, threads, regions) |
| `/api/v1/regions` | GET | List regions + jurisdiction mappings |

### Data Sanitization (HARD RULE)

The API has a mandatory sanitization layer that:
1. **Strips internal fields** before building prompts: `copap_relationship`,
   `copap_products`, `copap_incoterms`, `source_report`, `priority`, etc.
2. **Scans all values** for blocked terms (COPAP, customer names, system names)
3. **Redacts** blocked terms to `[REDACTED]` and logs a warning (was: hard-fail HTTP 400)
4. **Final gate**: assembled prompt is scanned again before SSH dispatch

Blocked terms list is in `_BLOCKED_TERMS` in main.py. Add new terms as needed.

> **TEMP 2026-05-22:** step 3 was downgraded from hard-fail to silent redact
> while we triage which GC field/term combo trips false positives (was
> killing throughput with HTTP 400s). The `[REDACTED]` substitution still
> prevents leakage to OpenClaw, but we lose the loud backstop. Revert
> conditions in `memory/project_sanitizer_softened.md`. Grep
> `journalctl --user -u copap-cir-api` for `sanitize_payload redacted:` to
> see what's firing.

### CIR Dark Web Enrichment

Every CIR job automatically triggers a dark-web scan after the regional research
completes. The enrichment flow:
1. Regional VM completes CIR research → SFTP blob to crawldevvm
2. Gateway PARSES the CIR blob to extract ALL discovered directors, UBOs,
   shareholders, affiliates, trade names, and domain (not just seed data)
3. Gateway SSH-calls crawl-darkweb VM with entity + all discovered targets + domain
4. Dark-web gateway searches 37 sources via Tor (~30-60s)
5. Findings injected into CIR blob JSON at 4 locations:
   - `dark_web_screening` — risk level, key findings, breakdown, breach summary,
     infostealer summary, analyst note (same level as `sanctions_screening`)
   - `dark_web_intelligence` — full structured dataset: breach records by database,
     infostealer exposure, web mentions with fetched article content, clean sources list
   - `executive_summary` — dark web risk line appended
   - `risk_assessment` — dark web addendum appended
6. Enriched blob re-uploaded to osint-staging
7. `report_summary` in job gets markdown dark web table appended
8. API response `dark_web` field shows alert level at a glance:
   - `CLEAN` (0 findings), `LOW` (1-5), `MEDIUM` (6-15), `HIGH` (16+), `CRITICAL` (breach/darknet/sanctions)

### Dark Web Sources (22 total, $15/mo)

| # | Source | Cost | Type |
|---|--------|------|------|
| 1 | Ahmia | Free | Tor .onion search engine |
| 2 | Torch | Free | Tor .onion search engine |
| 3 | DuckDuckGo via Tor | Free | Anonymous web search (uses .onion endpoint) |
| 4 | DuckDuckGo adverse | Free | Targeted fraud/sanctions/leak keyword queries |
| 5 | **Dehashed** | **$15/mo** | Breach DB — emails, passwords, database names |
| 7 | LeakCheck | Free | Breach lookup by domain/email |
| 8 | BreachDirectory | Free | Breach search via RapidAPI |
| 9 | Psbdmp | Free | Pastebin dump aggregator |
| 10 | LeakIX | Free | Exposed services & data leaks |
| 11 | HudsonRock Cavalier | Free | Infostealer/credential exposure by domain |
| 12 | Ransomlook | Free | Ransomware group victim lists |
| 13 | OCCRP Aleph | Free | Organized crime & corruption project |
| 14 | ICIJ Offshore Leaks | Free | Panama/Paradise/Pandora Papers |
| 15 | OpenSanctions | Free | Global sanctions & PEP database |
| 16 | WikiLeaks | Free | Cables & leaked documents (exact phrase match) |
| 17 | Telegram (TGStat) | Free | Public channel/group search |
| 18 | Web Archive | Free | Removed/changed web content |
| 19 | Court records | Free | Legal filings via Tor-routed DDG |
| 20 | PulseDive | Free | Threat intel, IOCs, passive DNS |
| 21 | FullHunt | Free | Attack surface / exposed subdomains |
| 22 | Greynoise | Free | IP reputation / scanner detection |

### Dehashed API

**Account:** round.total.user@staycloaked.com
**API key:** stored in systemd env on crawl-darkweb VM
**Endpoint:** POST https://api.dehashed.com/v2/search
**Header:** DeHashed-Api-Key
**Cost:** $15/mo
**Rate limit:** 10 req/sec

### Multilogin Anti-Detect Browser (All Verification)

**HARD RULE:** ALL outbound HTTP from crawl-verify must go through Multilogin
anti-detect browser with country-targeted proxy. No curl_cffi, no direct requests,
no exposed VM IP. The proxy must exit from the target entity's country
(e.g., PT entity → PT exit IP). Only exception: proper APIs with API keys (e.g., EIA).

**Account:** teppinette@copap.com (Business 300, €75/mo, 300 profiles, ~1GB proxy/mo)
**Agent:** Multilogin X v12.2.0 on crawl-verify VM (`/opt/mlxapp/desktop.bin`)
**Services:** `xvfb.service` (virtual display :99) + `mlx.service` (agent)
**Proxy:** Multilogin residential (`gate.multilogin.com:8080`) with country targeting
  via `xcli proxy-get --country-code XX --protocol http --type rotating`
**CAPTCHA:** Claude Haiku vision OCR (~$0.001/solve)
**Shared helper:** `api/mlx_http.py` — provides `mlx_get()`, `mlx_post()`, `mlx_navigate()`
**Bespoke modules:** `multilogin_fbr.py` (PK FBR), `multilogin_dgft.py` (IN DGFT),
  `multilogin_bizfile.py` (SG Bizfile)

**Flow (mlx_http):** Acquire pool profile → set country proxy → launch headless →
Playwright CDP → execute fetch()/navigate → extract response → stop profile → return to pool.

**Flow (FBR):** Create temp profile → launch headless → Playwright CDP → navigate IRIS →
fill NTN → OCR canvas CAPTCHA → click Verify → parse result → stop + delete profile.

**Concurrency:** 5 pool profiles (shared across all countries).
**Bandwidth:** ~2.5 MB/lookup. 200 lookups/mo = ~500 MB (within 1 GB plan).

**Managing Multilogin:**
```bash
sudo systemctl status mlx               # agent status
sudo systemctl restart mlx              # restart agent
sudo systemctl status xvfb              # virtual display
/home/copapadmin/mlx/deps/cli/xcli profile-list    # list profiles
/home/copapadmin/mlx/deps/cli/xcli profile-stat    # running profiles
```

### Key design decisions
- Scenario-based routing: single-region, multi-region fan-out, or dark-web direct
- SSH dispatch in `ThreadPoolExecutor(10)` — API never blocks, backpressure at 20 jobs
- Auto-retry: 1 retry with 10s backoff on transient failures
- Rate limiting: 30 req/60s per IP via middleware
- Blob upload centralized on crawldevvm (SFTP from VM, then az upload via SAS)
- Blob paths: `<scenario>/<region>/<entity>_<date>.json`
- Dark-web VM has NO OpenClaw — standalone Tor gateway with its own API
- Dark-web enrichment is automatic for CIR — no extra API call needed
- OpenClaw agents NEVER know who requested the research
- ALL verification traffic via Multilogin anti-detect browser with country-targeted proxy
- Shared `mlx_http.py` module for simple API calls; bespoke modules for CAPTCHA sites

**Managing the service:**
```bash
sudo systemctl status crawl-gateway      # check status
sudo systemctl restart crawl-gateway     # restart
sudo systemctl stop crawl-gateway        # stop
journalctl -u crawl-gateway -f           # tail logs
sudo systemctl status nginx              # TLS proxy status
```

**TLS access:** `https://crawldevvm:8443/api/v1/health` (self-signed cert)
**Plain HTTP:** `http://crawldevvm:8400/api/v1/health` (still available, bind localhost later)
**Job files:** `/home/copapadmin/crawl/api/jobs/<job_id>.json` (cleaned up daily, archived after 30d)

## GC App Integration

The Global Compliance app (172.20.0.11) connects to this API via:
- `openclaw_bridge.py` — API client (submit, poll, read blob, format)
- `deepdive.py` — wired to submit -> poll -> read blob -> inject into synthesis
- Env vars on GC app:
  - `CIR_API_KEY=cpk_cir_2026Q2_a7f3e9d1b4c8`
  - `OSINT_BLOB_KEY=<read-only SAS token>` (expires 2027-04-13)

## Azure Resources (COPAPCrawl Subscription)

| Resource | Name | RG | Purpose |
|----------|------|----|---------|
| Resource Group | crawldevvm_group | -- | All crawl resources |
| Storage Account | stcrawlosint | crawldevvm_group | Report staging (East US 2, RA-GRS) |
| Blob Container | osint-staging | -- | JSON reports from all regions |
| VMs (5x regional) | crawl-{americas,europe,gulf,china,india} | crawldevvm_group | Regional OSINT agents |
| VM (dark web) | crawldarkwebvm | crawldevvm_group | Tor research (West Europe / Netherlands) |
| VM (verify) | crawl-verify | crawldevvm_group | Entity verification — 34 countries + GLEIF LEI (East US 2) |
| Key Vault | crawlkeyvault | crawldevvm_group | All platform secrets (East US 2, purge-protected) |
| Backup Vault | crawl-backup-vault | crawldevvm_group | VM backups — East US 2 (crawldevvm, americas, verify) |
| Backup Vault | crawl-backup-westeurope | crawldevvm_group | VM backup — West Europe (darkweb) |
| Backup Vault | crawl-backup-eastasia | crawldevvm_group | VM backup — East Asia (china) |
| Backup Vault | crawl-backup-centralindia | crawldevvm_group | VM backup — Central India (india) |
| Backup Vault | crawl-backup-francecentral | crawldevvm_group | VM backup — France Central (europe) |
| Backup Vault | crawl-backup-uaenorth | crawldevvm_group | VM backup — UAE North (gulf) |

## Azure Key Vault

**Name:** `crawlkeyvault` | **URL:** `https://crawlkeyvault.vault.azure.net/`
**Access:** System-assigned managed identity on crawldevvm (get + list)
**Helper module:** `api/keyvault.py` (caches all secrets in-memory, falls back to env vars)

**Stored secrets (41 as of 2026-05-22 — `az keyvault secret list --vault-name crawlkeyvault` for current count):**
| Secret | Used By |
|--------|---------|
| `cir-api-key` | Gateway API auth |
| `darkweb-api-key` | Dark web VM dispatch |
| `vm-token-{americas,europe,gulf,china,india}` | Regional VM auth (5 secrets) |
| `anthropic-api-key` | Usage monitor + FBR CAPTCHA solve |
| `deepseek-api-key` | Usage monitor, China VM |
| `tavily-api-key`, `perplexity-api-key`, `exa-api-key` | Usage monitor / plugins |
| `firecrawl-api-key`, `moonshot-api-key`, `sarvam-api-key` | Usage monitor / plugins |
| `brightdata-api-key` | Verify endpoint proxy |
| `multilogin-email`, `multilogin-password` | Multilogin API auth |
| `multilogin-folder-id`, `multilogin-profile-pk` | Multilogin profile management |
| `multilogin-proxy-user`, `multilogin-proxy-pass` | Multilogin PK residential proxy |
| `db-host`, `db-name`, `db-user`, `db-password` | Monitoring DB (PostgreSQL) |
| `blob-sas-token` | Blob uploads to osint-staging |
| `teams-webhook-url` | Teams alerts |

**Adding a new secret:**
```bash
az keyvault secret set --vault-name crawlkeyvault --name "my-new-key" --value "the-value"
```
Then use `get_secret("my-new-key")` in Python.

**NEVER hardcode secrets in source files.** Always use `keyvault.get_secret()`.

## Blob Storage

**Account:** `stcrawlosint` | **Container:** `osint-staging`
**Redundancy:** Standard_RAGRS (geo-replicated to Central US, read-access secondary)
**Soft delete:** Blob soft delete 30 days, container soft delete 30 days

**Two SAS tokens (both expire 2027-04-13):**
1. **Write token (rwl)** — on crawldevvm + all VMs at `~/crawl/config/blob_sas_token`
2. **Read token (rl)** — on GC app as `OSINT_BLOB_KEY`

**Upload flow:** crawldevvm SFTPs report from regional VM after research, then
uploads to blob. Regional VMs do NOT upload directly.

**Blob naming:** `<scenario>/<region>/<entity_snake>_<YYYYMMDD>.json`

**Listing blobs:**
```bash
SAS_TOKEN=$(cat ~/crawl/config/blob_sas_token)
az storage blob list --account-name stcrawlosint --container-name osint-staging \
  --sas-token "$SAS_TOKEN" --query '[].{name:name,size:properties.contentLength}' -o table
```

## PostgreSQL Databases

**Server:** `crawl-monitor-db.postgres.database.azure.com`
**User:** `crawladmin` (password in Key Vault `db-password`)
**Connection helper:** `api/keyvault.py` → `load_db_config()`

Three databases on the same server, segmented by purpose:

### crawlmonitor — Ops & Observability

| Table | Purpose | Writer |
|-------|---------|--------|
| `job_events` | Job lifecycle (submitted/dispatched/completed/failed) | main.py via event_log.py |
| `api_access_log` | Every HTTP request (IP, path, status, duration) | main.py middleware |
| `pipeline_events` | Infrastructure health (SSH, OpenClaw, SAS) | health_check.py (every 15m) |
| `api_usage_daily` | Daily API spend per provider | usage_monitor.py (daily) |
| `daily_prices` | Petrochem daily spot prices | petrochem scraper (daily cron) |
| `futures_prices` | Petrochem futures/contracts | petrochem scraper (daily cron) |

### crawl_reports — CIR Research Output

| Table | Purpose | Writer |
|-------|---------|--------|
| `cir_reports` | Completed CIR jobs with dark web enrichment summary | main.py via report_db.py |

Columns: `job_id` (UNIQUE), `entity_name`, `country`, `region`, `status`, `blob_path`,
`report_summary`, `dark_web_findings`, `dark_web_sources`, `dark_web_alert`,
`seed_data` (JSONB), `duration_ms`, `created_at`, `completed_at`

### crawl_verification — Entity Verification Results

| Table | Purpose | Writer |
|-------|---------|--------|
| `verification_results` | Every /api/v1/verify call (all countries) | main.py via report_db.py |

Columns: `entity_name`, `country`, `registry_source`, `status`, `verified` (bool),
`registration_number`, `registration_date`, `legal_status`, `address`,
`directors` (JSONB), `raw_response` (JSONB), `error`, `duration_ms`, `created_at`

### Writer Modules

- `api/event_log.py` — writes to `crawlmonitor` (job_events, api_access_log)
- `api/report_db.py` — writes to `crawl_reports` and `crawl_verification`
- Both use fire-and-forget background threads (never block the API)

**Querying:**
```bash
# Connect to any database
psql "host=crawl-monitor-db.postgres.database.azure.com dbname=crawlmonitor user=crawladmin sslmode=require"
psql "host=crawl-monitor-db.postgres.database.azure.com dbname=crawl_reports user=crawladmin sslmode=require"
psql "host=crawl-monitor-db.postgres.database.azure.com dbname=crawl_verification user=crawladmin sslmode=require"

# Recent job failures
SELECT job_id, scenario, region, error FROM job_events WHERE status='failed' ORDER BY event_time DESC LIMIT 10;

# CIR reports by country
SELECT entity_name, country, dark_web_alert, completed_at FROM cir_reports ORDER BY completed_at DESC LIMIT 10;

# Verification success rate by country
SELECT country, count(*), sum(verified::int), round(100.0*sum(verified::int)/count(*),1) as pct FROM verification_results GROUP BY country ORDER BY count(*) DESC;

# API access from unknown IPs
SELECT client_ip, count(*), min(event_time) FROM api_access_log WHERE status_code=403 GROUP BY client_ip;
```

## VM Specifications

| VM | IP | User | Primary Model | Region-Specific Sources |
|----|-----|------|---------------|------------------------|
| crawldevvm | 20.94.45.219 (TS: 100.68.236.16) | copapadmin | N/A (API host) | -- |
| crawl-americas | 172.206.2.41 | copapadmin | claude-sonnet-4-6 | SEC EDGAR, PACER, OFAC SDN/SSI/BIS, OpenCorporates, state SOS, RUES (Colombia), CanLII |
| crawl-europe | 172.189.56.218 | copapadmin | claude-sonnet-4-6 | EGRUL (DeepSeek), Companies House PSC, OpenSanctions, EU sanctions (14 packages), OCCRP, MERSIS, APR |
| crawl-gulf | 20.233.46.58 | **copadmin** | claude-sonnet-4-6 | JAFZA/DMCC/ADGM, Dubai DED, SECP, Iran front-co patterns, CBUAE sanctions, DIFC Courts |
| crawl-china | 10.0.0.4 | copapadmin | deepseek-chat | Qichacha, Tianyancha, NECIPS/GSXT, UFLPA Entity List, BIS MEU, wenshu.court.gov.cn |
| crawl-india | 20.193.150.43 | copapadmin | claude-sonnet-4-6 | MCA21, Zauba Corp, DIN cross-ref, GST Portal, DGFT IEC, eCourts, Indian Kanoon, NCLT, SEBI, RBI |
| crawl-darkweb | 20.86.161.6 | copapadmin | N/A (no OpenClaw) | 37 sources via Tor: Ahmia, Torch, Haystak, DDG-Tor, DDG-adverse, Onion.live, Dehashed($15/mo), HIBP, LeakCheck, BreachDirectory, Psbdmp, JustPaste.it, LeakIX, HudsonRock, GitHub, Ransomlook, OCCRP, ICIJ, OpenSanctions, OpenCorporates, Interpol Red Notices, World Bank Debarment, WikiLeaks, Telegram, Web Archive, Court records, Reddit, PulseDive, FullHunt, Greynoise, Shodan, VirusTotal, AlienVault OTX, AbuseIPDB, crt.sh, URLScan.io, IntelX |
| crawl-verify | 180.20.0.4 | copapadmin | N/A (API host) | 34 countries: PK,IN,SG,TR,AE,CN,GB,BR,US,KR,SA,CL,CO,PE,MX,IL,CA,FR,TW,EC,HK,CH,AU,JP,NL,IT,AR,EG,ES,DE,BE,PT,ZA,PL + GLEIF LEI. Multilogin anti-detect browser + Bright Data residential proxy. Port 8460. |

**IMPORTANT:** Gulf VM uses `copadmin` not `copapadmin` (typo during VM creation).
**IMPORTANT:** Dark web VM has NO OpenClaw — standalone Tor gateway (port 8450).
**IMPORTANT:** crawl-verify runs verify-gateway (port 8460), NOT OpenClaw. Multilogin agent + xcli at `/home/copapadmin/mlx/deps/cli/xcli`.

SSH key for all VMs: `~/.ssh/crawldevvm_key.pem`
SSH alias: `ssh crawl-darkweb` (configured in ~/.ssh/config)
Regional gateways on port 18789. Tokens in `~/.openclaw/openclaw.json`.
Dark web gateway on port 8450. API key: `dwk_crawl_2026Q2_f8a3b7e1d9c4`.

**Standard regional VM hardening (audited 2026-05-22 — all 5 VMs):**
- 2GB `/swapfile` enabled and persisted in `/etc/fstab` (prevents OpenClaw
  OOM crashes that surface as "Report file not found on remote VM"; americas
  was hit hardest at 3.8 GiB RAM, others have 7.7 GiB)
- sshd `MaxStartups 100:30:200` (default `10:30:100` lets the gateway's
  parallel paramiko connects trip the throttle → "Error reading SSH protocol
  banner [Errno 104]" → all jobs fail with "No existing session")

**SSH wedge recovery (when paramiko gets `Errno 104` from a regional VM):**
1. Stop gateway: `systemctl --user stop copap-cir-api` (kills the retry hammer)
2. Try plain SSH. If still banner-resets, the VM agent is wedged
3. `az vm get-instance-view -g crawldevvm_group -n <vmname> --query "instanceView.statuses"` —
   if ProvisioningState is stuck `Updating`, MDE.Linux extension is the
   common culprit
4. Nuclear: `az vm deallocate` then `az vm start` (IPs are static — won't
   change). Clean boot, ~3 min total
5. After boot, verify swap survived (`free -h`) and OpenClaw is listening
   (`ss -tln | grep 18789`)
6. Restart gateway: `systemctl --user start copap-cir-api`

## SwarmClaw Control Plane

**Location:** crawl-americas (172.206.2.41), port 3456
**Config:** `~/swarmclaw/config.json` (5 agents, jurisdiction routing, Azure blob storage)
**Service:** `systemctl --user {start|stop|status} swarmclaw`
**Dashboard:** `http://172.206.2.41:3456` (once build completes)

Features: jurisdiction-based routing, heartbeat monitoring (5min interval),
task board with auto-pickup, durable sessions, 30-day transcript retention.

Note: First start requires Next.js build (~3min on B2ms with 2GB swap).
Swap file at `/swapfile` (2GB) enabled on crawl-americas for this purpose.

## Directory Structure (crawldevvm)

```
~/crawl/
  CLAUDE.md              -- this file
  .env.example           -- API key template

  api/                   -- Crawl Research Gateway v3.0
    main.py              -- FastAPI app (port 8400, systemd-managed)
    mlx_http.py          -- Shared Multilogin HTTP helper (mlx_get/mlx_post/mlx_navigate)
    multilogin_fbr.py    -- FBR ATL via Multilogin anti-detect browser
    keyvault.py          -- Azure Key Vault helper (managed identity, caches secrets)
    event_log.py         -- Job event + API access logging to PostgreSQL (crawlmonitor)
    report_db.py         -- CIR report + verification persistence (crawl_reports, crawl_verification)
    proxy_cfg.py         -- Bright Data proxy config (residential + datacenter)
    verify_*.py          -- Country verification adapters (34 countries, mirrored to crawl-verify VM)
    crawl-gateway.service      -- systemd unit file (copied to /etc/systemd/system/)
    jobs/                -- Job state files (JSON per job_id, archived after 30d)
    openapi.json         -- API spec
    BUILD_SPEC_GC_HANDOFF.md -- GC integration spec

  config/                -- Local config (secrets in Azure Key Vault)
    blob_sas_token       -- Fallback SAS token (primary source: Key Vault)

  output/                -- Reports SFTPed back from regional VMs
    <region>_<entity>_<CC>_<date>.json

  reports/               -- Formatted reports + security audit
    SECURITY_AUDIT_20260503.md -- Full security audit report

  logs/                  -- Operational logs (rotated weekly, 4x, 10MB max)
    health_check.log     -- Health monitor output (every 15 min)
    usage_monitor.log    -- Daily spend report
    heartbeat_guard.log  -- Heartbeat watchdog
    job_cleanup.log      -- Job archival (daily 03:00 UTC)

  infra/                 -- Azure provisioning (az CLI)
  skills/                -- Custom SKILL.md files (deployed to VMs)
    counterparty-research/SKILL.md  -- CIR research skill
    product-intel/SKILL.md          -- product intelligence skill
    dark-web/SKILL.md               -- dark web intelligence skill
    region-{americas,europe,gulf,china,india}/SKILL.md
  scripts/               -- Operational helpers
  plugins/               -- OpenClaw plugins (deployed to VMs)
    crawl-gateway/       -- Gateway tools plugin (dark web, cross-region, status)
```

## OpenClaw Gateway Plugin (crawl-gateway)

**Installed on:** All 5 regional VMs (americas, europe, gulf, china, india)
**Source:** `~/crawl/plugins/crawl-gateway/` (on crawldevvm)
**Deployed to:** `~/.openclaw/extensions/crawl-gateway/` (on each VM)
**NSG rule:** `AllowAPI-RegionalVMs` (priority 220) — allows regional VM IPs to crawldevvm:8400

The plugin registers 5 agent tools that let OpenClaw agents interact with the
full Crawl platform when users chat with them directly (e.g. via Tailscale):

| Tool | Description |
|------|-------------|
| `dark_web_search` | Search 22 dark web/OSINT sources for entity/person/domain (30-90s) |
| `gateway_submit` | Submit research job to any scenario (CIR, product-intel, dark-web) |
| `gateway_status` | Check job status or list recent jobs |
| `report_search` | Search existing reports by scenario |
| `platform_health` | Check gateway health, scenarios, regions |

**Updating the plugin:**
```bash
# Edit source on crawldevvm
vim ~/crawl/plugins/crawl-gateway/index.js

# Deploy to a VM (repeat for each)
scp -i ~/.ssh/crawldevvm_key.pem ~/crawl/plugins/crawl-gateway/index.js \
  copapadmin@<VM_IP>:~/.openclaw/extensions/crawl-gateway/index.js

# Restart OpenClaw on that VM
ssh -i ~/.ssh/crawldevvm_key.pem copapadmin@<VM_IP> "openclaw gateway restart"
```

## Directory Structure (each regional VM)

```
~/crawl/
  config/blob_sas_token          -- SAS token (deployed from crawldevvm)
  output/                        -- Research JSON (written by OpenClaw agent)
  skills/counterparty-research/  -- core DD skill + region sources

~/.openclaw/
  openclaw.json                  -- gateway config (model, token, plugins)
  workspace/                     -- AGENTS.md, SOUL.md, IDENTITY.md, skills/
  agents/main/                   -- agent sessions
```

## Directory Structure (crawl-verify VM — 180.20.0.4)

```
~/verify-gateway/
  main.py              -- FastAPI verify app (port 8460, systemd-managed)
  mlx_http.py          -- Shared Multilogin HTTP helper
  keyvault.py          -- Azure Key Vault helper (same as crawldevvm)
  proxy_cfg.py         -- Bright Data proxy config
  multilogin_fbr.py    -- PK FBR ATL via Multilogin
  multilogin_dgft.py   -- IN DGFT IEC via Multilogin
  multilogin_bizfile.py -- SG Bizfile ACRA via Multilogin
  verify_*.py          -- 34 country adapters (mirrored from crawldevvm api/)
  verify_lei.py        -- GLEIF LEI corporate hierarchy lookup

~/mlx/deps/cli/xcli   -- Multilogin X CLI tool
```

**Service:** `verify-gateway.service` (systemd)
**Managing:** `sudo systemctl {status|stop|start} verify-gateway`
**Health:** `curl http://127.0.0.1:8460/health`

## Security Rules

### Secrets Management
- ALL secrets in Azure Key Vault (`crawlkeyvault`), accessed via managed identity
- Soft delete: 90 days | Purge protection: **ON** (secrets cannot be permanently purged)
- Helper module: `api/keyvault.py` — use `get_secret("name")` everywhere
- **NEVER hardcode secrets in source files** — vault is the single source of truth
- Adding a secret: `az keyvault secret set --vault-name crawlkeyvault --name X --value Y`

### Network Isolation (NSG Hardening — audited 2026-05-03)
- Regional VMs: SSH allowed ONLY from crawldevvm (20.94.45.219)
- China VM: also allows crawldevvm private IP (180.20.0.5) via VNet peering
- Regional VMs: OpenClaw gateway (18789) VNet-internal only
- crawl-darkweb: SSH allowed ONLY from crawldevvm, all other inbound denied
- crawldevvm: API (8400/8443) allowed from GC App + VPN + regional VMs only
- crawldevvm: SSH allowed ONLY from VPN (108.41.234.102)
- crawldevvm: Tailscale (100.68.236.16) — phone access bypasses NSG via tunnel
- crawldevvm: nginx TLS on port 8443, fail2ban on SSH (3 attempts = 1hr ban)
- All 7 NSGs have explicit DenyAllInbound as final rule
- Full NSG audit: `reports/SECURITY_AUDIT_20260503.md`

### SSH Hardening
- Host key verification via `~/.ssh/crawl_known_hosts` (ed25519 keys, 7 entries)
- `paramiko.RejectPolicy()` in all SSH connections (no AutoAddPolicy)
- No `StrictHostKeyChecking=no` anywhere in config or scripts
- SSH key: `~/.ssh/crawldevvm_key.pem` (mode 600)
- Password authentication disabled system-wide

### Observability
- **Job events**: every state transition logged to PostgreSQL `job_events` table
- **API access**: every HTTP request logged to PostgreSQL `api_access_log` table
- **Health checks**: every 15 min to PostgreSQL `pipeline_events` (14,900+ rows)
- **Daily spend**: API usage to PostgreSQL `api_usage_daily` + Teams alert
- **Auth failures**: logged to journal with client IP + key prefix
- **Log rotation**: logrotate weekly, journald capped at 500MB/30d

### Data Rules
- NEVER send COPAP name, customer/supplier names to OpenClaw
- API sanitization layer HARD FAILS on blocked terms (no silent redaction)
- No production DB credentials on any VM
- Only custom skills -- NO community/third-party skills
- Write SAS token: read + write + list, no delete (expires 2027-04-13)
- Read SAS token (GC app): read + list only (expires 2027-04-13)
- Blob staging is the ONLY data path to production
- Human analyst must approve every report before import
- API key auth on all gateway endpoints (including /api/v1/regions)
- Output files restricted to owner-read (mode 640)

## Backup & Disaster Recovery (configured 2026-05-07)

### VM Backups (Azure Backup, DefaultPolicy = daily, 30-day retention)

| Vault | Region | VMs Protected |
|-------|--------|---------------|
| crawl-backup-vault | East US 2 | crawldevvm, crawlamericasvm, crawl-verify |
| crawl-backup-westeurope | West Europe | crawldarkwebvm |
| crawl-backup-eastasia | East Asia | crawlchinavm |
| crawl-backup-centralindia | Central India | crawlindiavm |
| crawl-backup-francecentral | France Central | crawleuropevm |
| crawl-backup-uaenorth | UAE North | crawl-gulf |

**Managing backups:**
```bash
# List backup items in a vault
az backup item list --resource-group crawldevvm_group --vault-name crawl-backup-vault -o table

# Trigger on-demand backup
az backup protection backup-now --resource-group crawldevvm_group \
  --vault-name crawl-backup-vault --container-name <container> --item-name <item> --retain-until <date>

# List recovery points
az backup recoverypoint list --resource-group crawldevvm_group \
  --vault-name crawl-backup-vault --container-name <container> --item-name <item> -o table
```

### Storage Protection
- **stcrawlosint**: Standard_RAGRS (geo-replicated to Central US)
- Blob soft delete: 30 days | Container soft delete: 30 days
- Accidental deletes recoverable for 30 days

### Key Vault Protection
- Soft delete: 90 days | Purge protection: ON
- Deleted secrets recoverable for 90 days, cannot be permanently purged

### PostgreSQL (crawl-monitor-db)
- 3 databases: `crawlmonitor` (ops), `crawl_reports` (CIR output), `crawl_verification` (verify results)
- Auto-backup: 7-day retention, same region (East US 2)
- Geo-redundancy: disabled (acceptable for monitoring + research data)

## Cost Estimate

| Item | Monthly |
|------|---------|
| 5x regional VMs (auto-shutdown) | $150-200 |
| 1x dark-web VM D2s_v3 (auto-shutdown 22:00 UTC) | $40-60 |
| Dehashed API (breach database) | $15 |
| Multilogin Business 300 (FBR anti-detect browser) | $80 |
| Storage account (stcrawlosint, RA-GRS) | $8-10 |
| Azure Backup (8 VMs, daily, 30-day retention) | $80-120 |
| Networking/egress | $10-20 |
| Claude API (4 VMs + FBR CAPTCHA) | $50-100 |
| DeepSeek API (China) | $15-30 |
| Sarvam API (India backup) | $5-10 |
| **Total** | **$455-645/month** |
