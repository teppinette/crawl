"""
Intercept MSC tracking API calls to get full raw data.
The API at /api/feature/tools/TrackingInfo may return more fields than rendered on page.
Also try "Show all Intermediate Port Calls" to get granular events.
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
log = logging.getLogger("msc-api")

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


def intercept_and_extract(page) -> dict:
    """Navigate MSC tracking, intercept API calls, get raw API response."""
    result = {"api_calls": [], "raw_responses": [], "error": None}

    # Set up request interception to capture API calls
    api_requests = []
    api_responses = []

    def handle_route(route):
        """Intercept API requests to capture full request/response."""
        url = route.request.url
        if '/api/feature/tools/TrackingInfo' in url:
            log.info(f"  INTERCEPTED API CALL: {url}")
            api_requests.append({
                "url": url,
                "method": route.request.method,
                "headers": dict(route.request.headers),
                "post_data": route.request.post_data,
            })
        route.continue_()

    # Also capture responses via page events
    def handle_response(response):
        url = response.url
        if '/api/feature/tools/TrackingInfo' in url:
            try:
                body = response.json()
                log.info(f"  API RESPONSE: {url} status={response.status} size={len(json.dumps(body))}")
                api_responses.append({
                    "url": url,
                    "status": response.status,
                    "body": body,
                })
            except Exception as e:
                try:
                    text = response.text()
                    api_responses.append({"url": url, "status": response.status, "text": text[:5000]})
                except:
                    api_responses.append({"url": url, "status": response.status, "error": str(e)})

    page.on("response", handle_response)
    page.route("**/*", handle_route)

    # Navigate to tracking page
    log.info("Navigating to MSC tracking...")
    page.goto("https://www.msc.com/track-a-shipment", wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)

    # Accept cookies
    try:
        page.locator('button:has-text("Accept All")').click(timeout=5000)
        time.sleep(2)
    except Exception:
        pass

    # Fill BL number and search
    tracking_input = page.locator('#trackingNumber')
    tracking_input.wait_for(state="visible", timeout=10000)
    tracking_input.click()
    time.sleep(0.5)
    tracking_input.fill(BL_NUMBER)
    time.sleep(1)
    tracking_input.press("Enter")
    log.info(f"Submitted BL search: {BL_NUMBER}")

    # Wait for API response
    time.sleep(15)

    log.info(f"Captured {len(api_requests)} API requests, {len(api_responses)} API responses")

    # Now click "Show all Intermediate Port Calls"
    try:
        show_all = page.locator('a:has-text("Show all")')
        if show_all.count() > 0:
            show_all.first.click()
            log.info("Clicked 'Show all Intermediate Port Calls'")
            time.sleep(10)
            log.info(f"After 'Show all': {len(api_responses)} total API responses")
        else:
            # Try clicking the text directly
            page.evaluate("""() => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.textContent.includes('Show all')) {
                        a.click();
                        return true;
                    }
                }
                return false;
            }""")
            time.sleep(10)
    except Exception as e:
        log.warning(f"Show all click: {e}")

    # Also try to call the API directly from the browser context
    log.info("Trying direct API call from browser context...")
    direct_api = page.evaluate("""async () => {
        try {
            // Try calling the tracking API directly
            const resp = await fetch('/api/feature/tools/TrackingInfo?trackingNumber=MEDUFX870746&trackingType=billOfLading&showIntermediatePorts=true', {
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                }
            });
            const data = await resp.json();
            return {status: resp.status, data: data};
        } catch(e) {
            return {error: e.message};
        }
    }""")
    log.info(f"Direct API call result: status={direct_api.get('status', '?')}")

    if direct_api.get('data'):
        api_responses.append({
            "url": "/api/feature/tools/TrackingInfo?showIntermediatePorts=true",
            "status": direct_api.get('status'),
            "body": direct_api['data'],
            "source": "direct_fetch"
        })

    # Try POST version too
    direct_post = page.evaluate("""async () => {
        try {
            const resp = await fetch('/api/feature/tools/TrackingInfo', {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    trackingNumber: 'MEDUFX870746',
                    trackingType: 'billOfLading',
                    showIntermediatePorts: true
                })
            });
            const data = await resp.json();
            return {status: resp.status, data: data};
        } catch(e) {
            return {error: e.message};
        }
    }""")
    log.info(f"Direct POST result: status={direct_post.get('status', '?')}")

    if direct_post.get('data'):
        api_responses.append({
            "url": "/api/feature/tools/TrackingInfo (POST)",
            "status": direct_post.get('status'),
            "body": direct_post['data'],
            "source": "direct_post"
        })

    # Now also try individual container with showIntermediatePorts
    containers_to_check = ["DFSU6580527", "FSCU8108794"]
    for cid in containers_to_check:
        log.info(f"Direct API for container {cid}...")
        container_api = page.evaluate(f"""async () => {{
            try {{
                const resp = await fetch('/api/feature/tools/TrackingInfo?trackingNumber={cid}&trackingType=container&showIntermediatePorts=true', {{
                    headers: {{'Accept': 'application/json'}}
                }});
                const data = await resp.json();
                return {{status: resp.status, data: data}};
            }} catch(e) {{
                return {{error: e.message}};
            }}
        }}""")
        if container_api.get('data'):
            api_responses.append({
                "url": f"/api/feature/tools/TrackingInfo?container={cid}&showIntermediatePorts=true",
                "status": container_api.get('status'),
                "body": container_api['data'],
                "source": f"direct_container_{cid}"
            })
            log.info(f"  Got data for {cid}")

    result["api_requests"] = api_requests
    result["api_responses"] = api_responses
    return result


def main():
    log.info(f"=== MSC API Interception — BL {BL_NUMBER} ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    profile_id = create_profile_no_proxy(token, "msc-api-intercept")
    log.info(f"Profile: {profile_id}")

    try:
        port = launch_profile(token, profile_id)
        log.info(f"CDP port: {port}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(60000)

            result = intercept_and_extract(page)
            page.close()

        # Save raw API responses
        with open(OUTPUT_DIR / "msc_api_raw.json", "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(f"Saved {len(result['api_responses'])} API responses")

        # Analyze responses for additional fields
        for i, resp in enumerate(result['api_responses']):
            body = resp.get('body', {})
            source = resp.get('source', 'intercepted')
            url = resp.get('url', '')

            print(f"\n{'='*80}")
            print(f"API Response #{i+1} ({source}): {url[:80]}")
            print(f"Status: {resp.get('status')}")

            if isinstance(body, dict):
                # Print top-level keys
                print(f"Top-level keys: {list(body.keys())}")

                # Look for any fields we haven't seen before
                data = body.get('Data', body.get('data', body))
                if isinstance(data, dict):
                    print(f"Data keys: {list(data.keys())}")

                    # Check for delivery order, notify party, etc.
                    for key in data:
                        val = data[key]
                        if isinstance(val, str) and val:
                            if any(kw in key.lower() for kw in ['delivery', 'notify', 'agent', 'haulier',
                                                                  'transport', 'truck', 'driver', 'gate',
                                                                  'consignee', 'shipper', 'forwarder']):
                                print(f"  >>> {key}: {val}")

                    # If there are containers, check their fields
                    containers = data.get('Containers', data.get('containers', []))
                    if containers and len(containers) > 0:
                        first_c = containers[0]
                        if isinstance(first_c, dict):
                            print(f"Container keys: {list(first_c.keys())}")
                            # Print ALL fields of first container
                            for k, v in first_c.items():
                                if k != 'Events' and v:
                                    print(f"  {k}: {json.dumps(v, default=str)[:200]}")

                            # Check events for additional fields
                            events = first_c.get('Events', [])
                            if events:
                                print(f"  Event count: {len(events)}")
                                print(f"  Event keys: {list(events[0].keys())}")
                                # Print first event fully
                                print(f"  First event: {json.dumps(events[0], indent=2, default=str)[:500]}")
                                # Print last event (likely the delivery)
                                print(f"  Last event: {json.dumps(events[-1], indent=2, default=str)[:500]}")

                # Also dump a portion of the full response
                print(f"\nFull response preview ({len(json.dumps(body))} chars):")
                print(json.dumps(body, indent=2, default=str)[:3000])
            elif isinstance(body, str):
                print(f"Text response: {body[:1000]}")

        print(f"\n{'='*80}")

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Done")


if __name__ == "__main__":
    main()
