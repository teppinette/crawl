#!/usr/bin/env python3
"""
Deploy an agent YAML to Azure AI Foundry Agents.

Usage:
    python3 scripts/deploy_agent.py agents/collectors/verify_uk.yaml

Reads the YAML, resolves the referenced OpenAPI tool specs from disk, and
calls Foundry's Agents SDK to create (or update if name exists) the agent.
On success, writes the returned agent_id back into the YAML's metadata
block so the catalog stays in sync with what's actually deployed.

Required env / KV:
    FOUNDRY_PROJECT_ENDPOINT       project URL, e.g.
                                   https://copapfoundry-resource.services.ai.azure.com/api/projects/<project>
                                   (falls back to KV secret 'foundry-project-endpoint')

Auth: DefaultAzureCredential — managed identity on crawldevvm. The MI needs
the 'Azure AI Developer' role assignment on the Foundry resource.
"""

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "scripts"))

from agent_version import content_hash, _git_sha  # noqa: E402

try:
    from azure.ai.agents import AgentsClient
    from azure.ai.agents.models import (
        OpenApiAnonymousAuthDetails,
        OpenApiTool,
    )
    from azure.identity import DefaultAzureCredential
except ImportError as e:
    print(f"FATAL: SDK missing — pip install azure-ai-agents azure-identity\n  {e}")
    sys.exit(2)

from keyvault import get_secret  # noqa: E402


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_tool_refs(agent_yaml: dict, agent_path: Path) -> list[dict]:
    """Resolve every '$ref' under tools to a parsed OpenAPI spec dict."""
    out = []
    for entry in agent_yaml.get("tools") or []:
        if isinstance(entry, dict) and "$ref" in entry:
            ref_path = (agent_path.parent / entry["$ref"]).resolve()
            if not ref_path.exists():
                print(f"  ! tool ref not found: {ref_path}")
                continue
            spec = _load_yaml(ref_path)
            out.append({"path": ref_path, "spec": spec})
    return out


def _project_endpoint() -> str:
    ep = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if ep:
        return ep
    try:
        ep = get_secret("foundry-project-endpoint")
        if ep:
            return ep
    except Exception:
        pass
    print("""
FATAL: FOUNDRY_PROJECT_ENDPOINT not set.

You need the project URL from Azure AI Foundry. It looks like:
    https://copapfoundry-resource.services.ai.azure.com/api/projects/<project_name>

Pick one to provide it:
  a) export FOUNDRY_PROJECT_ENDPOINT='https://...'
  b) az keyvault secret set --vault-name crawlkeyvault \\
       --name foundry-project-endpoint --value 'https://...'

To find the project name: Azure portal → copapfoundry-resource →
"Projects" (left nav, under Azure AI Foundry) → the name shown.
""")
    sys.exit(3)


def _persist_agent_id(agent_path: Path, agent_id: str, project_endpoint: str):
    """Write the deployed agent_id back into the YAML metadata block so the
    repo catalog stays in sync with what's actually live in Foundry."""
    raw = agent_path.read_text(encoding="utf-8")
    block = (
        f"\ndeployed:\n"
        f"  foundry_agent_id: {agent_id}\n"
        f"  project_endpoint: {project_endpoint}\n"
    )
    # Replace existing block if present, else append
    import re
    if re.search(r"^deployed:\s*$", raw, re.MULTILINE):
        raw = re.sub(r"^deployed:.*?(?=^\S|\Z)", block.strip() + "\n", raw,
                     flags=re.MULTILINE | re.DOTALL)
    else:
        raw = raw.rstrip() + "\n" + block
    agent_path.write_text(raw, encoding="utf-8")


def _preflight_audit(agent_path: Path, agent: dict) -> dict:
    """Refuse to deploy anything that isn't a committed, stamped version — so
    every live Foundry agent maps to a real git SHA + content hash a lender can
    be shown. Returns the agent's audit block."""
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", str(agent_path)], cwd=ROOT, text=True
    ).strip()
    if dirty:
        print(f"FATAL: {agent_path.name} has uncommitted changes.\n"
              f"  Commit first so the deployed agent maps to a real git SHA "
              f"(audit requirement). Then re-run.")
        sys.exit(5)

    audit = agent.get("audit") or {}
    want = content_hash(agent)
    if not audit.get("version"):
        print("FATAL: agent has no audit.version. Run:\n"
              "  python3 scripts/agent_version.py stamp   (then commit)")
        sys.exit(6)
    if audit.get("content_hash") != want:
        print("FATAL: agent content_hash is stale/unstamped — the file changed "
              "since it was last stamped.\n"
              "  If this is a behaviour change, bump audit.version, then:\n"
              "  python3 scripts/agent_version.py stamp && git commit, then deploy.")
        sys.exit(6)
    return audit


def _append_deploy_log(agent: dict, audit: dict, agent_id: str):
    """Append-only bank-auditable record of exactly what was deployed, when."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = ROOT / "agents" / "DEPLOY_LOG.md"
    if not log.exists():
        log.write_text(
            "# Agent deploy log — bank-auditable trail of what ran, and when\n\n"
            "Append-only. Each row is one deploy of a verification agent to Azure "
            "AI Foundry. `content_hash` + `git_sha` pin the exact agent definition; "
            "`foundry_agent_id` is the live agent that produced evidence from that "
            "point until the next row for the same agent.\n\n"
            "| deployed_at (UTC) | agent | version | content_hash | git_sha | foundry_agent_id |\n"
            "|---|---|---|---|---|---|\n",
            encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"| {ts} | {agent['name']} | {audit.get('version')} | "
                f"{audit.get('content_hash')} | {_git_sha()} | {agent_id} |\n")
    print(f"  audit: appended DEPLOY_LOG.md ({agent['name']} v{audit.get('version')})")


def deploy(agent_yaml_path: str):
    agent_path = Path(agent_yaml_path).resolve()
    if not agent_path.exists():
        print(f"FATAL: {agent_path} not found")
        sys.exit(1)

    print(f"--- loading {agent_path.relative_to(ROOT)} ---")
    agent = _load_yaml(agent_path)
    audit = _preflight_audit(agent_path, agent)
    print(f"  audit: v{audit.get('version')} {audit.get('content_hash')}")

    name = agent["name"]
    description = agent.get("description", "").strip()
    model_deployment = agent["model"]["deployment"]
    system_prompt = agent["system_prompt"].strip()

    tools = _resolve_tool_refs(agent, agent_path)
    print(f"  name={name}")
    print(f"  model={model_deployment}")
    print(f"  tools={len(tools)} resolved")

    project_endpoint = _project_endpoint()
    print(f"--- connecting: {project_endpoint} ---")

    credential = DefaultAzureCredential()
    client = AgentsClient(endpoint=project_endpoint, credential=credential)

    tool_defs = []
    for t in tools:
        spec = t["spec"]
        op_id = (list(spec.get("paths", {}).values())[0].get(
            list(spec.get("paths", {}).values())[0].keys().__iter__().__next__()
        ) or {}).get("operationId") if spec.get("paths") else None
        # Compose an OpenApi tool entry
        try:
            tool = OpenApiTool(
                name=op_id or spec.get("info", {}).get("title", "tool").replace(" ", "_"),
                description=spec.get("info", {}).get("description", "").strip()[:512],
                spec=spec,
                auth=OpenApiAnonymousAuthDetails(),
            )
            tool_defs.extend(tool.definitions)
        except Exception as e:
            print(f"  ! could not build OpenApiTool for {t['path'].name}: {e}")

    print(f"--- create_agent ---")
    try:
        new_agent = client.create_agent(
            model=model_deployment,
            name=name,
            description=description,
            instructions=system_prompt,
            tools=tool_defs,
        )
    except Exception as e:
        print(f"FATAL: Foundry create_agent failed: {e}")
        sys.exit(4)

    agent_id = new_agent.get("id") if isinstance(new_agent, dict) else getattr(new_agent, "id", None)
    print(f"  agent_id={agent_id}")

    _persist_agent_id(agent_path, agent_id, project_endpoint)
    print(f"  persisted agent_id back into {agent_path.relative_to(ROOT)}")
    _append_deploy_log(agent, audit, agent_id)
    print("\nDEPLOYED. Commit the updated YAML (agent_id) + agents/DEPLOY_LOG.md.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    deploy(sys.argv[1])
