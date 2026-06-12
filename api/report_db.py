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
    report_json: dict = None,
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
                        dark_web_alert, seed_data, duration_ms, created_at,
                        report_json)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (job_id) DO UPDATE SET
                           status = EXCLUDED.status,
                           blob_path = EXCLUDED.blob_path,
                           report_summary = EXCLUDED.report_summary,
                           dark_web_findings = EXCLUDED.dark_web_findings,
                           dark_web_sources = EXCLUDED.dark_web_sources,
                           dark_web_alert = EXCLUDED.dark_web_alert,
                           duration_ms = EXCLUDED.duration_ms,
                           report_json = COALESCE(EXCLUDED.report_json, cir_reports.report_json),
                           completed_at = NOW()
                    """,
                    (
                        job_id, entity_name, country, region, status, blob_path,
                        report_summary, dark_web_findings, dark_web_sources,
                        dark_web_alert,
                        json.dumps(seed_data, default=str) if seed_data else None,
                        duration_ms, created_at,
                        json.dumps(report_json, default=str) if report_json else None,
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
# Standalone dark-web reports (crawl_reports database)
# -----------------------------------------------------------------------


def save_darkweb_report(
    job_id: str,
    entity_name: str,
    country: str,
    owners: list = None,
    domain: str = None,
    depth: str = None,
    status: str = "completed",
    blob_path: str = None,
    findings_count: int = 0,
    sources_searched: int = 0,
    sources_with_results: int = 0,
    alert_level: str = None,
    report_summary: str = None,
    error: str = None,
    report_json: dict = None,
    created_at: str = None,
):
    """Persist a completed standalone dark-web job. Non-blocking."""
    def _write():
        try:
            with _write_lock:
                conn = _get_conn()
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO darkweb_reports
                       (job_id, entity_name, country, owners, domain, depth,
                        status, blob_path, findings_count, sources_searched,
                        sources_with_results, alert_level, report_summary,
                        error, report_json, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s)
                       ON CONFLICT (job_id) DO UPDATE SET
                           status = EXCLUDED.status,
                           blob_path = EXCLUDED.blob_path,
                           findings_count = EXCLUDED.findings_count,
                           sources_searched = EXCLUDED.sources_searched,
                           sources_with_results = EXCLUDED.sources_with_results,
                           alert_level = EXCLUDED.alert_level,
                           report_summary = EXCLUDED.report_summary,
                           error = EXCLUDED.error,
                           report_json = COALESCE(EXCLUDED.report_json, darkweb_reports.report_json),
                           completed_at = NOW()
                    """,
                    (
                        job_id, entity_name, country,
                        json.dumps(owners, default=str) if owners else None,
                        domain, depth, status, blob_path,
                        findings_count, sources_searched, sources_with_results,
                        alert_level, report_summary, error,
                        json.dumps(report_json, default=str) if report_json else None,
                        created_at,
                    ),
                )
                conn.commit()
                cur.close()
                conn.close()
                log.info("report_db: saved darkweb report %s (%s/%s) findings=%d alert=%s",
                         job_id[:8], entity_name, country, findings_count, alert_level)
        except Exception as e:
            log.warning("report_db: failed to save darkweb report %s: %s", job_id[:8], e)

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
                        (((resp.get("validation_source", {}) or {}).get("primary") or
                          (resp.get("validation_source", {}) or {}).get("registry") or
                          resp.get("enrichment_source") or
                          "")[:200]),
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
