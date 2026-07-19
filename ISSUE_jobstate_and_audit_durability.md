# ISSUE — job-state durability + droppable audit (latent slip-through risks)

**Found:** 2026-07-01 (during a read of the gateway to model a sibling scoring service on it).
**Severity:** Medium (works today via a workaround; can silently lose jobs/audit under failure).

Crawl is a solid pattern, but two choices can let a job or its audit trail slip through. Flagging
so they're a conscious decision, not a surprise later.

## 1. Primary job STATE is file-based
`api/main.py`: `save_job()` writes `JOBS_DIR/{job_id}.json`, `load_job()` reads it, `list_jobs()`
does `glob("*.json")`, concurrency via file-locks. Cross-replica loss was already hit (`deploy/
container-app.md`: *"without this `/verify/bulk` polls 404 on different replica"*) and patched with
an **Azure Files** mount.
- **Why it's risky:** a network file share for primary state is not transactional, not queryable
  ("which jobs are stuck/incomplete?" = a glob + parse), and lock-contention-prone. If a replica
  dies mid-write, a job file can be partial/orphaned.
- **Suggested fix:** keep run STATE in Postgres (you already run `crawl-pg`) — one `runs` row per
  job with status; files/blob only for large artifacts. Queryable, ACID, survives replicas natively.

## 2. Audit + result writes are fire-and-forget (can be dropped)
- `api/event_log.py`: *"If the DB is unreachable, events are logged to stderr and **dropped** (no
  queue buildup)."*
- `api/report_db.py`: `_bg_write` writes results on a **background thread**.
- **Why it's risky:** under DB blips or a crash before the bg thread flushes, a job can complete
  with **no durable record** — you can't later prove it ran or what it produced.
- **Suggested fix:** for anything you must be able to prove happened, write it **synchronously /
  transactionally** (commit before returning success), or persist to a durable queue and drain —
  don't drop.

## Not asking for a rewrite
For on-demand CIR this is mostly fine. Raising it because (a) it's a real failure mode under
load/restart, and (b) the new **scoring platform** (auditable, "nothing slips through") deliberately
does NOT copy these two patterns — it uses DB-backed state + transactional writes + a reconciliation
sweep. Happy to help port those ideas back here if useful.
