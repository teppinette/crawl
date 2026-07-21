# Crawl-side reply — Onboarding `/api/discover` parity checklist

**Re:** `DISCOVERY_HANDOFF_FROM_ONBOARDING.md` (onboarding → crawl parity spec: replace GC
`/api/discover` name→identity lookup with a crawl-served path; apply/write-back stays
onboarding-side).
**From:** crawl (`~/crawl`, `github.com/teppinette/crawl`), gateway `crawl-gateway-v2`.
**Date:** 2026-07-21. **Reviewer:** crawl session.

---

## TL;DR — yes, this can be handled

**4 of 6 asks are already live; 2 are partial with small, well-scoped work.** The one thing
the wizard actually needs — an **interactive-latency name→identity fast lane** — **already
exists** as `POST /api/v2/lookup` (built for the iPhone app: one-shot, parallel fan-out, *not*
the 60–90 s deep CIR). The genuine gaps are narrow: (a) no **stock-exchange listing** source,
(b) **sanctions is not yet in the fast-lane fan-out** (the source exists; it just needs adding),
and a naming reconciliation: crawl screens sanctions via **OpenSanctions / OFSI / CSL, not
Bridger**. None are heavy builds.

Do **not** point the wizard at `/api/v2/enrich` — that is the slow deep-lookup (the CIR path,
~60–90 s). Use `/api/v2/lookup` for interactive.

---

---

## ⚠️ LIVE-TEST CORRECTION (2026-07-21, supersedes the code-level review below)

The onboarding session **tested the live gateway** — I had reviewed code + docstrings, not the
running endpoint. Corrections:

1. **`/api/v2/lookup` is NOT the fast lane.** Measured **70 s** (Apple US) — it bundles registry +
   LEI + **media + enrichment + screening**, and the deep tasks dominate. Do not point the wizard at it.
2. **The right identity endpoint is `POST /api/v2/verify`** — but its speed depends on the registry
   backend:
   - **API-backed registries are fast + rich:** Apple (US, SEC EDGAR) → **2 s**, returns legal name,
     CIK, status, address, **tickers `[AAPL]` + exchanges `[Nasdaq]`**, former names, `validation_source`.
     (This also partly covers the EXCHANGE ask — for US SEC filers.)
   - **Browser-scraped registries are slow / inconsistent:** Reliance (IN, MCA/Tofler via Multilogin)
     → **27 s**, reg# resolves but **no tickers**; Aarti (IN) → 7 s, **unresolved**; Samsung (KR) →
     2 s, **unresolved**.
3. **Net:** the interactive **≤12 s** budget holds only for API-backed countries. Non-US scrape-backed
   registries (IN, etc.) are 7–27 s and ticker-less, and name-resolution is hit-or-miss.

**Both onboarding blockers stand, refined:**
- **(A) Latency** — need a true interactive lane ≤12 s. Options: (i) API-first everywhere (retire
  browser-scrape on the interactive path), or (ii) **two-phase** — return instant identity from fast
  sources + background-fill the slow registries + defer screening/media entirely.
- **(B) Registry + exchanges in the composite** — `/api/v2/verify` resolves US richly (incl.
  tickers/exchanges) but non-US is inconsistent and ticker-less. Point the wizard at `/api/v2/verify`
  (not `/api/v2/lookup`), and close non-US resolution + ticker coverage.

**This is a genuine crawl-side ball.** Until (A) and (B) close, onboarding correctly keeps
`DISCOVERY_BACKEND=gc` (the GC kill is coded and one env flip away, held). The revised, honest
guidance below replaces "nothing blocks starting."

---

## Endpoint inventory (what to wire the wizard to)

| Endpoint | Purpose | Latency | Notes |
|---|---|---|---|
| **`POST /api/v2/lookup`** | **name → identity, one-shot** | interactive (parallel fan-out) | verify (registry) + GLEIF LEI + adverse media + enrich, in parallel. Body `{entity_name, country_code, ticker?, domain?}`. **This is the fast lane.** |
| `POST /api/v1/lookup` | **id → identity** (deterministic) | fast | when you already have a registry id (CIN/USCC/CIK/CNPJ/KvK/BRN…). Same shape as `/api/v1/verify`. |
| `POST /api/v1/verify` | name → registry record | fast (single registry) | per-country adapter loopback. |
| `POST /api/v1/sources/country_registry/lookup` | registry + OpenCorporates aggregator | fast | returns `{primary, aggregator, commercial}` blocks, each with `validation_source` (tier + confidence). |
| `POST /api/v1/cir/run` | **deep** CIR (dossier) | **~4 min** | agent-mesh; NOT for the wizard. |

Auth: `X-API-Key` (gateway key). All are token-auth on the gateway.

---

## The 6-point ask

| # | Ask | Status | Detail |
|---|---|---|---|
| 1 | Name→identity endpoint | ✅ **Yes** | `/api/v2/lookup` (name) + `/api/v1/lookup` (id) + `/api/v1/verify`. |
| 2 | Full field coverage | ✅ **Yes** | legal name, registration number, status, registration/incorporation date, registered address, directors, company type, LEI — from the registry `primary` + OpenCorporates `aggregator` blocks. |
| 3 | Per-source provenance + match-quality | 🟡 **Partial** | Every source returns a `validation_source` object (upstream source, **tier**, **confidence**: e.g. OpenCorporates → tier `COMMERCIAL_AGGREGATOR`, confidence `low`). A 5-level tier scale exists (`PRIMARY_GOVERNMENT > OFFICIAL_LIST > COMMERCIAL_AGGREGATOR > OSINT > DARKWEB`). **Gap:** there is no single **normalized numeric match-quality score (0–1)** per source for your tier/confidence math — today it's tier + a coarse confidence label. Small addition if you need a number. |
| 4 | All 4 source classes | 🟡 **3 of 4** | see the source-class table below. |
| 5 | Identity **+ sanctions in one call** | 🟡 **Partial** | `/api/v2/lookup` fans out registry + LEI + media + enrich, **but not sanctions**. The sanctions source (`opensanctions_search` / CSL) already exists and is used elsewhere in the pipeline — adding it as one more parallel task in the `v2_lookup` fan-out is a small change. |
| 6 | Interactive fast lane | ✅ **Yes** | `/api/v2/lookup` is exactly this — one-shot parallel fan-out, built for the iPhone app / quick lookups. The 60–90 s path (`/api/v2/enrich`, `/api/v1/cir/run`) is a *different*, deep path. |

---

## The 4 source classes

| Source class (onboarding spec) | Crawl status | Detail |
|---|---|---|
| **GLEIF** | ✅ **Live** | `source_gleif.py` / `gleif_lei_lookup`; in the `/api/v2/lookup` fan-out. |
| **REGISTRY** (Companies House, Brreg, 168-country fallback) | ✅ **Live, broad** | **43 direct national adapters** (deployed `verify_<cc>` collectors, incl. Companies House GB, Brreg NO, MCA IN, SEC US, Tianyancha CN, …) **+ 67-country aggregator scrape** (`aggregator.COUNTRIES`) **+ OpenCorporates (~140 jurisdictions)** as the generic fallback. **Coverage-count reconcile:** crawl's effective reach is ~**140+** jurisdictions, not exactly the spec's 168 — worth aligning the number, but the *fallback mechanism* the spec wants (a broad non-per-country path) exists (OpenCorporates generic collector, live 2026-07-21). |
| **EXCHANGE** (10 exchanges) | ⚠️ **Gap** | No dedicated stock-exchange / listing lookup. `/api/v2/lookup` accepts a `ticker` param but does **not** resolve exchange listing today (the only `listed_on` field in crawl is a *sanctions-designation regime*, unrelated). **This is the one genuine new source to build** (or confirm whether identity+registry+LEI already satisfies the wizard's exchange need — a listed company usually surfaces via GLEIF/registry). |
| **BRIDGER** sanctions | ⚠️ **Different provider** | Crawl screens sanctions via **OpenSanctions + OFSI + CSL**, **not Bridger**. Sanctions screening *capability* exists and is strong; the *provider* differs. **Decision needed:** does the wizard require Bridger specifically (that's GC/onboarding-side — GC already runs Bridger), or is crawl's OpenSanctions/CSL acceptable as the sanctions source for discovery? |

---

## Gaps & effort

1. **Add sanctions to the `/api/v2/lookup` fan-out** — small (source exists; add one parallel task + merge into the response). Closes ask #5 and gives identity+sanctions in one interactive call.
2. **Exchange-listing source** — the only net-new source. Scope depends on whether the wizard needs true exchange/ticker resolution or just "is this a listed entity" (which GLEIF/registry often already answer). Confirm the requirement before building.
3. **Bridger vs OpenSanctions** — a decision, not a build: accept crawl's OpenSanctions/CSL as the discovery sanctions source, or keep Bridger on the GC side and have discovery call both.
4. **Normalized per-source match-score (0–1)** — optional; only if your confidence math needs a number rather than tier + confidence label. Small.

**Nothing here blocks starting.** The wizard can wire to `/api/v2/lookup` **today** for identity + LEI + media + registry across ~140 jurisdictions at interactive latency; items 1–4 are incremental.

---

## Recommended path

1. Onboarding wizard → `POST /api/v2/lookup` for the interactive name→identity step.
2. Crawl adds the sanctions task to the `v2_lookup` fan-out (closes identity+sanctions in one call).
3. Reconcile Bridger-vs-OpenSanctions and the 140-vs-168 coverage number.
4. Decide exchange-listing scope; build only if truly required.

Reply / questions: raise on the crawl repo or ping the crawl session. Endpoint contracts for
`/api/v2/lookup` request+response can be dumped from `api/main.py` (`v2_lookup`) on request.
