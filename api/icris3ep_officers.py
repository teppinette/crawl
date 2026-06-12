"""
ICRIS3EP Directors Index Search — cache-first, pay-on-miss.

Authenticated paid search at HK Companies Registry e-Services. Each LIVE
call costs the account ~HKD 22 per Particulars Search tariff. Cached
results live in `crawl_reports.icris3ep_officers` indexed by BRN; every
subsequent lookup for the same BRN is free until the caller explicitly
requests `refresh=True`.

Flow:
  1. cache lookup by BRN. If hit AND not forced refresh → return cached.
  2. cache miss / forced refresh → Multilogin browser session:
     login → Directors Index Search (Company-based) → BRN → parse table
  3. persist to cache table with raw_html for audit
  4. return shape that distinguishes paid vs cached calls
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import psycopg2
import psycopg2.extras
from playwright.sync_api import sync_playwright

import mlx_http

log = logging.getLogger("verify-gateway")

_ICRIS_PORTAL = "https://www.e-services.cr.gov.hk/"
_DIRECTORS_SEARCH_PATH = (
    "https://www.e-services.cr.gov.hk/ICRIS3EP/system/registeredCom/cps_director_index.do"
)
_PER_CALL_COST_HKD = 22.0

_USER: str | None = None
_PASSWORD: str | None = None
_DB_CFG: dict | None = None


def init(get_secret):
    global _USER, _PASSWORD, _DB_CFG
    _USER = get_secret("icris-eservices-user") or os.environ.get("ICRIS_ESERVICES_USER", "")
    _PASSWORD = get_secret("icris-eservices-password") or os.environ.get("ICRIS_ESERVICES_PASSWORD", "")
    # DB config — pulls host/db/user/password from Key Vault / env (load_db_config helper
    # lives in keyvault.py on the gateway VM; on verify VM we read .env directly).
    _DB_CFG = {
        "host": get_secret("db-host") or os.environ.get("DB_HOST", ""),
        "user": get_secret("db-user") or os.environ.get("DB_USER", "crawladmin"),
        "password": get_secret("db-password") or os.environ.get("DB_PASSWORD", ""),
        "dbname": "crawl_reports",
        "sslmode": "require",
    }
    _ensure_cache_table()
    if _USER and _PASSWORD:
        log.info("ICRIS3EP officers ready — cache-first lookup wired (DB=%s)", _DB_CFG.get("dbname"))
    else:
        log.warning("ICRIS3EP officers: no credentials configured "
                    "(set ICRIS_ESERVICES_USER / ICRIS_ESERVICES_PASSWORD)")


def is_available() -> bool:
    return bool(_USER and _PASSWORD)


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

_CACHE_DDL = """
CREATE SCHEMA IF NOT EXISTS public;
CREATE TABLE IF NOT EXISTS icris3ep_officers (
    brn               VARCHAR(8)  PRIMARY KEY,
    cr_number         VARCHAR(10),
    entity_name       TEXT,
    officers          JSONB       NOT NULL,
    officer_count     INT         NOT NULL,
    fetched_at        TIMESTAMPTZ NOT NULL,
    raw_html          TEXT,
    cost_hkd_paid     NUMERIC(10,2) NOT NULL DEFAULT 22.0,
    refresh_count     INT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_icris3ep_officers_cr_number ON icris3ep_officers (cr_number);
"""


def _ensure_cache_table():
    if not _DB_CFG or not _DB_CFG.get("host"):
        log.warning("ICRIS3EP officers: DB host not set — cache table not provisioned")
        return
    try:
        with psycopg2.connect(**_DB_CFG, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(_CACHE_DDL)
            conn.commit()
    except Exception as e:
        log.warning("ICRIS3EP cache table ensure failed: %s", str(e)[:200])


def _cache_lookup(brn: str) -> dict | None:
    if not _DB_CFG or not _DB_CFG.get("host"):
        return None
    try:
        with psycopg2.connect(**_DB_CFG, connect_timeout=8) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT brn, cr_number, entity_name, officers, officer_count,
                              fetched_at, cost_hkd_paid, refresh_count
                       FROM icris3ep_officers WHERE brn = %s""",
                    (brn,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        log.warning("ICRIS3EP cache lookup failed for %s: %s", brn, str(e)[:160])
        return None


def _cache_total_spend(brn: str) -> float:
    """Sum cost_hkd_paid * refresh_count for a BRN — running ledger."""
    row = _cache_lookup(brn)
    if not row:
        return 0.0
    return float(row.get("cost_hkd_paid") or 22.0) * int(row.get("refresh_count") or 1)


def _cache_upsert(brn: str, cr_number: str, entity_name: str,
                  officers: list, raw_html: str, is_refresh: bool):
    if not _DB_CFG or not _DB_CFG.get("host"):
        return
    try:
        with psycopg2.connect(**_DB_CFG, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO icris3ep_officers
                        (brn, cr_number, entity_name, officers, officer_count,
                         fetched_at, raw_html, cost_hkd_paid, refresh_count)
                    VALUES (%s, %s, %s, %s::jsonb, %s, NOW(), %s, %s, 1)
                    ON CONFLICT (brn) DO UPDATE SET
                        cr_number     = COALESCE(EXCLUDED.cr_number, icris3ep_officers.cr_number),
                        entity_name   = COALESCE(EXCLUDED.entity_name, icris3ep_officers.entity_name),
                        officers      = EXCLUDED.officers,
                        officer_count = EXCLUDED.officer_count,
                        fetched_at    = NOW(),
                        raw_html      = EXCLUDED.raw_html,
                        refresh_count = icris3ep_officers.refresh_count + 1
                    """,
                    (brn, cr_number or None, entity_name or None,
                     psycopg2.extras.Json(officers), len(officers),
                     raw_html[:500_000] if raw_html else None,
                     _PER_CALL_COST_HKD),
                )
            conn.commit()
    except Exception as e:
        log.warning("ICRIS3EP cache upsert failed for %s: %s", brn, str(e)[:200])


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_directors_table(body_text: str) -> list[dict]:
    """
    Parse the body inner-text of the ICRIS3EP Directors Index Search results.
    The user-confirmed format is:
      No. | Name in English | Name in Chinese | HKID No. / BRN | Passport No. |
      Passport Issuing Country/Region | Director Type | ...

    Rows look like (from a real result):
      "1    CHEN, MING YAO    陳明耀    K970***    36021****    台灣 TAIWAN    Natural Person"
    """
    directors: list[dict] = []
    if not body_text:
        return directors

    text = re.sub(r"\s+", " ", body_text)

    row_pattern = re.compile(
        r"\b(\d{1,2})\s+"                                  # position
        r"([A-Z][A-Z, .'\-]{2,80}?)\s+"                    # English name
        r"(?:([一-鿿]{2,8})|-)?\s*"               # Chinese name or "-"
        r"(?:(K\d{3}\*+|A\d{3}\*+|[A-Z]\d{3}\*+|-)\s+)?"  # HKID (masked) or "-"
        r"(?:(\d{4,7}\*+|-)\s+)?"                          # passport (masked) or "-"
        r"(?:([一-鿿]+\s+[A-Z][A-Z .]+|[一-鿿]+)\s+)?"  # country (CJK [+ EN])
        r"(Natural Person|Body Corporate|Corporate)"        # director type
    )

    for m in row_pattern.finditer(text):
        directors.append({
            "position": int(m.group(1)),
            "name_english": m.group(2).strip(),
            "name_chinese": m.group(3) if m.group(3) and m.group(3) != "-" else None,
            "hkid_masked": m.group(4) if m.group(4) and m.group(4) != "-" else None,
            "passport_masked": m.group(5) if m.group(5) and m.group(5) != "-" else None,
            "country_or_region": m.group(6).strip() if m.group(6) else None,
            "director_type": m.group(7),
        })

    return directors


# ---------------------------------------------------------------------------
# Multilogin live flow (only invoked on cache miss / refresh)
# ---------------------------------------------------------------------------

def _do_live_search(port: int, brn: str, profile_id: str) -> dict:
    """Multilogin-profile-scoped Playwright flow. Called by mlx_http._with_profile."""
    proxy = mlx_http._get_country_proxy("hk")
    result: dict[str, Any] = {"officers": [], "raw_html": "", "error": None}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx_kwargs = {"ignore_https_errors": True}
            if proxy:
                ctx_kwargs["proxy"] = proxy
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            try:
                # PORTAL_NAVIGATION_RETRY: ICRIS3EP SPA is slow; try twice before giving up
                _portal_ok = False
                for _attempt in range(2):
                    try:
                        page.goto(_ICRIS_PORTAL, timeout=120_000, wait_until="load")
                        _portal_ok = True
                        break
                    except Exception as _e:
                        log.warning("ICRIS3EP portal load attempt %d failed: %s", _attempt+1, str(_e)[:120])
                        time.sleep(5)
                if not _portal_ok:
                    result["error"] = "icris3ep portal unreachable after 2 attempts (240s total)"
                    return result
                time.sleep(4)

                # Login
                login_selectors = [
                    ("input[name='userId']", _USER),
                    ("input[name='password']", _PASSWORD),
                ]
                for sel, val in login_selectors:
                    try:
                        page.fill(sel, val, timeout=8_000)
                    except Exception:
                        # Try generic fallback fields
                        if "user" in sel.lower():
                            page.fill("input[id*='user' i]:not([type='hidden'])", val, timeout=6_000)
                        else:
                            page.fill("input[type='password']", val, timeout=6_000)
                try:
                    with page.expect_navigation(timeout=25_000, wait_until="domcontentloaded"):
                        page.click("button:has-text('Login'), input[type='submit'][value*='ogin'], button#loginButton", timeout=8_000)
                except Exception:
                    time.sleep(5)

                time.sleep(3)

                # Navigate to Directors Index Search (Company-based)
                try:
                    page.goto(_DIRECTORS_SEARCH_PATH, timeout=45_000, wait_until="domcontentloaded")
                except Exception as _se:
                    log.warning("ICRIS3EP search page slow load — proceeding anyway: %s", str(_se)[:120])
                time.sleep(8)  # let SPA finish hydrating
                time.sleep(4)

                # Select BRN search if radio exists
                try:
                    page.click("input[type='radio'][value*='BRN'], label:has-text('BRN'), label:has-text('Business Registration')", timeout=3_000)
                except Exception:
                    pass

                # Fill BRN
                try:
                    page.fill("input[name='brn'], input[name*='BRN'], input[name*='businessReg']", brn, timeout=8_000)
                except Exception as e:
                    # DEBUG: dump page state to /tmp so we can find the right selector
                    try:
                        import time as _tm
                        ts = _tm.strftime("%Y%m%d_%H%M%S")
                        html_path = f"/tmp/icris_brn_debug_{ts}.html"
                        with open(html_path, "w") as _f:
                            _f.write(page.content())
                        inputs = page.eval_on_selector_all(
                            "input, select, textarea",
                            "els => els.map(e => ({tag: e.tagName, name: e.name, id: e.id, type: e.type, placeholder: e.placeholder}))"
                        )
                        log.warning("ICRIS3EP BRN field not found. URL=%s. Fields: %s. HTML at %s",
                                    page.url, inputs[:30], html_path)
                        result["debug_fields"] = inputs[:30]
                        result["debug_url"] = page.url
                        result["debug_html_path"] = html_path
                    except Exception as _de:
                        log.warning("ICRIS3EP debug dump failed: %s", _de)
                    result["error"] = f"BRN field fill failed: {str(e)[:160]}"
                    return result

                # Submit
                try:
                    with page.expect_navigation(timeout=40_000, wait_until="domcontentloaded"):
                        page.click("button:has-text('Search'), input[type='submit'][value*='earch']", timeout=8_000)
                except Exception:
                    time.sleep(8)
                time.sleep(6)

                body = page.inner_text("body")
                html = page.content()
                result["raw_html"] = html
                result["officers"] = _parse_directors_table(body)
                # Try to also pull entity_name from the result panel
                m = re.search(r"Company Name\s+([A-Z][A-Z, .'&\-]+)", body)
                if m:
                    result["entity_name"] = m.group(1).strip()
            finally:
                try:
                    page.close(); context.close(); browser.close()
                except Exception:
                    pass
    except Exception as e:
        result["error"] = f"icris3ep live flow exception: {str(e)[:240]}"
    return result


# ---------------------------------------------------------------------------
# Public entry — cache-first, pay-on-miss
# ---------------------------------------------------------------------------

def fetch_officers(country_code: str, brn: str, cr_number: str = "",
                   entity_name: str = "", refresh: bool = False) -> dict:
    if (country_code or "").upper() != "HK":
        return _error_response(country_code, brn,
                               f"officer lookup is HK-only ({country_code} not supported)")
    if not is_available():
        return _error_response("HK", brn,
                               "ICRIS3EP credentials not configured "
                               "(ICRIS_ESERVICES_USER / ICRIS_ESERVICES_PASSWORD)")
    clean_brn = re.sub(r"\D", "", brn or "")
    if not clean_brn or len(clean_brn) != 8:
        return _error_response("HK", clean_brn,
                               "BRN must be 8 digits (Business Registration Number)")

    # Cache-first
    if not refresh:
        cached = _cache_lookup(clean_brn)
        if cached:
            officers = cached["officers"] or []
            return {
                "country_code": "HK",
                "brn": clean_brn,
                "cr_number": cached.get("cr_number") or cr_number or None,
                "entity_name": cached.get("entity_name") or entity_name or None,
                "officers": officers,
                "officer_count": len(officers),
                "cached": True,
                "cost_hkd_paid_this_call": 0.0,
                "total_cost_hkd_to_date": _cache_total_spend(clean_brn),
                "fetched_at": cached["fetched_at"].isoformat() if cached.get("fetched_at") else None,
                "source": "HK Companies Registry ICRIS3EP — Directors Index Search (cached)",
            }

    # Cache miss / forced refresh → live paid search
    live = mlx_http._with_profile(_do_live_search, clean_brn)

    if not isinstance(live, dict) or live.get("error"):
        # Still attempt cache fallback even on live error
        cached = _cache_lookup(clean_brn)
        if cached:
            officers = cached["officers"] or []
            return {
                "country_code": "HK", "brn": clean_brn,
                "cr_number": cached.get("cr_number") or cr_number,
                "entity_name": cached.get("entity_name") or entity_name,
                "officers": officers, "officer_count": len(officers),
                "cached": True, "stale_cache_returned_due_to_live_error": True,
                "cost_hkd_paid_this_call": 0.0,
                "total_cost_hkd_to_date": _cache_total_spend(clean_brn),
                "error": (live or {}).get("error") if isinstance(live, dict) else "live call failed",
                "source": "HK Companies Registry ICRIS3EP — Directors Index Search (cached, stale)",
            }
        return _error_response("HK", clean_brn,
                               (live or {}).get("error") if isinstance(live, dict) else "live call failed")

    officers = live.get("officers") or []
    derived_entity = live.get("entity_name") or entity_name
    _cache_upsert(clean_brn, cr_number, derived_entity, officers, live.get("raw_html", ""),
                  is_refresh=bool(refresh))

    return {
        "country_code": "HK",
        "brn": clean_brn,
        "cr_number": cr_number or None,
        "entity_name": derived_entity,
        "officers": officers,
        "officer_count": len(officers),
        "cached": False,
        "cost_hkd_paid_this_call": _PER_CALL_COST_HKD,
        "total_cost_hkd_to_date": _cache_total_spend(clean_brn),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "HK Companies Registry ICRIS3EP — Directors Index Search (Company-based)",
    }


def _error_response(country: str, brn: str, msg: str) -> dict:
    return {
        "country_code": country or "?",
        "brn": brn or None,
        "officers": [],
        "officer_count": 0,
        "cached": False,
        "cost_hkd_paid_this_call": 0.0,
        "total_cost_hkd_to_date": 0.0,
        "error": msg,
    }
