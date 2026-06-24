#!/usr/bin/env python3
"""
Generate agents/collectors/verify_<cc>.yaml for every country crawl-verify
supports, modeled on verify_uk.yaml. Skips GB (already deployed).

Usage:
    python3 scripts/generate_country_collectors.py
"""
import json
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "agents/collectors"

# Country code -> human name (ISO-friendly). For unknowns we just use the code.
COUNTRY_NAMES = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AU": "Australia",
    "BE": "Belgium", "BR": "Brazil", "CA": "Canada", "CH": "Switzerland",
    "CL": "Chile", "CN": "China", "CO": "Colombia", "CZ": "Czech Republic",
    "DE": "Germany", "DK": "Denmark", "EC": "Ecuador", "EG": "Egypt",
    "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "HK": "Hong Kong", "IL": "Israel", "IN": "India", "IT": "Italy",
    "JP": "Japan", "KR": "South Korea", "LT": "Lithuania", "LV": "Latvia",
    "MX": "Mexico", "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand",
    "PE": "Peru", "PK": "Pakistan", "PL": "Poland", "PT": "Portugal",
    "SA": "Saudi Arabia", "SG": "Singapore", "TR": "Turkey", "TW": "Taiwan",
    "US": "United States", "ZA": "South Africa",
}

def yaml_for(cc: str, registry_desc: str) -> str:
    name = COUNTRY_NAMES.get(cc, cc)
    return f"""name: verify_{cc.lower()}_collector
description: >
  Collects evidence for a {name} ({cc}) entity. Queries the {cc} gov registry
  via the gateway (resolved upstream: {registry_desc[:200]}), CSL screening,
  and GLEIF LEI. Writes raw responses + extracted fields to the evidence
  table. Does not synthesize narrative.

metadata:
  tier: collector
  country: {cc}
  schema_version: 1
  sources:
    - {cc.lower()}_registry
    - csl_screening
    - gleif_lei
  auditable_for_banks: true
  cost_tier: free
  est_latency_seconds: 12

runtime:
  service: azure_ai_foundry_agents
  resource: copapfoundry-resource
  region: eastus2

model:
  deployment: gpt-4.1-mini-778742
  resource: copapfoundry-resource
  region: eastus2
  temperature: 0.0
  max_tokens: 4096

inputs:
  - name: run_id
    type: uuid
    required: true
  - name: entity_name
    type: string
    required: true
  - name: registration_number
    type: string
    required: false

system_prompt: |
  You are the {name} ({cc}) evidence collector. Your only job is to query
  sources and persist what they return. You do not write narrative. You do
  not synthesize. You do not invent fields. If a source returns nothing,
  still record it as an evidence row with extracted={{}} so the audit trail
  is complete.

  For the given entity_name and run_id, execute these steps in order:

    1. Call country_registry_lookup with country="{cc}" and the entity_name
       (pass registration_number if provided). The response now contains
       a "primary" block (always present) AND optionally an "aggregator"
       block (OpenCorporates, when available). Persist:
         - Always: evidence_add(source_id="{cc.lower()}_registry",
                                extracted=<the primary block>)
         - Only if response.aggregator is NOT null:
                   evidence_add(source_id="opencorporates",
                                extracted=<the aggregator block>)
       This yields a banker-friendly dual-source comparison per entity.

    2. Call opensanctions_search with the entity name (country="{cc.lower()}").
       Persist via evidence_add with source_id="csl_screening".

    3. Call gleif_lei_lookup with the entity name (country="{cc}"). Persist
       via evidence_add with source_id="gleif_lei".

  Always pass run_id to evidence_add. Always pass the exact source_url
  the tool returned. Always pass language_original.

  When all three steps are done, call collector_complete(run_id) and stop.

tools:
  - $ref: ../tools/country_registry_lookup.openapi.yaml
  - $ref: ../tools/opensanctions_search.openapi.yaml
  - $ref: ../tools/gleif_lei_lookup.openapi.yaml
  - $ref: ../tools/evidence_add.openapi.yaml
  - $ref: ../tools/collector_complete.openapi.yaml

output:
  description: >
    Side effect only — 3 evidence rows written to crawl_reports.evidence
    (one per source), then run status transitioned to "extracting" by
    collector_complete.
"""


def main():
    # Pull country list from crawl-verify
    sc = requests.get("http://180.20.0.4:8460/health", timeout=10).json()["supported_countries"]
    written = 0
    for cc in sorted(sc.keys()):
        if cc == "GB":
            continue  # already deployed with the richer UK template
        path = COLLECTORS / f"verify_{cc.lower()}.yaml"
        path.write_text(yaml_for(cc, sc[cc]), encoding="utf-8")
        written += 1
    print(f"wrote {written} collector YAMLs (GB skipped — already deployed)")


if __name__ == "__main__":
    main()
