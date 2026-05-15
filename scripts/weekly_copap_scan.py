#!/usr/bin/env python3
"""
Weekly COPAP Group Digital Footprint Scanner.

Scans 7 active COPAP entities across dark web, sanctions, and adverse media.
Compares to previous week's baseline. Generates delta PDF report.
Uploads to blob storage. Sends Teams adaptive card with summary + download link.

Runs: Sunday 22:00 UTC (cron)
Delivery: Teams webhook + blob storage PDF

Usage:
    python3 weekly_copap_scan.py              # full scan + report
    python3 weekly_copap_scan.py --dry-run    # scan only, no Teams/blob
    python3 weekly_copap_scan.py --report-only # regenerate report from last scan
"""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

import requests

# ── Paths ──
BASE_DIR = Path("/home/copapadmin/crawl")
CONFIG_FILE = BASE_DIR / "config" / "copap_weekly_entities.json"
DATA_DIR = BASE_DIR / "data" / "copap-weekly"
LOG_FILE = BASE_DIR / "logs" / "weekly_copap_scan.log"
REPORT_DIR = BASE_DIR / "output" / "copap-weekly"
SSH_KEY = Path.home() / ".ssh" / "crawldevvm_key.pem"

# ── Dark web VM ──
DARKWEB_IP = "20.86.161.6"
DARKWEB_USER = "copapadmin"
DARKWEB_PORT = 8450

# ── Gateway API (local, for CIR) ──
GATEWAY_URL = "http://127.0.0.1:8400"
GATEWAY_MAIN_PY = BASE_DIR / "api" / "main.py"

# ── Blob storage ──
BLOB_ACCOUNT = "stcrawlosint"
BLOB_CONTAINER = "osint-staging"

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("weekly-copap")


def _get_secret(name: str) -> str:
    """Get secret from Key Vault (with env fallback)."""
    try:
        sys.path.insert(0, str(BASE_DIR / "api"))
        from keyvault import get_secret
        return get_secret(name) or ""
    except Exception:
        return os.environ.get(name.replace("-", "_").upper(), "")


def _load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _load_baseline() -> dict:
    """Load last week's scan results for delta comparison."""
    baseline_file = DATA_DIR / "latest_baseline.json"
    if baseline_file.exists():
        with open(baseline_file) as f:
            return json.load(f)
    return {}


def _save_baseline(data: dict):
    """Save this week's scan as next week's baseline."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Archive with date
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    archive_file = DATA_DIR / f"scan_{date_str}.json"
    with open(archive_file, "w") as f:
        json.dump(data, f, indent=2, default=str)
    # Update latest
    latest_file = DATA_DIR / "latest_baseline.json"
    with open(latest_file, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info(f"Baseline saved: {archive_file}")


# ─────────────────────────────────────────────────────────────────────
# Dark Web Scanning (direct SSH to dark web VM, bypasses gateway block)
# ─────────────────────────────────────────────────────────────────────

def _ssh_darkweb_disable_block() -> bool:
    """SSH to dark web VM: back up gateway, remove 'copap' from blocked terms, restart."""
    ssh_cmd = [
        "ssh", "-i", str(SSH_KEY),
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        f"{DARKWEB_USER}@{DARKWEB_IP}",
        """
        GATEWAY=/home/copapadmin/crawl/api/darkweb_gateway.py
        cp "$GATEWAY" "$GATEWAY.weekly_bak"
        sed -i 's/"copap", //' "$GATEWAY"
        sudo systemctl restart darkweb-gateway
        sleep 3
        echo "BLOCK_DISABLED_OK"
        """
    ]
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
        if "BLOCK_DISABLED_OK" in result.stdout:
            log.info("Dark web: blocked terms disabled for self-scan")
            return True
        log.error(f"Dark web disable failed: {result.stderr[:200]}")
        return False
    except Exception as e:
        log.error(f"Dark web disable error: {e}")
        return False


def _ssh_darkweb_restore_block():
    """SSH to dark web VM: restore blocked terms backup, restart."""
    ssh_cmd = [
        "ssh", "-i", str(SSH_KEY),
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        f"{DARKWEB_USER}@{DARKWEB_IP}",
        """
        GATEWAY=/home/copapadmin/crawl/api/darkweb_gateway.py
        if [ -f "$GATEWAY.weekly_bak" ]; then
            cp "$GATEWAY.weekly_bak" "$GATEWAY"
            rm "$GATEWAY.weekly_bak"
            sudo systemctl restart darkweb-gateway
            echo "BLOCK_RESTORED_OK"
        else
            echo "NO_BACKUP_FOUND"
        fi
        """
    ]
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
        if "BLOCK_RESTORED_OK" in result.stdout:
            log.info("Dark web: blocked terms restored")
        else:
            log.warning(f"Dark web restore: {result.stdout.strip()}")
    except Exception as e:
        log.error(f"Dark web restore error: {e}")


def _ssh_darkweb_scan_single(entity_name: str, country: str, domain: str | None) -> dict:
    """Run a single dark web scan (assumes block already disabled).

    The dark web API returns a summary with job_id. The full findings are
    in /home/copapadmin/crawl/output/<job_id>.json on the VM. We run the
    scan, extract the job_id, then cat the output file to get findings.
    """
    payload = {
        "entity_name": entity_name,
        "country": country,
        "depth": "standard",
    }
    if domain:
        payload["domain"] = domain

    payload_json = json.dumps(payload).replace("'", "'\\''")

    # Step 1: Run scan and get job_id
    ssh_cmd = [
        "ssh", "-i", str(SSH_KEY),
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=10",
        f"{DARKWEB_USER}@{DARKWEB_IP}",
        f"curl -s -X POST http://127.0.0.1:{DARKWEB_PORT}/api/v1/research "
        f"-H 'Content-Type: application/json' "
        f"-H 'X-API-Key: dwk_crawl_2026Q2_f8a3b7e1d9c4' "
        f"-d '{payload_json}'"
    ]

    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            log.error(f"Dark web SSH failed for {entity_name}: {result.stderr[:200]}")
            return {"error": result.stderr[:200], "findings": []}

        output = result.stdout.strip()
        start = output.find("{")
        end = output.rfind("}") + 1
        if start < 0 or end <= start:
            log.error(f"No JSON in dark web response for {entity_name}: {output[:200]}")
            return {"error": "no JSON in response", "findings": []}

        api_resp = json.loads(output[start:end])
        job_id = api_resp.get("job_id", "")
        findings_count = api_resp.get("findings_count", 0)
        log.info(f"  {entity_name}: job_id={job_id}, findings_count={findings_count}")

        if not job_id:
            return {"error": "no job_id", "findings": [], "summary": api_resp.get("summary", {})}

        # Step 2: Fetch full findings from output file on VM
        fetch_cmd = [
            "ssh", "-i", str(SSH_KEY),
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10",
            f"{DARKWEB_USER}@{DARKWEB_IP}",
            f"cat /home/copapadmin/crawl/output/{job_id}.json"
        ]
        fetch_result = subprocess.run(fetch_cmd, capture_output=True, text=True, timeout=30)
        if fetch_result.returncode == 0 and fetch_result.stdout.strip():
            full_data = json.loads(fetch_result.stdout)
            return full_data
        else:
            # Fallback: return API summary with empty findings
            log.warning(f"Could not fetch findings file for {entity_name} (job {job_id})")
            return {"error": None, "findings": [], "summary": api_resp.get("summary", {})}

    except subprocess.TimeoutExpired:
        log.error(f"Dark web scan timed out for {entity_name}")
        return {"error": "timeout", "findings": []}
    except Exception as e:
        log.error(f"Dark web scan error for {entity_name}: {e}")
        return {"error": str(e), "findings": []}


def _filter_noise(findings: list, entity_name: str) -> list:
    """Remove irrelevant dark web findings (Reddit noise, generic web mentions).

    Reddit social_mention hits match on common words like 'Inc', 'USA',
    'trading' — not the actual entity. Filter them unless they contain
    the entity name (or a meaningful substring) in the title/content.

    For entities with multiple common words (e.g. 'SIGMA TRADE FINANCE'),
    require at least 2 distinctive words to co-occur — a single match on
    'sigma' or 'finance' alone catches too much noise.
    """
    # Build match terms from entity name (skip generic words)
    skip_words = {"inc", "inc.", "llc", "co.", "co", "ltd", "usa", "europe",
                  "middle", "east", "general", "trading", "international",
                  "the", "of", "and"}
    name_parts = [w.lower() for w in entity_name.split() if w.lower() not in skip_words]
    # Distinctive words: 4+ chars, not super-common financial terms
    common_financial = {"trade", "finance", "sigma", "capital", "global", "group"}
    truly_unique = [w for w in name_parts if len(w) >= 4 and w not in common_financial]
    semi_distinctive = [w for w in name_parts if len(w) >= 4]

    filtered = []
    removed = 0
    for f in findings:
        source = (f.get("source") or "").lower()
        ftype = (f.get("type") or "").lower()

        # Always keep: breaches, sanctions, leaked docs, darknet, ransomware
        if ftype in ("breach_record", "infostealer_exposure", "sanctions_hit",
                      "debarment_record", "leaked_document", "darknet_mention",
                      "ransomware_victim", "paste", "certificate"):
            filtered.append(f)
            continue

        # Reddit social_mention: only keep if entity name actually appears
        if source == "reddit" and ftype == "social_mention":
            text = (f.get("title", "") + " " + f.get("content", "")).lower()

            if truly_unique:
                # Has a unique word like "copap" — one match is enough
                if any(w in text for w in truly_unique):
                    filtered.append(f)
                else:
                    removed += 1
            elif len(semi_distinctive) >= 2:
                # All words are common (sigma, trade, finance) — require 2+ to co-occur
                matches = sum(1 for w in semi_distinctive if w in text)
                if matches >= 2:
                    filtered.append(f)
                else:
                    removed += 1
            else:
                # Fallback: single word entity, keep if it matches
                if any(w in text for w in semi_distinctive):
                    filtered.append(f)
                else:
                    removed += 1
            continue

        # All other findings: keep
        filtered.append(f)

    if removed:
        log.info(f"  Filtered {removed} irrelevant Reddit mentions for {entity_name}")
    return filtered


def scan_darkweb_batch(entities: list[dict]) -> dict[str, dict]:
    """Scan all entities on dark web in one batch.

    Disables blocked terms once, scans all entities, restores once.
    Much faster than per-entity restart (1 restart vs N restarts).
    """
    results = {}

    # Disable block once
    if not _ssh_darkweb_disable_block():
        log.error("Cannot disable dark web block — skipping all dark web scans")
        for e in entities:
            results[e["name"]] = {
                "entity": e["name"], "source": "darkweb",
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "total_findings": 0, "findings": [], "error": "block disable failed",
            }
        return results

    try:
        for entity in entities:
            log.info(f"Dark web: {entity['name']}")
            raw = _ssh_darkweb_scan_single(entity["name"], entity["country"], entity.get("domain"))

            findings = raw.get("findings", [])
            findings = _filter_noise(findings, entity["name"])
            for f in findings:
                fp = hashlib.md5(json.dumps(f, sort_keys=True, default=str).encode()).hexdigest()[:12]
                f["_fingerprint"] = fp

            results[entity["name"]] = {
                "entity": entity["name"],
                "source": "darkweb",
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "total_findings": len(findings),
                "findings": findings,
                "error": raw.get("error"),
            }
            time.sleep(2)  # Pace requests to dark web VM
    finally:
        # Always restore, even if scans fail
        _ssh_darkweb_restore_block()

    return results


# ─────────────────────────────────────────────────────────────────────
# Sanctions Screening (direct API calls, no gateway)
# ─────────────────────────────────────────────────────────────────────

def _screen_csl(entity_name: str, country: str) -> dict:
    """US Consolidated Screening List (11 federal lists)."""
    try:
        csl_key = _get_secret("csl-subscription-key")
        url = "https://data.trade.gov/consolidated_screening_list/v1/search"
        params = {"name": entity_name, "fuzzy_name": "true", "size": 10}
        if country:
            params["countries"] = country
        headers = {}
        if csl_key:
            headers["subscription-key"] = csl_key
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            # Only count exact/close matches
            hits = [r for r in results if r.get("score", 0) > 80]
            return {"source": "CSL", "status": "clear" if not hits else "hit",
                    "hit_count": len(hits), "hits": hits[:5]}
        return {"source": "CSL", "status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"source": "CSL", "status": "error", "error": str(e)}


def _screen_opensanctions(entity_name: str) -> dict:
    """OpenSanctions API."""
    try:
        url = "https://api.opensanctions.org/match/default"
        resp = requests.post(url, json={
            "queries": {"q1": {"schema": "LegalEntity", "properties": {"name": [entity_name]}}}
        }, timeout=15, headers={"Authorization": "ApiKey " + _get_secret("opensanctions-api-key")} if _get_secret("opensanctions-api-key") else {})
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("responses", {}).get("q1", {}).get("results", [])
            hits = [r for r in results if r.get("score", 0) > 0.7]
            return {"source": "OpenSanctions", "status": "clear" if not hits else "hit",
                    "hit_count": len(hits), "hits": [{"name": h.get("caption", ""), "score": h.get("score")} for h in hits[:5]]}
        return {"source": "OpenSanctions", "status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"source": "OpenSanctions", "status": "error", "error": str(e)}


def _screen_interpol(entity_name: str) -> dict:
    """Interpol Red Notices."""
    try:
        url = "https://ws-public.interpol.int/notices/v1/red"
        resp = requests.get(url, params={"name": entity_name, "resultPerPage": 20}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("total", 0)
            return {"source": "INTERPOL", "status": "clear" if total == 0 else "hit", "hit_count": total}
        return {"source": "INTERPOL", "status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"source": "INTERPOL", "status": "error", "error": str(e)}


def scan_screening(entity: dict) -> dict:
    """Screen entity against sanctions lists."""
    log.info(f"Screening: {entity['name']}")
    results = []
    results.append(_screen_csl(entity["name"], entity["country"]))
    results.append(_screen_opensanctions(entity["name"]))
    results.append(_screen_interpol(entity["name"]))

    any_hit = any(r.get("status") == "hit" for r in results)
    return {
        "entity": entity["name"],
        "source": "screening",
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "overall_status": "HIT" if any_hit else "CLEAR",
        "sources": results,
    }


# ─────────────────────────────────────────────────────────────────────
# Adverse Media (GDELT direct — no gateway needed)
# ─────────────────────────────────────────────────────────────────────

def scan_media(entity: dict) -> dict:
    """Check GDELT for adverse media in last 7 days."""
    log.info(f"Media: {entity['name']}")
    name = entity["name"]
    articles = []
    try:
        query = f'"{name}" tone<-3'
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 20,
            "sort": "datedesc",
            "timespan": "1w",
        }
        resp = requests.get(url, params=params, timeout=35)
        if resp.status_code == 200:
            data = resp.json()
            for art in data.get("articles", []):
                articles.append({
                    "title": (art.get("title") or "")[:200],
                    "url": art.get("url", ""),
                    "source": art.get("domain", ""),
                    "date": art.get("seendate", ""),
                    "tone": art.get("tone", 0),
                })
    except Exception as e:
        log.warning(f"GDELT error for {name}: {e}")

    # Fingerprint
    for a in articles:
        fp = hashlib.md5(a.get("url", "").encode()).hexdigest()[:12]
        a["_fingerprint"] = fp

    return {
        "entity": name,
        "source": "media",
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "total_articles": len(articles),
        "articles": articles,
    }


# ─────────────────────────────────────────────────────────────────────
# CIR (OpenClaw) Research — full counterparty intelligence
# ─────────────────────────────────────────────────────────────────────

def _gateway_disable_block() -> bool:
    """Temporarily remove copap-related entries from _BLOCKED_TERMS in main.py, restart gateway."""
    try:
        content = GATEWAY_MAIN_PY.read_text()
        # Back up
        bak = GATEWAY_MAIN_PY.with_suffix(".py.weekly_bak")
        bak.write_text(content)

        # Replace the specific _BLOCKED_TERMS line that contains copap entries
        # Only target the line inside the list definition, not other occurrences
        old_line = '    "copap", "copapadmin", "copap ai", "copap trading",'
        new_line = '    # copap terms temporarily disabled for self-scan'
        content = content.replace(old_line, new_line, 1)
        GATEWAY_MAIN_PY.write_text(content)

        # Clear Python bytecode cache so workers load the modified source
        pycache_dir = GATEWAY_MAIN_PY.parent / "__pycache__"
        if pycache_dir.exists():
            import shutil
            shutil.rmtree(pycache_dir)

        # Stop service and kill any orphan uvicorn workers, then start fresh
        subprocess.run(
            ["sudo", "systemctl", "stop", "crawl-gateway"],
            capture_output=True, text=True, timeout=15,
        )
        # Kill any orphan workers that survived the stop
        subprocess.run(
            "sudo kill $(pgrep -x uvicorn) 2>/dev/null; true",
            shell=True, capture_output=True, text=True, timeout=10,
        )
        time.sleep(2)
        subprocess.run(
            ["sudo", "systemctl", "start", "crawl-gateway"],
            capture_output=True, text=True, timeout=30,
        )

        # Wait for workers to become ready (uvicorn takes ~5-8s after restart)
        for attempt in range(8):
            time.sleep(3)
            try:
                resp = requests.get(f"{GATEWAY_URL}/api/v1/health", timeout=5)
                if resp.status_code == 200:
                    log.info("Gateway: blocked terms disabled for self-scan, service restarted")
                    return True
            except Exception:
                pass

        log.error("Gateway failed to restart after block disable")
        _gateway_restore_block()
        return False
    except Exception as e:
        log.error(f"Gateway block disable error: {e}")
        return False


def _gateway_restore_block():
    """Restore original main.py and restart gateway."""
    try:
        bak = GATEWAY_MAIN_PY.with_suffix(".py.weekly_bak")
        if bak.exists():
            bak.rename(GATEWAY_MAIN_PY)
            # Clear bytecode cache
            pycache_dir = GATEWAY_MAIN_PY.parent / "__pycache__"
            if pycache_dir.exists():
                import shutil
                shutil.rmtree(pycache_dir)
            # Stop service, kill orphans, start fresh
            subprocess.run(
                ["sudo", "systemctl", "stop", "crawl-gateway"],
                capture_output=True, text=True, timeout=15,
            )
            subprocess.run(
                "sudo kill $(pgrep -x uvicorn) 2>/dev/null; true",
                shell=True, capture_output=True, text=True, timeout=10,
            )
            time.sleep(2)
            subprocess.run(
                ["sudo", "systemctl", "start", "crawl-gateway"],
                capture_output=True, text=True, timeout=30,
            )
            time.sleep(5)
            log.info("Gateway: blocked terms restored, service restarted")
        else:
            log.warning("Gateway: no backup file found to restore")
    except Exception as e:
        log.error(f"Gateway restore error: {e}")


def _submit_cir(entity: dict, api_key: str) -> str | None:
    """Submit CIR job, return job_id."""
    payload = {
        "entity_legal_name": entity["name"],
        "entity_country": entity["country"],
    }
    if entity.get("domain"):
        payload["entity_website"] = entity["domain"]

    try:
        resp = requests.post(
            f"{GATEWAY_URL}/api/v1/research",
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            job_id = data.get("job_id")
            log.info(f"  CIR submitted: {entity['name']} -> job_id={job_id}")
            return job_id
        else:
            log.error(f"  CIR submit failed for {entity['name']}: HTTP {resp.status_code} {resp.text[:200]}")
            return None
    except Exception as e:
        log.error(f"  CIR submit error for {entity['name']}: {e}")
        return None


def _poll_cir(job_id: str, api_key: str, timeout_sec: int = 600) -> dict:
    """Poll CIR job until completed or failed. Returns job data."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{GATEWAY_URL}/api/v1/research/{job_id}",
                headers={"X-API-Key": api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "")
                if status in ("completed", "failed"):
                    return data
        except Exception:
            pass
        time.sleep(15)  # CIR takes 3-10 minutes

    return {"status": "timeout", "error": "polling timeout"}


def _fetch_cir_blob(blob_path: str) -> dict:
    """Fetch CIR blob JSON from osint-staging."""
    sas_token = ""
    sas_file = BASE_DIR / "config" / "blob_sas_token"
    if sas_file.exists():
        sas_token = sas_file.read_text().strip()
    if not sas_token:
        sas_token = _get_secret("blob-sas-token")

    # Strip container prefix if already in blob_path (e.g. "osint-staging/cir/..." -> "cir/...")
    clean_path = blob_path
    if clean_path.startswith(f"{BLOB_CONTAINER}/"):
        clean_path = clean_path[len(BLOB_CONTAINER) + 1:]

    url = f"https://{BLOB_ACCOUNT}.blob.core.windows.net/{BLOB_CONTAINER}/{clean_path}?{sas_token}"
    try:
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        else:
            log.warning(f"Blob fetch HTTP {resp.status_code} for {clean_path}")
    except Exception as e:
        log.error(f"Blob fetch error for {clean_path}: {e}")
    return {}


def _extract_cir_findings(blob: dict) -> dict:
    """Extract key CIR findings from blob for the weekly report."""
    if not blob:
        return {}

    findings = {}

    # Executive summary
    exec_summary = blob.get("executive_summary", "")
    if exec_summary:
        findings["executive_summary"] = exec_summary[:2000]

    # Risk assessment
    risk = blob.get("risk_assessment", "")
    if risk:
        findings["risk_assessment"] = risk[:1500]

    # Risk score
    risk_score = blob.get("risk_score")
    if risk_score is not None:
        findings["risk_score"] = risk_score

    # Corporate registry
    corp_reg = blob.get("corporate_registry", {})
    if corp_reg:
        findings["corporate_registry"] = {
            "status": corp_reg.get("registration_status", corp_reg.get("status", "")),
            "registration_date": corp_reg.get("registration_date", corp_reg.get("date_of_incorporation", "")),
            "registered_address": corp_reg.get("registered_address", corp_reg.get("address", "")),
            "business_type": corp_reg.get("business_type", corp_reg.get("entity_type", "")),
        }

    # Key individuals / directors / UBOs
    key_people = []
    for section in ["key_individuals", "beneficial_ownership", "related_entities"]:
        items = blob.get(section)
        if isinstance(items, list):
            for person in items[:10]:
                if isinstance(person, dict):
                    name = person.get("name", person.get("individual_name", ""))
                    role = person.get("title", person.get("role", person.get("relationship", "")))
                    if name:
                        key_people.append({"name": name, "role": role})
                elif isinstance(person, str):
                    key_people.append({"name": person, "role": ""})
        elif isinstance(items, str) and items.strip():
            findings[f"{section}_text"] = items[:500]

    if key_people:
        findings["key_people"] = key_people[:15]

    # Sanctions from CIR
    sanctions = blob.get("sanctions_screening", {})
    if sanctions:
        findings["sanctions"] = sanctions

    # Adverse media from CIR
    adverse = blob.get("adverse_media", {})
    if adverse:
        findings["adverse_media"] = adverse

    # Litigation
    litigation = blob.get("litigation", {})
    if litigation:
        findings["litigation"] = litigation

    # Fingerprint the whole thing for delta detection
    fp_str = json.dumps(findings, sort_keys=True, default=str)
    findings["_fingerprint"] = hashlib.md5(fp_str.encode()).hexdigest()[:12]

    return findings


def scan_cir_batch(entities: list[dict]) -> dict[str, dict]:
    """Run CIR (OpenClaw) research for all entities.

    Disables gateway blocked terms once, submits all jobs, polls for
    completion, fetches blobs, extracts findings, restores block.
    """
    results = {}
    api_key = _get_secret("cir-api-key")
    if not api_key:
        log.error("CIR: no API key available — skipping all CIR scans")
        for e in entities:
            results[e["name"]] = {
                "entity": e["name"], "source": "cir",
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "error": "no API key", "findings": {},
            }
        return results

    # Disable blocked terms on gateway
    if not _gateway_disable_block():
        log.error("CIR: cannot disable gateway block — skipping all CIR scans")
        for e in entities:
            results[e["name"]] = {
                "entity": e["name"], "source": "cir",
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "error": "gateway block disable failed", "findings": {},
            }
        return results

    try:
        # Submit all CIR jobs
        jobs = {}  # entity_name -> job_id
        for entity in entities:
            log.info(f"CIR: {entity['name']}")
            job_id = _submit_cir(entity, api_key)
            if job_id:
                jobs[entity["name"]] = job_id
            else:
                results[entity["name"]] = {
                    "entity": entity["name"], "source": "cir",
                    "scan_time": datetime.now(timezone.utc).isoformat(),
                    "error": "submit failed", "findings": {},
                }
            time.sleep(2)  # Pace submissions

        # Poll all jobs for completion (parallel polling via round-robin)
        pending = dict(jobs)
        completed = {}
        deadline = time.time() + 900  # 15 min max

        while pending and time.time() < deadline:
            for entity_name, job_id in list(pending.items()):
                try:
                    resp = requests.get(
                        f"{GATEWAY_URL}/api/v1/research/{job_id}",
                        headers={"X-API-Key": api_key},
                        timeout=15,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        status = data.get("status", "")
                        if status == "completed":
                            completed[entity_name] = data
                            del pending[entity_name]
                            log.info(f"  CIR completed: {entity_name}")
                        elif status == "failed":
                            completed[entity_name] = data
                            del pending[entity_name]
                            log.warning(f"  CIR failed: {entity_name}: {data.get('error', '?')}")
                except Exception:
                    pass

            if pending:
                time.sleep(20)

        # Mark timed-out jobs
        for entity_name in pending:
            completed[entity_name] = {"status": "timeout", "error": "polling timeout"}
            log.warning(f"  CIR timed out: {entity_name}")

        # Fetch blobs and extract findings
        for entity_name, job_data in completed.items():
            blob_path = job_data.get("blob_path", "")
            findings = {}
            error = job_data.get("error")

            if job_data.get("status") == "completed" and blob_path:
                blob = _fetch_cir_blob(blob_path)
                if blob:
                    findings = _extract_cir_findings(blob)
                    log.info(f"  CIR blob fetched: {entity_name} ({len(findings)} sections)")
                else:
                    error = error or "blob fetch failed"

            results[entity_name] = {
                "entity": entity_name,
                "source": "cir",
                "scan_time": datetime.now(timezone.utc).isoformat(),
                "status": job_data.get("status", "unknown"),
                "job_id": jobs.get(entity_name, ""),
                "blob_path": blob_path,
                "findings": findings,
                "error": error,
            }

    finally:
        # Always restore blocked terms
        _gateway_restore_block()

    return results


# ─────────────────────────────────────────────────────────────────────
# Delta Computation
# ─────────────────────────────────────────────────────────────────────

def compute_delta(current: dict, baseline: dict) -> dict:
    """Compare current scan to baseline, return changes."""
    changes = {}

    for entity_name, entity_data in current.items():
        prev = baseline.get(entity_name, {})
        entity_changes = {"entity": entity_name, "has_changes": False, "details": []}

        # Dark web delta
        curr_dw = entity_data.get("darkweb", {})
        prev_dw = prev.get("darkweb", {})
        curr_fps = {f["_fingerprint"] for f in curr_dw.get("findings", []) if "_fingerprint" in f}
        prev_fps = {f["_fingerprint"] for f in prev_dw.get("findings", []) if "_fingerprint" in f}
        new_fps = curr_fps - prev_fps
        removed_fps = prev_fps - curr_fps

        if new_fps:
            new_findings = [f for f in curr_dw.get("findings", []) if f.get("_fingerprint") in new_fps]
            entity_changes["details"].append({
                "type": "darkweb_new",
                "count": len(new_findings),
                "findings": new_findings,
            })
            entity_changes["has_changes"] = True

        if removed_fps:
            entity_changes["details"].append({
                "type": "darkweb_removed",
                "count": len(removed_fps),
            })

        # Screening delta
        curr_scr = entity_data.get("screening", {}).get("overall_status", "CLEAR")
        prev_scr = prev.get("screening", {}).get("overall_status", "CLEAR")
        if curr_scr != prev_scr:
            entity_changes["details"].append({
                "type": "screening_change",
                "from": prev_scr,
                "to": curr_scr,
            })
            entity_changes["has_changes"] = True

        # Media delta
        curr_media = entity_data.get("media", {})
        prev_media = prev.get("media", {})
        curr_media_fps = {a["_fingerprint"] for a in curr_media.get("articles", []) if "_fingerprint" in a}
        prev_media_fps = {a["_fingerprint"] for a in prev_media.get("articles", []) if "_fingerprint" in a}
        new_media = curr_media_fps - prev_media_fps
        if new_media:
            new_articles = [a for a in curr_media.get("articles", []) if a.get("_fingerprint") in new_media]
            entity_changes["details"].append({
                "type": "media_new",
                "count": len(new_articles),
                "articles": new_articles,
            })
            entity_changes["has_changes"] = True

        # CIR delta
        curr_cir = entity_data.get("cir", {})
        prev_cir = prev.get("cir", {})
        curr_cir_fp = curr_cir.get("findings", {}).get("_fingerprint", "")
        prev_cir_fp = prev_cir.get("findings", {}).get("_fingerprint", "")

        if curr_cir_fp and curr_cir_fp != prev_cir_fp:
            entity_changes["details"].append({
                "type": "cir_new" if not prev_cir_fp else "cir_changed",
                "findings": curr_cir.get("findings", {}),
            })
            entity_changes["has_changes"] = True

        # Totals for quick reference
        cir_status = curr_cir.get("status", "")
        entity_changes["current_totals"] = {
            "darkweb_findings": curr_dw.get("total_findings", 0),
            "screening_status": curr_scr,
            "media_articles": curr_media.get("total_articles", 0),
            "cir_status": cir_status if cir_status else "N/A",
        }

        changes[entity_name] = entity_changes

    return changes


# ─────────────────────────────────────────────────────────────────────
# PDF Report Generation
# ─────────────────────────────────────────────────────────────────────

def _web_synopsis(domain: str, title: str) -> str:
    """Generate a short synopsis for a web mention based on the source domain."""
    d = domain.lower()
    t = title.lower()

    # Corporate registries
    if any(x in d for x in ["opencorporates", "canadacompanyregistry", "federalcorporation",
                             "opengovca", "opengovus", "opencorpdata", "annuaire-entreprises",
                             "pappers.fr", "societe.com", "northdata", "traderegistry"]):
        return "Corporate registry listing"
    # LEI / legal entity
    if "lei" in d or "lei" in t:
        return "LEI record"
    # LinkedIn
    if "linkedin" in d:
        return "LinkedIn company profile"
    # Business intelligence / profiles
    if any(x in d for x in ["dnb.com", "crunchbase", "craft.co", "bloomberg",
                             "rocketreach", "contactout", "datanyze", "signalhire",
                             "zoominfo", "apollo"]):
        return "Business intelligence profile"
    # Trade / import-export data
    if any(x in d for x in ["importinfo", "importgenius", "importkey", "panjiva",
                             "tendata", "trademo", "nbd.ltd"]):
        return "Import/export trade records"
    # Employment
    if any(x in d for x in ["indeed", "glassdoor", "ziprecruiter"]):
        return "Job listings / employer profile"
    # Industry directories
    if any(x in d for x in ["paper-world", "bizapedia", "cortera",
                             "lagazettefrance", "scribd", "diligenciagroup",
                             "clarifiedby"]):
        return "Industry/business directory"
    # Company's own website
    if "copap.com" in d:
        return "Company website (own)"
    # Generic fallback using title
    if "import" in t or "export" in t or "shipment" in t:
        return "Trade/shipping data"
    if "profile" in t or "company" in t:
        return "Company profile"
    if "directory" in t:
        return "Business directory listing"
    return "Web presence"


def _clean_url(raw_url: str) -> str:
    """Extract actual URL from DuckDuckGo redirect wrappers."""
    if not raw_url:
        return ""
    # DDG Tor redirect: /l/?uddg=https%3A%2F%2F...
    if raw_url.startswith("/l/?uddg=") or "uddg=" in raw_url:
        try:
            parsed = parse_qs(urlparse(raw_url).query)
            if "uddg" in parsed:
                return unquote(parsed["uddg"][0])
        except Exception:
            pass
    # Already a clean URL
    if raw_url.startswith("http"):
        return raw_url
    return raw_url


def generate_pdf(scan_data: dict, delta: dict, is_baseline: bool) -> str:
    """Generate weekly PDF report. Returns file path."""
    from fpdf import FPDF

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    week_str = datetime.now(timezone.utc).strftime("%d %B %Y")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = str(REPORT_DIR / f"COPAP_Weekly_{date_str}.pdf")

    def safe(text):
        if isinstance(text, list):
            text = ", ".join(str(t) for t in text)
        if not isinstance(text, str):
            text = str(text)
        return text.encode("latin-1", "replace").decode("latin-1")

    class WeeklyReport(FPDF):
        def header(self):
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(120, 120, 120)
            self.cell(0, 5, f"CONFIDENTIAL  |  COPAP Weekly Scan  |  {week_str}", align="C")
            self.ln(7)
            self.set_draw_color(200, 200, 200)
            self.line(10, self.get_y(), 200, self.get_y())
            self.ln(2)

        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    pdf = WeeklyReport()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ── Cover ──
    pdf.add_page()
    pdf.ln(25)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(25, 60, 120)
    pdf.cell(0, 12, "COPAP Group", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 16)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 10, "Weekly Digital Footprint Scan", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_draw_color(25, 60, 120)
    pdf.line(70, pdf.get_y(), 140, pdf.get_y())
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 7, f"Week of {week_str}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Classification: CONFIDENTIAL", align="C", new_x="LMARGIN", new_y="NEXT")
    if is_baseline:
        pdf.ln(5)
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(200, 140, 0)
        pdf.cell(0, 7, "BASELINE SCAN -- all findings shown (no prior week to compare)", align="C",
                 new_x="LMARGIN", new_y="NEXT")

    # ── Executive Summary ──
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(25, 60, 120)
    pdf.cell(0, 10, safe("Executive Summary"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(25, 60, 120)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    # Count changes
    total_changes = sum(1 for e in delta.values() if e.get("has_changes"))
    entities_scanned = len(delta)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(40, 40, 40)
    if is_baseline:
        pdf.multi_cell(0, 5, safe(
            f"Baseline scan completed for {entities_scanned} COPAP entities. "
            "All findings are listed below. Future weekly reports will show only changes."
        ))
    elif total_changes == 0:
        pdf.set_text_color(30, 130, 30)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 10, safe("NO CHANGES THIS WEEK"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 5, safe(
            f"All {entities_scanned} entities scanned. No new breach records, no sanctions changes, "
            "no adverse media. Digital footprint unchanged from last week."
        ))
    else:
        pdf.set_text_color(180, 30, 30)
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 10, safe(f"{total_changes} ENTITY/ENTITIES WITH CHANGES"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(40, 40, 40)

    pdf.ln(4)

    # Summary table
    widths = [50, 22, 22, 18, 22, 56]
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_fill_color(25, 60, 120)
    pdf.set_text_color(255, 255, 255)
    headers = ["Entity", "Dark Web", "Sanctions", "Media", "CIR", "Status"]
    for i, h in enumerate(headers):
        pdf.cell(widths[i], 6, safe(h), fill=True,
                 new_x="RIGHT" if i < len(headers) - 1 else "LMARGIN",
                 new_y="TOP" if i < len(headers) - 1 else "NEXT")

    pdf.set_font("Helvetica", "", 7)
    for i, (entity_name, edata) in enumerate(delta.items()):
        totals = edata.get("current_totals", {})
        has_changes = edata.get("has_changes", False)
        if is_baseline:
            status = "BASELINE"
        elif has_changes:
            status = "CHANGED"
        else:
            status = "No change"

        if has_changes and not is_baseline:
            pdf.set_fill_color(255, 240, 240)
        elif i % 2 == 0:
            pdf.set_fill_color(245, 245, 250)
        else:
            pdf.set_fill_color(255, 255, 255)

        pdf.set_text_color(40, 40, 40)
        cir_status = totals.get("cir_status", "N/A")
        if cir_status == "completed":
            cir_status = "OK"
        row = [
            entity_name,
            str(totals.get("darkweb_findings", 0)),
            totals.get("screening_status", "?"),
            str(totals.get("media_articles", 0)),
            cir_status,
            status,
        ]
        for j, val in enumerate(row):
            pdf.cell(widths[j], 5, safe(val), fill=True,
                     new_x="RIGHT" if j < len(row) - 1 else "LMARGIN",
                     new_y="TOP" if j < len(row) - 1 else "NEXT")

    pdf.ln(6)

    # ── Detail sections for entities with changes (or all if baseline) ──
    for entity_name, edata in delta.items():
        if not is_baseline and not edata.get("has_changes"):
            continue

        if pdf.get_y() > 240:
            pdf.add_page()

        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(25, 60, 120)
        pdf.cell(0, 8, safe(entity_name), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(180, 180, 180)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)

        entity_scan = scan_data.get(entity_name, {})

        # Render findings with full detail (used for both delta + baseline)
        def _render_findings(findings_list, label, color_rgb):
            nonlocal pdf
            if not findings_list:
                return
            if pdf.get_y() > 240:
                pdf.add_page()

            def mc(w, h, text):
                """multi_cell with x reset to left margin."""
                pdf.set_x(10)
                pdf.multi_cell(w, h, safe(text))

            # Group findings by type for structured output
            breaches = [f for f in findings_list if f.get("type") == "breach_record"]
            infostealers = [f for f in findings_list if f.get("type") == "infostealer_exposure"]
            web_mentions = [f for f in findings_list if f.get("type") == "web_mention"]
            sanctions = [f for f in findings_list if f.get("type") in ("sanctions_hit", "debarment_record")]
            leaked = [f for f in findings_list if f.get("type") == "leaked_document"]
            other = [f for f in findings_list if f.get("type") not in
                     ("breach_record", "infostealer_exposure", "web_mention",
                      "sanctions_hit", "debarment_record", "leaked_document")]

            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(*color_rgb)
            pdf.cell(0, 6, safe(f"{label}: {len(findings_list)} finding(s)"), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)

            # ── Breach Records (highest priority) ──
            if breaches:
                if pdf.get_y() > 250:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(180, 30, 30)
                pdf.cell(0, 5, safe(f"BREACH RECORDS ({len(breaches)})  --  ACTION: reset passwords, enable MFA"),
                         new_x="LMARGIN", new_y="NEXT")

                # Table header
                bw = [55, 45, 50, 40]
                pdf.set_font("Helvetica", "B", 7)
                pdf.set_fill_color(180, 30, 30)
                pdf.set_text_color(255, 255, 255)
                for i, h in enumerate(["Email", "Breach Database", "Password Exposed?", "Source"]):
                    pdf.cell(bw[i], 5, safe(h), fill=True,
                             new_x="RIGHT" if i < 3 else "LMARGIN", new_y="TOP" if i < 3 else "NEXT")

                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(40, 40, 40)
                for j, b in enumerate(breaches):
                    if pdf.get_y() > 270:
                        pdf.add_page()
                    bg = (255, 245, 245) if j % 2 == 0 else (255, 255, 255)
                    pdf.set_fill_color(*bg)
                    email = b.get("email", "")
                    if not email:
                        # Company record (e.g. Bureau van Dijk) — show name instead
                        name_val = b.get("name", "")
                        if isinstance(name_val, list):
                            name_val = ", ".join(name_val)
                        email = name_val or "N/A"
                    db_name = b.get("database_name", "Unknown")
                    has_pw = "YES" if b.get("hashed_password") else "No"
                    src = b.get("source", "dehashed")
                    row = [email[:30], db_name[:25], has_pw, src]
                    for i, val in enumerate(row):
                        pdf.cell(bw[i], 4, safe(val), fill=True,
                                 new_x="RIGHT" if i < 3 else "LMARGIN", new_y="TOP" if i < 3 else "NEXT")
                pdf.ln(3)

            # ── Infostealer Exposure ──
            if infostealers:
                for ist in infostealers:
                    if pdf.get_y() > 250:
                        pdf.add_page()
                    total = ist.get("total_stealers", 0)
                    domain = ist.get("domain", "?")
                    if total > 0:
                        pdf.set_font("Helvetica", "B", 9)
                        pdf.set_text_color(180, 30, 30)
                        pdf.cell(0, 5, safe(f"INFOSTEALER EXPOSURE  --  {total} credential(s) stolen from {domain}"),
                                 new_x="LMARGIN", new_y="NEXT")
                        pdf.set_font("Helvetica", "", 7)
                        pdf.set_text_color(80, 80, 80)
                        mc(190, 3.5,
                            f"Source: HudsonRock Cavalier  |  ACTION: compromised machines need forensic review, "
                            f"all credentials for {domain} should be rotated"
                        )
                    else:
                        pdf.set_font("Helvetica", "", 8)
                        pdf.set_text_color(30, 130, 30)
                        pdf.cell(0, 5, safe(f"Infostealer check: CLEAN (no stolen credentials for {domain})"),
                                 new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(2)

            # ── Sanctions / Debarment ──
            if sanctions:
                if pdf.get_y() > 250:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(180, 30, 30)
                pdf.cell(0, 5, safe(f"SANCTIONS/DEBARMENT HITS ({len(sanctions)})  --  ACTION: immediate review"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(40, 40, 40)
                for s in sanctions:
                    title = s.get("title", s.get("name", "Unknown"))
                    src = s.get("source", "?")
                    mc(190, 3.5, f"  {src}: {title}")
                pdf.ln(2)

            # ── Leaked Documents ──
            if leaked:
                if pdf.get_y() > 250:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(200, 140, 0)
                pdf.cell(0, 5, safe(f"LEAKED DOCUMENTS ({len(leaked)})"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(40, 40, 40)
                for ld in leaked:
                    title = (ld.get("title") or "Untitled")[:80]
                    src = ld.get("source", "?")
                    url = _clean_url(ld.get("url", ""))
                    mc(190, 3.5, f"  [{src}] {title}")
                    if url:
                        pdf.set_text_color(80, 80, 180)
                        mc(190, 3.5, f"  {url[:90]}")
                        pdf.set_text_color(40, 40, 40)
                pdf.ln(2)

            # ── Web Mentions (public exposure) — table with synopsis ──
            if web_mentions:
                if pdf.get_y() > 240:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(60, 60, 60)
                pdf.cell(0, 5, safe(f"PUBLIC WEB EXPOSURE ({len(web_mentions)})"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.ln(1)

                # Table header
                ww = [50, 65, 75]  # Source, Synopsis, URL
                pdf.set_font("Helvetica", "B", 6.5)
                pdf.set_fill_color(60, 60, 60)
                pdf.set_text_color(255, 255, 255)
                for i, h in enumerate(["Source", "Synopsis", "URL"]):
                    pdf.cell(ww[i], 5, safe(h), fill=True,
                             new_x="RIGHT" if i < 2 else "LMARGIN", new_y="TOP" if i < 2 else "NEXT")

                pdf.set_font("Helvetica", "", 6)
                pdf.set_text_color(40, 40, 40)
                for j, wm in enumerate(web_mentions):
                    if pdf.get_y() > 270:
                        pdf.add_page()
                    title = (wm.get("title") or "Untitled")
                    url = _clean_url(wm.get("url", ""))
                    domain_name = ""
                    if url:
                        try:
                            domain_name = urlparse(url).netloc.replace("www.", "")
                        except Exception:
                            pass

                    synopsis = _web_synopsis(domain_name, title)

                    bg = (245, 245, 250) if j % 2 == 0 else (255, 255, 255)
                    pdf.set_fill_color(*bg)
                    pdf.set_text_color(40, 40, 40)

                    row = [
                        (domain_name or "?")[:28],
                        synopsis[:38],
                        (url or "")[:42],
                    ]
                    for i, val in enumerate(row):
                        pdf.cell(ww[i], 4, safe(val), fill=True,
                                 new_x="RIGHT" if i < 2 else "LMARGIN", new_y="TOP" if i < 2 else "NEXT")
                pdf.ln(3)

            # ── Other findings ──
            if other:
                if pdf.get_y() > 250:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(60, 60, 60)
                pdf.cell(0, 5, safe(f"OTHER FINDINGS ({len(other)})"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(40, 40, 40)
                for o in other:
                    src = o.get("source", "?")
                    ftype = o.get("type", "?")
                    url = _clean_url(o.get("url", ""))

                    # Build a meaningful description based on finding type
                    if ftype == "certificate_summary":
                        domain = o.get("domain", "?")
                        total = o.get("total_certificates", 0)
                        subs = o.get("all_subdomains", [])
                        desc = f"{domain}: {total} SSL certificates, subdomains: {', '.join(subs[:5])}"
                    elif ftype == "certificate_transparency":
                        cn = o.get("common_name", "?")
                        issuer = o.get("issuer", "?")
                        not_after = (o.get("not_after") or "")[:10]
                        desc = f"{cn} | Issuer: {issuer} | Expires: {not_after}"
                    elif ftype == "domain_scan_summary":
                        domain = o.get("domain", "?")
                        total = o.get("total_scans", o.get("total_results", ""))
                        desc = f"{domain}: {total} scans" if total else domain
                    elif ftype == "website_scan":
                        title = o.get("title", o.get("page_title", ""))
                        desc = title or o.get("domain", "?")
                    elif ftype == "error":
                        detail = o.get("detail", o.get("message", "unknown error"))
                        desc = f"ERROR: {detail}"
                    elif ftype == "social_mention":
                        title = (o.get("title") or o.get("content", ""))[:80]
                        desc = title
                    else:
                        desc = (o.get("title") or o.get("domain") or o.get("detail") or "")[:80]

                    mc(190, 3.5, f"  [{src}] {desc[:120]}")
                    if url:
                        pdf.set_text_color(80, 80, 180)
                        mc(190, 3.5, f"  {url[:90]}")
                        pdf.set_text_color(40, 40, 40)
                pdf.ln(2)

        # Delta mode: show new/changed findings
        for detail in edata.get("details", []):
            dtype = detail.get("type", "")

            if dtype == "darkweb_new":
                lbl = "DARK WEB FINDINGS" if is_baseline else "NEW DARK WEB FINDINGS"
                clr = (60, 60, 60) if is_baseline else (180, 30, 30)
                _render_findings(detail.get("findings", []), lbl, clr)

            elif dtype == "screening_change":
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(180, 30, 30)
                pdf.cell(0, 6, safe(f"SCREENING STATUS CHANGED: {detail['from']} -> {detail['to']}"),
                         new_x="LMARGIN", new_y="NEXT")

            elif dtype == "media_new":
                if pdf.get_y() > 250:
                    pdf.add_page()
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(200, 140, 0)
                pdf.cell(0, 6, safe(f"NEW ADVERSE MEDIA ({detail['count']})"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(40, 40, 40)
                for a in detail.get("articles", [])[:10]:
                    title = (a.get("title") or "Untitled")[:80]
                    src = a.get("source", "")
                    url = a.get("url", "")
                    tone = a.get("tone", 0)
                    date = (a.get("date") or "")[:10]
                    pdf.set_x(10); pdf.multi_cell(190, 3.5, safe(f"  {title}"))
                    pdf.set_text_color(80, 80, 80)
                    pdf.set_x(10); pdf.multi_cell(190, 3.5, safe(f"  Source: {src}  |  Date: {date}  |  Sentiment: {tone:.1f}"))
                    if url:
                        pdf.set_text_color(80, 80, 180)
                        pdf.set_x(10); pdf.multi_cell(190, 3.5, safe(f"  {url[:90]}"))
                    pdf.set_text_color(40, 40, 40)
                    pdf.ln(1)

            elif dtype in ("cir_new", "cir_changed"):
                cir_f = detail.get("findings", {})
                if cir_f:
                    if pdf.get_y() > 220:
                        pdf.add_page()

                    lbl = "CIR INTELLIGENCE REPORT" if is_baseline or dtype == "cir_new" else "CIR REPORT CHANGES"
                    clr = (25, 60, 120) if is_baseline else (180, 30, 30)
                    pdf.set_font("Helvetica", "B", 10)
                    pdf.set_text_color(*clr)
                    pdf.cell(0, 6, safe(lbl), new_x="LMARGIN", new_y="NEXT")
                    pdf.ln(1)

                    # Risk score
                    risk_score = cir_f.get("risk_score")
                    if risk_score is not None:
                        pdf.set_font("Helvetica", "B", 9)
                        if isinstance(risk_score, (int, float)) and risk_score >= 7:
                            pdf.set_text_color(180, 30, 30)
                        elif isinstance(risk_score, (int, float)) and risk_score >= 4:
                            pdf.set_text_color(200, 140, 0)
                        else:
                            pdf.set_text_color(30, 130, 30)
                        pdf.cell(0, 5, safe(f"Risk Score: {risk_score}/10"), new_x="LMARGIN", new_y="NEXT")
                        pdf.ln(1)

                    # Executive summary
                    exec_sum = cir_f.get("executive_summary", "")
                    if exec_sum:
                        pdf.set_font("Helvetica", "B", 8)
                        pdf.set_text_color(25, 60, 120)
                        pdf.cell(0, 5, safe("Executive Summary"), new_x="LMARGIN", new_y="NEXT")
                        pdf.set_font("Helvetica", "", 7)
                        pdf.set_text_color(40, 40, 40)
                        pdf.set_x(10)
                        pdf.multi_cell(190, 3.5, safe(exec_sum[:800]))
                        pdf.ln(2)

                    # Corporate registry
                    corp = cir_f.get("corporate_registry", {})
                    if corp and any(corp.values()):
                        if pdf.get_y() > 260:
                            pdf.add_page()
                        pdf.set_font("Helvetica", "B", 8)
                        pdf.set_text_color(25, 60, 120)
                        pdf.cell(0, 5, safe("Corporate Registry"), new_x="LMARGIN", new_y="NEXT")
                        pdf.set_font("Helvetica", "", 7)
                        pdf.set_text_color(40, 40, 40)
                        for k, v in corp.items():
                            if v:
                                label = k.replace("_", " ").title()
                                pdf.set_x(10)
                                pdf.multi_cell(190, 3.5, safe(f"  {label}: {v}"))
                        pdf.ln(2)

                    # Key people (directors, UBOs, related entities)
                    people = cir_f.get("key_people", [])
                    if people:
                        if pdf.get_y() > 240:
                            pdf.add_page()
                        pdf.set_font("Helvetica", "B", 8)
                        pdf.set_text_color(25, 60, 120)
                        pdf.cell(0, 5, safe(f"Key Individuals / Beneficial Owners ({len(people)})"),
                                 new_x="LMARGIN", new_y="NEXT")

                        # Table
                        pw = [80, 110]
                        pdf.set_font("Helvetica", "B", 6.5)
                        pdf.set_fill_color(25, 60, 120)
                        pdf.set_text_color(255, 255, 255)
                        pdf.cell(pw[0], 5, safe("Name"), fill=True, new_x="RIGHT", new_y="TOP")
                        pdf.cell(pw[1], 5, safe("Role / Relationship"), fill=True, new_x="LMARGIN", new_y="NEXT")

                        pdf.set_font("Helvetica", "", 6.5)
                        pdf.set_text_color(40, 40, 40)
                        for j, p in enumerate(people):
                            if pdf.get_y() > 270:
                                pdf.add_page()
                            bg = (245, 245, 250) if j % 2 == 0 else (255, 255, 255)
                            pdf.set_fill_color(*bg)
                            pname = (p.get("name", "") or "")[:45]
                            prole = (p.get("role", "") or "")[:60]
                            pdf.cell(pw[0], 4, safe(pname), fill=True, new_x="RIGHT", new_y="TOP")
                            pdf.cell(pw[1], 4, safe(prole), fill=True, new_x="LMARGIN", new_y="NEXT")
                        pdf.ln(2)

                    # Risk assessment
                    risk_text = cir_f.get("risk_assessment", "")
                    if risk_text:
                        if pdf.get_y() > 240:
                            pdf.add_page()
                        pdf.set_font("Helvetica", "B", 8)
                        pdf.set_text_color(25, 60, 120)
                        pdf.cell(0, 5, safe("Risk Assessment"), new_x="LMARGIN", new_y="NEXT")
                        pdf.set_font("Helvetica", "", 7)
                        pdf.set_text_color(40, 40, 40)
                        pdf.set_x(10)
                        pdf.multi_cell(190, 3.5, safe(risk_text[:600]))
                        pdf.ln(2)

                    # Sanctions from CIR
                    sanctions_cir = cir_f.get("sanctions", {})
                    if sanctions_cir and isinstance(sanctions_cir, dict):
                        status_val = sanctions_cir.get("status", sanctions_cir.get("result", ""))
                        if status_val:
                            pdf.set_font("Helvetica", "B", 8)
                            pdf.set_text_color(25, 60, 120)
                            pdf.cell(0, 5, safe(f"CIR Sanctions Screening: {status_val}"),
                                     new_x="LMARGIN", new_y="NEXT")
                        pdf.ln(1)

                    # Litigation
                    lit = cir_f.get("litigation", {})
                    if lit and isinstance(lit, dict):
                        lit_text = lit.get("summary", lit.get("details", ""))
                        if lit_text:
                            pdf.set_font("Helvetica", "B", 8)
                            pdf.set_text_color(25, 60, 120)
                            pdf.cell(0, 5, safe("Litigation"), new_x="LMARGIN", new_y="NEXT")
                            pdf.set_font("Helvetica", "", 7)
                            pdf.set_text_color(40, 40, 40)
                            pdf.set_x(10)
                            pdf.multi_cell(190, 3.5, safe(str(lit_text)[:400]))
                            pdf.ln(2)

            elif dtype == "darkweb_removed":
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(30, 130, 30)
                pdf.cell(0, 5, safe(f"{detail['count']} finding(s) no longer appearing"),
                         new_x="LMARGIN", new_y="NEXT")

        # Baseline mode: show sanctions + media status (dark web already rendered via delta)
        if is_baseline:
            scr = entity_scan.get("screening", {})
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(60, 60, 60)
            status = scr.get("overall_status", "?")
            if status == "CLEAR":
                pdf.set_text_color(30, 130, 30)
                pdf.cell(0, 6, safe("Sanctions: CLEAR (CSL/OFAC, OpenSanctions, Interpol)"),
                         new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.set_text_color(180, 30, 30)
                pdf.cell(0, 6, safe(f"Sanctions: {status} -- IMMEDIATE REVIEW REQUIRED"),
                         new_x="LMARGIN", new_y="NEXT")

            media = entity_scan.get("media", {})
            articles = media.get("articles", [])
            pdf.set_font("Helvetica", "B", 9)
            if not articles:
                pdf.set_text_color(30, 130, 30)
                pdf.cell(0, 6, safe("Adverse media: CLEAN (no negative coverage in last 7 days)"),
                         new_x="LMARGIN", new_y="NEXT")
            else:
                pdf.set_text_color(200, 140, 0)
                pdf.cell(0, 6, safe(f"Adverse media: {len(articles)} article(s)"),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 7)
                pdf.set_text_color(40, 40, 40)
                for a in articles[:10]:
                    pdf.set_x(10); pdf.multi_cell(190, 3.5, safe(f"  {(a.get('title') or '')[:80]}  ({a.get('source', '')})"))
                    if a.get("url"):
                        pdf.set_text_color(80, 80, 180)
                        pdf.set_x(10); pdf.multi_cell(190, 3.5, safe(f"  {a['url'][:90]}"))
                        pdf.set_text_color(40, 40, 40)

        pdf.ln(4)

    # ── Methodology note ──
    if pdf.get_y() > 250:
        pdf.add_page()
    pdf.ln(5)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(130, 130, 130)
    pdf.multi_cell(0, 3.5, safe(
        "Sources: Dark web (37 sources via Tor incl. Dehashed, HudsonRock, OCCRP, ICIJ, OpenSanctions, Interpol, "
        "World Bank, URLScan), Sanctions (CSL/OFAC, OpenSanctions, Interpol), Adverse media (GDELT), "
        "CIR (OpenClaw counterparty intelligence — corporate registry, directors/UBOs, litigation, risk assessment). "
        "Scanned by Crawl OSINT Intelligence Platform. Confidential -- for COPAP shareholders only."
    ))

    pdf.output(pdf_path)
    log.info(f"PDF generated: {pdf_path} ({os.path.getsize(pdf_path):,} bytes, {pdf.page_no()} pages)")
    return pdf_path


# ─────────────────────────────────────────────────────────────────────
# Blob Upload
# ─────────────────────────────────────────────────────────────────────

def upload_to_blob(pdf_path: str) -> str:
    """Upload PDF to blob storage, return download URL."""
    sas_token = ""
    sas_file = BASE_DIR / "config" / "blob_sas_token"
    if sas_file.exists():
        sas_token = sas_file.read_text().strip()
    if not sas_token:
        sas_token = _get_secret("blob-sas-token")

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    blob_path = f"reports/copap-weekly/COPAP_Weekly_{date_str}.pdf"

    upload_url = f"https://{BLOB_ACCOUNT}.blob.core.windows.net/{BLOB_CONTAINER}/{blob_path}?{sas_token}"

    with open(pdf_path, "rb") as f:
        resp = requests.put(
            upload_url,
            data=f.read(),
            headers={
                "x-ms-blob-type": "BlockBlob",
                "Content-Type": "application/pdf",
            },
            timeout=30,
        )

    if resp.status_code in (200, 201):
        download_url = f"https://{BLOB_ACCOUNT}.blob.core.windows.net/{BLOB_CONTAINER}/{blob_path}?{sas_token}"
        log.info(f"Uploaded to blob: {blob_path}")
        return download_url
    else:
        log.error(f"Blob upload failed: HTTP {resp.status_code} {resp.text[:200]}")
        return ""


# ─────────────────────────────────────────────────────────────────────
# Teams Notification
# ─────────────────────────────────────────────────────────────────────

def send_teams_card(delta: dict, pdf_url: str, is_baseline: bool):
    """Send Teams adaptive card with weekly scan summary."""
    webhook_url = _get_secret("teams-webhook-url")
    if not webhook_url:
        log.warning("No Teams webhook URL configured")
        return

    date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    total_changes = sum(1 for e in delta.values() if e.get("has_changes"))
    entities_scanned = len(delta)

    if is_baseline:
        title = f"COPAP Weekly Scan -- BASELINE ({date_str})"
        color = "accent"
        summary_text = f"Baseline scan of {entities_scanned} entities. All findings recorded. Future reports show changes only."
    elif total_changes == 0:
        title = f"COPAP Weekly Scan -- NO CHANGES ({date_str})"
        color = "good"
        summary_text = f"All {entities_scanned} entities scanned. No new findings."
    else:
        title = f"COPAP Weekly Scan -- {total_changes} CHANGE(S) ({date_str})"
        color = "attention"
        changed = [n for n, e in delta.items() if e.get("has_changes")]
        summary_text = f"Changes detected in: {', '.join(changed)}"

    # Build entity rows
    entity_rows = []
    for name, edata in delta.items():
        totals = edata.get("current_totals", {})
        status = "CHANGED" if edata.get("has_changes") else "OK"
        entity_rows.append({
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [
                    {"type": "TextBlock", "text": name, "size": "small", "weight": "bolder" if edata.get("has_changes") else "default",
                     "color": "attention" if edata.get("has_changes") else "default"}
                ]},
                {"type": "Column", "width": "auto", "items": [
                    {"type": "TextBlock", "text": f"DW:{totals.get('darkweb_findings', 0)} | Sanctions:{totals.get('screening_status', '?')} | Media:{totals.get('media_articles', 0)}",
                     "size": "small"}
                ]},
            ]
        })

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "bolder", "size": "medium", "color": color},
                    {"type": "TextBlock", "text": summary_text, "wrap": True, "size": "small"},
                    {"type": "Container", "items": entity_rows},
                ],
                "actions": ([{
                    "type": "Action.OpenUrl",
                    "title": "Download Report (PDF)",
                    "url": pdf_url,
                }] if pdf_url else []),
            }
        }]
    }

    try:
        resp = requests.post(webhook_url, json=card, timeout=10)
        if resp.status_code in (200, 202):
            log.info("Teams notification sent")
        else:
            log.error(f"Teams notification failed: HTTP {resp.status_code}")
    except Exception as e:
        log.error(f"Teams notification error: {e}")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weekly COPAP scan")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, no Teams/blob")
    parser.add_argument("--report-only", action="store_true", help="Regenerate report from last scan")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Weekly COPAP scan starting")

    config = _load_config()
    entities = config["entities"]
    baseline = _load_baseline()
    is_baseline = len(baseline) == 0

    if args.report_only:
        if not baseline:
            log.error("No baseline data to generate report from")
            sys.exit(1)
        delta = compute_delta(baseline, {})  # All as new
        pdf_path = generate_pdf(baseline, delta, is_baseline=True)
        print(f"Report: {pdf_path}")
        return

    # ── Run scans ──
    scan_data = {e["name"]: {} for e in entities}

    # Dark web: batch all entities (disable block once, scan all, restore once)
    dw_entities = [e for e in entities if "darkweb" in e.get("scan", ["darkweb", "screening", "media"])]
    if dw_entities:
        dw_results = scan_darkweb_batch(dw_entities)
        for name, result in dw_results.items():
            scan_data[name]["darkweb"] = result

    # Screening + media: per entity (direct API calls, no SSH needed)
    for entity in entities:
        name = entity["name"]
        scan_types = entity.get("scan", ["darkweb", "screening", "media"])

        if "screening" in scan_types:
            scan_data[name]["screening"] = scan_screening(entity)

        if "media" in scan_types:
            scan_data[name]["media"] = scan_media(entity)

    # CIR (OpenClaw): batch all entities (disable block, submit all, poll, restore)
    cir_entities = [e for e in entities if "cir" in e.get("scan", [])]
    if cir_entities:
        cir_results = scan_cir_batch(cir_entities)
        for name, result in cir_results.items():
            scan_data[name]["cir"] = result

    # ── Compute delta ──
    delta = compute_delta(scan_data, baseline)
    total_changes = sum(1 for e in delta.values() if e.get("has_changes"))
    log.info(f"Scan complete: {len(entities)} entities, {total_changes} with changes")

    # ── Save baseline ──
    _save_baseline(scan_data)

    # ── Generate PDF ──
    pdf_path = generate_pdf(scan_data, delta, is_baseline)

    if args.dry_run:
        log.info(f"Dry run — report at {pdf_path}")
        return

    # ── Upload to blob ──
    pdf_url = upload_to_blob(pdf_path)

    # ── Send Teams notification ──
    send_teams_card(delta, pdf_url, is_baseline)

    log.info("Weekly COPAP scan complete")


if __name__ == "__main__":
    main()
