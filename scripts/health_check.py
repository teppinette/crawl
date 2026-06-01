#!/usr/bin/env python3
"""
Crawl Platform Health Monitor

Runs every 15 minutes via cron. Checks:
  1. SSH connectivity to all 5 regional VMs
  2. copap-cir-api.service (user unit) on crawldevvm (port 8400)
  3. Blob storage SAS token expiry

OpenClaw + SwarmClaw checks removed 2026-06-01 — platform pivoting off
OpenClaw to centralized Foundry/Mistral. Pulse tests retained.

Writes results to PostgreSQL (PipelineEvents table).
Sends Teams alert on any failure.
Sends daily summary at 08:00 UTC.

Usage:
    python3 health_check.py              # Normal check (every 15 min)
    python3 health_check.py --summary    # Force daily summary
    python3 health_check.py --test       # Test Teams webhook
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from keyvault import get_secret, load_db_config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = Path(os.path.expanduser("~/crawl/config"))

# Teams webhook — from Key Vault
TEAMS_WEBHOOK = get_secret("teams-webhook-url")

# SSH key
SSH_KEY = os.path.expanduser("~/.ssh/crawldevvm_key.pem")

# VMs to check
VMS = {
    "americas": {"ip": "172.206.2.41", "user": "copapadmin"},
    "europe":   {"ip": "172.189.56.218", "user": "copapadmin"},
    "gulf":     {"ip": "20.233.46.58", "user": "copadmin"},
    "china":    {"ip": "184.0.0.4", "user": "copapadmin"},
    "india":    {"ip": "20.193.150.43", "user": "copapadmin"},
}

# SAS token expiry (hardcoded — update when rotated)
SAS_EXPIRY = "2027-04-13"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db_conn():
    """Get a PostgreSQL connection."""
    cfg = load_db_config()
    return psycopg2.connect(
        host=cfg["host"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        port=int(cfg["port"]),
        sslmode=cfg["sslmode"],
        connect_timeout=10,
    )


def ensure_table():
    """Create PipelineEvents table if it doesn't exist."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_events (
            id SERIAL PRIMARY KEY,
            event_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type VARCHAR(50) NOT NULL,
            component VARCHAR(100) NOT NULL,
            status VARCHAR(20) NOT NULL,
            details JSONB,
            region VARCHAR(50),
            resolved BOOLEAN DEFAULT FALSE,
            resolved_at TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS idx_pe_time ON pipeline_events(event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_pe_component ON pipeline_events(component, event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_pe_status ON pipeline_events(status) WHERE status = 'FAIL';
    """)
    conn.commit()
    cur.close()
    conn.close()


def write_event(event_type: str, component: str, status: str,
                details: dict = None, region: str = None):
    """Write a single event to PipelineEvents."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pipeline_events (event_type, component, status, details, region)
        VALUES (%s, %s, %s, %s, %s)
    """, (event_type, component, status,
          json.dumps(details) if details else None, region))
    conn.commit()
    cur.close()
    conn.close()


def get_recent_failures(hours: int = 24) -> list:
    """Get unresolved failures from the last N hours."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT event_time, component, status, details, region
        FROM pipeline_events
        WHERE status = 'FAIL'
          AND event_time > NOW() - INTERVAL '%s hours'
          AND NOT resolved
        ORDER BY event_time DESC
    """, (hours,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_ssh(region: str, vm: dict) -> dict:
    """Check SSH connectivity to a regional VM."""
    try:
        result = subprocess.run(
            ["ssh", "-i", SSH_KEY,
             "-o", "UserKnownHostsFile=~/.ssh/crawl_known_hosts",
             "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes",
             f"{vm['user']}@{vm['ip']}", "echo OK"],
            capture_output=True, text=True, timeout=15
        )
        ok = result.stdout.strip() == "OK"
        return {"check": "ssh", "ok": ok,
                "error": result.stderr.strip()[:200] if not ok else None}
    except (subprocess.TimeoutExpired, Exception) as e:
        return {"check": "ssh", "ok": False, "error": str(e)[:200]}


def check_gateway() -> dict:
    """Check copap-cir-api.service (user unit) on crawldevvm.
    Checks BOTH the HTTP health endpoint AND the systemd service status,
    so a stale process holding the port while the service crash-loops is caught.
    """
    try:
        # Check HTTP health
        result = subprocess.run(
            ["curl", "-s", "-m", "5",
             "http://localhost:8400/api/v1/health"],
            capture_output=True, text=True, timeout=10
        )
        try:
            health = json.loads(result.stdout.strip())
            http_ok = health.get("status") == "ok"
        except json.JSONDecodeError:
            http_ok = False

        # Check systemd service is actually running (not crash-looping).
        # Canonical unit is the USER-level copap-cir-api.service; the legacy
        # system unit crawl-gateway.service was disabled 2026-05-20.
        # Under cron XDG_RUNTIME_DIR is unset, so `systemctl --user` cannot
        # find the user dbus socket — inject it explicitly. Linger is enabled
        # so /run/user/<uid> exists at boot regardless of login sessions.
        svc_env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        svc_result = subprocess.run(
            ["systemctl", "--user", "is-active", "copap-cir-api"],
            capture_output=True, text=True, timeout=5, env=svc_env
        )
        svc_ok = svc_result.stdout.strip() == "active"

        ok = http_ok and svc_ok
        error = None
        if not ok:
            if not svc_ok:
                error = f"systemd service is {svc_result.stdout.strip()} (may be crash-looping with stale process on port)"
            elif not http_ok:
                error = f"HTTP health check failed: {result.stdout[:100]}"

        return {"check": "gateway", "ok": ok, "version": health.get("version") if http_ok else None,
                "http_ok": http_ok, "service_ok": svc_ok, "error": error}
    except Exception as e:
        return {"check": "gateway", "ok": False, "error": str(e)[:200]}


def check_sas_token() -> dict:
    """Check if SAS token is expiring soon."""
    try:
        expiry = datetime.strptime(SAS_EXPIRY, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_left = (expiry - datetime.now(timezone.utc)).days
        ok = days_left > 30
        return {"check": "sas_token", "ok": ok,
                "days_left": days_left, "expires": SAS_EXPIRY,
                "warning": days_left <= 30}
    except Exception as e:
        return {"check": "sas_token", "ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Teams notifications
# ---------------------------------------------------------------------------

def send_teams_alert(failures: list[dict]):
    """Send a Teams alert card for failures."""
    if not TEAMS_WEBHOOK:
        print("No Teams webhook configured, skipping alert")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fail_lines = []
    for f in failures:
        err = f.get("error", "unknown")
        fail_lines.append(f"- **{f['region']}/{f['check']}**: {err}")

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "size": "Large",
                        "weight": "Bolder",
                        "text": "🔴 Crawl Platform Alert",
                        "color": "Attention"
                    },
                    {
                        "type": "TextBlock",
                        "text": f"**{len(failures)} check(s) failed** at {now}",
                        "wrap": True
                    },
                    {
                        "type": "TextBlock",
                        "text": "\n".join(fail_lines),
                        "wrap": True
                    },
                    {
                        "type": "TextBlock",
                        "text": "Server: crawldevvm (20.94.45.219)",
                        "isSubtle": True,
                        "size": "Small"
                    }
                ]
            }
        }]
    }

    try:
        r = requests.post(TEAMS_WEBHOOK, json=payload, timeout=10)
        print(f"Teams alert sent: {r.status_code}")
    except Exception as e:
        print(f"Teams alert failed: {e}")


def send_teams_summary(results: dict):
    """Send daily summary to Teams."""
    if not TEAMS_WEBHOOK:
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    all_ok = all(
        r["ok"] for checks in results.values()
        for r in (checks if isinstance(checks, list) else [checks])
    )

    status_emoji = "🟢" if all_ok else "🟡"
    status_text = "All Systems Operational" if all_ok else "Some Issues Detected"

    lines = []
    # Gateway
    gw = results.get("gateway", {})
    gw_icon = "✅" if gw.get("ok") else "❌"
    gw_note = f" (v{gw.get('version', '?')})" if gw.get("ok") else f" ({gw.get('error', 'down')[:60]})"
    lines.append(f"- **Gateway**: {gw_icon}{gw_note}")

    # SAS Token
    sas = results.get("sas_token", {})
    sas_icon = "✅" if sas.get("ok") else "⚠️"
    lines.append(f"- **SAS Token**: {sas_icon} ({sas.get('days_left', '?')} days left)")

    # Regional VMs
    for region in ["americas", "europe", "gulf", "china", "india"]:
        checks = results.get(region, [])
        ssh_ok = any(c.get("check") == "ssh" and c.get("ok") for c in checks)
        lines.append(f"- **{region}**: SSH {'✅' if ssh_ok else '❌'}")

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "size": "Large",
                        "weight": "Bolder",
                        "text": f"{status_emoji} Crawl Daily Health Report"
                    },
                    {
                        "type": "TextBlock",
                        "text": f"**{status_text}** — {now}",
                        "wrap": True
                    },
                    {
                        "type": "TextBlock",
                        "text": "\n".join(lines),
                        "wrap": True
                    }
                ]
            }
        }]
    }

    try:
        r = requests.post(TEAMS_WEBHOOK, json=payload, timeout=10)
        print(f"Teams summary sent: {r.status_code}")
    except Exception as e:
        print(f"Teams summary failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_checks() -> dict:
    """Run all health checks. Returns {component: result(s)}."""
    results = {}
    failures = []

    # Gateway (HTTP + systemd)
    gw = check_gateway()
    results["gateway"] = gw
    if not gw["ok"]:
        failures.append({"region": "crawldevvm", **gw})

    # SAS token
    sas = check_sas_token()
    results["sas_token"] = sas
    if not sas["ok"]:
        failures.append({"region": "crawldevvm", **sas})

    # Regional VMs
    for region, vm in VMS.items():
        checks = []

        ssh_result = check_ssh(region, vm)
        ssh_result["region"] = region
        checks.append(ssh_result)
        if not ssh_result["ok"]:
            failures.append({"region": region, **ssh_result})

        results[region] = checks

    return results, failures


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    # Test webhook
    if mode == "--test":
        send_teams_alert([{"region": "test", "check": "test", "error": "This is a test alert"}])
        print("Test alert sent.")
        return

    # Ensure DB table exists
    try:
        ensure_table()
    except Exception as e:
        print(f"DB connection failed: {e}")
        # Still run checks and alert via Teams even if DB is down
        pass

    # Run checks
    results, failures = run_checks()
    now = datetime.now(timezone.utc)

    # Write to DB
    for component, data in results.items():
        if isinstance(data, list):
            for check in data:
                try:
                    write_event(
                        event_type="health_check",
                        component=f"{component}/{check['check']}",
                        status="OK" if check["ok"] else "FAIL",
                        details=check,
                        region=component,
                    )
                except Exception as e:
                    print(f"DB write failed for {component}/{check['check']}: {e}")
        else:
            try:
                write_event(
                    event_type="health_check",
                    component=component,
                    status="OK" if data["ok"] else "FAIL",
                    details=data,
                )
            except Exception as e:
                print(f"DB write failed for {component}: {e}")

    # Print results
    print(f"\n{'='*50}")
    print(f"Health Check — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}")

    gw = results["gateway"]
    gw_detail = ""
    if gw.get("ok"):
        gw_detail = ""
    else:
        gw_detail = f" [{gw.get('error', '')}]"
    print(f"Gateway:    {'OK' if gw['ok'] else 'FAIL'}{gw_detail}")
    sas = results["sas_token"]
    print(f"SAS Token:  {'OK' if sas['ok'] else 'WARN'} ({sas.get('days_left', '?')} days)")

    for region in VMS:
        checks = results.get(region, [])
        ssh_ok = any(c["check"] == "ssh" and c["ok"] for c in checks)
        print(f"{region:12s} SSH={'OK' if ssh_ok else 'FAIL'}")

    print(f"\nFailures: {len(failures)}")

    # Alert on failures
    if failures:
        send_teams_alert(failures)

    # Daily summary at 08:00 UTC (or forced)
    if mode == "--summary" or now.hour == 8 and now.minute < 15:
        send_teams_summary(results)

    # Exit code
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
