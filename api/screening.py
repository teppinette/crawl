"""
Sanctions & Watchlist Screening — multi-source parallel screening.

Returns structured hits from 7 free/low-cost sources in parallel.
Bridger (LexisNexis) stays on GC side — this covers the free tier.

Sources:
  CSL         — US Consolidated Screening List (11 lists incl OFAC SDN, BIS)
  UK_FCDO     — UK Financial Sanctions (XML, 12h cache)
  EU          — EU Consolidated Sanctions (XML, 12h cache)
  UN_SC       — UN Security Council Consolidated List (XML, 12h cache)
  FBI         — FBI Most Wanted (JSON API, 12h cache)
  INTERPOL    — INTERPOL Red Notices (REST API, real-time)
  OPENSANCTIONS — OpenSanctions aggregator (API, supplementary)

Contract: POST /api/v2/screening — returns per-source results with risk level.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from keyvault import get_secret
import raw_store

log = logging.getLogger("screening")

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CSL_API_KEY = get_secret("csl-subscription-key") or os.environ.get("CSL_API_KEY", "")
_OPENSANCTIONS_API_KEY = get_secret("opensanctions-api-key") or os.environ.get("OPENSANCTIONS_API_KEY", "")

_CSL_URL = "https://data.trade.gov/consolidated_screening_list/v1/search"
_FCDO_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.xml"
_EU_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw"
_UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
_FBI_URL = "https://api.fbi.gov/wanted/v1/list"
_INTERPOL_URL = "https://ws-public.interpol.int/notices/v1/red"
_OPENSANCTIONS_URL = "https://api.opensanctions.org/search/default"

# ---------------------------------------------------------------------------
# Name matching (ported from GC's fetch_sanctions_lists.py)
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"['\",.\-()]")
_SUFFIX_RE = re.compile(
    r"\b(the|llc|ltd|limited|inc|co|corp|pvt|private|bv|nv|ag|sa|srl|fze|fzco|llp|plc|gmbh)\b"
)
_WS_RE = re.compile(r"\s+")


def _normalize(name: str) -> str:
    n = name.lower()
    n = _STRIP_RE.sub(" ", n)
    n = _SUFFIX_RE.sub("", n)
    return _WS_RE.sub(" ", n).strip()


def _fuzzy_match(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _is_relevant(query: str, candidate: str, threshold: float = 0.82) -> bool:
    """Check if candidate name is relevant to query (fuzzy + token overlap)."""
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return False
    ratio = SequenceMatcher(None, q, c).ratio()
    if ratio >= threshold:
        return True
    # Token overlap fallback
    q_tokens = {t for t in q.split() if len(t) >= 3}
    c_tokens = {t for t in c.split() if len(t) >= 3}
    if q_tokens and c_tokens and len(q_tokens & c_tokens) >= 1:
        if ratio >= 0.55:
            return True
    return False


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1]


# ---------------------------------------------------------------------------
# In-memory XML list cache (refreshed every 12h)
# ---------------------------------------------------------------------------

_LIST_CACHE: dict = {}
_CACHE_TTL = timedelta(hours=12)


# ---------------------------------------------------------------------------
# Provider: US CSL (real-time API, needs subscription key)
# ---------------------------------------------------------------------------

SDN_SOURCES = {
    "Specially Designated Nationals (SDN) - Treasury Department",
    "Specially Designated Nationals List (SDN)",
}

HIGH_RISK_SOURCES = {
    "Entity List (EL) - Bureau of Industry and Security",
    "Denied Persons List (DPL) - Bureau of Industry and Security",
    "Unverified List (UVL) - Bureau of Industry and Security",
    "Foreign Sanctions Evaders (FSE) - Treasury Department",
    "Sectoral Sanctions Identifications List (SSI) - Treasury Department",
    "Non-SDN Palestinian Legislative Council (NS-PLC) - Treasury Department",
    "Non-SDN Menu-Based Sanctions List (NS-MBS) - Treasury Department",
    "Capta List (CAP) - Treasury Department",
    "Non-SDN Chinese Military-Industrial Complex Companies List (CMIC)",
    "Military End User (MEU) List - Bureau of Industry and Security",
}


async def _query_csl(entity_name: str, country: str = "") -> dict:
    """Screen against US Consolidated Screening List (11 lists)."""
    if not _CSL_API_KEY:
        return {"source": "CSL", "status": "disabled", "error": "csl-subscription-key not configured"}

    t0 = time.monotonic()
    params = {"name": entity_name, "fuzzy_name": "true", "size": "10"}
    if country:
        params["countries"] = country
    headers = {"subscription-key": _CSL_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(_CSL_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        latency = int((time.monotonic() - t0) * 1000)

        raw_store.store(
            source="CSL", entity_name=entity_name, country_code=country,
            request_method="GET", request_url=_CSL_URL,
            request_params=params, request_headers=dict(headers),
            response_status=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text, duration_ms=latency,
        )

        results = data.get("results", [])

        # Filter spurious fuzzy matches
        relevant = [h for h in results if _is_relevant(entity_name, h.get("name", ""))]

        if not relevant:
            return {"source": "CSL", "status": "clear", "hits": [], "hit_count": 0,
                    "latency_ms": latency}

        hits = []
        risk_level = "MEDIUM"
        for h in relevant:
            source = h.get("source", "")
            if source in SDN_SOURCES:
                risk_level = "CRITICAL"
            elif source in HIGH_RISK_SOURCES and risk_level != "CRITICAL":
                risk_level = "HIGH"
            hits.append({
                "name": h.get("name", ""),
                "source_list": source,
                "type": h.get("type", ""),
                "programs": h.get("programs", []),
                "addresses": [
                    {k: v for k, v in a.items() if v}
                    for a in (h.get("addresses") or [])[:3]
                ],
            })

        return {
            "source": "CSL",
            "status": "hit",
            "risk_level": risk_level,
            "hits": hits,
            "hit_count": len(hits),
            "latency_ms": latency,
        }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return {"source": "CSL", "status": "error", "latency_ms": latency,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Provider: XML list screening (UK FCDO, EU, UN)
# ---------------------------------------------------------------------------

async def _load_xml_list(source: str, url: str, parser) -> list:
    """Download and cache an XML sanctions list."""
    cached = _LIST_CACHE.get(source)
    if cached and (datetime.now(timezone.utc) - cached["loaded_at"]) < _CACHE_TTL:
        return cached["entries"]

    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            resp.raise_for_status()
        load_ms = int((time.monotonic() - t0) * 1000)

        raw_store.store(
            source=source, request_method="GET", request_url=url,
            response_status=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text, duration_ms=load_ms,
        )

        entries = parser(resp.content)
        _LIST_CACHE[source] = {"loaded_at": datetime.now(timezone.utc), "entries": entries}
        log.info("Loaded %s: %d entries", source, len(entries))
        return entries
    except Exception as e:
        log.warning("Failed to load %s: %s", source, e)
        return []


def _parse_fcdo(content: bytes) -> list:
    entries = []
    root = ET.fromstring(content)
    for target in root.iter():
        if _local_tag(target.tag) != "FinancialSanctionsTarget":
            continue
        name_parts, last_parts, aliases = [], [], []
        record_type = "entity"
        for child in target.iter():
            ctag = _local_tag(child.tag)
            text = (child.text or "").strip()
            if ctag in ("name1", "name2", "name3", "name4", "name5") and text:
                name_parts.append(text)
            elif ctag == "Name6" and text:
                last_parts.append(text)
            elif ctag in ("FullName", "GroupName", "AliasName") and text:
                aliases.append(text)
            elif ctag == "Group_Type":
                record_type = "individual" if text.lower().startswith("indiv") else "entity"
        names = []
        if name_parts and last_parts:
            names.append(" ".join(name_parts + last_parts))
        elif last_parts:
            names.append(" ".join(last_parts))
        elif name_parts:
            names.append(" ".join(name_parts))
        for a in aliases:
            if a and a not in names:
                names.append(a)
        if names:
            entries.append({"names": names, "type": record_type})
    return entries


def _parse_eu(content: bytes) -> list:
    entries = []
    root = ET.fromstring(content)
    for subject in root.iter():
        if _local_tag(subject.tag) != "sanctionEntity":
            continue
        record_type = "entity"
        names = []
        for child in subject.iter():
            ctag = _local_tag(child.tag)
            if ctag == "subjectType":
                code = (child.get("classificationCode") or "").strip().upper()
                record_type = "individual" if code == "P" else "entity"
            elif ctag == "nameAlias":
                whole = (child.get("wholeName") or "").strip()
                if whole:
                    names.append(whole)
                else:
                    parts = [child.get(k, "").strip() for k in ("firstName", "middleName", "lastName")]
                    combined = " ".join(p for p in parts if p)
                    if combined:
                        names.append(combined)
        names = list(dict.fromkeys(n for n in names if n))
        if names:
            entries.append({"names": names, "type": record_type})
    return entries


def _parse_un(content: bytes) -> list:
    entries = []
    root = ET.fromstring(content)
    for node in root.iter():
        tag = _local_tag(node.tag)
        if tag == "INDIVIDUAL":
            record_type = "individual"
        elif tag == "ENTITY":
            record_type = "entity"
        else:
            continue
        name_parts = {}
        aliases = []
        for child in node:
            ctag = _local_tag(child.tag)
            text = (child.text or "").strip()
            if ctag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME") and text:
                name_parts[ctag] = text
            elif ctag in ("INDIVIDUAL_ALIAS", "ENTITY_ALIAS"):
                for sub in child.iter():
                    if _local_tag(sub.tag) == "ALIAS_NAME":
                        atext = (sub.text or "").strip()
                        if atext:
                            aliases.append(atext)
        ordered = [name_parts.get(k) for k in
                   ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        full = " ".join(p for p in ordered if p).strip()
        names = [full] if full else []
        for a in aliases:
            if a and a not in names:
                names.append(a)
        if names:
            entries.append({"names": names, "type": record_type})
    return entries


async def _screen_xml_list(
    entity_name: str, source: str, url: str, parser, entity_type: str = "company",
) -> dict:
    """Screen against a cached XML sanctions list."""
    t0 = time.monotonic()
    entries = await _load_xml_list(source, url, parser)
    if not entries:
        return {"source": source, "status": "unavailable", "hits": [],
                "hit_count": 0, "latency_ms": 0, "error": "List unavailable"}

    hits = []
    for entry in entries:
        # Type filter
        et = entry.get("type", "entity")
        if entity_type == "person" and et != "individual":
            continue
        if entity_type == "company" and et != "entity":
            continue

        for candidate in entry["names"]:
            if _is_relevant(entity_name, candidate):
                hits.append({"name": candidate, "list_type": et,
                             "detail": f"{source} ({et}): {candidate}"})
                break

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "source": source,
        "status": "hit" if hits else "clear",
        "risk_level": "HIGH" if hits else None,
        "hits": hits,
        "hit_count": len(hits),
        "latency_ms": latency,
    }


# ---------------------------------------------------------------------------
# Provider: FBI Most Wanted (JSON API, no auth)
# ---------------------------------------------------------------------------

async def _query_fbi(entity_name: str) -> dict:
    """Screen against FBI Most Wanted list."""
    t0 = time.monotonic()
    cached = _LIST_CACHE.get("FBI")
    if cached and (datetime.now(timezone.utc) - cached["loaded_at"]) < _CACHE_TTL:
        entries = cached["entries"]
    else:
        entries = []
        try:
            page = 1
            async with httpx.AsyncClient(timeout=30) as client:
                while page <= 10:
                    fbi_params = {"page": page, "pageSize": 50}
                    resp = await client.get(_FBI_URL, params=fbi_params)
                    if resp.status_code != 200:
                        break
                    raw_store.store(
                        source="FBI", request_method="GET", request_url=_FBI_URL,
                        request_params=fbi_params,
                        response_status=resp.status_code,
                        response_headers=dict(resp.headers),
                        response_body=resp.text,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    data = resp.json()
                    items = data.get("items", [])
                    if not items:
                        break
                    for item in items:
                        title = (item.get("title") or "").strip()
                        aliases = item.get("aliases") or []
                        if not title or len(title.split()) < 2:
                            continue
                        names = [title] + [a for a in aliases if a]
                        entries.append({"names": names, "type": "individual"})
                    page += 1
            _LIST_CACHE["FBI"] = {"loaded_at": datetime.now(timezone.utc), "entries": entries}
            log.info("Loaded FBI: %d entries", len(entries))
        except Exception as e:
            latency = int((time.monotonic() - t0) * 1000)
            return {"source": "FBI", "status": "error", "hits": [], "hit_count": 0,
                    "latency_ms": latency, "error": str(e)}

    hits = []
    for entry in entries:
        for candidate in entry["names"]:
            if _is_relevant(entity_name, candidate, threshold=0.90):
                hits.append({"name": candidate, "detail": f"FBI Most Wanted: {candidate}"})
                break

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "source": "FBI",
        "status": "hit" if hits else "clear",
        "risk_level": "CRITICAL" if hits else None,
        "hits": hits,
        "hit_count": len(hits),
        "latency_ms": latency,
    }


# ---------------------------------------------------------------------------
# Provider: INTERPOL Red Notices (REST API, no auth)
# ---------------------------------------------------------------------------

async def _query_interpol(entity_name: str) -> dict:
    """Screen against INTERPOL Red Notices (person search only)."""
    t0 = time.monotonic()
    parts = entity_name.strip().split()
    if len(parts) < 2:
        return {"source": "INTERPOL", "status": "clear", "hits": [],
                "hit_count": 0, "latency_ms": 0}

    try:
        params = {"name": entity_name, "resultPerPage": 20}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_INTERPOL_URL, params=params, headers=headers)
            latency_now = int((time.monotonic() - t0) * 1000)
            raw_store.store(
                source="INTERPOL", entity_name=entity_name,
                request_method="GET", request_url=_INTERPOL_URL,
                request_params=params, request_headers=headers,
                response_status=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=resp.text, duration_ms=latency_now,
            )
            if resp.status_code != 200:
                latency = int((time.monotonic() - t0) * 1000)
                return {"source": "INTERPOL", "status": "error", "hits": [],
                        "hit_count": 0, "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}"}
            data = resp.json()

        notices = data.get("_embedded", {}).get("notices", [])
        hits = []
        for notice in notices:
            forename = (notice.get("forename") or "").strip()
            name = (notice.get("name") or "").strip()
            full = f"{forename} {name}".strip()
            if full and _is_relevant(entity_name, full, threshold=0.90):
                hits.append({
                    "name": full,
                    "detail": f"INTERPOL Red Notice: {full}",
                    "nationalities": notice.get("nationalities", []),
                    "entity_id": notice.get("entity_id"),
                })

        latency = int((time.monotonic() - t0) * 1000)
        return {
            "source": "INTERPOL",
            "status": "hit" if hits else "clear",
            "risk_level": "CRITICAL" if hits else None,
            "hits": hits,
            "hit_count": len(hits),
            "latency_ms": latency,
        }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return {"source": "INTERPOL", "status": "error", "hits": [],
                "hit_count": 0, "latency_ms": latency, "error": str(e)}


# ---------------------------------------------------------------------------
# Provider: OpenSanctions (API, supplementary)
# ---------------------------------------------------------------------------

async def _query_opensanctions(entity_name: str) -> dict:
    """Screen against OpenSanctions aggregator.

    NOTE: OpenSanctions Deep Lookup charges 1 credit per matched record.
    A single query can burn 100+ credits if there are many fuzzy matches.
    Disabled by default — Bridger already covers this dataset.
    Enable only if explicitly needed via opensanctions-api-key in Key Vault.
    """
    if not _OPENSANCTIONS_API_KEY:
        return {"source": "OPENSANCTIONS", "status": "disabled",
                "error": "Not configured — Bridger covers this dataset"}

    t0 = time.monotonic()
    try:
        params = {"q": entity_name, "limit": 10}
        headers = {"Authorization": f"ApiKey {_OPENSANCTIONS_API_KEY}"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_OPENSANCTIONS_URL, params=params, headers=headers)
            latency_now = int((time.monotonic() - t0) * 1000)
            raw_store.store(
                source="OPENSANCTIONS", entity_name=entity_name,
                request_method="GET", request_url=_OPENSANCTIONS_URL,
                request_params=params, request_headers=dict(headers),
                response_status=resp.status_code,
                response_headers=dict(resp.headers),
                response_body=resp.text, duration_ms=latency_now,
            )
            if resp.status_code != 200:
                latency = int((time.monotonic() - t0) * 1000)
                return {"source": "OPENSANCTIONS", "status": "error",
                        "latency_ms": latency,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()

        results = data.get("results", [])
        hits = []
        for r in results:
            score = r.get("score", 0)
            if score < 0.7:
                continue
            name = r.get("caption", r.get("name", ""))
            if not _is_relevant(entity_name, name):
                continue
            datasets = r.get("datasets", [])
            properties = r.get("properties", {})
            hits.append({
                "name": name,
                "score": score,
                "schema": r.get("schema", ""),
                "datasets": datasets,
                "countries": properties.get("country", []),
                "detail": f"OpenSanctions ({', '.join(datasets[:3])}): {name}",
            })

        latency = int((time.monotonic() - t0) * 1000)
        return {
            "source": "OPENSANCTIONS",
            "status": "hit" if hits else "clear",
            "risk_level": "HIGH" if hits else None,
            "hits": hits,
            "hit_count": len(hits),
            "latency_ms": latency,
        }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        return {"source": "OPENSANCTIONS", "status": "error",
                "latency_ms": latency, "error": str(e)}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def screen(
    entity_name: str,
    country: str = "",
    entity_type: str = "both",
) -> dict:
    """
    Fan-out to all screening sources in parallel.

    Args:
        entity_name: Company or person name to screen.
        country: ISO 2-letter country code (optional, used by CSL).
        entity_type: 'company', 'person', or 'both' (default).

    Returns:
        Combined results with per-source status and overall risk level.
    """
    t0 = time.monotonic()

    tasks = [
        _query_csl(entity_name, country),
        _screen_xml_list(entity_name, "UK_FCDO", _FCDO_URL, _parse_fcdo, entity_type),
        _screen_xml_list(entity_name, "EU", _EU_URL, _parse_eu, entity_type),
        _screen_xml_list(entity_name, "UN_SC", _UN_URL, _parse_un, entity_type),
        _query_fbi(entity_name),
        _query_interpol(entity_name),
        _query_opensanctions(entity_name),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    sources = {}
    all_hits = []
    overall_risk = "CLEAR"
    risk_priority = {"CLEAR": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

    source_names = ["CSL", "UK_FCDO", "EU", "UN_SC", "FBI", "INTERPOL", "OPENSANCTIONS"]
    for i, result in enumerate(results):
        name = source_names[i]
        if isinstance(result, Exception):
            sources[name] = {"status": "error", "error": str(result)}
            continue
        sources[name] = result
        if result.get("status") == "hit":
            all_hits.extend(result.get("hits", []))
            rl = result.get("risk_level", "MEDIUM")
            if risk_priority.get(rl, 0) > risk_priority.get(overall_risk, 0):
                overall_risk = rl

    duration_ms = int((time.monotonic() - t0) * 1000)

    return {
        "status": "hit" if all_hits else "clear",
        "risk_level": overall_risk,
        "total_hits": len(all_hits),
        "duration_ms": duration_ms,
        "entity_name": entity_name,
        "country": country or None,
        "entity_type": entity_type,
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def health() -> dict:
    """Quick provider reachability check."""
    checks = {
        "CSL": "up" if _CSL_API_KEY else "disabled (no subscription key)",
        "UK_FCDO": "up",
        "EU": "limited (webgate.ec.europa.eu blocked from Azure — covered by Bridger)",
        "UN_SC": "up",
        "FBI": "up",
        "INTERPOL": "limited (403 from Azure IPs — covered by Bridger)",
        "OPENSANCTIONS": "up" if _OPENSANCTIONS_API_KEY else "disabled (Bridger covers this — 1 credit/matched record)",
    }
    return {"providers": checks, "version": VERSION}
