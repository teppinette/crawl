# verify-gateway — THE verification process (single source of truth)

Entity verification is **one process**. This directory is the canonical mirror of
that process. Do not create a second copy.

## Where it runs
- Host: **crawl-verify-new** — `172.20.0.26`, `~/verify-gateway/`
- Service: `verify-gateway.service` → `uvicorn main:app --host 0.0.0.0 --port 8460 --workers 2`
- Browser layer: Multilogin/`mlx` (per-country residential exits) on the same VM.
- `main.py` imports + `init(get_secret)`s + dispatches **all ~46 `verify_<cc>.py`** country modules.

## Who calls it
The `crawl-gateway-v2` container does **NOT** verify. It **proxies** every request here
via `VERIFY_VM_URL` (see `api/main.py::_verify_vm_call`). The container only owns the
shared `source_*` data-lookup layer (GLEIF / sanctions / domain) served at `/sources/*`.

## The rule
- Enhance verification **here only**. There is no `api/verify_<cc>.py` anymore — those were
  dead pre-VM-split copies and were removed (2026-07-17) after they caused a drift incident.
- Deploy = `scp <file> copapadmin@172.20.0.26:~/verify-gateway/` then
  `ssh … sudo systemctl restart verify-gateway`. **Not** a container rebuild — the container ≠ this VM.
- After any on-VM hotfix, mirror it straight back here and commit, or it will be lost on rebuild.

## Data sources
- OpenCorporates is a **paid, approved** secondary (AR/AE/TR/EG/MA + `source_opencorporates`).
  Token in Key Vault `crawl-kv` → `opencorporates-token`. Keep it wired.
- Everything else (GLEIF, VIES, gov registries) is free / gov.
