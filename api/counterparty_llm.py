#!/usr/bin/env python3
"""
CounterpartyLLM — the grounded Opus brain for counterparty intelligence.

A dedicated LLM engine (separate from copapllm) for counterparties, sanctions,
and crawling. This module is its brain: claude-opus-4-8 on copapfoundry-resource,
called via the Anthropic Messages API, under a hard grounding rule — every fact
must trace to collected evidence, and the model ABSTAINS rather than invent.

Foundry Opus endpoint (verified 2026-07-17):
    POST https://copapfoundry-resource.services.ai.azure.com/anthropic/v1/messages
         ?api-version=2024-05-01-preview
    Authorization: Bearer <AAD token, scope cognitiveservices.azure.com/.default>
    header anthropic-version: 2023-06-01
    body   {"model":"claude-opus-4-8","max_tokens":N,"messages":[...],"system":...}
"""

import json
import os
import time
from typing import Optional

import requests

log = __import__("logging").getLogger("counterparty-llm")

_ENDPOINT = os.environ.get(
    "FOUNDRY_ANTHROPIC_URL",
    "https://copapfoundry-resource.services.ai.azure.com/anthropic/v1/messages"
    "?api-version=2024-05-01-preview",
)
_MODEL = os.environ.get("COUNTERPARTY_LLM_MODEL", "claude-opus-4-8")
_SCOPE = "https://cognitiveservices.azure.com/.default"

_TOKEN = {"value": None, "exp": 0.0}


def _token() -> str:
    """Cached AAD token for the Foundry resource (managed identity)."""
    now = time.time()
    if _TOKEN["value"] and now < _TOKEN["exp"] - 120:
        return _TOKEN["value"]
    # DefaultAzureCredential chains ManagedIdentity (container) + AzureCli
    # (copapdevvm `az login --identity`), so it works in both places.
    from azure.identity import DefaultAzureCredential
    client_id = os.environ.get("AZURE_CLIENT_ID")
    cred = DefaultAzureCredential(managed_identity_client_id=client_id) if client_id \
        else DefaultAzureCredential()
    tok = cred.get_token(_SCOPE)
    _TOKEN["value"], _TOKEN["exp"] = tok.token, tok.expires_on
    return tok.token


def opus(messages: list, *, system: str = None, max_tokens: int = 4096,
         timeout: int = 120) -> str:
    """One grounded Opus call. Returns the assistant text. Raises on transport error.
    NB: claude-opus-4-8 rejects `temperature` (deprecated for this model)."""
    body = {"model": _MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    r = requests.post(
        _ENDPOINT,
        headers={"Authorization": f"Bearer {_token()}",
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    parts = [b.get("text", "") for b in (data.get("content") or [])
             if b.get("type") == "text"]
    return "".join(parts).strip()


def opus_json(messages: list, *, system: str = None, max_tokens: int = 4096,
              timeout: int = 120) -> dict:
    """Opus call that must return a single JSON object. Tolerates ```json fences."""
    txt = opus(messages, system=system, max_tokens=max_tokens, timeout=timeout)
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip("` \n")
    # last resort: slice from first { to last }
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
    return json.loads(s)


# ---------------------------------------------------------------------------
# Capability 1: grounded principal extraction (the deep-dive planner's first step)
# ---------------------------------------------------------------------------
_PRINCIPALS_SYSTEM = (
    "You are CounterpartyLLM, a grounded counterparty-intelligence engine. Hard "
    "rule: use ONLY the evidence provided. Never invent a person, company, role, "
    "or identifier that is not present in the evidence. If the evidence names no "
    "principals, return an empty list. Every item you return must cite the "
    "evidence_id it came from. You are identifying WHO and WHICH entities a deep "
    "dive should investigate next — you are not judging or scoring them."
)


def extract_principals(run_id: str) -> dict:
    """Read a run's collected evidence and extract, GROUNDED, the principals to
    deep-dive: named people (directors/executives/legal-rep/UBO) and corporate
    entities (shareholders/parents/affiliates). Returns
    {people:[...], entities:[...]} each item citing its evidence_id."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import evidence_db

    ev = evidence_db.list_evidence(run_id)
    # Compact the evidence for the prompt: id + source + extracted payload.
    packed = [{"evidence_id": e.get("id"), "source_id": e.get("source_id"),
               "extracted": e.get("extracted")} for e in ev]

    user = (
        "Evidence collected for this counterparty (JSON array):\n\n"
        + json.dumps(packed, ensure_ascii=False, default=str)[:120000]
        + "\n\nFrom ONLY this evidence, extract the principals to deep-dive. Return "
        "a single JSON object:\n"
        '{"people":[{"name":"","name_native":"","role":"","evidence_id":""}],'
        '"entities":[{"name":"","name_native":"","relationship":"shareholder|parent|'
        'affiliate|subsidiary|actual_controller","stake_pct":null,"evidence_id":""}]}\n'
        "Rules: name_native = original-script name if the evidence has it (e.g. Chinese), "
        "else empty. role = e.g. legal_representative, chairman, director, executive, "
        "beneficial_owner. Include a person/entity ONLY if the evidence actually names "
        "them. No duplicates. No commentary — JSON only."
    )
    result = opus_json([{"role": "user", "content": user}],
                       system=_PRINCIPALS_SYSTEM, max_tokens=4096)
    result.setdefault("people", [])
    result.setdefault("entities", [])
    log.info("counterparty_llm: run %s — extracted %d people, %d entities",
             run_id[:8], len(result["people"]), len(result["entities"]))
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "ping":
        print(opus([{"role": "user", "content": "Reply with the single word READY."}],
                   max_tokens=20))
    elif len(sys.argv) == 3 and sys.argv[1] == "principals":
        print(json.dumps(extract_principals(sys.argv[2]), indent=2, ensure_ascii=False))
    else:
        print("usage: counterparty_llm.py ping | principals <run_id>")
