"""
Find haulier/truck pickup details for BL MEDUFX870746 containers at Jebel Ali.
Try every possible source: DP World terminal, Dubai Trade, JAFZA, trade data platforms.
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
log = logging.getLogger("haulier")

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
    resp = requests.post(
        "https://api.multilogin.com/profile/create",
        json={
            "name": name,
            "browser_type": "mimic",
            "folder_id": MLX_FOLDER_ID,
            "parameters": {"fingerprint": {}},
        },
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


def try_site(page, name, url, screenshot_name=None):
    """Navigate to a site, screenshot, return page text."""
    log.info(f"[{name}] {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        title = page.title() or ""
        final_url = page.url
        fname = screenshot_name or name.lower().replace(" ", "_").replace("/", "_")
        page.screenshot(path=str(OUTPUT_DIR / f"haulier_{fname}.png"), full_page=True)
        blocked = "Access Denied" in text or "403 Forbidden" in text or "ERR_" in text
        log.info(f"  title='{title[:60]}' blocked={blocked} chars={len(text)}")
        return {"name": name, "url": url, "final_url": final_url, "title": title,
                "text": text[:8000], "blocked": blocked}
    except Exception as e:
        log.warning(f"  FAILED: {e}")
        return {"name": name, "url": url, "error": str(e)}


def search_importgenius(page) -> dict:
    """Search ImportGenius for BL data — shows consignee, notify party, clearing agent."""
    log.info("[ImportGenius] Searching BL...")
    try:
        page.goto("https://www.importgenius.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

        # Accept cookies
        try:
            page.locator('button:has-text("Accept")').click(timeout=3000)
            time.sleep(1)
        except Exception:
            pass

        text = page.evaluate("() => document.body.innerText") or ""
        page.screenshot(path=str(OUTPUT_DIR / "haulier_importgenius.png"), full_page=True)

        # Try to find search input
        inputs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input')).map(i => ({
                type: i.type, placeholder: i.placeholder, id: i.id,
                name: i.name, visible: i.offsetParent !== null
            })).filter(i => i.visible);
        }""")
        log.info(f"  Inputs: {json.dumps(inputs)}")

        # Search for our BL or company
        for inp in inputs:
            if inp['type'] in ('text', 'search'):
                sel = f"#{inp['id']}" if inp['id'] else f"input[name='{inp['name']}']" if inp['name'] else "input[type='text']:visible"
                try:
                    field = page.locator(sel).first
                    field.click()
                    field.fill("Super Save General Trading")
                    field.press("Enter")
                    time.sleep(8)
                    result_text = page.evaluate("() => document.body.innerText") or ""
                    page.screenshot(path=str(OUTPUT_DIR / "haulier_importgenius_result.png"), full_page=True)
                    return {"text": result_text[:5000], "inputs": inputs}
                except Exception as e:
                    log.warning(f"  Search failed: {e}")
                break

        return {"text": text[:3000], "inputs": inputs}
    except Exception as e:
        return {"error": str(e)}


def search_52wmb(page) -> dict:
    """Search 52wmb.com — UAE trade data with BL details."""
    log.info("[52wmb] Searching...")
    try:
        page.goto("https://www.52wmb.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        text = page.evaluate("() => document.body.innerText") or ""
        page.screenshot(path=str(OUTPUT_DIR / "haulier_52wmb.png"), full_page=True)
        return {"text": text[:3000]}
    except Exception as e:
        return {"error": str(e)}


def search_volza(page) -> dict:
    """Search Volza.com — import/export trade data with shipment details."""
    log.info("[Volza] Searching UAE imports...")
    try:
        url = "https://www.volza.com/p/super-save-general-trading/import/import-in-uae/"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        text = page.evaluate("() => document.body.innerText") or ""
        page.screenshot(path=str(OUTPUT_DIR / "haulier_volza.png"), full_page=True)
        log.info(f"  Volza: {len(text)} chars")
        return {"url": url, "text": text[:8000]}
    except Exception as e:
        return {"error": str(e)}


def search_panjiva(page) -> dict:
    """Search Panjiva (S&P Global) for shipment records."""
    log.info("[Panjiva] Searching...")
    try:
        url = "https://panjiva.com/search?q=super+save+general+trading+dubai"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        text = page.evaluate("() => document.body.innerText") or ""
        page.screenshot(path=str(OUTPUT_DIR / "haulier_panjiva.png"), full_page=True)
        return {"text": text[:5000]}
    except Exception as e:
        return {"error": str(e)}


def search_trademo(page) -> dict:
    """Search Trademo for UAE import data."""
    log.info("[Trademo] Searching...")
    try:
        url = "https://www.trademo.com/search?q=MEDUFX870746"
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(6)
        text = page.evaluate("() => document.body.innerText") or ""
        page.screenshot(path=str(OUTPUT_DIR / "haulier_trademo.png"), full_page=True)
        return {"text": text[:5000]}
    except Exception as e:
        return {"error": str(e)}


def check_dpworld_terminal(page) -> dict:
    """Try DP World terminal-specific container tracking pages."""
    results = []

    # DP World terminal services / container inquiry
    urls = [
        ("DP World Flow", "https://flow.dpworld.com"),
        ("DP World Trade", "https://trade.dpworld.com"),
        ("DP World UAE Track", "https://www.dpworld.com/en/smart-services/track-trace"),
        ("DP World Jebel Ali Services", "https://www.dpworld.ae/en/our-services/container-tracking"),
        ("JAFZA Portal", "https://www.jafza.ae"),
        ("JAFZA e-Services", "https://eservices.jafza.ae"),
    ]

    for name, url in urls:
        r = try_site(page, name, url)
        results.append(r)

    return results


def main():
    log.info("=== Haulier / Pickup Detail Search ===")
    log.info(f"BL: {BL_NUMBER} | Containers: {len(CONTAINERS)}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    log.info("MLX authenticated")

    profile_id = create_profile_no_proxy(token, "haulier-search")
    log.info(f"Profile created: {profile_id}")

    all_results = {}

    try:
        port = launch_profile(token, profile_id)
        log.info(f"Profile launched on CDP port {port}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(45000)

            # 1. Trade data platforms (may have UAE customs/import data)
            log.info("\n=== TRADE DATA PLATFORMS ===")
            all_results["importgenius"] = search_importgenius(page)
            all_results["volza"] = search_volza(page)
            all_results["panjiva"] = search_panjiva(page)

            # 2. DP World terminal sites
            log.info("\n=== DP WORLD TERMINAL SITES ===")
            all_results["dpworld_terminals"] = check_dpworld_terminal(page)

            # 3. Try direct BL search on various platforms
            log.info("\n=== DIRECT BL SEARCHES ===")

            # Searates
            r = try_site(page, "Searates BL", f"https://www.searates.com/container/tracking/?number={BL_NUMBER}")
            all_results["searates"] = r

            # TrackTrace
            r = try_site(page, "Track-Trace", f"https://www.track-trace.com/container?number={CONTAINERS[0]}")
            all_results["tracktrace"] = r

            # ShipsGo
            r = try_site(page, "ShipsGo", f"https://shipsgo.com/container-tracking/{CONTAINERS[0]}")
            all_results["shipsgo"] = r

            page.close()

        # Save all results
        with open(OUTPUT_DIR / "haulier_search_results.json", "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        log.info("Results saved")

        # Print findings
        print("\n" + "=" * 80)
        print("HAULIER / PICKUP DETAIL SEARCH RESULTS")
        print("=" * 80)

        for key, val in all_results.items():
            if isinstance(val, dict):
                text = val.get("text", "")
                error = val.get("error", "")
                if error:
                    print(f"\n  [{key}] ERROR: {error}")
                elif text:
                    # Look for haulier/transport/truck/delivery keywords
                    for kw in ["haulier", "transport", "truck", "delivery", "gate out",
                               "clearing", "freight forwarder", "notify", "agent",
                               "super save", "MEDUFX", "DFSU", "consignee"]:
                        if kw.lower() in text.lower():
                            # Find the line containing the keyword
                            for line in text.split('\n'):
                                if kw.lower() in line.lower():
                                    print(f"\n  [{key}] {line.strip()[:200]}")
                else:
                    print(f"\n  [{key}] No text captured")
            elif isinstance(val, list):
                for item in val:
                    text = item.get("text", "")
                    name = item.get("name", "")
                    if text and any(kw in text.lower() for kw in ["container", "tracking", "gate", "delivery"]):
                        print(f"\n  [{name}] Has tracking content ({len(text)} chars)")

        print("\n" + "=" * 80)

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Profile stopped and deleted")


if __name__ == "__main__":
    main()
