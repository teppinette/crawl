#!/usr/bin/env python3
"""
Crawl Platform — Daily API Usage & Spend Report

Runs daily via cron (07:55 UTC). Collects usage and spend data from all
API providers, writes to PostgreSQL (api_usage_daily), sends a single
consolidated Teams report.

Data sources:
  - Anthropic Admin API   — ACTUAL billed cost & token usage (primary source)
  - OpenClaw VM sessions  — operational status (which regions active, heartbeat vs research)
  - DeepSeek              — /user/balance
  - Moonshot              — /v1/users/me/balance
  - Tavily                — /usage (queries used/limit)
  - Firecrawl             — /v2/team/credit-usage (credits used/limit)
  - Perplexity            — key validation only (no usage API)
  - Exa                   — key validation only (no usage API)
  - Sarvam                — key validation only (no usage API)

Usage:
    python3 usage_monitor.py              # Normal daily check
    python3 usage_monitor.py --test       # Test Teams webhook
    python3 usage_monitor.py --backfill N # Backfill last N days
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from keyvault import get_secret, load_db_config

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.path.expanduser("~/crawl/config"))

TEAMS_WEBHOOK = get_secret("teams-webhook-url")

SSH_KEY = os.path.expanduser("~/.ssh/crawldevvm_key.pem")

VMS = {
    "americas": {"ip": "172.206.2.41", "user": "copapadmin"},
    "europe":   {"ip": "172.189.56.218", "user": "copapadmin"},
    "gulf":     {"ip": "20.233.46.58", "user": "copadmin"},
    "china":    {"ip": "10.0.0.4", "user": "copapadmin"},
    "india":    {"ip": "20.193.150.43", "user": "copapadmin"},
}

# API keys — loaded from Azure Key Vault (managed identity)
ANTHROPIC_ADMIN_KEY = get_secret("anthropic-admin-key")
ANTHROPIC_API_KEY = get_secret("anthropic-api-key")
DEEPSEEK_API_KEY = get_secret("deepseek-api-key")
TAVILY_API_KEY = get_secret("tavily-api-key")
PERPLEXITY_API_KEY = get_secret("perplexity-api-key")
EXA_API_KEY = get_secret("exa-api-key")
FIRECRAWL_API_KEY = get_secret("firecrawl-api-key")
MOONSHOT_API_KEY = get_secret("moonshot-api-key")
SARVAM_API_KEY = get_secret("sarvam-api-key")

# Thresholds
DAILY_SPEND_ALERT_USD = 25.0

# Pricing per million tokens (USD)
ANTHROPIC_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5":  {"input": 0.80, "output": 4.00, "cache_read": 0.08, "cache_write": 1.00},
}
ANTHROPIC_DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db_conn():
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
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_daily (
            id              SERIAL PRIMARY KEY,
            report_date     DATE NOT NULL,
            provider        VARCHAR(50) NOT NULL,
            model           VARCHAR(100),
            input_tokens    BIGINT DEFAULT 0,
            output_tokens   BIGINT DEFAULT 0,
            cache_read_tokens   BIGINT DEFAULT 0,
            cache_write_tokens  BIGINT DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            estimated_cost_usd  NUMERIC(10, 4) DEFAULT 0,
            raw_response    JSONB,
            anomalies       JSONB,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(report_date, provider, model)
        );
        CREATE INDEX IF NOT EXISTS idx_aud_date ON api_usage_daily(report_date DESC);
        CREATE INDEX IF NOT EXISTS idx_aud_provider ON api_usage_daily(provider, report_date DESC);
    """)
    conn.commit()
    cur.close()
    conn.close()


def upsert_usage(row: dict):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO api_usage_daily
            (report_date, provider, model, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, total_requests,
             estimated_cost_usd, raw_response, anomalies)
        VALUES
            (%(report_date)s, %(provider)s, %(model)s, %(input_tokens)s,
             %(output_tokens)s, %(cache_read_tokens)s, %(cache_write_tokens)s,
             %(total_requests)s, %(estimated_cost_usd)s,
             %(raw_response)s, %(anomalies)s)
        ON CONFLICT (report_date, provider, model) DO UPDATE SET
            input_tokens       = EXCLUDED.input_tokens,
            output_tokens      = EXCLUDED.output_tokens,
            cache_read_tokens  = EXCLUDED.cache_read_tokens,
            cache_write_tokens = EXCLUDED.cache_write_tokens,
            total_requests     = EXCLUDED.total_requests,
            estimated_cost_usd = EXCLUDED.estimated_cost_usd,
            raw_response       = EXCLUDED.raw_response,
            anomalies          = EXCLUDED.anomalies,
            created_at         = NOW()
    """, row)
    conn.commit()
    cur.close()
    conn.close()


def _db_row(report_date, provider, model=None, **kwargs):
    """Helper to build and upsert a DB row."""
    row = {
        "report_date": report_date if isinstance(report_date, str) else report_date.isoformat(),
        "provider": provider,
        "model": model,
        "input_tokens": kwargs.get("input_tokens", 0),
        "output_tokens": kwargs.get("output_tokens", 0),
        "cache_read_tokens": kwargs.get("cache_read_tokens", 0),
        "cache_write_tokens": kwargs.get("cache_write_tokens", 0),
        "total_requests": kwargs.get("total_requests", 0),
        "estimated_cost_usd": kwargs.get("estimated_cost_usd", 0),
        "raw_response": kwargs.get("raw_response"),
        "anomalies": kwargs.get("anomalies"),
    }
    try:
        upsert_usage(row)
    except Exception as e:
        print(f"  DB write failed for {provider}/{model}: {e}")


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _ssh_cmd(vm, cmd, timeout=20):
    """Run a command on a regional VM via SSH."""
    try:
        r = subprocess.run(
            ["ssh", "-i", SSH_KEY,
             "-o", "UserKnownHostsFile=~/.ssh/crawl_known_hosts",
             "-o", "ConnectTimeout=10",
             "-o", "BatchMode=yes",
             f"{vm['user']}@{vm['ip']}", cmd],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception as e:
        return ""


def collect_vm_sessions() -> dict:
    """Scan all VMs for OpenClaw session data. Returns per-region dict.
    Only includes sessions updated in the current calendar month."""
    # Calculate start of current month as epoch millis for filtering
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    month_start_ms = int(month_start.timestamp() * 1000)

    scan_script = (
        'import json, os, sys\n'
        f'cutoff = {month_start_ms}\n'
        'home = os.path.expanduser("~")\n'
        'sf = os.path.join(home, ".openclaw/agents/main/sessions/sessions.json")\n'
        'if not os.path.exists(sf): exit(0)\n'
        'd = json.load(open(sf))\n'
        'for k, v in d.items():\n'
        '    if not isinstance(v, dict): continue\n'
        '    if v.get("updatedAt", 0) < cutoff: continue\n'
        '    o = v.get("origin", {})\n'
        '    op = o.get("provider", "") if isinstance(o, dict) else ""\n'
        '    print(json.dumps({"key": k, "origin": op,\n'
        '        "model": v.get("modelProvider", "unknown"),\n'
        '        "status": v.get("status", ""),\n'
        '        "inputTokens": v.get("inputTokens", 0),\n'
        '        "outputTokens": v.get("outputTokens", 0),\n'
        '        "totalTokens": v.get("totalTokens", 0),\n'
        '        "cacheRead": v.get("cacheRead", 0),\n'
        '        "cacheWrite": v.get("cacheWrite", 0),\n'
        '        "estimatedCost": v.get("estimatedCostUsd", 0)}))\n'
    )

    regions = {}
    for region, vm in VMS.items():
        output = _ssh_cmd(vm, "python3 << 'PYSCAN'\n" + scan_script + "PYSCAN")
        sessions = []
        for line in (output or "").splitlines():
            try:
                sessions.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        regions[region] = sessions
    return regions


def collect_tool_calls() -> dict:
    """Count tool calls from session JSONL files on each VM."""
    count_script = """
import json, os, glob
home = os.path.expanduser("~")
counts = {}
for f in glob.glob(os.path.join(home, ".openclaw/agents/main/sessions/*.jsonl")):
    with open(f) as fh:
        for line in fh:
            try:
                d = json.loads(line.strip())
                msg = d.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "toolCall":
                            name = c.get("name", "")
                            if name:
                                counts[name] = counts.get(name, 0) + 1
            except:
                pass
print(json.dumps(counts))
"""
    results = {}
    for region, vm in VMS.items():
        output = _ssh_cmd(vm, "python3 << 'PYTOOL'\n" + count_script + "PYTOOL", timeout=30)
        try:
            results[region] = json.loads(output) if output else {}
        except json.JSONDecodeError:
            results[region] = {}
    return results


def collect_anthropic_cost(report_date) -> dict:
    """Get ACTUAL billed cost from Anthropic Admin API (Cost + Usage reports).
    Returns dict with daily cost by model, total, and token breakdown."""
    if not ANTHROPIC_ADMIN_KEY:
        print("  No Anthropic admin key, skipping actual cost collection")
        return {}

    # Query for the specific report_date (one day window)
    start = f"{report_date}T00:00:00Z"
    end_date = report_date + timedelta(days=1) if isinstance(report_date, datetime) else \
        datetime.strptime(str(report_date), "%Y-%m-%d").date() + timedelta(days=1)
    end = f"{end_date}T00:00:00Z"

    result = {"models": {}, "total_usd": 0, "tokens": {}, "web_searches": 0}

    # --- Cost report (actual USD billed, amounts in cents) ---
    try:
        r = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            params={
                "starting_at": start,
                "ending_at": end,
                "group_by[]": "description",
                "bucket_width": "1d",
            },
            headers={
                "anthropic-version": "2023-06-01",
                "x-api-key": ANTHROPIC_ADMIN_KEY,
            },
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            for bucket in data.get("data", []):
                for item in bucket.get("results", []):
                    amt_usd = float(item["amount"]) / 100  # cents -> USD
                    model = item.get("model", "other")
                    result["models"][model] = result["models"].get(model, 0) + amt_usd
                    result["total_usd"] += amt_usd
        else:
            print(f"  Anthropic cost API: HTTP {r.status_code}")
    except Exception as e:
        print(f"  Anthropic cost API error: {e}")

    # --- Usage report (token counts + web searches) ---
    try:
        r = requests.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            params={
                "starting_at": start,
                "ending_at": end,
                "group_by[]": "model",
                "bucket_width": "1d",
            },
            headers={
                "anthropic-version": "2023-06-01",
                "x-api-key": ANTHROPIC_ADMIN_KEY,
            },
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            for bucket in data.get("data", []):
                for item in bucket.get("results", []):
                    model = item.get("model", "unknown")
                    result["tokens"][model] = {
                        "input": item.get("uncached_input_tokens", 0),
                        "output": item.get("output_tokens", 0),
                        "cache_read": item.get("cache_read_input_tokens", 0),
                        "cache_write": sum(
                            item.get("cache_creation", {}).get(k, 0)
                            for k in ["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"]
                        ),
                    }
                    result["web_searches"] += item.get("server_tool_use", {}).get("web_search_requests", 0)
        else:
            print(f"  Anthropic usage API: HTTP {r.status_code}")
    except Exception as e:
        print(f"  Anthropic usage API error: {e}")

    return result


def collect_anthropic_trailing(report_date, days=7) -> dict:
    """Get trailing N-day cost breakdown from Anthropic Admin API.
    Returns dict with daily_costs list, total, and per-model totals."""
    if not ANTHROPIC_ADMIN_KEY:
        return {}

    end_date = report_date + timedelta(days=1) if isinstance(report_date, datetime) else \
        datetime.strptime(str(report_date), "%Y-%m-%d").date() + timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    result = {"days": [], "total_usd": 0, "models": {}, "period": f"{start_date} to {report_date}"}

    try:
        r = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            params={
                "starting_at": f"{start_date}T00:00:00Z",
                "ending_at": f"{end_date}T00:00:00Z",
                "group_by[]": "description",
                "bucket_width": "1d",
            },
            headers={
                "anthropic-version": "2023-06-01",
                "x-api-key": ANTHROPIC_ADMIN_KEY,
            },
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            for bucket in data.get("data", []):
                day_date = bucket["starting_at"][:10]
                day_total = 0
                for item in bucket.get("results", []):
                    amt_usd = float(item["amount"]) / 100
                    model = item.get("model", "other")
                    day_total += amt_usd
                    result["models"][model] = result["models"].get(model, 0) + amt_usd
                result["days"].append({"date": day_date, "cost": day_total})
                result["total_usd"] += day_total
        else:
            print(f"  Anthropic trailing cost API: HTTP {r.status_code}")
    except Exception as e:
        print(f"  Anthropic trailing cost error: {e}")

    return result


def collect_api_status() -> dict:
    """Collect balance/usage/status from all API providers."""
    status = {}

    # DeepSeek — balance
    try:
        r = requests.get("https://api.deepseek.com/user/balance",
                         headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                         timeout=15)
        if r.status_code == 200:
            data = r.json()
            bals = data.get("balance_infos", [data])
            total = sum(float(b.get("total_balance", 0)) for b in bals)
            status["deepseek"] = {"ok": True, "balance": total, "raw": data}
        else:
            status["deepseek"] = {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        status["deepseek"] = {"ok": False, "error": str(e)[:80]}

    # Moonshot — balance
    try:
        r = requests.get("https://api.moonshot.ai/v1/users/me/balance",
                         headers={"Authorization": f"Bearer {MOONSHOT_API_KEY}"},
                         timeout=15)
        if r.status_code == 200:
            bal = r.json().get("data", {}).get("available_balance", 0)
            status["moonshot"] = {"ok": True, "balance": bal}
        else:
            status["moonshot"] = {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        status["moonshot"] = {"ok": False, "error": str(e)[:80]}

    # Tavily — usage
    try:
        r = requests.get("https://api.tavily.com/usage",
                         headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
                         timeout=15)
        if r.status_code == 200:
            data = r.json()
            acct = data.get("account", {})
            status["tavily"] = {
                "ok": True,
                "used": acct.get("plan_usage", 0),
                "limit": acct.get("plan_limit", 0),
                "plan": acct.get("current_plan", ""),
                "raw": data,
            }
        else:
            status["tavily"] = {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        status["tavily"] = {"ok": False, "error": str(e)[:80]}

    # Firecrawl — credits
    try:
        r = requests.get("https://api.firecrawl.dev/v2/team/credit-usage",
                         headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}"},
                         timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", {})
            status["firecrawl"] = {
                "ok": True,
                "remaining": data.get("remainingCredits", 0),
                "plan": data.get("planCredits", 0),
                "raw": data,
            }
        else:
            status["firecrawl"] = {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        status["firecrawl"] = {"ok": False, "error": str(e)[:80]}

    # Perplexity — key check (no usage API)
    try:
        r = requests.get("https://api.perplexity.ai/chat/completions",
                         headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
                         timeout=10)
        status["perplexity"] = {"ok": r.status_code in (200, 405), "status": "Active" if r.status_code in (200, 405) else f"HTTP {r.status_code}"}
    except Exception as e:
        status["perplexity"] = {"ok": False, "error": str(e)[:80]}

    # Exa — key check (no usage API)
    try:
        r = requests.get("https://api.exa.ai/search",
                         headers={"x-api-key": EXA_API_KEY},
                         timeout=10)
        status["exa"] = {"ok": r.status_code != 401, "status": "Active" if r.status_code != 401 else "Invalid/expired"}
    except Exception as e:
        status["exa"] = {"ok": False, "error": str(e)[:80]}

    # Sarvam — key check (no usage API)
    try:
        r = requests.get("https://api.sarvam.ai/translate",
                         headers={"API-Subscription-Key": SARVAM_API_KEY},
                         timeout=10)
        status["sarvam"] = {"ok": r.status_code != 401, "status": "Active" if r.status_code != 401 else "Invalid/expired"}
    except Exception as e:
        status["sarvam"] = {"ok": False, "error": str(e)[:80]}

    return status


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(report_date) -> dict:
    """Collect everything, write to DB, return report dict."""
    report = {"date": report_date.isoformat()}
    alerts = []

    # --- Anthropic Admin API (ACTUAL billed cost — primary source) ---
    print("Querying Anthropic Admin API for actual cost...")
    anthropic_actual = collect_anthropic_cost(report_date)
    report["anthropic_actual"] = anthropic_actual

    print("Querying Anthropic 7-day trailing cost...")
    trailing = collect_anthropic_trailing(report_date, days=7)
    report["trailing_7d"] = trailing

    actual_total = anthropic_actual.get("total_usd", 0)
    actual_models = anthropic_actual.get("models", {})
    actual_tokens = anthropic_actual.get("tokens", {})
    actual_searches = anthropic_actual.get("web_searches", 0)

    # Write actual cost to DB per model
    for model, cost_usd in actual_models.items():
        tok = actual_tokens.get(model, {})
        _db_row(report_date, "anthropic-actual", model,
                input_tokens=tok.get("input", 0),
                output_tokens=tok.get("output", 0),
                cache_read_tokens=tok.get("cache_read", 0),
                cache_write_tokens=tok.get("cache_write", 0),
                total_requests=0,
                estimated_cost_usd=round(cost_usd, 4),
                raw_response=json.dumps({"source": "admin_api", "web_searches": actual_searches}))

    report["total_llm"] = actual_total
    if actual_total > DAILY_SPEND_ALERT_USD:
        alerts.append(f"Anthropic spend: ${actual_total:.2f}")

    # --- VM Sessions (operational status only — NOT used for cost) ---
    print("Scanning VM sessions (operational status)...")
    vm_data = collect_vm_sessions()

    regions_summary = {}
    regions_online = 0
    for region, sessions in vm_data.items():
        if not sessions:
            regions_summary[region] = {"status": "offline", "model": "—", "sessions": 0}
            continue
        regions_online += 1
        model = "unknown"
        source = "idle"
        session_count = len(sessions)
        for sess in sessions:
            provider = sess.get("model", "unknown")
            if provider == "anthropic":
                model = "claude-sonnet-4-6"
            elif provider == "deepseek":
                model = "deepseek-chat"
            else:
                model = provider
            origin = sess.get("origin", "")
            if origin == "heartbeat":
                source = "heartbeat"
            else:
                source = "research"
        regions_summary[region] = {
            "status": "online", "model": model, "source": source,
            "sessions": session_count,
        }

    report["regions"] = regions_summary
    report["regions_online"] = regions_online

    if regions_online < len(VMS):
        offline = [r for r, rs in regions_summary.items() if rs.get("status") == "offline"]
        alerts.append(f"Regions offline: {', '.join(offline)}")

    # --- Tool calls from session logs ---
    print("Counting tool calls...")
    tool_calls = collect_tool_calls()
    report["tool_calls"] = tool_calls

    total_searches = sum(tc.get("web_search", 0) for tc in tool_calls.values())
    total_fetches = sum(tc.get("web_fetch", 0) for tc in tool_calls.values())
    report["total_searches"] = total_searches
    report["total_fetches"] = total_fetches

    # --- API Status ---
    print("Collecting API status...")
    apis = collect_api_status()
    report["apis"] = apis

    # DeepSeek
    ds = apis.get("deepseek", {})
    if ds.get("ok"):
        _db_row(report_date, "deepseek", "balance",
                raw_response=json.dumps(ds.get("raw", {})))
        if ds["balance"] < 5.0:
            alerts.append(f"DeepSeek balance: ${ds['balance']:.2f}")

    # Moonshot
    ms = apis.get("moonshot", {})
    if ms.get("ok"):
        _db_row(report_date, "moonshot", "balance",
                raw_response=json.dumps({"balance": ms["balance"]}))
        if ms["balance"] < 5.0:
            alerts.append(f"Moonshot balance: ${ms['balance']:.2f}")

    # Tavily
    tv = apis.get("tavily", {})
    if tv.get("ok"):
        _db_row(report_date, "tavily", "usage",
                total_requests=tv.get("used", 0),
                raw_response=json.dumps(tv.get("raw", {})))

    # Firecrawl
    fc = apis.get("firecrawl", {})
    if fc.get("ok"):
        _db_row(report_date, "firecrawl", "credits",
                total_requests=fc.get("plan", 0) - fc.get("remaining", 0),
                raw_response=json.dumps(fc.get("raw", {})))

    # Key failures
    for name in ["perplexity", "exa", "sarvam", "moonshot"]:
        a = apis.get(name, {})
        if not a.get("ok"):
            alerts.append(f"{name.capitalize()}: {a.get('error', a.get('status', 'down'))}")

    report["alerts"] = alerts
    return report


# ---------------------------------------------------------------------------
# Teams card
# ---------------------------------------------------------------------------

def send_teams_report(report: dict):
    if not TEAMS_WEBHOOK:
        print("No Teams webhook, skipping")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rd = report["date"]
    alerts = report.get("alerts", [])
    apis = report.get("apis", {})
    regions = report.get("regions", {})
    total_llm = report.get("total_llm", 0)

    tool_calls = report.get("tool_calls", {})

    # --- Section 1: 7-Day Spend (always shows real data) ---
    trailing = report.get("trailing_7d", {})
    trailing_total = trailing.get("total_usd", 0)
    trailing_days = trailing.get("days", [])
    trailing_models = trailing.get("models", {})

    spend_lines = []
    for day in trailing_days:
        d = day["date"][5:]  # MM-DD
        c = day["cost"]
        bar = "█" * max(1, int(c / 5)) if c > 0 else "·"
        spend_lines.append(f"- {d}: ${c:.2f} {bar}")

    model_lines = []
    for model in sorted(trailing_models, key=lambda m: -trailing_models[m]):
        amt = trailing_models[model]
        if amt < 0.01:
            continue
        short = (model or "other").replace("claude-", "").replace("-20251001", "").replace("-20250514", "")
        model_lines.append(f"- {short}: ${amt:.2f}")

    avg_daily = trailing_total / max(len(trailing_days), 1)
    projected_monthly = avg_daily * 30

    # --- Section 2: Today's cost breakdown ---
    anthropic_actual = report.get("anthropic_actual", {})
    actual_models = anthropic_actual.get("models", {})
    actual_searches = anthropic_actual.get("web_searches", 0)

    today_lines = []
    for model in sorted(actual_models, key=lambda m: -actual_models[m]):
        amt = actual_models[model]
        if amt < 0.01:
            continue
        short = (model or "other").replace("claude-", "").replace("-20251001", "").replace("-20250514", "")
        today_lines.append(f"- {short}: ${amt:.2f}")
    if actual_searches:
        today_lines.append(f"- Web searches: {actual_searches}")
    if not today_lines:
        today_lines.append("- No activity")

    # --- Section 3: Region Status ---
    region_lines = []
    regions_online = report.get("regions_online", 0)
    for r in ["americas", "europe", "gulf", "china", "india"]:
        rs = regions.get(r)
        if not rs or rs.get("status") == "offline":
            region_lines.append(f"- {r}: offline")
            continue
        model = rs.get("model", "—")
        source = rs.get("source", "idle")
        sessions = rs.get("sessions", 0)
        tc = tool_calls.get(r, {})
        searches = tc.get("web_search", 0)
        fetches = tc.get("web_fetch", 0)
        tools_str = f" | {searches}s, {fetches}f" if (searches + fetches) > 0 else ""
        region_lines.append(f"- {r} ({model}): {sessions} sessions ({source}){tools_str}")

    # --- Section 4: Balances & APIs ---
    balance_lines = []
    ds = apis.get("deepseek", {})
    if ds.get("ok"):
        balance_lines.append(f"- DeepSeek: ${ds['balance']:.2f}")
    else:
        balance_lines.append(f"- DeepSeek: {ds.get('error', 'unavailable')}")

    ms = apis.get("moonshot", {})
    if ms.get("ok"):
        balance_lines.append(f"- Moonshot: ${ms['balance']:.2f}")
    else:
        balance_lines.append(f"- Moonshot: {ms.get('error', 'unavailable')}")

    api_lines = []
    tv = apis.get("tavily", {})
    if tv.get("ok"):
        api_lines.append(f"- Tavily: {tv['used']}/{tv['limit']} queries ({tv['plan']})")
    else:
        api_lines.append(f"- Tavily: {tv.get('error', 'unavailable')}")

    fc = apis.get("firecrawl", {})
    if fc.get("ok"):
        used = fc["plan"] - fc["remaining"]
        api_lines.append(f"- Firecrawl: {used}/{fc['plan']} credits ({fc['remaining']} remaining)")
    else:
        api_lines.append(f"- Firecrawl: {fc.get('error', 'unavailable')}")

    total_searches = report.get("total_searches", 0)
    total_fetches = report.get("total_fetches", 0)

    # --- Build card ---
    status_icon = "🔴" if alerts else "🟢"

    summary = (
        f"**Yesterday: ${total_llm:.2f}** | "
        f"**7-day: ${trailing_total:.2f}** (avg ${avg_daily:.2f}/day) | "
        f"**Projected: ${projected_monthly:.0f}/mo**"
    )

    body_text = (
        f"**7-Day Spend (Anthropic billed):**\n"
        + "\n".join(spend_lines)
        + f"\n\n**By model (7d):**\n"
        + "\n".join(model_lines)
        + f"\n\n**Yesterday breakdown:**\n"
        + "\n".join(today_lines)
        + f"\n\n**Regions ({regions_online}/{len(VMS)} online):**\n"
        + "\n".join(region_lines)
        + f"\n\n**Balances:**\n"
        + "\n".join(balance_lines)
        + f"\n\n**APIs:**\n"
        + "\n".join(api_lines)
        + f"\n- Tool calls: {total_searches} searches, {total_fetches} fetches"
    )

    body_blocks = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder",
         "text": f"{status_icon} Crawl Daily Spend — {rd}"},
        {"type": "TextBlock", "text": summary, "wrap": True},
        {"type": "TextBlock", "text": body_text, "wrap": True},
    ]

    if alerts:
        body_blocks.append({
            "type": "TextBlock",
            "text": "**Alerts:**\n" + "\n".join(f"⚠️ {a}" for a in alerts),
            "wrap": True, "color": "Attention",
        })

    body_blocks.append({
        "type": "TextBlock",
        "text": f"Generated {now} | crawldevvm",
        "isSubtle": True, "size": "Small",
    })

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4",
                "msteams": {"width": "Full"},
                "body": body_blocks,
            },
        }],
    }

    try:
        r = requests.post(TEAMS_WEBHOOK, json=payload, timeout=10)
        print(f"Teams report sent: {r.status_code}")
    except Exception as e:
        print(f"Teams report failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--check"

    if mode == "--test":
        send_teams_report({
            "date": "2026-05-04",
            "regions": {
                "americas": {"status": "online", "model": "claude-sonnet-4-6", "source": "research", "sessions": 3},
                "europe":   {"status": "online", "model": "claude-sonnet-4-6", "source": "heartbeat", "sessions": 2},
                "gulf":     {"status": "online", "model": "claude-sonnet-4-6", "source": "research", "sessions": 1},
                "china":    {"status": "online", "model": "deepseek-chat", "source": "heartbeat", "sessions": 2},
                "india":    {"status": "offline", "model": "—", "sessions": 0},
            },
            "regions_online": 4,
            "anthropic_actual": {
                "total_usd": 36.10,
                "models": {"claude-sonnet-4-6": 13.11, "claude-opus-4-6": 21.29, "claude-haiku-4-5-20251001": 0.17},
                "web_searches": 147,
            },
            "trailing_7d": {
                "total_usd": 202.54,
                "days": [
                    {"date": "2026-04-28", "cost": 35.35},
                    {"date": "2026-04-29", "cost": 46.37},
                    {"date": "2026-04-30", "cost": 54.36},
                    {"date": "2026-05-01", "cost": 28.80},
                    {"date": "2026-05-02", "cost": 1.56},
                    {"date": "2026-05-03", "cost": 0.00},
                    {"date": "2026-05-04", "cost": 36.10},
                ],
                "models": {"claude-sonnet-4-6": 110.00, "claude-opus-4-6": 89.00, "claude-haiku-4-5-20251001": 0.62},
            },
            "total_llm": 36.10,
            "tool_calls": {},
            "total_searches": 0, "total_fetches": 0,
            "apis": {
                "deepseek":   {"ok": True, "balance": 20.29},
                "moonshot":   {"ok": True, "balance": 24.13},
                "tavily":     {"ok": True, "used": 531, "limit": 4000, "plan": "Project"},
                "firecrawl":  {"ok": True, "remaining": 2645, "plan": 3000},
                "perplexity": {"ok": True, "status": "Active"},
                "exa":        {"ok": True, "status": "Active"},
                "sarvam":     {"ok": True, "status": "Active"},
            },
            "alerts": [],
        })
        print("Test report sent.")
        return

    try:
        ensure_table()
        print("DB ready.")
    except Exception as e:
        print(f"DB setup failed: {e}")

    if mode == "--backfill":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        for i in range(days, 0, -1):
            d = (datetime.now(timezone.utc) - timedelta(days=i)).date()
            print(f"\n--- {d} ---")
            report = build_report(d)
            if i == 1:
                send_teams_report(report)
        return

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    print(f"\nCrawl Daily Spend — {yesterday}")
    print("=" * 40)

    report = build_report(yesterday)

    # Console output
    anthropic = report.get("anthropic_actual", {})
    print(f"\n  Anthropic billed: ${anthropic.get('total_usd', 0):.2f}")
    for model, cost in sorted(anthropic.get("models", {}).items(), key=lambda x: -x[1]):
        print(f"    {model}: ${cost:.2f}")
    if anthropic.get("web_searches"):
        print(f"    Web searches: {anthropic['web_searches']}")

    print(f"\n  Regions: {report.get('regions_online', 0)}/{len(VMS)} online")
    for r in ["americas", "europe", "gulf", "china", "india"]:
        rs = report["regions"].get(r, {})
        status = rs.get("status", "unknown")
        if status == "offline":
            print(f"    {r}: offline")
        else:
            print(f"    {r}: {rs.get('sessions', 0)} sessions ({rs.get('source', 'idle')})")

    apis = report["apis"]
    ds = apis.get("deepseek", {})
    ms = apis.get("moonshot", {})
    if ds.get("ok"): print(f"  DeepSeek balance: ${ds['balance']:.2f}")
    if ms.get("ok"): print(f"  Moonshot balance: ${ms['balance']:.2f}")

    tv = apis.get("tavily", {})
    fc = apis.get("firecrawl", {})
    if tv.get("ok"): print(f"  Tavily: {tv['used']}/{tv['limit']}")
    if fc.get("ok"): print(f"  Firecrawl: {fc['remaining']}/{fc['plan']} remaining")

    print(f"\n  Total billed: ${report['total_llm']:.2f}")

    if report["alerts"]:
        print(f"\n  Alerts: {len(report['alerts'])}")
        for a in report["alerts"]:
            print(f"    - {a}")

    send_teams_report(report)
    sys.exit(1 if report["alerts"] else 0)


if __name__ == "__main__":
    main()
