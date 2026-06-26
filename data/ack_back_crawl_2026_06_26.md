# ACK-back from crawl platform (2026-06-26)

**To:** Onboarding + GC ops
**Re:** `ack_crawl_2026_06_26.md`

---

## Received + confirmed on our side

We see your cutover landed cleanly. Old-gateway traffic from `104.209.146.16` went from ~28 requests/min → 2/min → 0; last call was `2026-06-26T19:06:35Z`. Bulk verify and PK SECP results match what you reported (`/api/v1/verify` for PACKAGES LIMITED returns `verified=true, founding_year=1956, registration_number=0000792`).

Thank you for the surprise correction — we were about to chase a 40k-hits/day phantom we didn't know about until the access log review.

## Your question — confirmed: all 4 endpoints are permanent

| Path | Status | Code location |
|---|---|---|
| `POST /tools/adverse_media` | ✅ Permanent | `api/main.py:6595` (+ `/tools/adverse_media/health` at `:6611`) |
| `POST /api/v1/linkedin/lookup` | ✅ Permanent | `api/main.py:5488` |
| `POST /api/v1/person-photo` | ✅ Permanent | `api/main.py:5710` |
| `POST /api/v1/cir/run` | ✅ Permanent | `api/cir_orchestrator.py:250` (router mounted at `/api/v1`) |

None of these are transitional, deprecated, or shimmed. Same definitions as on `crawldevvm` — the Container App image is built from the same `api/main.py` + `api/cir_orchestrator.py`. Future contract changes go through normal versioning (`/api/v2/...` for breaking changes), and we'll handoff again if anything moves.

`/api/v1/verify`, `/api/v1/verify/bulk`, `/api/v1/lookup` are also permanent.

## Deallocation timer

Per the agreement in the handoff doc, the 1-week observation window starts now (2026-06-26 ~19:30 UTC). Specifically:

- **Crawl-verify (legacy, in COPAPCrawl)** — deallocate target **2026-07-03**
- **Crawldevvm (legacy gateway, in COPAPCrawl)** — deallocate target **2026-07-03**
- COPAPCrawl subscription retire — after the deallocations stay green for another week (~2026-07-10)

If anything regresses on your side during the window, raise it and the timer pauses. If we see any old-gateway traffic resurge above zero we'll reach out before deallocation.

## DNS — separate workstream as agreed

Tracked. The custom-domain piece (`crawl.copap.com`) is your DNS provider action. When the CNAME `crawl` + TXT `asuid.crawl` records propagate, ping us — Container App side is `az containerapp hostname add` + `bind`, ~5 min total. Until then everyone keeps using the `…orangemoss-d67e0a38…` URL.

## Heads-up — extra wins shipped in the same session

In case useful:

- `/api/v1/verify/bulk` (now confirmed good on your 160-list) — keep using this over sequential `/verify` calls; concurrency defaults to 10, ramp via `options.concurrency` up to 30
- IN CIN-path name-match gate (kills the BOGUS_CONSTANT TWS-SYSTEMS bug)
- PK SECP rebuild via Multilogin + crawl-verify (76% fill rate on PK 17)
- Deep Lookup founding-year fallback wired for AE / MA / EG / TR when GLEIF / OC misses
- Foundry agent tool URLs updated (48 agents redeployed) — `/api/v1/cir/run` agent dispatch now points at the new gateway
- 2 of 3 cron jobs migrated from crawldevvm to Container Apps Jobs (health monitor, daily usage report). Weekly COPAP scan still legacy; we'll either rewrite or retire after the observation window.

## Contact

teppinette@copap.com or DM on Teams.
