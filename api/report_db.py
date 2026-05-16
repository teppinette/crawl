"""
Research output persistence — separate from crawlmonitor (ops/observability).

Databases:
  crawl_reports       — CIR research output (table: cir_reports)
  crawl_verification  — Entity verification results (table: verification_results)

Same PostgreSQL server as crawlmonitor.
"""

import json
import logging
import threading

from keyvault import load_db_config

log = logging.getLogger("crawl-gateway")

_db_cfg = None
_write_lock = threading.Lock()


def _get_conn():
    """Get connection to crawl_reports database."""
    global _db_cfg
    if _db_cfg is None:
        base = load_db_config()
        _db_cfg = dict(base)
        _db_cfg["dbname"] = "crawl_reports"
    import psycopg2
    return psycopg2.connect(**_db_cfg, connect_timeout=5)


def _bg_write(fn):
    """Run a DB write in a daemon thread so it never blocks the caller."""
    t = threading.Thread(target=fn, daemon=True)
    t.start()


def save_cir_report(
    job_id: str,
    entity_name: str,
    country: str,
    region: str,
    status: str = "completed",
    blob_path: str = None,
    report_summary: str = None,
    dark_web_findings: int = 0,
    dark_web_sources: int = 0,
    dark_web_alert: str = None,
    seed_data: dict = None,
    duration_ms: int = None,
    created_at: str = None,
):
    """Persist a completed CIR report. Non-blocking."""
    def _write():
        try:
            with _write_lock:
                conn = _get_conn()
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO cir_reports
                       (job_id, entity_name, country, region, status, blob_path,
                        report_summary, dark_web_findings, dark_web_sources,
                        dark_web_alert, seed_data, duration_ms, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (job_id) DO UPDATE SET
                           status = EXCLUDED.status,
                           blob_path = EXCLUDED.blob_path,
                           report_summary = EXCLUDED.report_summary,
                           dark_web_findings = EXCLUDED.dark_web_findings,
                           dark_web_sources = EXCLUDED.dark_web_sources,
                           dark_web_alert = EXCLUDED.dark_web_alert,
                           duration_ms = EXCLUDED.duration_ms,
                           completed_at = NOW()
                    """,
                    (
                        job_id, entity_name, country, region, status, blob_path,
                        report_summary, dark_web_findings, dark_web_sources,
                        dark_web_alert,
                        json.dumps(seed_data, default=str) if seed_data else None,
                        duration_ms, created_at,
                    ),
                )
                conn.commit()
                cur.close()
                conn.close()
                log.info("report_db: saved CIR report %s (%s/%s)", job_id[:8], entity_name, country)
        except Exception as e:
            log.warning("report_db: failed to save CIR report %s: %s", job_id[:8], e)

    _bg_write(_write)


# -----------------------------------------------------------------------
# Verification results (crawl_verification database)
# -----------------------------------------------------------------------

_verify_db_cfg = None


def _get_verify_conn():
    """Get connection to crawl_verification database."""
    global _verify_db_cfg
    if _verify_db_cfg is None:
        base = load_db_config()
        _verify_db_cfg = dict(base)
        _verify_db_cfg["dbname"] = "crawl_verification"
    import psycopg2
    return psycopg2.connect(**_verify_db_cfg, connect_timeout=5)


def save_verification(resp: dict):
    """
    Persist a verification result. Non-blocking.
    Accepts the full response dict returned by the /api/v1/verify endpoint.
    """
    def _write():
        try:
            with _write_lock:
                conn = _get_verify_conn()
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO verification_results
                       (entity_name, country, registry_source, status, verified,
                        registration_number, registration_date, legal_status,
                        address, directors, raw_response, error, duration_ms)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resp.get("entity_name", "")[:500],
                        resp.get("country_code", "")[:10],
                        (resp.get("validation_source", {}) or {}).get("registry", "")[:200],
                        "completed" if not resp.get("error") else "error",
                        resp.get("verified", False),
                        (resp.get("registration_number") or resp.get("cin") or
                         resp.get("uen") or resp.get("vkn") or resp.get("trn") or
                         resp.get("uscc") or resp.get("company_number") or
                         resp.get("cnpj") or resp.get("cik") or resp.get("corp_code") or
                         resp.get("cr_number") or resp.get("rut") or resp.get("nit") or
                         resp.get("ruc") or ""),
                        (resp.get("registration_date") or resp.get("incorporation_date") or
                         resp.get("date_opened") or resp.get("established_date") or
                         resp.get("issue_date") or resp.get("activity_start_date") or ""),
                        resp.get("status", ""),
                        (resp.get("registered_address") or resp.get("address") or
                         resp.get("business_address") or resp.get("location") or ""),
                        json.dumps(resp.get("directors") or resp.get("partners") or
                                   resp.get("owners") or resp.get("managers") or [],
                                   default=str) or None,
                        json.dumps(resp, default=str),
                        resp.get("error"),
                        resp.get("duration_ms"),
                    ),
                )
                conn.commit()
                cur.close()
                conn.close()
                log.info("report_db: saved verification %s/%s verified=%s",
                         resp.get("entity_name", "?")[:30], resp.get("country_code"),
                         resp.get("verified"))
        except Exception as e:
            log.warning("report_db: failed to save verification %s: %s",
                        resp.get("entity_name", "?")[:30], e)

    _bg_write(_write)
