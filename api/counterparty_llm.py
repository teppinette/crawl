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


# ---------------------------------------------------------------------------
# Capability 2: Opus-authored CIR markdown synthesis (the grounded brain, via the
# DIRECT Anthropic API — no Foundry Agent needed. We load evidence in code, Opus
# writes the grounded report, we persist the render in code.)
# ---------------------------------------------------------------------------
_CIR_MD_SYSTEM = (
    "You write Counterparty Intelligence Reports (CIR) for compliance officers at a "
    "global commodity trader who decide whether to clear or block trades worth "
    "millions. Be precise, be cited, be honest about what the evidence does and does "
    "not say.\n\n"
    "HARD RULES:\n"
    "1. EVERY factual assertion MUST cite at least one evidence id as [E<8-char>] — "
    "the `E` field on each evidence item. If you cannot cite it, you cannot say it.\n"
    "2. NEVER invent facts. If the evidence is silent (e.g. UBO not disclosed), say so "
    "explicitly. An evidence item with extracted={} or found=false means the source was "
    "queried but returned NOTHING — it is proof of an attempt, NOT corroboration; never "
    "write 'the registry confirms…' from such a row. The legal_name on a row often just "
    "echoes the query input — treat it as input-echo unless a source returned real data.\n"
    "3. WEIGHT by source_tier: PRIMARY_GOVERNMENT > OFFICIAL_LIST > COMMERCIAL_AGGREGATOR "
    "> OSINT > DARKWEB. Flag conflicts and say which tier you trust.\n"
    "4. DARKWEB/OSINT are INFORMATIONAL ONLY — put them under 'OSINT signals', never under "
    "Registry facts or Sanctions.\n"
    "5. Sections: Executive Summary; Registry Facts (identity/registration/status/address/"
    "directors/UBO); Ownership & Control (parent chain, shareholders, actual controller); "
    "Corporate Network (affiliates/subsidiaries); Named Executives & Directors; Sanctions "
    "Screening; Adverse Media; OSINT signals; Risk Assessment; Source Coverage Matrix "
    "(source | tier | found_data y/n). Omit any sub-item with no cited evidence — never pad.\n"
    "Output ONLY the markdown report. No preamble."
)


def _grounding_rating(md: str, ev: list, claims: list = None) -> dict:
    """Deterministic hallucination/grounding audit of an Opus-authored report.

    Computed in CODE (never self-assessed by the LLM) so it can actually catch the
    model. Three signals:
      - coverage_pct : share of factual lines (bullets + table data rows) carrying a
        valid [E<8char>] citation.
      - phantom_citations : [E..] refs that map to NO real evidence id — a fabricated
        reference; the strongest hallucination signal. MUST be 0 for a clean report.
      - tier_mix : how many cited evidence rows are PRIMARY_GOVERNMENT/OFFICIAL_LIST vs
        OSINT/DARKWEB.
    Returns a dict + a rendered markdown block appended to the report.
    """
    import re as _re
    # A citation is valid if it points to a real EVIDENCE id OR a real CLAIM id —
    # Opus cites both (the synthesis input carries evidence + extracted claims).
    # Only ids matching neither are fabricated (phantom).
    valid = {(e.get("id") or "")[:8].lower() for e in ev if e.get("id")}
    valid |= {(c.get("id") or "")[:8].lower() for c in (claims or []) if c.get("id")}
    tier_by_e = {(e.get("id") or "")[:8].lower(): ((e.get("extracted") or {}) if isinstance(
        e.get("extracted"), dict) else {}).get("source_tier")
        or e.get("source_tier") or "" for e in ev if e.get("id")}
    # Opus cites the evidence id in brackets — accept BOTH [E<id8>] and the bare
    # [<id8>] it actually emits. A line only counts as GROUNDED if it cites an id
    # that really exists in the evidence store.
    cite_re = _re.compile(r"\[E?([0-9a-fA-F]{6,8})\]")

    def _toks(text):
        return [m.lower()[:8] for m in cite_re.findall(text)]

    # Factual lines = bullet points and table data rows (skip separators/headers/blank).
    fact_lines, grounded_lines = 0, 0
    for ln in md.splitlines():
        s = ln.strip()
        if not s:
            continue
        is_bullet = s.startswith(("- ", "* ", "• "))
        is_table = s.startswith("|") and not _re.match(r"^\|[\s:|-]+\|?$", s)
        if not (is_bullet or is_table):
            continue
        # A table header row (tier | found | ...) with no evidence isn't an assertion.
        fact_lines += 1
        if any(t in valid for t in _toks(s)):
            grounded_lines += 1

    all_cites = _toks(md)
    # Phantom = a full-length (8-char) bracketed id that matches NO real evidence
    # item — a fabricated reference. Shorter tokens are ignored (not id-shaped).
    phantom = sorted({c for c in all_cites if c not in valid and len(c) == 8})
    cited_e = {c for c in all_cites if c in valid}
    primary = sum(1 for e in cited_e if tier_by_e.get(e, "").upper() in
                  ("PRIMARY_GOVERNMENT", "OFFICIAL_LIST"))
    osint = sum(1 for e in cited_e if tier_by_e.get(e, "").upper() in
                ("OSINT", "DARKWEB"))

    coverage = round(100.0 * grounded_lines / fact_lines, 1) if fact_lines else 100.0
    # Overall grounding score: coverage, hard-penalised by any phantom citation.
    score = coverage
    if phantom:
        score = round(min(score, 100.0 - 100.0 * len(phantom) / max(len(all_cites), 1)), 1)
    verdict = ("CLEAN — grounded" if not phantom and coverage >= 99
               else "PASS — grounded" if not phantom and coverage >= 90
               else "REVIEW — uncited assertions" if not phantom
               else "FAIL — fabricated citation(s)")
    rating = {
        "grounding_score": score, "coverage_pct": coverage,
        "factual_lines": fact_lines, "grounded_lines": grounded_lines,
        "distinct_evidence_cited": len(cited_e),
        "phantom_citations": phantom, "phantom_count": len(phantom),
        "primary_tier_cites": primary, "osint_tier_cites": osint,
        "verdict": verdict,
    }
    block = (
        "\n\n---\n\n## Grounding & Hallucination Rating\n\n"
        "_Computed deterministically from the report against the evidence store — "
        "not self-assessed by the model._\n\n"
        f"- **Grounding score: {score}%** — {verdict}\n"
        f"- Cited factual lines: {grounded_lines}/{fact_lines} ({coverage}%)\n"
        f"- Distinct evidence items cited: {len(cited_e)} "
        f"(primary/official: {primary} · OSINT/darkweb: {osint})\n"
        f"- Fabricated (phantom) citations: **{len(phantom)}**"
        + (f" ⚠ {', '.join('[E'+p+']' for p in phantom)}" if phantom else " ✓")
        + "\n"
    )
    return {"rating": rating, "block": block}


def synthesize_cir_markdown(run_id: str, *, persist: bool = True) -> dict:
    """Opus writes the grounded CIR markdown from a run's evidence+claims and (by
    default) persists it as a cir_markdown render. Returns {markdown, render_id,
    evidence_ids_cited}. Runs on claude-opus-4-8 via the direct Messages API — the
    working path (Foundry Agents can't run Claude)."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    import evidence_db

    ev = evidence_db.list_evidence(run_id)
    claims = evidence_db.list_claims(run_id)
    packed_ev = [{"E": (e.get("id") or "")[:8], "evidence_id": e.get("id"),
                  "source_id": e.get("source_id"), "extracted": e.get("extracted")}
                 for e in ev]
    user = (
        "EVIDENCE (cite the `E` value as [E<value>]):\n"
        + json.dumps(packed_ev, ensure_ascii=False, default=str)[:150000]
        + "\n\nEXTRACTED CLAIMS:\n"
        + json.dumps(claims, ensure_ascii=False, default=str)[:40000]
        + "\n\nWrite the grounded CIR markdown now."
    )
    md = opus([{"role": "user", "content": user}], system=_CIR_MD_SYSTEM,
              max_tokens=8192, timeout=300)
    cited = [e.get("id") for e in ev]
    # Deterministic grounding/hallucination audit, appended to the report.
    grade = _grounding_rating(md, ev, claims)
    md = md + grade["block"]
    rating = grade["rating"]
    out = {"markdown": md, "evidence_ids_cited": cited, "render_id": None,
           "grounding": rating}
    if persist:
        out["render_id"] = evidence_db.save_render(
            run_id, render_type="cir_markdown",
            payload={"markdown": md, "model": _MODEL,
                     "synthesizer": "counterparty_llm_direct_opus",
                     "evidence_ids_cited": cited, "grounding": rating})
    log.info("counterparty_llm: run %s — Opus cir_markdown %d chars, render %s, "
             "grounding %s%% (%s, phantom=%d)",
             run_id[:8], len(md), out["render_id"], rating["grounding_score"],
             rating["verdict"], rating["phantom_count"])
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "synth":
        print(synthesize_cir_markdown(sys.argv[2], persist=True)["markdown"])
    elif len(sys.argv) == 2 and sys.argv[1] == "ping":
        print(opus([{"role": "user", "content": "Reply with the single word READY."}],
                   max_tokens=20))
    elif len(sys.argv) == 3 and sys.argv[1] == "principals":
        print(json.dumps(extract_principals(sys.argv[2]), indent=2, ensure_ascii=False))
    else:
        print("usage: counterparty_llm.py ping | principals <run_id>")
