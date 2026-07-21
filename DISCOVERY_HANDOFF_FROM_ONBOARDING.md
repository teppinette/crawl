# Discovery → Crawl Gateway: capability handoff & gap-check

**From:** Onboarding (Compliance Onboarding app)
**To:** Crawl gateway team
**Date:** 2026-07-21

## Why you're getting this

We are **retiring the GlobalCompliance (GC) web app.** GC currently hosts a
`POST /api/discover` endpoint that the onboarding new-counterparty **wizard** calls to
auto-fill company identity (name/country → LEI, reg number, address, listing, sanctions
flag). It's the **last live HTTP dependency** on the GC web app.

Good news: your gateway **already exposes an equivalent** — `POST /api/v2/lookup`. This
doc confirms the mapping and flags a short list of gaps so we can cut the wizard over to
crawl cleanly and turn GC off. (GC's `globalcompliance` DB and the .11 screening/CIR
engines stay — only the Flask **web app** dies.)

**Action requested from crawl:** confirm the 4 gap items in the last section (or tell us
they're out of scope), so onboarding can repoint `discovery_client.py` → `/api/v2/lookup`.

---

## The replacement: GC `/api/discover` → crawl `POST /api/v2/lookup`

Your `/api/v2/lookup` (`api/main.py:7052`) fans out registry-verify + GLEIF + adverse
media + enrichment + screening in one call and returns `{registry, lei, media,
enrichment, screening}`. That is the shape onboarding needs. GC's `/api/discover/apply`
writeback (fill-only into our tables) is **onboarding's job to reimplement locally** —
you do **not** need to build it.

### GC `/api/discover` request (what the wizard sends)
```json
{ "name": "AARTI DRUGS LIMITED", "country": "IN",
  "lei": null, "reg_number": null, "ticker": null, "exchange": null,
  "include_sanctions": true, "force_refresh": false,
  "requested_by": "user@copap.com", "requested_from": "onboarding-wizard" }
```
→ maps cleanly to `/api/v2/lookup` `{ entity_name, country_code, ticker }`.

### GC `DiscoveryResult` fields the wizard consumes → crawl source
| GC field | Crawl `/api/v2/lookup` source | Status |
|---|---|---|
| `legal_name`, `status`, `reg_number` | `registry{legal_name, status, registration_number, validation_source}` | ✅ |
| `lei` | `lei{lei, entity_name, parent, ultimate_parent, jurisdiction}` (GLEIF) | ✅ |
| `country`, `address{lines,city,postal_code,country}` | `registry` (US EDGAR full; other countries partial) | 🟡 partial by country |
| `ticker`, `exchange`, `isin` | US via EDGAR (`tickers/exchanges/cik`); KR via DART | 🟡 US/KR only |
| `sanctions_hit` | `screening{status, risk_level, total_hits, sources{}}` — 7 free lists inline | ✅ (see note) |
| `website` | `enrichment.profile.website` (Crunchbase/Deep Lookup) | ✅ |
| `tier`, `confidence`, `provenance`, `gaps` | onboarding computes from the above | ⚠ needs match-quality signals — **gap #1** |

---

## What crawl ALREADY covers (no action needed)

- **One-shot name+country discovery** — `POST /api/v2/lookup`, `entity_name` the only
  required field. ✅ direct `/api/discover` replacement.
- **GLEIF (LEI registry)** — `api/source_gleif.py`; LEI, legal name, registration number,
  HQ, parent/ultimate parent, status. ✅
- **SEC EDGAR (US)** — full CIK / EIN / SIC / tickers / exchanges / addresses. ✅
- **DART/FSS (KR)** — corp name, stock code, CEO, market, BRN. ✅
- **~43 country registries** (`agents/collectors/verify_<cc>.yaml`, name-first input) +
  generic `country_registry_lookup` with **OpenCorporates** cross-check. Covers GC's
  "Companies House GB / Brreg NO / 168-country fallback" role. ✅
- **Sanctions inline** — CSL (incl. OFAC SDN), UK OFSI/FCDO, EU, UN SC, FBI, INTERPOL,
  OpenSanctions — 7 free lists, returned inside `/api/v2/lookup`. ✅

**Net: ~90% parity today via `/api/v2/lookup`.**

---

## GAPS — the 4 items to confirm/close with crawl

**Gap #1 — per-source match-quality signals (needed for our tier/confidence).**
Onboarding must reproduce GC's confidence/tier math (below). To do that, the `/api/v2/lookup`
response needs to tell us, per source: (a) was the LEI **identifier-confirmed** vs
name-matched; (b) was the registry hit **by reg_number** vs by name; (c) was it a real
registry vs a Claude-agent fallback; (d) exchange **listed** yes/no; (e) sanctions
**hit / clear / unknown**. Today we see `registry.validation_source` and the `screening`
block — **please confirm whether these five signals are (or can be) exposed.** This is the
one thing that blocks a clean cutover.

> GC confidence rules we're reproducing: 1.00 LEI-confirmed-ACTIVE · 0.90 GLEIF-strict +
> registry same-country · 0.80 exchange-listed strict name · 0.70 registry by reg_number ·
> 0.50 GLEIF-strict alone · 0.40 registry by name · 0.30 Claude-agent only · 0.00 none.
> Tier 0 HOLD short-circuits on any sanctions hit.

**Gap #2 — non-US/KR exchange listing detection.** GC's EXCHANGE source claimed 10
exchanges (SEC/NSE/BSE/SGX/HKEX/DART/ASX/B3/PSX/KAP). Crawl has **US (EDGAR)** and
**KR (DART)** only; **NSE/BSE/SGX/HKEX/ASX/B3/PSX** have no adapter (GLEIF sets
`is_listed:False`, and these funnel through the generic registry loopback). **Decision:**
onboarding can fill global ticker/ISIN from **OpenFIGI on our side** (we already run it in
`public_company/openfigi_client.py`), so we do **not** need crawl to build these exchange
adapters — **unless** crawl wants listing-status centralized. Please confirm you're fine
leaving ticker/ISIN to onboarding's OpenFIGI.

**Gap #3 — OpenFIGI (ticker/ISIN global).** Not in crawl. **Staying on onboarding's side**
— no action for crawl. (Noted only so it's not assumed missing.)

**Gap #4 — interactive latency.** The wizard is a live form — it needs a **fast** lane
(p95 ≤ ~6s, hard cap ~10s, matching GC's budget). We know `/api/v2/enrich` is the slow
deep path (60–90s). **Please confirm `/api/v2/lookup`'s typical latency** and whether the
slow `enrichment` block can be made optional (e.g. a flag to skip Crunchbase/Deep-Lookup
so we get registry+LEI+screening fast, and fetch firmographics async later).

---

## Sanctions note (not a gap, just alignment)

Crawl's inline screening uses the **7 free lists**; **Bridger/LexisNexis stays GC-side /
onboarding's screening gateway** (by design). That's fine for discovery: the inline
screen is a **pre-flight HOLD signal** only. The authoritative Bridger/OFAC screen still
runs in onboarding's screening pipeline after the entity is created — discovery does not
replace it.

---

---

## ⚠ LIVE VALIDATION (onboarding, 2026-07-21) — two blockers found

We built the onboarding→crawl adapter and tested `POST /api/v2/lookup` against the
**live** gateway from .11 (with the real `CIR_API_KEY`). Two blockers that the
code-level parity review did not surface:

**Blocker A — latency.** `/api/v2/lookup` took **61s cold, 33–56s warm** (Apple/US,
Microsoft/US, Aarti Drugs/IN). The wizard budget is **<12s** (`DISCOVERY_TIMEOUT_SEC`).
Enrichment returned `{status:"disabled"}` (no Bright Data key), so enrichment is NOT the
cost — the registry-verify + screening fan-out itself is slow. **The "fast lane" is not
fast in practice.** We need either a genuinely interactive lane (≤12s) or a way to run
only the cheap blocks (LEI + registry summary) synchronously and defer screening/media.

**Blocker B — registry doesn't resolve in the composite.** For all three test entities
the `registry` block came back `{verified:false, legal_name:"", status:null,
summary:"", validation_source:null}` — **only the `lei` block resolved** (`found:true`).
So today `/api/v2/lookup` effectively returns **LEI data only**: no `registration_number`,
no `registered_address`, no `tickers/exchanges`, no `status`. That is far less than GC
`/api/discover` auto-filled. Those rich fields appear to live in **`/api/v2/verify`**, not
the `lookup` composite — please confirm the intended path: should onboarding call
`/api/v2/verify` for identity and `/api/v2/lookup` (or `/api/v2/screening`) for sanctions,
and what is `verify`'s latency + country coverage?

**Actual `/api/v2/lookup` response keys observed:** top-level `entity_name, country_code,
lookup_time_ms, registry, lei, media, enrichment, screening, timestamp`;
`registry{verified, legal_name, status, summary, validation_source}`;
`lei{found, lei, entity_name, parent, ultimate_parent, jurisdiction}`;
`screening{status, risk_level, total_hits, sources}`; `enrichment{status}`;
`media{total_articles, risk_level, error}`. (This is thinner than the code-path review
implied — no reg_number/address/tickers on `registry`.)

**Onboarding status:** the cutover client is BUILT and backend-switchable
(`DISCOVERY_BACKEND` env), adapter matched to the observed shape. Default stays **`gc`**
until A+B are closed; flipping to crawl is then an **env change, no code**. The two asks
above are now the only things between here and turning GC off.

## After crawl confirms (A + B closed)

1. Onboarding repoints `discovery_client.py` (`app/counterparties/`) from GC
   `/api/discover` → crawl `POST /api/v2/lookup`, with a response adapter (crawl blocks →
   our `DiscoveryResult` shape) + local `/apply` writeback.
2. Verify the wizard auto-fills from crawl.
3. GC web app then has **zero** HTTP dependents → turn it off.

Questions / to confirm the 4 gaps: reply here or ping onboarding.
