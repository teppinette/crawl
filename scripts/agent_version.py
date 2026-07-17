#!/usr/bin/env python3
"""
Version + audit-trail tooling for the crawl verification agent platform.

Every deployable agent YAML under agents/{collectors,synthesizers,extractors,
screening} carries a machine-managed `audit:` block so we can answer, months
later and in a lender's language, "which agent version verified this
counterparty, and what changed since":

    audit:
      version: 1.4.0            # semver — HUMAN-owned; bump when behaviour changes
      content_hash: sha256:...  # auto — over the deploy-relevant content only
      stamped_git_sha: a1b2c3d  # auto — HEAD when content_hash was last computed

"Deploy-relevant content" is exactly what deploy_agent.py ships to Foundry:
    name, description, model.deployment, system_prompt, sorted(tool $refs)
Nothing else (comments, deployed: block, ordering) affects the hash, so a
re-stamp is stable unless the agent's actual behaviour changed.

Subcommands
-----------
    stamp        recompute content_hash + stamped_git_sha for every agent and
                 write them back; seed audit.version=1.0.0 where missing.
    --check      verify only, no writes. Exit 1 if any agent is unstamped, its
                 stored hash is stale, or its content changed without a version
                 bump. Use as a CI / pre-deploy guard.
    manifest     regenerate agents/MANIFEST.yaml — the single registry of every
                 agent, its version, hash, sources, and live foundry_agent_id.

Usage
-----
    python3 scripts/agent_version.py stamp
    python3 scripts/agent_version.py stamp --check
    python3 scripts/agent_version.py manifest
"""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_resolve import resolve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
DEPLOY_DIRS = ["collectors", "synthesizers", "extractors", "screening"]
MANIFEST = AGENTS / "MANIFEST.yaml"


def agent_files() -> list[Path]:
    """Every deployable agent. Excludes _base.yaml and other _-prefixed
    partials — those are inherited-from, not deployed."""
    out: list[Path] = []
    for d in DEPLOY_DIRS:
        out.extend(sorted(p for p in (AGENTS / d).glob("*.yaml")
                          if not p.name.startswith("_")))
    return out


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def deploy_content(agent: dict) -> dict:
    """The subset of the YAML that actually ships to Foundry — the ONLY thing
    the content_hash is computed over. Mirrors deploy_agent.py::deploy()."""
    return {
        "name": agent.get("name"),
        "description": (agent.get("description") or "").strip(),
        "model_deployment": (agent.get("model") or {}).get("deployment"),
        "system_prompt": (agent.get("system_prompt") or "").strip(),
        "tools": sorted(
            e["$ref"]
            for e in (agent.get("tools") or [])
            if isinstance(e, dict) and "$ref" in e
        ),
    }


def content_hash(agent: dict) -> str:
    blob = json.dumps(deploy_content(agent), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_audit_block(path: Path, version: str, chash: str, sha: str) -> None:
    """Replace-or-append a trailing `audit:` block, preserving the rest of the
    file verbatim (same text-surgery approach deploy_agent.py uses so we never
    reflow comments or the system_prompt block scalar)."""
    raw = path.read_text(encoding="utf-8")
    block = (
        "audit:\n"
        f"  version: {version}\n"
        f"  content_hash: {chash}\n"
        f"  stamped_git_sha: {sha}\n"
    )
    if re.search(r"^audit:\s*$", raw, re.MULTILINE):
        raw = re.sub(r"^audit:.*?(?=^\S|\Z)", block, raw,
                     flags=re.MULTILINE | re.DOTALL)
    else:
        raw = raw.rstrip() + "\n\n" + block
    path.write_text(raw, encoding="utf-8")


def cmd_stamp(check: bool) -> int:
    sha = _git_sha()
    problems: list[str] = []
    stamped = 0
    for path in agent_files():
        agent = resolve(path)  # merge base+overlay -> effective deployed agent
        audit = agent.get("audit") or {}
        want = content_hash(agent)
        have = audit.get("content_hash")
        version = str(audit.get("version") or "").strip()
        rel = path.relative_to(ROOT)

        if check:
            if not version:
                problems.append(f"{rel}: missing audit.version")
            if have != want:
                if not have:
                    problems.append(f"{rel}: unstamped (no content_hash) — run `agent_version.py stamp`")
                else:
                    problems.append(
                        f"{rel}: content changed but not re-stamped/bumped "
                        f"(version still {version or '?'}). Bump audit.version, then stamp.")
            continue

        # write mode
        if have and have != want and version:
            print(f"  ⚠ {rel}: content changed since v{version} — "
                  f"bump audit.version if this is a behaviour change")
        _write_audit_block(path, version or "1.0.0", want, sha)
        stamped += 1

    if check:
        if problems:
            print("AUDIT CHECK FAILED:")
            for p in problems:
                print(f"  ✗ {p}")
            return 1
        print(f"✓ all {len(agent_files())} agents stamped, hashes current, versions present")
        return 0

    print(f"stamped {stamped} agents @ git {sha}")
    return 0


def cmd_manifest() -> int:
    rows = []
    for path in agent_files():
        agent = resolve(path)  # effective (base+overlay) view
        meta = agent.get("metadata") or {}
        audit = agent.get("audit") or {}
        deployed = agent.get("deployed") or {}
        rows.append({
            "agent": agent.get("name"),
            "file": str(path.relative_to(AGENTS)),
            "tier": meta.get("tier"),
            "country": meta.get("country"),
            "version": audit.get("version"),
            "content_hash": audit.get("content_hash"),
            "stamped_git_sha": audit.get("stamped_git_sha"),
            "auditable_for_banks": meta.get("auditable_for_banks"),
            "sources": meta.get("sources"),
            "foundry_agent_id": deployed.get("foundry_agent_id"),
        })
    doc = {
        "_note": (
            "GENERATED by scripts/agent_version.py manifest — do not hand-edit. "
            "The registry of every deployable verification agent, its audited "
            "version + content hash, and the live Foundry agent it maps to. "
            "This is the 'what is running' answer for a lender/audit."),
        "generated_from_git_sha": _git_sha(),
        "agent_count": len(rows),
        "agents": rows,
    }
    MANIFEST.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(ROOT)} ({len(rows)} agents)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    if args[0] == "stamp":
        return cmd_stamp(check="--check" in args)
    if args[0] == "manifest":
        return cmd_manifest()
    if args[0] == "render":
        if len(args) < 2:
            print("usage: agent_version.py render <agent.yaml>")
            return 1
        # full effective (base+overlay) agent — the 'what actually ran' view for audit
        print(yaml.safe_dump(resolve(args[1]), sort_keys=False, allow_unicode=True, width=100))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
