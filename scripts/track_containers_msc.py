"""
Track MSC containers via Multilogin anti-detect browser + Playwright.
Search BL number → click each container bar to expand → extract Alpine.js event data.
Focus: gate-out events at Jebel Ali = truck pickup.
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
log = logging.getLogger("msc-tracker")

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


def search_and_extract(page) -> dict:
    """Search BL on MSC, click expand on each container, extract Alpine.js event data."""
    result = {"bl_number": BL_NUMBER, "containers": [], "error": None}

    # Step 1: Navigate to tracking page
    log.info("Navigating to MSC tracking page...")
    page.goto("https://www.msc.com/track-a-shipment", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)

    content = page.content()
    if "Access Denied" in content:
        result["error"] = "Access Denied by WAF"
        return result

    # Accept cookies
    try:
        accept_btn = page.locator('button:has-text("Accept All")')
        accept_btn.wait_for(timeout=5000)
        accept_btn.click()
        log.info("Accepted cookies")
        time.sleep(2)
    except Exception:
        log.info("No cookie banner")

    # Step 2: Find and fill the tracking input
    log.info("Filling BL number...")
    tracking_input = page.locator('#trackingNumber')
    tracking_input.wait_for(state="visible", timeout=10000)
    tracking_input.click()
    time.sleep(0.5)
    tracking_input.fill(BL_NUMBER)
    time.sleep(1)
    tracking_input.press("Enter")
    log.info(f"Submitted BL search: {BL_NUMBER}")

    # Wait for results
    time.sleep(15)

    # Verify results loaded
    page_text = page.evaluate("() => document.body.innerText") or ""
    if "CONTAINERS" not in page_text:
        log.error("Results didn't load")
        page.screenshot(path=str(OUTPUT_DIR / "msc_no_results.png"), full_page=True)
        result["error"] = "Results didn't load"
        return result

    log.info("BL results loaded — all containers visible")
    page.screenshot(path=str(OUTPUT_DIR / "msc_bl_overview.png"), full_page=True)

    # Step 3: Click "Show all Intermediate Port Calls" link
    try:
        show_all_link = page.locator('a:has-text("Show all")')
        if show_all_link.count() > 0:
            show_all_link.first.click()
            log.info("Clicked 'Show all Intermediate Port Calls'")
            time.sleep(3)
    except Exception:
        log.info("No 'Show all' link found")

    # Step 4: Click each container bar to expand it
    # The bars have class "msc-flow-tracking__bar" with x-on:click="more()"
    bars = page.locator('.msc-flow-tracking__bar')
    bar_count = bars.count()
    log.info(f"Found {bar_count} container bars")

    for i in range(bar_count):
        try:
            bars.nth(i).click()
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"  Could not click bar {i}: {e}")

    log.info(f"Clicked {bar_count} container bars to expand")
    time.sleep(5)

    page.screenshot(path=str(OUTPUT_DIR / "msc_bl_expanded.png"), full_page=True)

    # Step 5: Extract Alpine.js data from each container component
    # Alpine.js stores data on the DOM element via __x.$data or _x_dataStack
    container_data = page.evaluate("""() => {
        const containers = [];
        const els = document.querySelectorAll('.msc-flow-tracking__container');

        els.forEach(el => {
            let data = null;

            // Alpine.js v3 stores data in _x_dataStack
            if (el._x_dataStack) {
                data = el._x_dataStack[0];
            }
            // Alpine.js v2 stores data in __x.$data
            else if (el.__x && el.__x.$data) {
                data = el.__x.$data;
            }

            if (data) {
                const container = {
                    containerNumber: data.container ? data.container.ContainerNumber : null,
                    containerType: data.container ? data.container.ContainerType : null,
                    latestMove: data.container ? data.container.LatestMove : null,
                    isComplete: data.isComplete || false,
                    events: [],
                };

                // Extract orderedEvents
                const events = data.orderedEvents || data.events || [];
                events.forEach(evt => {
                    container.events.push({
                        date: evt.Date || evt.date || '',
                        location: evt.Location || evt.location || '',
                        description: evt.Description || evt.description || '',
                        vesselVoyage: evt.Voyage || evt.VesselName || evt.voyage || '',
                        facility: evt.EquipmentHandlingFacilityName || evt.Facility || evt.facility || '',
                        laden: evt.Laden || evt.laden || '',
                    });
                });

                containers.push(container);
            }
        });

        return containers;
    }""")

    log.info(f"Extracted Alpine.js data for {len(container_data)} containers")

    if container_data:
        result["containers"] = container_data
    else:
        # Fallback: try to extract from expanded text
        log.info("Alpine.js extraction returned empty — trying text extraction from expanded page")
        expanded_text = page.evaluate("() => document.body.innerText") or ""
        with open(OUTPUT_DIR / "msc_bl_expanded.txt", "w") as f:
            f.write(expanded_text)
        log.info(f"Expanded page text: {len(expanded_text)} chars")

        # Try alternative Alpine access
        alt_data = page.evaluate("""() => {
            const results = [];

            // Try accessing Alpine via the global Alpine object
            if (typeof Alpine !== 'undefined') {
                results.push({method: 'Alpine global found'});
            }

            // Try to find data on tracking container divs
            const divs = document.querySelectorAll('[x-data*="mscFlowTrackingContainer"]');
            divs.forEach((div, idx) => {
                const info = {index: idx, hasXData: !!div._x_dataStack};

                // Try to read the x-data expression
                const xDataAttr = div.getAttribute('x-data');
                info.xDataAttr = xDataAttr;

                // Check for Alpine v3
                if (div._x_dataStack && div._x_dataStack.length > 0) {
                    const d = div._x_dataStack[0];
                    info.keys = Object.keys(d);
                    if (d.container) {
                        info.containerNumber = d.container.ContainerNumber;
                    }
                    if (d.orderedEvents) {
                        info.eventCount = d.orderedEvents.length;
                        if (d.orderedEvents.length > 0) {
                            info.firstEvent = JSON.stringify(d.orderedEvents[0]).substring(0, 500);
                        }
                    }
                }

                results.push(info);
            });

            return results;
        }""")

        log.info(f"Alternative Alpine data: {json.dumps(alt_data, indent=2)[:3000]}")

        # If we still have no events, try clicking bars and reading the rendered HTML
        if not any(r.get('eventCount', 0) > 0 for r in alt_data if isinstance(r, dict)):
            log.info("Trying to extract events from rendered HTML after expand...")

            # Click each bar one at a time and extract its events
            for i in range(bar_count):
                try:
                    # Click to expand
                    bars.nth(i).click()
                    time.sleep(2)

                    # Extract the visible tracking text from the expanded section
                    visible_events = page.evaluate(f"""() => {{
                        const sections = document.querySelectorAll('.msc-flow-tracking__tracking');
                        const visible = [];
                        sections.forEach((s, idx) => {{
                            if (s.offsetParent !== null && s.style.display !== 'none') {{
                                visible.push({{index: idx, text: s.innerText.trim()}});
                            }}
                        }});
                        return visible;
                    }}""")

                    if visible_events:
                        log.info(f"  Bar {i}: {len(visible_events)} visible tracking sections")
                        for ve in visible_events:
                            log.info(f"    Section {ve['index']}: {ve['text'][:200]}")

                    # Click again to collapse (optional - keep it open)
                    time.sleep(1)

                except Exception as e:
                    log.warning(f"  Bar {i} expand failed: {e}")

        # Store whatever we got
        result["alpine_debug"] = alt_data
        result["containers"] = container_data or []

    return result


def main():
    log.info(f"=== MSC Container Tracking — BL {BL_NUMBER} ===")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    log.info("MLX authenticated")

    profile_id = create_profile_no_proxy(token, "msc-bl-detail")
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

            result = search_and_extract(page)

            page.close()

        # Save results
        output_path = OUTPUT_DIR / "container_tracking.json"
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(f"Results saved to {output_path}")

        # Print summary
        print("\n" + "=" * 80)
        print(f"BL {BL_NUMBER} — CONTAINER EVENT TIMELINE")
        print("=" * 80)

        if result.get("error"):
            print(f"ERROR: {result['error']}")
            return

        gate_outs = []
        for c in result.get("containers", []):
            cid = c.get("containerNumber") or c.get("container_id", "?")
            events = c.get("events", [])
            print(f"\n  {cid} ({c.get('containerType', '?')}): {len(events)} events")
            print(f"    Latest move: {c.get('latestMove', '?')}")

            for evt in events:
                date = evt.get("date", "")
                loc = evt.get("location", "")
                desc = evt.get("description", "")
                vessel = evt.get("vesselVoyage", "")
                facility = evt.get("facility", "")

                line = f"    {date} | {loc} | {desc} | {vessel} | {facility}"

                desc_lower = desc.lower()
                if "gate out" in desc_lower or "delivered" in desc_lower:
                    print(f"    >>> GATE OUT: {line}")
                    gate_outs.append({"container": cid, "date": date, "location": loc,
                                      "description": desc, "facility": facility})
                elif "gate in" in desc_lower:
                    print(f"    >>> GATE IN: {line}")
                elif "jebel ali" in loc.lower() or "jebel ali" in desc.lower():
                    print(f"    [JA] {line}")
                else:
                    print(f"    {line}")

        print("\n" + "=" * 80)
        print(f"GATE-OUT EVENTS (truck pickups at Jebel Ali): {len(gate_outs)}")
        for go in gate_outs:
            print(f"  {go['container']} | {go['date']} | {go['description']} | {go['facility']}")
        print("=" * 80)

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Profile stopped and deleted")


if __name__ == "__main__":
    main()
