#!/usr/bin/env python3
"""
Daily petrochemical price scraper — Multilogin + Bright Data proxy.

Runs on crawl-verify VM (180.20.0.4) where Multilogin agent is installed.
Scrapes echemi.com and sunsirs.com for daily petrochemical prices.
Uploads results as JSON to stcrawlosint blob storage.

ALL website access goes through Multilogin anti-detect browser with
Bright Data residential proxy. No direct HTTP to target sites.

Usage:
    python3 petrochem_scraper.py                    # scrape all targets
    python3 petrochem_scraper.py --dry-run           # parse only, no blob upload

Cron (nightly, randomized 01:00-03:00 UTC):
    0 1 * * * sleep $((RANDOM % 7200)) && /home/copapadmin/verify-env/bin/python3 /home/copapadmin/petrochem_scraper.py >> /home/copapadmin/petrochem_scraper.log 2>&1
"""

import hashlib
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("petrochem-scraper")

# ---------------------------------------------------------------------------
# Credentials from environment (loaded from .env on crawl-verify)
# ---------------------------------------------------------------------------
MLX_EMAIL = os.environ.get("MULTILOGIN_EMAIL", "")
MLX_PASSWORD = os.environ.get("MULTILOGIN_PASSWORD", "")
MLX_FOLDER_ID = os.environ.get("MULTILOGIN_FOLDER_ID", "")
MLX_PROXY_USER = os.environ.get("MULTILOGIN_PROXY_USER", "")
MLX_PROXY_PASS = os.environ.get("MULTILOGIN_PROXY_PASS", "")
BLOB_SAS_TOKEN = os.environ.get("BLOB_SAS_TOKEN", "")

# PostgreSQL (crawl-monitor-db)
DB_HOST = os.environ.get("DB_HOST", "crawl-monitor-db.postgres.database.azure.com")
DB_NAME = os.environ.get("DB_NAME", "crawlmonitor")
DB_USER = os.environ.get("DB_USER", "crawladmin")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# Pool profiles — reuse the same pool as verify-gateway
_pool_raw = os.environ.get("MULTILOGIN_POOL_PROFILES", "")
try:
    POOL_PROFILE_IDS = json.loads(_pool_raw)
except Exception:
    cleaned = _pool_raw.strip().strip("[]")
    POOL_PROFILE_IDS = [s.strip().strip('"').strip("'") for s in cleaned.split(",") if s.strip()]

CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

# Bright Data residential proxy
BRD_PROXY_USER = "brd-customer-hl_7bf69e76-zone-pk_residental"
BRD_PROXY_PASS = "o6nw1d0jrol0"

# Blob storage
BLOB_ACCOUNT = "stcrawlosint"
BLOB_CONTAINER = "osint-staging"

# Auth token cache
_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

# ---------------------------------------------------------------------------
# Echemi pages to scrape (correct URLs as of May 2026)
#
# Each zyc page has: latest China domestic prices, international prices,
# regional prices, enterprise prices for ALL chemicals in that category.
# Price-curve pages have international + regional for a single chemical.
# ---------------------------------------------------------------------------
ECHEMI_PAGES = [
    {
        "name": "aromatics",
        "url": "https://www.echemi.com/zyc/aromatics-market-zyc01.html",
        "chemicals": ["toluene", "benzene", "xylene", "o-xylene", "chlorobenzene",
                       "styrene", "paraxylene"],
    },
    {
        "name": "olefins",
        "url": "https://www.echemi.com/zyc/olefin-market-zyc02.html",
        "chemicals": ["ethylene", "propylene", "butadiene"],
    },
    {
        "name": "petrochemicals",
        "url": "https://www.echemi.com/zyc/petrochemical-market-zyc23.html",
        "chemicals": ["naphtha", "paraffin", "hexane", "cyclohexane"],
    },
    {
        "name": "methanol_downstream",
        "url": "https://www.echemi.com/zyc/methanol-downstream-zyc03.html",
        "chemicals": ["methanol", "acetic acid", "formaldehyde"],
    },
]

# Individual price-curve pages with international + regional detail.
# Keep this list SHORT — each page risks triggering a new WAF challenge.
# Only the 3 chemicals with FOB Korea / international prices we actually need.
ECHEMI_PRICE_CURVES = {
    "toluene": "https://www.echemi.com/price-curve/toluene-temppid160704000607-1.html",
    "benzene": "https://www.echemi.com/price-curve/benzene-pid_Seven2868-1.html",
    "xylene": "https://www.echemi.com/price-curve/xylene-pd1707041010-1.html",
}


# ---------------------------------------------------------------------------
# Multilogin helpers (same pattern as multilogin_fbr.py)
# ---------------------------------------------------------------------------

def _get_token() -> str:
    global _cached_token, _token_expiry
    with _token_lock:
        if time.monotonic() < _token_expiry and _cached_token:
            return _cached_token
        resp = requests.post(
            "https://api.multilogin.com/user/signin",
            json={
                "email": MLX_EMAIL,
                "password": hashlib.md5(MLX_PASSWORD.encode()).hexdigest(),
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"]["http_code"] != 200:
            raise RuntimeError(f"MLX sign-in failed: {data['status']['message']}")
        _cached_token = data["data"]["token"]
        _token_expiry = time.monotonic() + 300
        return _cached_token


def _launch_profile(token: str, profile_id: str) -> int:
    url = (
        f"https://launcher.mlx.yt:45001/api/v2/profile"
        f"/f/{MLX_FOLDER_ID}/p/{profile_id}"
        f"/start?automation_type=playwright&headless_mode=true"
    )
    resp = requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        verify=False,
        timeout=60,
    )
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX launch failed: {data['status']['message']}")
    return int(data["data"]["port"])


def _stop_profile(profile_id: str):
    try:
        subprocess.run(
            [str(CLI_PATH), "profile-stop", "--profile-id", profile_id],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Price extraction helpers
# ---------------------------------------------------------------------------

def _parse_price_text(text: str) -> list:
    """
    Parse echemi page body text into structured price records.

    Handles two formats:

    ZYC category pages (tab-separated single lines):
      "Toluene China Domestic Price\nMay 15, 2026\n6931.0 Yuan/mt"

    Price-curve pages (values on separate lines):
      "Toluene China\nFOB\n1010.0\n5.0\nUSD/ton\nMay 14, 2026"
      "Jiangsu Toluene Petroleum toluene\n7165.0\n15.0\nYuan/mt\nMay 14, 2026"
    """
    records = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]

        # --- Pattern 1: "ProductName China Domestic Price" + date + price ---
        if "china domestic price" in line.lower():
            product = line.replace("China Domestic Price", "").replace("China domestic price", "").strip()
            if i + 2 < len(lines):
                date_str = lines[i + 1]
                price_str = lines[i + 2]
                price_match = re.match(r'([\d,.]+)\s*(Yuan/mt|USD/ton|Cents/gallon)', price_str)
                if price_match:
                    records.append({
                        "product": product,
                        "type": "china_domestic",
                        "price": float(price_match.group(1).replace(",", "")),
                        "unit": price_match.group(2),
                        "date": date_str,
                    })
                    i += 3
                    continue

        # --- Pattern 2: Single-line international ---
        intl_match = re.match(
            r'(.+?)\s+(FOB|CFR|CIF)\s+([\d,.]+)\s+([\d,.]*)\s*(USD/ton|Cents/gallon|EUR/ton)\s+(.+)',
            line
        )
        if intl_match:
            change_str = intl_match.group(4).strip()
            records.append({
                "product": intl_match.group(1).strip(),
                "type": "international",
                "incoterm": intl_match.group(2),
                "price": float(intl_match.group(3).replace(",", "")),
                "change": float(change_str.replace(",", "")) if change_str else 0,
                "unit": intl_match.group(5),
                "date": intl_match.group(6).strip(),
            })
            i += 1
            continue

        # --- Pattern 3: Multi-line international (price-curve pages) ---
        # Line is a product name, next lines: FOB/CFR, price, change, unit, date
        if i + 5 < len(lines) and lines[i + 1] in ("FOB", "CFR", "CIF"):
            try:
                product = line
                incoterm = lines[i + 1]
                price_val = float(lines[i + 2].replace(",", ""))
                # Change might be a number or empty
                change_val = 0
                unit_idx = i + 3
                try:
                    change_val = float(lines[i + 3].replace(",", ""))
                    unit_idx = i + 4
                except ValueError:
                    pass
                unit = lines[unit_idx]
                date_str = lines[unit_idx + 1] if unit_idx + 1 < len(lines) else ""

                if unit in ("USD/ton", "Cents/gallon", "EUR/ton"):
                    records.append({
                        "product": product,
                        "type": "international",
                        "incoterm": incoterm,
                        "price": price_val,
                        "change": change_val,
                        "unit": unit,
                        "date": date_str,
                    })
                    i = unit_idx + 2
                    continue
            except (ValueError, IndexError):
                pass

        # --- Pattern 4: Multi-line regional (price-curve pages) ---
        # ProductName\nprice\nchange\nYuan/mt\ndate
        if i + 3 < len(lines):
            try:
                price_val = float(lines[i + 1].replace(",", ""))
                # Check if next is change number or unit
                change_val = 0
                unit_idx = i + 2
                try:
                    change_val = float(lines[i + 2].replace(",", ""))
                    unit_idx = i + 3
                except ValueError:
                    pass
                if unit_idx < len(lines) and lines[unit_idx] == "Yuan/mt":
                    date_str = lines[unit_idx + 1] if unit_idx + 1 < len(lines) else ""
                    # Only match if product name looks reasonable (not a number, not too short)
                    if len(line) > 3 and not re.match(r'^[\d,.]+$', line):
                        records.append({
                            "product": line,
                            "type": "regional",
                            "price": price_val,
                            "change": change_val,
                            "unit": "Yuan/mt",
                            "date": date_str,
                        })
                        i = unit_idx + 2
                        continue
            except (ValueError, IndexError):
                pass

        # --- Pattern 5: Single-line regional ---
        regional_match = re.match(
            r'(.+?)\s+([\d,.]+)\s+([\d,.]*)\s*(Yuan/mt)\s+(.+\d{4})',
            line
        )
        if regional_match:
            change_str = regional_match.group(3).strip()
            records.append({
                "product": regional_match.group(1).strip(),
                "type": "regional",
                "price": float(regional_match.group(2).replace(",", "")),
                "change": float(change_str.replace(",", "")) if change_str else 0,
                "unit": "Yuan/mt",
                "date": regional_match.group(5).strip(),
            })
            i += 1
            continue

        i += 1

    return records


def _parse_sunsirs_table(text: str) -> list:
    """
    Parse sunsirs spot price table.

    Format:
      Commodity    Sectors    05-14    05-15    Change
      Toluene    Chemical    6820.00    6931.00    1.63%
    """
    records = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for line in lines:
        # Match: ProductName    Sector    Price1    Price2    Change%
        match = re.match(
            r'(.+?)\t+(Chemical|Energy|Textile|Rubber|Plastic|Building Materials|'
            r'Steel|Non-ferrous metals|Agricultural|Paper)\t+'
            r'([\d,.]+)\t+([\d,.]+)\t+([+-]?[\d,.]+%)',
            line
        )
        if match:
            records.append({
                "product": match.group(1).strip(),
                "sector": match.group(2).strip(),
                "price_prev": float(match.group(3).replace(",", "")),
                "price_latest": float(match.group(4).replace(",", "")),
                "change_pct": match.group(5),
                "unit": "Yuan/mt",
                "source": "sunsirs.com",
            })

    return records


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def _scrape_in_session(port: int) -> dict:
    """
    Single Multilogin session: visit echemi + sunsirs, extract all prices.
    Human-like browsing with random delays.

    Echemi uses Multilogin's own proxy (gate.multilogin.com) because it
    passes the AWS WAF challenge. Bright Data gets blocked.
    Sunsirs uses Bright Data residential proxy.
    """
    all_data = {
        "echemi": {"pages": [], "price_curves": []},
        "sunsirs": {},
        "futures": [],
        "jpx": {},
    }
    error_log = []

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

        # --- ECHEMI: use Multilogin proxy (passes AWS WAF) ---
        echemi_ctx = browser.new_context(
            proxy={
                "server": "http://gate.multilogin.com:8080",
                "username": MLX_PROXY_USER,
                "password": MLX_PROXY_PASS,
            },
            ignore_https_errors=True,
        )
        page = echemi_ctx.new_page()

        try:
            # --- ECHEMI: solve challenge first (with retry) ---
            challenge_passed = False
            for attempt in range(3):
                try:
                    log.info("Opening echemi.com attempt %d/3 (MLX proxy)...", attempt + 1)
                    page.goto("https://www.echemi.com", timeout=60000, wait_until="domcontentloaded")

                    # Wait for AWS WAF challenge to auto-solve (needs ~5-15s)
                    for _ in range(6):
                        time.sleep(random.uniform(4, 6))
                        title = page.title()
                        if title and "verification" not in title.lower() and "human" not in title.lower():
                            challenge_passed = True
                            break
                    if challenge_passed:
                        break
                except Exception as e:
                    log.warning("Echemi attempt %d failed: %s", attempt + 1, str(e)[:80])
                    time.sleep(random.uniform(5, 10))

            if not challenge_passed:
                log.error("Echemi challenge failed after 3 attempts — skipping echemi")
                error_log.append("echemi_challenge_failed")
            else:
                log.info("Echemi challenge passed: %s", title[:60])

                # --- ECHEMI: zyc category pages ---
                for page_info in ECHEMI_PAGES:
                    try:
                        time.sleep(random.uniform(8, 20))
                        log.info("Scraping echemi %s...", page_info["name"])
                        page.goto(page_info["url"], timeout=45000, wait_until="domcontentloaded")
                        page.wait_for_load_state("load", timeout=30000)
                        time.sleep(random.uniform(3, 7))

                        body = page.inner_text("body")
                        records = _parse_price_text(body)

                        all_data["echemi"]["pages"].append({
                            "name": page_info["name"],
                            "url": page_info["url"],
                            "chemicals": page_info["chemicals"],
                            "records": records,
                            "record_count": len(records),
                            "raw_text_preview": body[:2000],
                        })
                        log.info("  %s: %d price records", page_info["name"], len(records))

                    except Exception as e:
                        log.error("  Failed %s: %s", page_info["name"], str(e)[:100])
                        error_log.append(f"echemi_{page_info['name']}: {str(e)[:100]}")

                # --- ECHEMI: individual price-curve pages (international + regional detail) ---
                for chem, url in ECHEMI_PRICE_CURVES.items():
                    try:
                        time.sleep(random.uniform(10, 25))

                        # Re-check challenge before each curve page (WAF may re-trigger)
                        title = page.title()
                        if "verification" in title.lower() or "human" in title.lower() or "confirm" in title.lower():
                            log.info("WAF re-triggered — re-solving challenge...")
                            page.goto("https://www.echemi.com", timeout=60000, wait_until="domcontentloaded")
                            for _ in range(6):
                                time.sleep(random.uniform(4, 6))
                                if "verification" not in page.title().lower() and "human" not in page.title().lower():
                                    break
                            time.sleep(random.uniform(5, 10))

                        log.info("Scraping echemi price curve: %s...", chem)
                        page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        page.wait_for_load_state("load", timeout=30000)
                        time.sleep(random.uniform(3, 7))

                        body = page.inner_text("body")

                        # Check if we got a challenge page instead of data
                        if "confirm you are human" in body.lower() or "security check" in body.lower():
                            log.warning("  %s: got WAF challenge instead of data — skipping", chem)
                            error_log.append(f"echemi_curve_{chem}: WAF challenge on page")
                            continue

                        records = _parse_price_text(body)

                        all_data["echemi"]["price_curves"].append({
                            "chemical": chem,
                            "url": url,
                            "records": records,
                            "record_count": len(records),
                            "raw_text_preview": body[:3000],
                        })
                        log.info("  %s: %d price records", chem, len(records))

                    except Exception as e:
                        log.error("  Failed %s: %s", chem, str(e)[:100])
                        error_log.append(f"echemi_curve_{chem}: {str(e)[:100]}")

        finally:
            page.close()
            echemi_ctx.close()

        # --- SUNSIRS: use Bright Data residential proxy ---
        time.sleep(random.uniform(15, 30))
        sunsirs_ctx = browser.new_context(
            proxy={
                "server": "http://brd.superproxy.io:33335",
                "username": BRD_PROXY_USER,
                "password": BRD_PROXY_PASS,
            },
            ignore_https_errors=True,
        )
        page2 = sunsirs_ctx.new_page()

        try:
            log.info("Scraping sunsirs.com (via Bright Data)...")
            page2.goto("https://www.sunsirs.com/uk/", timeout=45000, wait_until="domcontentloaded")
            page2.wait_for_load_state("load", timeout=30000)
            time.sleep(random.uniform(3, 7))

            body = page2.inner_text("body")
            records = _parse_sunsirs_table(body)

            all_data["sunsirs"] = {
                "url": "https://www.sunsirs.com/uk/",
                "records": records,
                "record_count": len(records),
                "raw_text_preview": body[:3000],
            }
            log.info("  sunsirs: %d price records", len(records))

        except Exception as e:
            log.error("  Failed sunsirs: %s", str(e)[:100])
            error_log.append(f"sunsirs: {str(e)[:100]}")
        finally:
            page2.close()
            sunsirs_ctx.close()

        # --- BARCHART FUTURES: use Bright Data proxy (lighter than CME) ---
        # Barchart.com renders settlement tables in plain HTML unlike CME's
        # heavy React app. Carries all CME energy futures contracts we need.
        time.sleep(random.uniform(15, 30))
        barchart_ctx = browser.new_context(
            proxy={
                "server": "http://brd.superproxy.io:33335",
                "username": BRD_PROXY_USER,
                "password": BRD_PROXY_PASS,
            },
            ignore_https_errors=True,
        )
        barchart_page = barchart_ctx.new_page()

        try:
            # Barchart ticker symbols for CME energy swap futures
            # INO = Naphtha CIF NWE Crack Spread (confirmed working)
            # JKS/JNP tickers don't exist on Barchart — use individual
            # contract month pages for Singapore Jet Kero (JK) and
            # Japan Naphtha (JNA or UN)
            barchart_targets = {
                "naphtha_nwe_crack": {
                    "url": "https://www.barchart.com/futures/quotes/INO*0/futures-prices",
                    "description": "Naphtha CIF NWE Platts Crack Spread — NWE Naphtha proxy for Hexane",
                },
                "singapore_fuel_oil": {
                    "url": "https://www.barchart.com/futures/quotes/UA*0/futures-prices",
                    "description": "Singapore Fuel Oil 180 CST — Asian refinery product proxy",
                },
                "brent_crude_ice": {
                    "url": "https://www.barchart.com/futures/quotes/CB*0/futures-prices",
                    "description": "ICE Brent Crude — benchmark for naphtha/kero spreads",
                },
            }

            all_data["futures"] = []
            for name, info in barchart_targets.items():
                try:
                    time.sleep(random.uniform(10, 20))
                    log.info("Scraping Barchart %s...", name)
                    barchart_page.goto(info["url"], timeout=60000, wait_until="domcontentloaded")
                    barchart_page.wait_for_load_state("load", timeout=30000)
                    time.sleep(random.uniform(5, 8))

                    # Scroll down to trigger lazy-loaded table
                    barchart_page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                    time.sleep(random.uniform(3, 6))
                    barchart_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(random.uniform(3, 6))

                    body = barchart_page.inner_text("body")

                    # First try parsing the table
                    records = _parse_barchart_futures(body, name)

                    # Fallback: at least extract the header price
                    if not records:
                        records = _parse_barchart_header(body, name)

                    all_data["futures"].append({
                        "contract": name,
                        "description": info["description"],
                        "url": info["url"],
                        "records": records,
                        "record_count": len(records),
                        "raw_text_preview": body[:3000],
                    })
                    log.info("  Barchart %s: %d futures records", name, len(records))
                except Exception as e:
                    log.error("  Barchart %s failed: %s", name, str(e)[:100])
                    error_log.append(f"barchart_{name}: {str(e)[:100]}")

        except Exception as e:
            log.error("Barchart scraping failed: %s", str(e)[:100])
            error_log.append(f"barchart_general: {str(e)[:100]}")
        finally:
            barchart_page.close()
            barchart_ctx.close()

        # --- JPX/TOCOM SETTLEMENT CSV: use Multilogin proxy ---
        # JPX publishes daily settlement price CSVs for all energy futures.
        # File pattern: rb_e{YYYYMMDD}.csv — contains Kerosene, Gasoline,
        # Gas Oil, Platts Dubai Crude futures with all contract months.
        # Bright Data can't tunnel to jpx.co.jp, so we use MLX proxy.
        time.sleep(random.uniform(10, 20))
        jpx_ctx = browser.new_context(
            proxy={
                "server": "http://gate.multilogin.com:8080",
                "username": MLX_PROXY_USER,
                "password": MLX_PROXY_PASS,
            },
            ignore_https_errors=True,
        )
        jpx_page = jpx_ctx.new_page()

        try:
            # Try today's CSV, then previous days (may not be published yet,
            # weekends/holidays have no data)
            today_dt = datetime.now(timezone.utc)
            dates_to_try = [
                (today_dt - timedelta(days=i)).strftime("%Y%m%d")
                for i in range(5)  # Try today through 4 days ago
            ]

            jpx_records = []
            jpx_csv_url = None

            # JPX energy settlement page — navigate here first to get
            # the latest available CSV download link
            try:
                log.info("Navigating to JPX settlement page...")
                jpx_page.goto(
                    "https://www.jpx.co.jp/english/markets/derivatives/settlement-price/",
                    timeout=30000, wait_until="domcontentloaded",
                )
                time.sleep(random.uniform(3, 6))

                # Find the latest CSV download link (rb_eYYYYMMDD.csv)
                links = jpx_page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href*="rb_e"]'))
                        .map(a => ({href: a.href, text: a.innerText}))
                        .slice(0, 5)
                """)
                log.info("  Found %d CSV links", len(links))

                for link in links:
                    csv_url = link["href"]
                    log.info("  Downloading: %s", csv_url.split("/")[-1])
                    try:
                        # Click the link to trigger download (goto fails on download URLs)
                        with jpx_page.expect_download(timeout=30000) as dl_info:
                            jpx_page.click('a[href*="rb_e"]', timeout=10000)
                        download = dl_info.value
                        tmp_path = "/tmp/jpx_settlement.csv"
                        download.save_as(tmp_path)
                        # Try multiple encodings (JPX uses Shift-JIS)
                        csv_text = ""
                        for enc in ["utf-8-sig", "shift_jis", "cp932"]:
                            try:
                                with open(tmp_path, "r", encoding=enc) as f:
                                    csv_text = f.read()
                                break
                            except UnicodeDecodeError:
                                continue
                        os.remove(tmp_path)

                        # Extract date from filename
                        import re as _re
                        date_match = _re.search(r"rb_e(\d{8})", csv_url)
                        d = date_match.group(1) if date_match else ""

                        jpx_records = _parse_jpx_csv(csv_text, d)
                        jpx_csv_url = csv_url
                        log.info("  JPX CSV: %d energy records", len(jpx_records))
                        break
                    except Exception as e:
                        log.warning("  JPX download failed: %s", str(e)[:80])
            except Exception as e:
                log.warning("  JPX settlement page failed: %s", str(e)[:80])

            all_data["jpx"] = {
                "url": jpx_csv_url or "https://www.jpx.co.jp/english/markets/derivatives/settlement-price/",
                "records": jpx_records,
                "record_count": len(jpx_records),
            }

        except Exception as e:
            log.error("  JPX failed: %s", str(e)[:100])
            error_log.append(f"jpx: {str(e)[:100]}")
        finally:
            jpx_page.close()
            jpx_ctx.close()

        browser.close()

    all_data["errors"] = error_log
    return all_data


# ---------------------------------------------------------------------------
# EIA API — proper REST API, no Multilogin needed, goes through Bright Data
# ---------------------------------------------------------------------------

EIA_API_KEY = "DEMO_KEY"  # Free tier, 1000 req/day — sufficient for daily scrape

EIA_SERIES = {
    "jet_kero_usgc": {
        "product": "EPJK",
        "description": "US Gulf Coast Kerosene-Type Jet Fuel Spot Price FOB",
        "relevance": "MOPS Kero proxy for Normal Paraffin formula",
    },
    "brent_crude": {
        "product": "EPCBRENT",
        "description": "UK Brent Crude Oil Spot Price FOB",
        "relevance": "Crude benchmark for naphtha/kero spread",
    },
    "wti_crude": {
        "product": "EPCWTI",
        "description": "WTI Crude Oil Spot Price",
        "relevance": "US crude benchmark",
    },
    "heating_oil": {
        "product": "EPD2F",
        "description": "No 2 Fuel Oil / Heating Oil Spot Price",
        "relevance": "Gasoil proxy",
    },
}


def fetch_eia_prices() -> dict:
    """Fetch daily spot prices from EIA API — direct, no proxy.

    EIA is a proper government REST API with an API key, not scraping.
    Bright Data blocks api.eia.gov, so we route direct.
    """
    results = {}

    for name, info in EIA_SERIES.items():
        try:
            url = (
                f"https://api.eia.gov/v2/petroleum/pri/spt/data/"
                f"?api_key={EIA_API_KEY}"
                f"&frequency=daily"
                f"&data[0]=value"
                f"&facets[product][]={info['product']}"
                f"&sort[0][column]=period"
                f"&sort[0][direction]=desc"
                f"&length=10"
            )
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            rows = data.get("response", {}).get("data", [])
            records = []
            for row in rows:
                records.append({
                    "date": row.get("period"),
                    "product": row.get("product-name", info["description"]),
                    "series": row.get("series"),
                    "price": float(row["value"]) if row.get("value") else None,
                    "unit": row.get("units", ""),
                    "area": row.get("duoarea", ""),
                })

            results[name] = {
                "description": info["description"],
                "relevance": info["relevance"],
                "records": records,
                "record_count": len(records),
            }
            log.info("  EIA %s: %d records (latest: %s)",
                     name, len(records),
                     records[0]["price"] if records else "none")

        except Exception as e:
            log.error("  EIA %s failed: %s", name, str(e)[:100])
            results[name] = {"error": str(e)[:200], "record_count": 0}

    return results


# ---------------------------------------------------------------------------
# CME/JPX parsers
# ---------------------------------------------------------------------------

def _parse_barchart_futures(text: str, contract_name: str) -> list:
    """
    Parse Barchart.com futures prices page.

    Barchart renders settlement data in plain HTML tables. Format:
      Contract    Last    Change    Open    High    Low    Previous    Volume
      Jun '26    68.50    +0.25    68.20    69.00    68.00    68.25    1234
    """
    records = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Month abbreviation pattern for contract rows
    month_re = re.compile(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*[''`]?\s*(\d{2,4})$",
        re.IGNORECASE,
    )

    i = 0
    while i < len(lines):
        m = month_re.match(lines[i])
        if m:
            contract_month = lines[i]
            # Collect numeric values from following lines
            prices = []
            for j in range(i + 1, min(i + 12, len(lines))):
                cleaned = lines[j].replace(",", "").replace("+", "").replace("s", "").strip()
                if month_re.match(lines[j]):
                    break
                try:
                    prices.append(float(cleaned))
                except ValueError:
                    if lines[j].strip() in ("-", "unch", ""):
                        prices.append(None)

            if prices:
                record = {
                    "contract_month": contract_month,
                    "contract": contract_name,
                    "source": "barchart.com",
                }
                # Barchart order: Last, Change, Open, High, Low, Previous, Volume
                if len(prices) >= 1 and prices[0] is not None:
                    record["last"] = prices[0]
                if len(prices) >= 4 and prices[3] is not None:
                    record["high"] = prices[3]
                if len(prices) >= 5 and prices[4] is not None:
                    record["low"] = prices[4]
                if len(prices) >= 6 and prices[5] is not None:
                    record["prior_settle"] = prices[5]
                records.append(record)
        i += 1

    # Also try tab-separated rows (Barchart sometimes renders these)
    if not records:
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 4:
                m2 = re.match(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*[''`]?\s*\d{2,4}",
                    parts[0], re.IGNORECASE,
                )
                if m2:
                    try:
                        record = {
                            "contract_month": parts[0].strip(),
                            "contract": contract_name,
                            "source": "barchart.com",
                            "last": float(parts[1].replace(",", "")) if parts[1].strip() not in ("-", "") else None,
                        }
                        records.append(record)
                    except ValueError:
                        pass

    return records


def _parse_barchart_header(text: str, contract_name: str) -> list:
    """
    Fallback: extract the header price from a Barchart futures page.

    Header format:
      Naphtha Platts Cargoes CIF NWE ... May '26 (INOK26)
      -4.098 -1.181 (-40.49%) 05/15/26 [NYMEX]
    """
    records = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for i, line in enumerate(lines):
        # Match ticker pattern like (INOK26) or (UAM26)
        ticker_match = re.search(r'\(([A-Z]{2,4}[A-Z]\d{2})\)', line)
        if ticker_match and i + 1 < len(lines):
            ticker = ticker_match.group(1)
            # Extract month from the line — e.g. "May '26"
            month_match = re.search(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*[''`]?\s*(\d{2,4})",
                line, re.IGNORECASE,
            )
            contract_month = month_match.group(0) if month_match else ticker

            # Next line should have the price
            price_line = lines[i + 1]
            price_match = re.match(r'([+-]?[\d,.]+)', price_line)
            if price_match:
                try:
                    price = float(price_match.group(1).replace(",", ""))
                    # Extract change
                    change_match = re.search(r'([+-][\d,.]+)\s*\(', price_line)
                    change = float(change_match.group(1).replace(",", "")) if change_match else 0
                    # Extract date
                    date_match = re.search(r'(\d{2}/\d{2}/\d{2,4})', price_line)

                    records.append({
                        "contract_month": contract_month,
                        "contract": contract_name,
                        "ticker": ticker,
                        "last": price,
                        "change": change,
                        "date": date_match.group(1) if date_match else "",
                        "source": "barchart.com (header)",
                    })
                except ValueError:
                    pass
            break  # Only one header per page

    return records


def _parse_jpx_csv(csv_text: str, date_str: str) -> list:
    """
    Parse JPX settlement price CSV.

    JPX CSV format:
      Issue Code,Issue Name,Put/Call,Contract Month,Strike Price,Settlement Price,...
      601060018,FUT_GASO_260611,,202606,,71200,...
      602060018,FUT_KERO_260611,,202606,,68500,...
      603060018,FUT_GASOIL_260611,,202606,,63400,...
      605060018,FUT_DUBAI_260611,,202606,,52300,...

    Energy product codes in Issue Name:
      GASO = Gasoline, KERO = Kerosene, GASOIL = Gas Oil,
      DUBAI = Platts Dubai Crude, NAPHTHA = Naphtha
    """
    records = []
    lines = [l.strip() for l in csv_text.split("\n") if l.strip()]

    # Energy products we want — match on Underlying Name (last column)
    # or on Issue Name codes
    energy_keywords = [
        "kerosene", "gasoline", "gas oil", "gasoil", "dubai crude",
        "crude oil", "naphtha",
    ]
    # Also match by Issue Name code prefix
    energy_codes = ["FUT_KRO", "FUT_GAS", "FUT_DBAI", "FUT_GASOIL", "FUT_NAPHTHA"]

    for line in lines:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 6:
            continue

        issue_name = parts[1] if len(parts) > 1 else ""
        # Underlying Name is the last column
        underlying = parts[-1].strip() if parts[-1].strip() else ""

        # Check if this is an energy futures product
        is_energy = False
        if any(kw in underlying.lower() for kw in energy_keywords):
            is_energy = True
        elif any(code in issue_name.upper() for code in energy_codes):
            is_energy = True

        if not is_energy:
            continue

        # Extract settlement price (column 5)
        try:
            settlement = float(parts[5].replace(",", ""))
        except (ValueError, IndexError):
            continue

        # Skip zero/empty settlement prices
        if settlement == 0:
            continue

        # Extract contract month (column 3, format: YYYYMM)
        contract_month = parts[3] if len(parts) > 3 else ""

        # Use readable product name from Underlying Name column
        product_name = underlying if underlying else issue_name

        record = {
            "product": product_name,
            "issue_code": parts[0],
            "issue_name": issue_name,
            "contract_month": contract_month,
            "settlement_price": settlement,
            "unit": "Yen/kl",
            "date": date_str,
            "source": "JPX/TOCOM",
        }
        records.append(record)

    return records


def scrape_all(profile_id: str) -> dict:
    """Run the full scrape: EIA API first, then Multilogin session for websites."""

    # --- EIA API (no Multilogin, proper API through Bright Data proxy) ---
    log.info("Fetching EIA API prices...")
    eia_data = fetch_eia_prices()

    # --- Multilogin session for websites ---
    result = {}
    error = None

    def _run():
        nonlocal result, error
        try:
            result = _scrape_in_session(port)
        except Exception as e:
            error = e

    token = _get_token()
    port = _launch_profile(token, profile_id)
    log.info("Multilogin profile launched on port %d", port)

    try:
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=900)  # 15 min max (added CME + JPX)

        if t.is_alive():
            log.error("Scrape session TIMED OUT (900s)")
            raise RuntimeError("Scrape session timed out")
        if error:
            raise error
    finally:
        _stop_profile(profile_id)

    result["eia"] = eia_data
    return result


# ---------------------------------------------------------------------------
# Blob upload
# ---------------------------------------------------------------------------

def upload_to_blob(data: dict, blob_name: str) -> str:
    """Upload JSON to stcrawlosint/osint-staging via REST API + SAS token."""
    blob_url = (
        f"https://{BLOB_ACCOUNT}.blob.core.windows.net"
        f"/{BLOB_CONTAINER}/{blob_name}?{BLOB_SAS_TOKEN}"
    )
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

    # Blob upload goes through Bright Data proxy too
    resp = requests.put(
        blob_url,
        data=body,
        headers={
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": "application/json; charset=utf-8",
        },
        proxies={
            "https": f"http://{BRD_PROXY_USER}:{BRD_PROXY_PASS}@brd.superproxy.io:33335",
            "http": f"http://{BRD_PROXY_USER}:{BRD_PROXY_PASS}@brd.superproxy.io:33335",
        },
        timeout=30,
    )
    if resp.status_code in (200, 201):
        log.info("Uploaded blob: %s (%d bytes)", blob_name, len(body))
        return blob_url.split("?")[0]
    else:
        log.error("Blob upload failed (%d): %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"Blob upload failed: {resp.status_code}")


# ---------------------------------------------------------------------------
# PostgreSQL write
# ---------------------------------------------------------------------------

def _get_db_conn():
    """Get PostgreSQL connection to crawl-monitor-db."""
    import psycopg2
    return psycopg2.connect(
        host=DB_HOST, dbname=DB_NAME, user=DB_USER,
        password=DB_PASSWORD, sslmode="require",
        connect_timeout=10,
    )


def _classify_price(record: dict, source: str) -> dict | None:
    """Map a scraped record to daily_prices columns."""
    product = record.get("product", "")
    price = record.get("price") or record.get("price_latest")
    if not product or price is None:
        return None

    # Determine currency and unit
    unit = record.get("unit", "")
    if "Yuan" in unit or "CNY" in unit:
        currency = "CNY"
    elif "Yen" in unit or "JPY" in unit:
        currency = "JPY"
    elif "EUR" in unit:
        currency = "EUR"
    else:
        currency = "USD"

    # Normalize unit
    norm_unit = unit.replace("Yuan/mt", "/mt").replace("USD/ton", "/mt") \
                    .replace("Cents/gallon", "/gal").replace("EUR/ton", "/mt")
    if not norm_unit:
        norm_unit = record.get("units", "/mt")

    # Price type
    ptype = record.get("type", "spot")
    if source == "sunsirs":
        ptype = "spot"

    # Region
    region = record.get("area") or record.get("incoterm") or record.get("sector")
    if record.get("type") == "china_domestic":
        region = "China Domestic"
    elif record.get("type") == "international":
        region = record.get("incoterm", "International")
    elif record.get("type") == "regional":
        region = "China Regional"

    # USD/MT normalization (approximate)
    price_usd_mt = None
    if currency == "USD" and "/mt" in norm_unit:
        price_usd_mt = float(price)
    elif currency == "USD" and "/gal" in norm_unit:
        price_usd_mt = float(price) * 317.0  # ~317 gal/mt for kero
    elif currency == "USD" and "/bbl" in norm_unit:
        price_usd_mt = float(price) * 7.33   # ~7.33 bbl/mt for crude

    return {
        "source": source,
        "product": product,
        "region": region,
        "price_type": ptype,
        "price": float(price),
        "currency": currency,
        "unit": unit,
        "price_usd_mt": price_usd_mt,
        "change_amount": record.get("change") or record.get("change_amount"),
        "change_pct": None,
        "incoterm": record.get("incoterm"),
        "raw_date": record.get("date", ""),
    }


def write_to_postgres(output: dict) -> int:
    """Write scraped data to daily_prices and futures_prices tables.

    Uses INSERT ... ON CONFLICT DO UPDATE to handle re-runs on the same day.
    Returns total rows written.
    """
    if not DB_PASSWORD:
        log.warning("DB_PASSWORD not set — skipping PostgreSQL write")
        return 0

    try:
        conn = _get_db_conn()
    except Exception as e:
        log.error("PostgreSQL connection failed: %s", str(e)[:100])
        return 0

    scrape_date = output.get("scrape_date", datetime.now(timezone.utc).strftime("%Y%m%d"))
    # Format as YYYY-MM-DD for PostgreSQL
    scrape_date_pg = f"{scrape_date[:4]}-{scrape_date[4:6]}-{scrape_date[6:8]}"

    daily_rows = 0
    futures_rows = 0

    try:
        cur = conn.cursor()

        # --- daily_prices: echemi pages ---
        for page in output.get("echemi", {}).get("pages", []):
            for rec in page.get("records", []):
                row = _classify_price(rec, "echemi")
                if not row:
                    continue
                try:
                    cur.execute("""
                        INSERT INTO daily_prices
                            (scrape_date, source, product, region, price_type,
                             price, currency, unit, price_usd_mt,
                             change_amount, change_pct, incoterm, raw_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (scrape_date, source, product, region, price_type)
                        DO UPDATE SET price = EXCLUDED.price,
                                      price_usd_mt = EXCLUDED.price_usd_mt,
                                      change_amount = EXCLUDED.change_amount,
                                      raw_date = EXCLUDED.raw_date
                    """, (scrape_date_pg, row["source"], row["product"],
                          row["region"], row["price_type"], row["price"],
                          row["currency"], row["unit"], row["price_usd_mt"],
                          row["change_amount"], row["change_pct"],
                          row["incoterm"], row["raw_date"]))
                    daily_rows += 1
                except Exception as e:
                    log.debug("daily_prices insert skip: %s", str(e)[:80])

        # --- daily_prices: echemi price curves ---
        for curve in output.get("echemi", {}).get("price_curves", []):
            for rec in curve.get("records", []):
                row = _classify_price(rec, "echemi")
                if not row:
                    continue
                try:
                    cur.execute("""
                        INSERT INTO daily_prices
                            (scrape_date, source, product, region, price_type,
                             price, currency, unit, price_usd_mt,
                             change_amount, change_pct, incoterm, raw_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (scrape_date, source, product, region, price_type)
                        DO UPDATE SET price = EXCLUDED.price,
                                      price_usd_mt = EXCLUDED.price_usd_mt,
                                      change_amount = EXCLUDED.change_amount,
                                      raw_date = EXCLUDED.raw_date
                    """, (scrape_date_pg, row["source"], row["product"],
                          row["region"], row["price_type"], row["price"],
                          row["currency"], row["unit"], row["price_usd_mt"],
                          row["change_amount"], row["change_pct"],
                          row["incoterm"], row["raw_date"]))
                    daily_rows += 1
                except Exception as e:
                    log.debug("daily_prices insert skip: %s", str(e)[:80])

        # --- daily_prices: sunsirs ---
        for rec in output.get("sunsirs", {}).get("records", []):
            try:
                cur.execute("""
                    INSERT INTO daily_prices
                        (scrape_date, source, product, region, price_type,
                         price, currency, unit, price_usd_mt,
                         change_amount, change_pct, incoterm, raw_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (scrape_date, source, product, region, price_type)
                    DO UPDATE SET price = EXCLUDED.price, change_pct = EXCLUDED.change_pct
                """, (scrape_date_pg, "sunsirs", rec.get("product", ""),
                      rec.get("sector", ""), "spot",
                      rec.get("price_latest", 0), "CNY", "Yuan/mt", None,
                      None, None, None, ""))
                daily_rows += 1
            except Exception as e:
                log.debug("sunsirs insert skip: %s", str(e)[:80])

        # --- daily_prices: EIA ---
        for series_name, series_data in output.get("eia", {}).items():
            if not isinstance(series_data, dict):
                continue
            for rec in series_data.get("records", []):
                if rec.get("price") is None:
                    continue
                unit = rec.get("unit", "")
                price_usd_mt = None
                if "GAL" in unit.upper():
                    price_usd_mt = float(rec["price"]) * 317.0
                elif "BBL" in unit.upper():
                    price_usd_mt = float(rec["price"]) * 7.33
                try:
                    cur.execute("""
                        INSERT INTO daily_prices
                            (scrape_date, source, product, region, price_type,
                             price, currency, unit, price_usd_mt,
                             change_amount, change_pct, incoterm, raw_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (scrape_date, source, product, region, price_type)
                        DO UPDATE SET price = EXCLUDED.price, price_usd_mt = EXCLUDED.price_usd_mt
                    """, (rec.get("date", scrape_date_pg), "eia",
                          rec.get("product", series_name),
                          rec.get("area", ""), "spot",
                          rec["price"], "USD", unit, price_usd_mt,
                          None, None, None, rec.get("date", "")))
                    daily_rows += 1
                except Exception as e:
                    log.debug("eia insert skip: %s", str(e)[:80])

        # --- futures_prices: barchart ---
        for contract in output.get("futures", []):
            for rec in contract.get("records", []):
                price = rec.get("last") or rec.get("settlement_price")
                if price is None:
                    continue
                try:
                    cur.execute("""
                        INSERT INTO futures_prices
                            (scrape_date, source, product, contract_month,
                             settlement_price, currency, unit, price_usd_mt,
                             ticker, exchange)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (scrape_date, source, product, contract_month)
                        DO UPDATE SET settlement_price = EXCLUDED.settlement_price
                    """, (scrape_date_pg, "barchart",
                          contract.get("description", rec.get("contract", "")),
                          rec.get("contract_month", ""),
                          price, "USD", "USD/mt", float(price),
                          rec.get("ticker", ""), "NYMEX"))
                    futures_rows += 1
                except Exception as e:
                    log.debug("barchart futures insert skip: %s", str(e)[:80])

        # --- futures_prices: JPX ---
        for rec in output.get("jpx", {}).get("records", []):
            price = rec.get("settlement_price")
            if price is None:
                continue
            try:
                cur.execute("""
                    INSERT INTO futures_prices
                        (scrape_date, source, product, contract_month,
                         settlement_price, currency, unit, price_usd_mt,
                         ticker, exchange)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (scrape_date, source, product, contract_month)
                    DO UPDATE SET settlement_price = EXCLUDED.settlement_price
                """, (scrape_date_pg, "jpx",
                      rec.get("product", ""),
                      rec.get("contract_month", ""),
                      price, "JPY", "Yen/kl", None,
                      rec.get("issue_name", ""), "JPX/TOCOM"))
                futures_rows += 1
            except Exception as e:
                log.debug("jpx futures insert skip: %s", str(e)[:80])

        conn.commit()
        log.info("PostgreSQL: %d daily_prices + %d futures_prices written",
                 daily_rows, futures_rows)

    except Exception as e:
        log.error("PostgreSQL write failed: %s", str(e)[:200])
        conn.rollback()
    finally:
        conn.close()

    return daily_rows + futures_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv

    if not MLX_EMAIL or not MLX_PASSWORD:
        log.error("MULTILOGIN_EMAIL / MULTILOGIN_PASSWORD not set")
        sys.exit(1)
    if not POOL_PROFILE_IDS:
        log.error("MULTILOGIN_POOL_PROFILES not set or empty")
        sys.exit(1)
    if not BLOB_SAS_TOKEN and not dry_run:
        log.error("BLOB_SAS_TOKEN not set (use --dry-run to skip upload)")
        sys.exit(1)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    log.info("=== Petrochem scraper starting (%s) ===", today)

    # Use the last profile in the pool (least likely to conflict with verify-gateway)
    profile_id = POOL_PROFILE_IDS[-1]

    raw_data = scrape_all(profile_id)

    # Build output
    output = {
        "scrape_date": today,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "echemi": raw_data.get("echemi", {}),
        "sunsirs": raw_data.get("sunsirs", {}),
        "eia": raw_data.get("eia", {}),
        "futures": raw_data.get("futures", []),
        "jpx": raw_data.get("jpx", {}),
        "errors": raw_data.get("errors", []),
    }

    # Count totals
    echemi_records = sum(
        p.get("record_count", 0)
        for p in output["echemi"].get("pages", [])
    ) + sum(
        p.get("record_count", 0)
        for p in output["echemi"].get("price_curves", [])
    )
    sunsirs_records = output["sunsirs"].get("record_count", 0)
    eia_records = sum(v.get("record_count", 0) for v in output["eia"].values() if isinstance(v, dict))
    futures_records = sum(c.get("record_count", 0) for c in output["futures"])
    jpx_records = output["jpx"].get("record_count", 0) if isinstance(output["jpx"], dict) else 0
    output["total_records"] = echemi_records + sunsirs_records + eia_records + futures_records + jpx_records

    log.info(
        "Scrape complete: echemi=%d, sunsirs=%d, eia=%d, futures=%d, jpx=%d, errors=%d",
        echemi_records, sunsirs_records, eia_records, futures_records, jpx_records,
        len(output["errors"]),
    )

    # Save locally
    local_path = Path(f"/home/copapadmin/petrochem_{today}.json")
    local_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info("Saved locally: %s", local_path)

    # Upload to blob
    if not dry_run:
        blob_name = f"prices/petrochem/daily_{today}.json"
        try:
            upload_to_blob(output, blob_name)
        except Exception as e:
            log.error("Blob upload failed: %s", e)

    # Write to PostgreSQL
    if not dry_run:
        try:
            write_to_postgres(output)
        except Exception as e:
            log.error("PostgreSQL write failed: %s", e)

    # Summary
    for p in output["echemi"].get("pages", []):
        log.info("  echemi/%-20s  %d records", p["name"], p["record_count"])
    for p in output["echemi"].get("price_curves", []):
        log.info("  echemi/curve/%-15s  %d records", p["chemical"], p["record_count"])
    if output["sunsirs"]:
        log.info("  sunsirs                     %d records", sunsirs_records)
    for name, data in output["eia"].items():
        if isinstance(data, dict):
            log.info("  eia/%-22s  %d records", name, data.get("record_count", 0))
    for c in output["futures"]:
        log.info("  futures/%-19s  %d records", c["contract"], c["record_count"])
    if jpx_records:
        log.info("  jpx                         %d records", jpx_records)

    log.info("=== Petrochem scraper done ===")


if __name__ == "__main__":
    main()
