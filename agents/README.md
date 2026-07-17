# Verification agents — one platform, versioned, bank-auditable

Entity verification is **one process** expressed as **reusable Foundry agents**.
Per-country collectors + shared synthesizers/extractors/screening all live here as
YAML we build upon. The actual gov-registry scraping runs on the crawl-verify VM
(`../verify-gateway/`) *behind* the `country_registry_lookup` tool — the agents never
scrape directly, they call reusable `$ref` tools in `tools/`.

## Layout
```
agents/
  collectors/verify_<cc>.yaml   per-country evidence collectors (Foundry)
  synthesizers/*.yaml           cir_markdown, ubo_map, banker_audit_pack, sanctions_screening
  extractors/, screening/       claim_extractor, darkweb_collector
  tools/*.openapi.yaml          reusable tool specs ($ref'd by agents)
  MANIFEST.yaml                 GENERATED registry: every agent + version + hash + live agent_id
  DEPLOY_LOG.md                 GENERATED append-only trail: what was deployed, when
```

## Versioning + audit trail — "in case a bank asks"
Every deployable agent carries a machine-managed `audit:` block:
```yaml
audit:
  version: 1.4.0            # semver — YOU own this; bump on any behaviour change
  content_hash: sha256:...  # auto — over the deploy-relevant content only
  stamped_git_sha: a1b2c3d  # auto — HEAD when the hash was computed
```
`content_hash` covers exactly what ships to Foundry (name, description, model,
system_prompt, tool refs) — nothing else. So a comment or reorder won't churn it,
but a prompt/tool/model change will.

## The workflow (do this, every time)
1. Edit an agent YAML (or, better, its base overlay — see roadmap).
2. If behaviour changed, **bump `audit.version`** (semver).
3. `python3 scripts/agent_version.py stamp`   → refreshes content_hash + git_sha.
4. `python3 scripts/agent_version.py manifest` → refreshes MANIFEST.yaml.
5. Commit. (CI/pre-deploy guard: `agent_version.py stamp --check` fails if any agent
   is unstamped, stale, or changed without a version bump.)
6. Deploy **from copapdev_vm** (SDK + managed identity):
   `python3 scripts/deploy_agent.py agents/collectors/verify_<cc>.yaml`
   - refuses a dirty/unstamped file (deployed must == committed),
   - appends `DEPLOY_LOG.md` with version + hash + git_sha + the new `foundry_agent_id`.
7. Commit the updated YAML (agent_id) + DEPLOY_LOG.md.

To answer a lender "how was entity X verified on date D": find the run's collector +
its `foundry_agent_id` in DEPLOY_LOG.md → the row gives version + content_hash + git_sha
→ `git show <sha>:agents/collectors/verify_<cc>.yaml` is the exact logic that ran, and
the previous row is the diff of what changed.

## Roadmap (see project_crawl_verify_yaml_agent_platform_northstar)
- P1 ✅ versioning + audit backbone (this).
- P2 shared `collectors/_base.yaml` + thin per-country overlays (kill the 42-duplicate drift).
- P3 close coverage drift (GR collector; reconcile IN/SG) + lockstep check yaml↔python;
  stamp each CIR run's evidence rows with the producing agent_version.
