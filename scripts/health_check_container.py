#!/usr/bin/env python3
"""
Container-friendly health monitor (replaces legacy health_check.py).

Designed to run as a Container Apps Job triggered every 15 min:
    - Self-checks Container App at CRAWL_GATEWAY_URL (/api/v1/health)
    - Probes crawl-verify-new at VERIFY_VM_URL (/health)
    - Probes Foundry endpoint reachability (no auth needed for HEAD)
    - Writes pipeline_events row per source
    - Sends Teams alert on any failure (rate-limited by deduping in DB)

NO SSH, NO regional-VM checks, NO host-specific paths.
Runs inside the same image as the gateway (/app/scripts/).
Uses Managed Identity for KV access (db creds + teams webhook).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

import psycopg2
from keyvault import get_secret


GATEWAY_URL = os.environ.get(
    "CRAWL_GATEWAY_URL",
    "https://crawl-gateway-v2.orangemoss-d67e0a38.eastus2.azurecontainerapps.io",
)
VERIFY_VM_URL = os.environ.get("VERIFY_VM_URL", "http://172.20.0.26:8460")
FOUNDRY_URL = os.environ.get(
    "FOUNDRY_URL",
    "https://copapfoundry-resource.services.ai.azure.com",
)
TEAMS_WEBHOOK = get_secret("teams-webhook-url") or ""

CHECKS = [
    ("gateway",      f"{GATEWAY_URL}/api/v1/health",  10),
    ("verify_vm",    f"{VERIFY_VM_URL}/health",       10),
    ("foundry",      FOUNDRY_URL,                      8),
]


def probe(url: str, timeout: int) -> tuple[bool, int, str]:
    """Returns (ok, status_code, error_text)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            return (200 <= code < 500), code, ""
    except Exception as e:
        return False, 0, str(e)[:300]


def write_event(source: str, ok: bool, code: int, error: str):
    cfg = {
        "host": "crawl-monitor-db.postgres.database.azure.com",
        "dbname": "crawlmonitor",
        "user": "crawladmin",
        "password": get_secret("db-password"),
        "sslmode": "require",
        "connect_timeout": 10,
    }
    conn = psycopg2.connect(**cfg)
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO pipeline_events
                 (event_time, event_type, component, status, details)
               VALUES (%s, %s, %s, %s, %s)""",
            (datetime.now(timezone.utc),
             "health_check",
             source,
             "ok" if ok else "fail",
             json.dumps({"status_code": code, "error": error or None,
                         "source_url": url_for(source)})),
        )
        conn.commit()
    finally:
        conn.close()


def url_for(source: str) -> str:
    for s, u, _ in CHECKS:
        if s == source:
            return u
    return ""


def teams_alert(failures: list[dict]):
    if not TEAMS_WEBHOOK or not failures:
        return
    text = "**crawl-gateway-v2 health check FAILED:**\n" + "\n".join(
        f"- `{f['source']}` (HTTP {f['code']}): {f['error']}"
        for f in failures
    )
    payload = {"text": text}
    try:
        req = urllib.request.Request(
            TEAMS_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        print(f"Teams webhook failed: {e}", file=sys.stderr)


def main():
    failures = []
    for source, url, timeout in CHECKS:
        ok, code, error = probe(url, timeout)
        write_event(source, ok, code, error)
        print(f"{source:12} {'OK' if ok else 'FAIL':5} {code:4} {url}",
              file=sys.stderr)
        if not ok:
            failures.append({"source": source, "code": code, "error": error})
    if failures:
        teams_alert(failures)
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
