"""
Event logging for the Crawl Research Gateway.

Two log streams:
  1. job_events    — every state transition for every research job
  2. api_access_log — every HTTP request to the gateway

All writes are fire-and-forget via a background thread so they never
block the API or SSH dispatch. If the DB is unreachable, events are
logged to stderr and dropped (no queue buildup).
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from keyvault import load_db_config

log = logging.getLogger("crawl-gateway")

_db_cfg = None
_write_lock = threading.Lock()


def _get_conn():
    """Get a fresh DB connection (no pooling — writes are infrequent)."""
    global _db_cfg
    if _db_cfg is None:
        _db_cfg = load_db_config()
    import psycopg2
    return psycopg2.connect(**_db_cfg, connect_timeout=5)


def _bg_write(fn):
    """Run a DB write in a daemon thread so it never blocks the caller."""
    t = threading.Thread(target=fn, daemon=True)
    t.start()


# -----------------------------------------------------------------------
# Job events
# -----------------------------------------------------------------------

def log_job_event(
    job_id: str,
    event: str,
    scenario: str = None,
    region: str = None,
    status: str = None,
    client_ip: str = None,
    duration_ms: int = None,
    details: dict = None,
    error: str = None,
):
    """Log a job lifecycle event. Non-blocking."""
    def _write():
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO job_events
                   (job_id, event, scenario, region, status, client_ip,
                    duration_ms, details, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    job_id, event, scenario, region, status, client_ip,
                    duration_ms,
                    json.dumps(details, default=str) if details else None,
                    error,
                ),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            log.warning("event_log: failed to write job_event %s/%s: %s", job_id[:8], event, e)

    _bg_write(_write)


# -----------------------------------------------------------------------
# API access log
# -----------------------------------------------------------------------

def log_api_access(
    client_ip: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int = None,
    job_id: str = None,
    user_agent: str = None,
    error: str = None,
):
    """Log an API request. Non-blocking."""
    def _write():
        try:
            conn = _get_conn()
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO api_access_log
                   (client_ip, method, path, status_code, duration_ms,
                    job_id, user_agent, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    client_ip, method, path[:255], status_code, duration_ms,
                    job_id, (user_agent or "")[:255], error,
                ),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            log.warning("event_log: failed to write api_access: %s %s: %s", method, path, e)

    _bg_write(_write)
