# Crawl → GlobalCompliance handoff response

**Date:** 2026-06-09
**From:** Crawl team (crawldevvm)
**Re:** Your handoff doc "copapcrawl / copapverify — Integration Handoff"
**TL;DR:** Your doc is correct that `/api/v2/enrich` was built but unconsumed.
While reviewing, we found and shipped a **wrong-company match bug** in `/enrich`
that would have silently fed garbage into your CIR the moment you wired it in.
Fix is additive. Read §3 before you start wiring.

---

## 1. What we confirmed on the gateway

| Your claim | Status |
|---|---|
| `/api/v2/enrich` and `/api/v2/media` exist but no client consumes them | ✅ Confirmed. Routes are mounted, healthy, returning data. Just no caller. |
| Firecrawl is retired; import guard in place | ✅ Confirmed. Nothing on our side calls it. The ~99 dead refs in `deepdive.py` are your cleanup; we have no equivalent residue. |
| `CIR_API_KEY → LINKEDIN_API_KEY` fallback works | ✅ Same gateway, same key. Both names resolve to the value of `crawlkeyvault/cir-api-key`. Current prefix is `cpk_cir_`. |
| Schema versioning via `X-Schema-Version` is enforced | ✅ Our endpoints pin per-route versions (see `_ENDPOINT_VERSIONS`). The fix below is **additive only** — your pin will not break. |

`GET /api/v2/health` is the right thing to probe before wiring anything. As of
today it reports `enrichment: { CRUNCHBASE: up, DEEP_LOOKUP: up }`, but see §4
on DEEP_LOOKUP latency reality.

---

## 2. Endpoint surface (what's live, what you should/shouldn't call)

All of these are mounted on the gateway and authenticated with the same key:

| Route | Status | Notes |
|---|---|---|
| `/api/v2/screening` | ✅ live, you already use it | Sanctions cross-check (CSL/UK/UN/FBI/Interpol/EU/OpenSanctions-via-Bridger) |
| `/api/v2/verify` | ✅ live | 77 countries via crawl-verify VM. Sync, 2-15s. |
| `/api/v2/verify/lei` | ✅ live | GLEIF hierarchy, 2-5s |
| `/api/v2/verify/pan`, `/api/v2/verify/gstin` | ✅ live | India-specific |
| `/api/v2/media` | ✅ live, **unconsumed by you** | GDELT + BD SERP + BD Discover + crt.sh + Wayback. Bing/SerpAPI retired. |
| `/api/v2/enrich` | ✅ live, **unconsumed by you** | Crunchbase + Deep Lookup. See §3 + §4 below before wiring. |
| `/api/v2/lookup` | ✅ live | Fan-out: verify + LEI + media + enrich in parallel. Consider this for your scale-card use case — one call, all signals. |
| `/api/v2/raw/{id}` | ✅ live | 90-day retention, ~150 MB stored. Pull the raw HTTP cycle behind any structured response for audit. |
| `/api/v2/health` | ✅ live | Per-source health + p95 budgets |

---

## 3. The wrong-company match bug we just shipped a fix for

### What was wrong

`/api/v2/enrich` built Crunchbase URLs from two slug sources (entity name,
domain hostname) and deduped them. For a payload like
`{"entity_name": "Tesla Inc", "domain": "tesla.com"}`:

- name slug: strip " inc" → `tesla`
- domain slug: split on "." → `tesla`
- dedupe → only one URL tried: `crunchbase.com/organization/tesla`

That slug is owned by a Panama taxi-leasing company. Tesla Inc lives at
`/tesla-motors`. The validation logic correctly detected the mismatch
(`tesla.com` ∉ `tesla-pa.com`) and tagged the result with a `match_warning`,
but the response builder **dropped that field on the floor**. You would have
received the Panama Tesla's profile with `status: "partial"` and zero signal.

This fails *loudest* exactly when DEEP_LOOKUP is degraded (frequently — see §4).
On a healthy DL run, DL would have correctly identified Tesla Inc and the
silent slug failure was masked. Worst-case combo: DL down + slug collision +
silent Crunchbase fallback. Common enough we'd have hit it in production.

### What we shipped (additive contract change)

Two surgical edits in `enrichment.py`:

1. **Refuse unvalidated Crunchbase profiles** when a domain was provided.
   Returns `profile: null` instead of the wrong company.
2. **Surface `match_warning`** into `providers.<NAME>.match_warning` so you
   can see what happened.

### Example: Tesla with domain hint, post-fix

```json
{
  "status": "error",
  "profile": null,
  "providers": {
    "CRUNCHBASE": {
      "status": "ok",
      "source_url": "https://www.crunchbase.com/organization/tesla",
      "match_warning": "Crunchbase result website doesn't match domain 'tesla.com'"
    },
    "DEEP_LOOKUP": { "status": "error", "error": "DEEP_LOOKUP: timed out (75s)" }
  }
}
```

### What this means for your client

- **`profile` can now be `null` even when one provider returned `status: ok`.**
  Null-check before consuming.
- **New optional field: `providers.<NAME>.match_warning: string`.** Surface it
  to the CIR / scale card as a "low confidence" flag, or filter the row out.
- **`status` semantics shifted slightly:** `"error"` no longer means "both
  providers errored." It can also mean "validation refused all candidates."
  Treat anything other than `"complete"` as non-authoritative.
- **Always pass `domain` if you have it.** With domain, the gateway validates
  and refuses on mismatch. Without domain, we trust Crunchbase blindly and
  the same slug collision could still bite you silently. Tesla Inc is the
  poster child but anyone whose canonical Crunchbase slug got squatted will
  fail the same way.

### Schema-version pin

Additive only. We did not rev `_ENDPOINT_VERSIONS["/api/v2/enrich"]` because
existing field shapes are unchanged and new fields are optional. If your pin
is configured to reject *unknown* optional fields, loosen it.

---

## 4. Operational gotchas before you wire it

- **DEEP_LOOKUP regularly times out at the 75s soft ceiling.** Documented
  p95 is 75000ms (`_ENDPOINT_COSTS`). In our two probes today, it timed out
  both times. Crunchbase carried the response. Your client **must** set a
  >75s read timeout and **must** treat `status: "partial"` as a normal,
  non-error outcome. If you fail-fast on partial, you'll see ~50% "failures"
  that are actually fine.
- **Crunchbase latency is also slow** (38-47s per probe). `/enrich` is not a
  hot-path call; budget accordingly.
- **For your scale-card use case, consider `/api/v2/lookup` instead.** It
  fans out verify + LEI + media + enrich in parallel — one call, one timeout,
  combined response. If you're going to call enrich + media + verify anyway,
  one call is cheaper.
- **Slug-collision tax is real.** Tesla, Apple, Amazon all own their canonical
  Crunchbase slugs and resolve fine. But common-word company names ("Atlas",
  "Pioneer", "Phoenix") are routinely squatted. Always pass `domain`.

---

## 5. Open items we are NOT shipping yet — your call

### 5a. Same bug class, different code path (`enrichment.py:422-426`)

When DEEP_LOOKUP returns a profile (primary) and Crunchbase is unvalidated
(wrong company), we still backfill `industries`, `funding`, `leadership`,
`financials`, `social_media` from the wrong-company Crunchbase profile onto
the right-company DL profile. **Cross-contamination of the response.**

For Tesla: DL would correctly return Tesla Inc's profile, then we'd overwrite
the empty `industries` field with `["Automotive", "Financial Services"]` from
the Panama taxi co. Subtle, dangerous, in your scale-card territory.

3-line fix. Tell us if you want it shipped before you start wiring.

### 5b. Crunchbase search fallback

When name/domain slugs miss or fail validation, we currently give up. The
right behavior is to fall through to BD's discover/SERP path with
`"<entity> site:crunchbase.com"`, pick the top result, scrape *that* URL.
This would rescue Tesla Inc, every common-word company name, and most
non-English entities. Bigger change. Tell us if you want it scoped.

---

## 6. Notes on your handoff doc

Two small corrections / clarifications:

- **"Confirm the live surface with `GET /api/v2/health` before relying on any
  endpoint."** Agreed, but be aware `/health` is slow (~5-15s) because it
  does live reachability probes per provider. Don't put it on a hot path.
  Cache the result for ~60s if you poll.
- **"copapverify is the verify-job endpoint on the same gateway."** Mostly
  right. `/api/v1/verify-job` runs *on* crawldevvm but the registry adapters
  it calls live on the separate `crawl-verify` VM (port 8460). For your
  purposes that distinction doesn't matter — one URL, one key — but if you
  see a job error with `verify-vm unreachable`, the issue is on our internal
  VM, not the gateway.

---

## 7. What we recommend you do next

In priority order, for the scale-card / CIR website-comprehension work:

1. **Wire `/api/v2/enrich`** with the contract notes from §3, **always
   passing `domain`** when you have it (LinkedIn `company.website` is a fine
   source).
2. **Handle `match_warning` and null profile** as a low-confidence outcome.
   Either flag it on the CIR card or fall back to LinkedIn + Foundry
   knowledge (your current `assess_company_scale` path).
3. **Replace `_fetch_website_excerpt`'s `requests.get` with `/api/v2/enrich`**
   or `/api/v2/lookup`. Per your own rule (no ad-hoc scraping), this is the
   sanctioned path.
4. **Ask us to extend the fix to §5a** before you start consuming the
   structured fields (`industries`, `financials`, etc.) — otherwise the
   cross-contamination will quietly poison the scale call.
5. **Optional: ask us to scope §5b** if slug collisions become a frequent
   complaint in QA.

---

## 8. Contact

If you hit anything weird wiring `/enrich`, ping the Crawl team. Smoke-test
endpoint:

```bash
curl -s -m 90 -X POST https://crawldevvm.eastus2.cloudapp.azure.com:8443/api/v2/enrich \
  -H "X-API-Key: $CIR_API_KEY" -H "Content-Type: application/json" \
  -d '{"entity_name":"Apple Inc","country_code":"US","domain":"apple.com"}'
```

Expected: `status: "complete"` or `"partial"`, `profile.name: "Apple"`,
`profile.website: "https://www.apple.com"`, no `match_warning`.
