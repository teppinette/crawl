"""
Extract FULL MSC tracking data with vessel names and facility details.
Clicks 'Show all Intermediate Port Calls' and dumps raw Alpine.js event objects.
"""

import hashlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("msc-detail")

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

BL_NUMBER = "MEDUFX870746"
OUTPUT_DIR = Path("/home/copapadmin/crawl/output/investigations/super-save-general-trading")


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


def extract_full_data(page) -> dict:
    """Search BL, click 'Show all Intermediate Port Calls', dump raw Alpine event objects."""
    result = {"bl_number": BL_NUMBER, "containers": [], "error": None}

    # Navigate to tracking page
    log.info("Navigating to MSC tracking page...")
    page.goto("https://www.msc.com/track-a-shipment", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)

    if "Access Denied" in (page.content() or ""):
        result["error"] = "Access Denied"
        return result

    # Accept cookies
    try:
        page.locator('button:has-text("Accept All")').click(timeout=5000)
        log.info("Accepted cookies")
        time.sleep(2)
    except Exception:
        pass

    # Fill BL number
    tracking_input = page.locator('#trackingNumber')
    tracking_input.wait_for(state="visible", timeout=10000)
    tracking_input.click()
    time.sleep(0.5)
    tracking_input.fill(BL_NUMBER)
    time.sleep(1)
    tracking_input.press("Enter")
    log.info(f"Submitted BL search: {BL_NUMBER}")
    time.sleep(15)

    # Verify results loaded
    page_text = page.evaluate("() => document.body.innerText") or ""
    if "CONTAINERS" not in page_text:
        result["error"] = "Results didn't load"
        return result
    log.info("BL results loaded")

    # Click "Show all Intermediate Port Calls" link
    try:
        show_all = page.locator('a:has-text("Show all")')
        if show_all.count() > 0:
            show_all.first.click()
            log.info("Clicked 'Show all Intermediate Port Calls'")
            time.sleep(8)
        else:
            log.info("No 'Show all' link")
    except Exception as e:
        log.warning(f"Show all click failed: {e}")

    # Click each container bar to expand
    bars = page.locator('.msc-flow-tracking__bar')
    bar_count = bars.count()
    log.info(f"Expanding {bar_count} container bars...")
    for i in range(bar_count):
        try:
            bars.nth(i).click()
            time.sleep(0.3)
        except Exception:
            pass
    time.sleep(5)

    # Screenshot expanded view
    page.screenshot(path=str(OUTPUT_DIR / "msc_bl_full_expanded.png"), full_page=True)

    # Dump raw Alpine.js event data — get ALL fields from each event object
    container_data = page.evaluate("""() => {
        const containers = [];
        const els = document.querySelectorAll('.msc-flow-tracking__container');

        els.forEach(el => {
            let data = null;
            if (el._x_dataStack) data = el._x_dataStack[0];
            else if (el.__x && el.__x.$data) data = el.__x.$data;

            if (!data) return;

            const c = {
                containerNumber: data.container ? data.container.ContainerNumber : null,
                containerType: data.container ? data.container.ContainerType : null,
                latestMove: data.container ? data.container.LatestMove : null,
                podEta: data.container ? data.container.PodEtaDate : null,
                isComplete: data.isComplete || false,
                rawContainer: data.container ? JSON.parse(JSON.stringify(data.container)) : null,
                events: [],
                rawEventKeys: [],
            };

            // Dump ALL keys from the first event to understand the structure
            const events = data.orderedEvents || [];
            if (events.length > 0) {
                c.rawEventKeys = Object.keys(events[0]);
            }

            events.forEach(evt => {
                // Dump the entire event object
                c.events.push(JSON.parse(JSON.stringify(evt)));
            });

            containers.push(c);
        });

        return containers;
    }""")

    log.info(f"Extracted data for {len(container_data)} containers")

    # Also get the expanded page text for comparison
    expanded_text = page.evaluate("() => document.body.innerText") or ""
    with open(OUTPUT_DIR / "msc_bl_full_text.txt", "w") as f:
        f.write(expanded_text)
    log.info(f"Full expanded text: {len(expanded_text)} chars")

    result["containers"] = container_data
    return result


def main():
    log.info(f"=== MSC Full Detail Extraction — BL {BL_NUMBER} ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    log.info("MLX authenticated")

    profile_id = create_profile_no_proxy(token, "msc-full-detail")
    log.info(f"Profile created: {profile_id}")

    try:
        port = launch_profile(token, profile_id)
        log.info(f"Profile launched on CDP port {port}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(60000)

            result = extract_full_data(page)
            page.close()

        # Save
        output_path = OUTPUT_DIR / "container_tracking_full.json"
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(f"Saved to {output_path}")

        # Analyze
        if result.get("error"):
            print(f"ERROR: {result['error']}")
            return

        # Show event field names
        if result["containers"] and result["containers"][0].get("rawEventKeys"):
            print(f"\nEvent fields: {result['containers'][0]['rawEventKeys']}")

        # Show raw container object keys
        if result["containers"] and result["containers"][0].get("rawContainer"):
            print(f"Container fields: {list(result['containers'][0]['rawContainer'].keys())}")

        # Print first container's raw events
        first = result["containers"][0]
        print(f"\n=== {first['containerNumber']} — Raw Events ===")
        for evt in first["events"]:
            print(json.dumps(evt, indent=2))

        # Summary of "Import to consignee" dates across all containers
        print("\n" + "=" * 80)
        print("IMPORT TO CONSIGNEE (= truck pickup from Jebel Ali)")
        print("=" * 80)
        for c in result["containers"]:
            for evt in c["events"]:
                desc = evt.get("Description", evt.get("description", ""))
                if "import to consignee" in desc.lower():
                    date = evt.get("Date", evt.get("date", ""))
                    loc = evt.get("Location", evt.get("location", ""))
                    vessel = evt.get("Voyage", evt.get("VesselName", evt.get("vesselVoyage", "")))
                    facility = evt.get("EquipmentHandlingFacilityName", evt.get("Facility", evt.get("facility", "")))
                    print(f"  {c['containerNumber']} | {date} | {loc} | vessel: {vessel} | facility: {facility}")

        print("\n" + "=" * 80)
        print("DISCHARGED AT JEBEL ALI (arrival at port)")
        print("=" * 80)
        for c in result["containers"]:
            for evt in c["events"]:
                desc = evt.get("Description", evt.get("description", ""))
                if "discharged" in desc.lower() and "jebel" in evt.get("Location", evt.get("location", "")).lower():
                    date = evt.get("Date", evt.get("date", ""))
                    vessel = evt.get("Voyage", evt.get("VesselName", ""))
                    facility = evt.get("EquipmentHandlingFacilityName", evt.get("Facility", ""))
                    print(f"  {c['containerNumber']} | {date} | vessel: {vessel} | facility: {facility}")

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Profile stopped and deleted")


if __name__ == "__main__":
    main()
