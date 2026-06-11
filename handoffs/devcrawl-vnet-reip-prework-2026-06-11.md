# DevCrawl_VN re-IP — Step 1 inventory (2026-06-11)

**Purpose:** record every reference to crawldevvm internal IP (`180.20.0.5`) and
crawl-verify internal IP (`180.20.0.4`) so the maintenance window can replay
all updates in one pass with zero drift.

**Current state:**
- DevCrawl_VN address space: `180.20.0.0/16`
- crawldevvm internal: `180.20.0.5` (Tailscale: `100.68.236.16`, public: `20.94.45.219`)
- crawl-verify internal: `180.20.0.4` (no Tailscale, no DNS name — **IP-only reachable**)
- Egress IP (Bright Data sees this): `20.94.45.219` — **does NOT change on re-IP** (separate public IP)
- Public gateway hostname: `crawldevvm.eastus2.cloudapp.azure.com` (Let's Encrypt cert, unaffected by internal-IP change)

## A. Hardcoded refs in `~/crawl/` (this repo)

### Code (must edit + commit)
| File | Line | Reference | Notes |
|---|---|---|---|
| `api/main.py` | 54 | `# Multilogin modules now run on crawl-verify VM (180.20.0.4:8460)` | comment |
| `api/main.py` | 58 | `VERIFY_VM_URL = "http://180.20.0.4:8460"` | **load-bearing — gateway → verify VM dispatch** |
| `api/main.py` | 524 | comment | comment |
| `api/main.py` | 2773 | docstring | comment |
| `scripts/petrochem_scraper.py` | 5 | docstring | comment |

### Docs (update for accuracy)
| File | Line | Reference |
|---|---|---|
| `CLAUDE.md` | 508 | crawl-verify row in VM table |
| `CLAUDE.md` | 642 | section heading "(crawl-verify VM — 180.20.0.4)" |
| `CLAUDE.md` | 674 | China VM peering allow note (mentions 180.20.0.5) |
| `api/DATA_GATEWAY_V2_SPEC.md` | 661 | infra table |
| `api/SALESTRACKER_FUTURES_CONTRACT_SPEC.md` | 145 | log location ref |

## B. SSH state on crawldevvm

| File | What |
|---|---|
| `~/.ssh/config` | line 15 — `HostName 180.20.0.4` for `crawl-verify` host alias |
| `~/.ssh/crawl_known_hosts` | line 7 — `180.20.0.4 ssh-ed25519 ...` host-key pin |

## C. crawl-verify VM (`180.20.0.4`) side

- **Zero internal references to crawldevvm IP** (no code paths from crawl-verify → crawldevvm by IP). Crawl-verify is purely the dispatch target.
- `verify-gateway.service` reads `.env` which has `DB_HOST=crawl-monitor-db.postgres.database.azure.com` (FQDN, no IP). 
- No `/etc/hosts` entries pinning IPs.
- **Single point to update on this VM after re-IP**: nothing (the IP belongs TO this VM; just confirm the new IP comes up and the systemd unit binds correctly).

## D. Regional VMs — all 5 reference `180.20.0.5`

Identical refs across americas / europe / gulf / india (gulf user = `copadmin`):

```
~/crawl/config/proxy.env  lines 5–6
  NO_PROXY='localhost,127.0.0.1,20.94.45.219,180.20.0.5,169.254.169.254,...'
```

This is the NO_PROXY list so requests *to* crawldevvm (which call back from regional VMs) skip the Bright Data proxy. **Must update on all 5 VMs** (americas/europe/gulf/india/[+dark-web pending check]) on cutover.

Dark-web VM (`20.86.161.6`): nothing found in this sweep, but recommend re-checking after re-IP.

China VM (`184.0.0.4` peered): SSH timed out from this CLI session — not unusual, the route works from the python pool not interactively. **Must check separately:** china VM has `~/crawl/config/proxy.env` (same pattern) AND its NSG has `AllowSSH-crawldevvm-private` rule pinned to `180.20.0.5` priority 110 (per `project_china_peering_enabled.md`). NSG rule update is one of the load-bearing items.

## E. Tailscale

| Node | On tailnet? |
|---|---|
| crawldevvm | YES — `100.68.236.16` |
| crawl-verify | NO |
| regional VMs | NO |

**Implication:** internal-IP path is the only path. Cannot fall back to Tailscale magic-DNS for crawl-verify or regional dispatch.

## F. Items requiring az login (you / re-auth needed)

These I couldn't inspect from my shell (token expired) — please confirm before the window:

1. **NIC + subnet** of crawldevvm and crawl-verify:
   ```
   az network nic list -g crawldevvm_group \
     --query "[?contains(name,'devvm') || contains(name,'verify')].{name:name,ip:ipConfigurations[0].privateIPAddress,subnet:ipConfigurations[0].subnet.id}" -o table
   ```
2. **NSG rules referencing `180.20.0.4` or `180.20.0.5`** across all NSGs in `crawldevvm_group`:
   ```
   az network nsg rule list -g crawldevvm_group --nsg-name <each-nsg> \
     -o table | grep -E '180\.20\.0\.[45]'
   ```
   Specifically the `AllowSSH-crawldevvm-private` (priority 110) on **crawlchinavm NSG** (different RG/VNet — needs cross-RG update).
3. **VNet peering** between DevCrawl_VN and crawlchinavm-vnet — confirm the peer references the VNet by name not by IP block (it does — peerings are VNet-to-VNet, so the re-IP on our side shouldn't break the peering itself, but the NSG allow rule on the China side WILL need the new private IP).
4. **Address-space availability** in the target range. GC's France/BNP-SFTP VNet is on `180.20.0.0/16` (the collision). Suggest **`10.180.0.0/16`** (RFC1918, no future collision risk) — confirm no Azure-side conflict in your subscription.

## G. Memory side — 10 files reference the IPs

Mostly historical project notes (singapore bizfile, nsg hardening, v3 gateway, china peering, petrochem scraper, etc.). After cutover, update the user-facing ones (`project_verify_cutover_status.md`, `reference_handoff_blob_pattern.md`, etc.) to reflect new IPs. Older project notes (singapore bizfile provisioning, etc.) preserved as point-in-time records.

## H. Bright Data + external

- Egress public IP `20.94.45.219` is unchanged by an internal-IP move → **Bright Data whitelist unaffected**.
- The Let's Encrypt TLS cert is on the public FQDN, not internal IP → **GC clients calling `https://crawldevvm.eastus2.cloudapp.azure.com:8443` are unaffected**.

## Cutover checklist (use during the window)

1. Snapshot crawldevvm + crawl-verify VMs to backup vaults
2. Note current internal IPs as fallback (`180.20.0.5`, `180.20.0.4`)
3. `az vm deallocate` both VMs
4. Detach NICs, create new NICs in the new subnet (e.g. `10.180.0.0/24`)
5. Attach + `az vm start`
6. Confirm new private IPs: write them down before touching anything else
7. Update on crawldevvm:
   - `api/main.py` line 58 → `VERIFY_VM_URL = "http://<new-crawl-verify-ip>:8460"`
   - `~/.ssh/config` HostName for crawl-verify
   - `~/.ssh/crawl_known_hosts` (delete old, accept new on first SSH)
   - All 4 doc/comment mentions in CLAUDE.md / spec files / main.py comments
8. SSH to each regional VM (americas/europe/gulf/india/dark-web/china) and update `~/crawl/config/proxy.env` NO_PROXY (replace `180.20.0.5` with the new crawldevvm internal IP).
9. Update China VM's NSG `AllowSSH-crawldevvm-private` rule source IP.
10. Restart on crawldevvm: `systemctl --user restart copap-cir-api`
11. Restart on crawl-verify: `sudo systemctl restart verify-gateway`
12. Smoke test: `curl https://crawldevvm:8443/api/v1/verify` for KR, GB, NO (a multi-transport sample).
13. Tell GC team the new VNet CIDR — they run their peering commands.
14. Update memory + CLAUDE.md with new IPs (commit).
15. After peering confirmed: snapshot retention can be released after 7 days.

## Open question for you

Before scheduling the window:
- **New subnet preference?** `10.180.0.0/16` (RFC1918, recommended) vs `180.30.0.0/16` (what GC suggested). Both avoid the collision; RFC1918 is more future-proof.
- **Window length budget?** Realistic is 90–120 min including smoke tests and the rollback option.
- **Acceptable downtime for `/api/v1/verify`?** GC/Onboarding will see HTTP 500/timeouts during steps 3–11.
