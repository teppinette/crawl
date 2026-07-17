#!/usr/bin/env python3
"""
Agent resolver — the 'build upon a base' layer of the verification platform.

A collector YAML may be a *thin overlay* that `extends: _base.yaml`. This module
merges base + overlay into the full effective agent that actually deploys, so the
shared skeleton (runtime, model, inputs, tools, output, and the templated
system_prompt/description) lives in ONE place and every country inherits it.
Enhancing collection logic = edit _base.yaml once.

resolve(path) -> effective agent dict, identical in deploy-content to a hand-
written full agent. Files with no `extends` pass through unchanged, so full
agents (synthesizers, extractors) keep working untouched.

Template substitution uses {{double_brace}} placeholders (single braces appear
literally in the prompts, e.g. extracted={}). Variables:
    cc_upper            metadata.country                 (e.g. DE)
    cc_lower            derived lower-case               (e.g. de)
    registry_source_id  overlay vars, default {cc}_registry
    country_name        overlay vars   (e.g. Germany)
    registry_detail     overlay vars   (the 'resolved upstream: ...' phrase)
"""

import copy
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# keys that are overlay-mechanics only and must never reach the deployed agent
_STRIP = ("extends", "vars")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _deep_merge(base: dict, over: dict) -> dict:
    """base ⊕ over. Dicts merge recursively (over wins); everything else
    (scalars, lists) is replaced wholesale by over when present."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _subst(text: str, ctx: dict) -> str:
    if not isinstance(text, str) or "{{" not in text:
        return text
    def repl(m):
        key = m.group(1).strip()
        return str(ctx.get(key, m.group(0)))
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", repl, text)


def resolve(path) -> dict:
    """Return the full effective agent dict for a collector overlay or a plain
    full agent. Deploy-content-equivalent to a hand-written full agent."""
    path = Path(path).resolve()
    over = _load(path)
    if not isinstance(over, dict) or "extends" not in over:
        return over  # already a full agent

    base_path = (path.parent / over["extends"]).resolve()
    base = _load(base_path)

    merged = _deep_merge(base, over)

    cc_upper = (merged.get("metadata") or {}).get("country") or ""
    v = over.get("vars") or {}
    ctx = {
        "cc_upper": cc_upper,
        "cc_lower": cc_upper.lower(),
        "registry_source_id": v.get("registry_source_id", f"{cc_upper.lower()}_registry"),
        "country_name": v.get("country_name", ""),
        "registry_detail": v.get("registry_detail", ""),
    }
    for field in ("description", "system_prompt"):
        if field in merged:
            merged[field] = _subst(merged[field], ctx)

    for k in _STRIP:
        merged.pop(k, None)
    return merged
