"""
Search trade intelligence platforms for haulier/clearing agent details.
Navigate one site at a time with proper resets between pages.
"""

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("haulier2")

import os


def get_secret(name: str) -> str:
    try:
        result = subprocess.run(
            ["az", "keyvault", "secret", "show", "--vault-name", "crawlkeyvault",
             "--name", name, "--query", "value", "-o", "tsv"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip()
    except Exception:
        return os.environ.get(name.upper().replace("-", "_"), "")


MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
MLX_PASSWORD = get_secret("multilogin-password")
MLX_FOLDER_ID = get_secret("multilogin-folder-id")
MLX_PROXY_USER = get_secret("multilogin-proxy-user")
MLX_PROXY_PASS = get_secret("multilogin-proxy-pass")
CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

OUTPUT_DIR = Path("/home/copapadmin/crawl/output/investigations/super-save-general-trading")
BL_NUMBER = "MEDUFX870746"
CONTAINER_SAMPLE = "DFSU6580527"


def get_token() -> str:
    resp = requests.post(
        "https://api.multilogin.com/user/signin",
        json={"email": MLX_EMAIL, "password": hashlib.md5(MLX_PASSWORD.encode()).hexdigest()},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX sign-in failed: {data['status']['message']}")
    return data["data"]["token"]


def create_profile_no_proxy(token: str, name: str) -> str:
    resp = requests.post(
        "https://api.multilogin.com/profile/create",
        json={"name": name, "browser_type": "mimic", "folder_id": MLX_FOLDER_ID,
              "parameters": {"fingerprint": {},
                  **({"proxy": {"type": "http", "host": "gate.multilogin.com", "port": 8080,
                      "username": MLX_PROXY_USER, "password": MLX_PROXY_PASS}}
                     if MLX_PROXY_USER and MLX_PROXY_PASS else {})}},
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = resp.json()
    if "data" not in data or "ids" not in data["data"]:
        raise RuntimeError(f"Profile create failed: {data}")
    return data["data"]["ids"][0]


def launch_profile(token: str, profile_id: str) -> int:
    url = (f"https://launcher.mlx.yt:45001/api/v2/profile"
           f"/f/{MLX_FOLDER_ID}/p/{profile_id}"
           f"/start?automation_type=playwright&headless_mode=true")
    resp = requests.get(url, headers={"Accept": "application/json",
                                       "Authorization": f"Bearer {token}"},
                        verify=False, timeout=90)
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX launch failed: {data['status']['message']}")
    return int(data["data"]["port"])


def stop_profile(profile_id: str):
    try:
        subprocess.run([str(CLI_PATH), "profile-stop", "--profile-id", profile_id],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def delete_profile(token: str, profile_id: str):
    try:
        requests.delete(f"https://api.multilogin.com/profile/delete",
                        json={"ids": [profile_id], "permanently": True},
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        timeout=15)
    except Exception:
        pass


def safe_navigate(page, url, wait=6):
    """Navigate with proper error handling and reset."""
    try:
        page.goto("about:blank", timeout=5000)
        time.sleep(1)
    except Exception:
        pass
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(wait)
    return page.evaluate("() => document.body ? document.body.innerText : ''") or ""


def main():
    log.info("=== Haulier Detail Search (v2) ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    profile_id = create_profile_no_proxy(token, "haulier-v2")
    log.info(f"Profile: {profile_id}")

    all_results = {}

    try:
        port = launch_profile(token, profile_id)
        log.info(f"CDP port: {port}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(45000)

            # ---------------------------------------------------------------
            # 1. ExportGenius — UAE import records
            # ---------------------------------------------------------------
            log.info("\n[1] ExportGenius")
            try:
                text = safe_navigate(page, "https://www.exportgenius.in/search?q=super+save+general+trading+dubai")
                page.screenshot(path=str(OUTPUT_DIR / "haulier_exportgenius.png"), full_page=True)
                all_results["exportgenius"] = text[:5000]
                log.info(f"  ExportGenius: {len(text)} chars")
            except Exception as e:
                all_results["exportgenius"] = f"ERROR: {e}"
                log.warning(f"  ExportGenius: {e}")

            # ---------------------------------------------------------------
            # 2. Volza — UAE import shipment records
            # ---------------------------------------------------------------
            log.info("\n[2] Volza - company search")
            try:
                text = safe_navigate(page, "https://www.volza.com/p/super-save-general-trading-llc/import/import-in-united-arab-emirates/")
                page.screenshot(path=str(OUTPUT_DIR / "haulier_volza.png"), full_page=True)
                all_results["volza"] = text[:8000]
                log.info(f"  Volza: {len(text)} chars")
            except Exception as e:
                all_results["volza"] = f"ERROR: {e}"

            # ---------------------------------------------------------------
            # 3. Volza — BL number search
            # ---------------------------------------------------------------
            log.info("\n[3] Volza - BL search")
            try:
                text = safe_navigate(page, f"https://www.volza.com/p/bl-{BL_NUMBER.lower()}/import/import-in-united-arab-emirates/")
                page.screenshot(path=str(OUTPUT_DIR / "haulier_volza_bl.png"), full_page=True)
                all_results["volza_bl"] = text[:8000]
                log.info(f"  Volza BL: {len(text)} chars")
            except Exception as e:
                all_results["volza_bl"] = f"ERROR: {e}"

            # ---------------------------------------------------------------
            # 4. ImportGenius - search for Super Save
            # ---------------------------------------------------------------
            log.info("\n[4] ImportGenius")
            try:
                text = safe_navigate(page, "https://www.importgenius.com/importers/super-save-general-trading")
                page.screenshot(path=str(OUTPUT_DIR / "haulier_importgenius.png"), full_page=True)
                all_results["importgenius"] = text[:5000]
                log.info(f"  ImportGenius: {len(text)} chars")
            except Exception as e:
                all_results["importgenius"] = f"ERROR: {e}"

            # ---------------------------------------------------------------
            # 5. ShipsGo — container tracking (may show more events)
            # ---------------------------------------------------------------
            log.info("\n[5] ShipsGo")
            try:
                text = safe_navigate(page, f"https://shipsgo.com/container-tracking/{CONTAINER_SAMPLE}", wait=10)
                page.screenshot(path=str(OUTPUT_DIR / "haulier_shipsgo.png"), full_page=True)
                all_results["shipsgo"] = text[:5000]
                log.info(f"  ShipsGo: {len(text)} chars")
            except Exception as e:
                all_results["shipsgo"] = f"ERROR: {e}"

            # ---------------------------------------------------------------
            # 6. Searates — container tracking
            # ---------------------------------------------------------------
            log.info("\n[6] Searates")
            try:
                text = safe_navigate(page, f"https://www.searates.com/container/tracking/?number={CONTAINER_SAMPLE}", wait=10)
                page.screenshot(path=str(OUTPUT_DIR / "haulier_searates.png"), full_page=True)
                all_results["searates"] = text[:5000]
                log.info(f"  Searates: {len(text)} chars")
            except Exception as e:
                all_results["searates"] = f"ERROR: {e}"

            # ---------------------------------------------------------------
            # 7. JAFZA company search
            # ---------------------------------------------------------------
            log.info("\n[7] JAFZA")
            try:
                text = safe_navigate(page, "https://www.jafza.ae/companies/?search=super+save")
                page.screenshot(path=str(OUTPUT_DIR / "haulier_jafza.png"), full_page=True)
                all_results["jafza"] = text[:5000]
                log.info(f"  JAFZA: {len(text)} chars")
            except Exception as e:
                all_results["jafza"] = f"ERROR: {e}"

            # ---------------------------------------------------------------
            # 8. MSC myMSC - try to get more detail (may need login)
            # ---------------------------------------------------------------
            log.info("\n[8] MSC myMSC / detailed tracking")
            try:
                text = safe_navigate(page, f"https://www.msc.com/track-a-shipment?agencyPath=msc&trackingNumber={BL_NUMBER}&trackingType=billOfLading", wait=12)
                page.screenshot(path=str(OUTPUT_DIR / "haulier_msc_detailed.png"), full_page=True)
                all_results["msc_detailed"] = text[:8000]
                log.info(f"  MSC detailed: {len(text)} chars")
            except Exception as e:
                all_results["msc_detailed"] = f"ERROR: {e}"

            page.close()

        # Save
        with open(OUTPUT_DIR / "haulier_search_v2.json", "w") as f:
            json.dump(all_results, f, indent=2, default=str)

        # Print findings
        print("\n" + "=" * 80)
        print("HAULIER / TRADE DATA SEARCH RESULTS")
        print("=" * 80)

        keywords = ["haulier", "transport", "truck", "delivery", "gate out", "gate-out",
                     "clearing", "freight forwarder", "notify", "agent", "super save",
                     "MEDUFX", "DFSU", "consignee", "importer", "customs broker",
                     "suzano", "paper", "board", "jebel ali", "container"]

        for source, text in all_results.items():
            if isinstance(text, str) and not text.startswith("ERROR"):
                found_kws = [kw for kw in keywords if kw.lower() in text.lower()]
                if found_kws:
                    print(f"\n--- {source} ({len(text)} chars, keywords: {', '.join(found_kws)}) ---")
                    for line in text.split('\n'):
                        if any(kw.lower() in line.lower() for kw in found_kws):
                            print(f"  {line.strip()[:200]}")
                else:
                    print(f"\n--- {source} ({len(text)} chars) — no relevant keywords ---")
            else:
                print(f"\n--- {source} — {str(text)[:100]} ---")

        print("\n" + "=" * 80)

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Profile stopped and deleted")


if __name__ == "__main__":
    main()
