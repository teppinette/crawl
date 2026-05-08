"""
Track containers via Cargoes.com (DP World) — switch to Container Number mode.
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
log = logging.getLogger("cargoes")

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

CONTAINERS = [
    "DFSU6580527", "FSCU8108794", "MEDU4958469", "MEDU7419218",
    "MSDU6153938", "MSDU7384361", "MSMU7788246", "MSNU5478137",
    "MSNU5529424", "MSNU6792965", "MSNU7760767", "MSNU9153090",
    "MSNU9166158", "SEKU6825445", "SEKU6842139", "TCNU7279232",
    "TCNU8790592", "TGBU5852746", "TIIU4035545", "TXGU8624573",
]

BL_NUMBER = "MEDUFX870746"


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
    profile_json = {
        "name": name,
        "browser_type": "mimic",
        "folder_id": MLX_FOLDER_ID,
        "parameters": {"fingerprint": {}},
    }
    if MLX_PROXY_USER and MLX_PROXY_PASS:
        profile_json["parameters"]["proxy"] = {
            "type": "http",
            "host": "gate.multilogin.com",
            "port": 8080,
            "username": MLX_PROXY_USER,
            "password": MLX_PROXY_PASS,
        }
    resp = requests.post(
        "https://api.multilogin.com/profile/create",
        json=profile_json,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        timeout=30,
    )
    data = resp.json()
    if "data" not in data or "ids" not in data["data"]:
        raise RuntimeError(f"Profile create failed: {data}")
    return data["data"]["ids"][0]


def launch_profile(token: str, profile_id: str) -> int:
    url = (
        f"https://launcher.mlx.yt:45001/api/v2/profile"
        f"/f/{MLX_FOLDER_ID}/p/{profile_id}"
        f"/start?automation_type=playwright&headless_mode=true"
    )
    resp = requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        verify=False,
        timeout=90,
    )
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
        requests.delete(
            f"https://api.multilogin.com/profile/delete",
            json={"ids": [profile_id], "permanently": True},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=15,
        )
    except Exception:
        pass


def track_on_cargoes(page) -> dict:
    """Navigate Cargoes.com, switch to Container Number mode, search containers."""
    result = {"containers": [], "error": None}

    log.info("Navigating to Cargoes.com container tracking...")
    page.goto("https://www.cargoes.com/container-tracking", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

    # Accept cookies if needed
    try:
        cookie_btn = page.locator('button:has-text("Accept"), button:has-text("accept")')
        if cookie_btn.count() > 0:
            cookie_btn.first.click()
            time.sleep(1)
    except Exception:
        pass

    page.screenshot(path=str(OUTPUT_DIR / "cargoes_initial.png"), full_page=True)

    # Switch tracking mode from "Booking ID" to "Container Number"
    # The "Track By" dropdown shows "Booking ID" — need to click it and select Container Number
    log.info("Switching to Container Number tracking mode...")
    try:
        # Click the "Booking ID" dropdown to open it
        dropdown = page.locator('text="Booking ID"').first
        dropdown.click()
        time.sleep(2)

        page.screenshot(path=str(OUTPUT_DIR / "cargoes_dropdown.png"), full_page=True)

        # Click "Container Number" option
        container_option = page.locator('text="Container Number"')
        if container_option.count() > 0:
            container_option.first.click()
            log.info("Selected Container Number mode")
            time.sleep(2)
        else:
            # Try looking for it in a listbox or dropdown
            page_text = page.evaluate("() => document.body.innerText") or ""
            log.info(f"Dropdown options (page text): {page_text[:1000]}")
    except Exception as e:
        log.warning(f"Could not switch mode: {e}")

    page.screenshot(path=str(OUTPUT_DIR / "cargoes_mode_switched.png"), full_page=True)

    # Now try searching for the first container
    log.info(f"Searching for container {CONTAINERS[0]}...")
    try:
        # Find the visible text input
        inputs = page.locator('input[type="text"]:visible')
        cnt = inputs.count()
        log.info(f"Found {cnt} visible text inputs")

        for i in range(cnt):
            inp = inputs.nth(i)
            placeholder = inp.get_attribute("placeholder") or ""
            log.info(f"  Input {i}: placeholder='{placeholder}'")

            # Use the input that looks like a tracking field
            if "Ex:" in placeholder or not placeholder:
                inp.click()
                time.sleep(0.5)
                inp.fill(CONTAINERS[0])
                time.sleep(1)

                # Press Enter or click Track button
                try:
                    track_btn = page.locator('button:has-text("Track")')
                    if track_btn.count() > 0:
                        track_btn.first.click()
                        log.info("Clicked Track button")
                    else:
                        inp.press("Enter")
                        log.info("Pressed Enter")
                except Exception:
                    inp.press("Enter")

                time.sleep(10)
                page.screenshot(path=str(OUTPUT_DIR / "cargoes_search_result.png"), full_page=True)

                search_text = page.evaluate("() => document.body.innerText") or ""
                result["search_text"] = search_text[:5000]
                log.info(f"Search result: {len(search_text)} chars")
                log.info(f"Result preview: {search_text[:500]}")

                # Check for login requirement
                if "sign in" in search_text.lower() or "login" in search_text.lower():
                    log.info("Cargoes.com requires login for tracking")
                    result["error"] = "Login required"

                break

    except Exception as e:
        log.error(f"Search failed: {e}")
        result["error"] = str(e)

    # Also try BoL search
    log.info(f"Trying BoL search with {BL_NUMBER}...")
    try:
        # Go back to tracking page
        page.goto("https://www.cargoes.com/container-tracking", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)

        # Switch to BoL mode
        dropdown = page.locator('text="Booking ID"').first
        dropdown.click()
        time.sleep(2)
        bol_option = page.locator('text="BoL"')
        if bol_option.count() > 0:
            bol_option.first.click()
            log.info("Selected BoL mode")
            time.sleep(2)
        else:
            # Try Bill of Lading
            bol_option = page.locator('text="Bill of Lading"')
            if bol_option.count() > 0:
                bol_option.first.click()
                log.info("Selected Bill of Lading mode")
                time.sleep(2)

        # Fill BL number
        inputs = page.locator('input[type="text"]:visible')
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            placeholder = inp.get_attribute("placeholder") or ""
            if "Ex:" in placeholder or not placeholder:
                inp.click()
                inp.fill(BL_NUMBER)
                try:
                    page.locator('button:has-text("Track")').first.click()
                except Exception:
                    inp.press("Enter")
                time.sleep(10)
                page.screenshot(path=str(OUTPUT_DIR / "cargoes_bol_result.png"), full_page=True)
                bol_text = page.evaluate("() => document.body.innerText") or ""
                result["bol_search_text"] = bol_text[:5000]
                log.info(f"BoL result: {len(bol_text)} chars")
                break

    except Exception as e:
        log.warning(f"BoL search failed: {e}")

    return result


def main():
    log.info("=== Cargoes.com (DP World) Container Tracking ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    log.info("MLX authenticated")

    profile_id = create_profile_no_proxy(token, "cargoes-track")
    log.info(f"Profile created: {profile_id}")

    try:
        port = launch_profile(token, profile_id)
        log.info(f"Profile launched on CDP port {port}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(30000)

            result = track_on_cargoes(page)
            page.close()

        with open(OUTPUT_DIR / "cargoes_tracking.json", "w") as f:
            json.dump(result, f, indent=2, default=str)

        if result.get("error"):
            print(f"\nError: {result['error']}")
        if result.get("search_text"):
            print(f"\nContainer search result:\n{result['search_text'][:2000]}")
        if result.get("bol_search_text"):
            print(f"\nBoL search result:\n{result['bol_search_text'][:2000]}")

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Profile stopped and deleted")


if __name__ == "__main__":
    main()
