"""
Check DP World Jebel Ali for container gate-out details (truck IDs, transporter names).
Uses Multilogin anti-detect browser.
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
log = logging.getLogger("dpworld")

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

# Container IDs from BL MEDUFX870746
CONTAINERS = [
    "DFSU6580527", "MSNU7760767", "MSNU9153090", "MSNU5478137",
    "MEDU4958469", "MEDU7419218", "MSDU6153938", "MSDU7384361",
    "TCNU8790592", "MSMU7788246", "TXGU8624573", "SEKU6842139",
    "MSNU6792965", "TIIU4035545", "MSNU5529424", "FSCU8108794",
    "SEKU6825445", "TCNU7279232", "TGBU5852746", "MSNU9166158",
]


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


def create_profile(token: str, name: str, use_proxy: bool = True) -> str:
    profile_json = {
        "name": name,
        "browser_type": "mimic",
        "folder_id": MLX_FOLDER_ID,
        "parameters": {"fingerprint": {}},
    }
    if use_proxy:
        proxy_user = MLX_PROXY_USER
        if "-country-" in proxy_user:
            proxy_user = proxy_user.rsplit("-country-", 1)[0] + "-country-ae"
        profile_json["parameters"]["proxy"] = {
            "type": "http",
            "host": "gate.multilogin.com",
            "port": 8080,
            "username": proxy_user,
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


def check_dpworld_sites(page) -> dict:
    """Try various DP World / Dubai Trade URLs to find container tracking."""
    results = {"sites_checked": [], "error": None}

    sites = [
        ("DP World Container Tracking", "https://www.dpworld.com/en/smart-services/container-tracking"),
        ("DP World Cargoes", "https://cargoes.com/container-tracking"),
        ("Dubai Trade", "https://www.dubaitrade.ae"),
        ("Dubai Trade Portal", "https://portal.dubaitrade.ae"),
        ("DP World UAE", "https://www.dpworld.ae"),
        ("DP World e-Services", "https://eservices.dpworld.ae"),
    ]

    for name, url in sites:
        log.info(f"Checking {name}: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)

            page_text = page.evaluate("() => document.body.innerText") or ""
            page_title = page.title()
            current_url = page.url

            page.screenshot(path=str(OUTPUT_DIR / f"dpworld_{name.replace(' ', '_').lower()}.png"), full_page=True)

            site_result = {
                "name": name,
                "url": url,
                "final_url": current_url,
                "title": page_title,
                "text_length": len(page_text),
                "text_preview": page_text[:1000],
                "has_tracking": any(kw in page_text.lower() for kw in ['container', 'tracking', 'track', 'gate']),
                "blocked": "Access Denied" in page_text or "403" in page_text,
            }
            results["sites_checked"].append(site_result)

            if site_result["has_tracking"] and not site_result["blocked"]:
                log.info(f"  >>> {name} has tracking capability!")

                # Try to find a container search field
                inputs = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('input, textarea')).map(i => ({
                        type: i.type, placeholder: i.placeholder, id: i.id,
                        name: i.name, visible: i.offsetParent !== null
                    }));
                }""")
                site_result["inputs"] = inputs

                # Try searching for a container
                if inputs:
                    for inp in inputs:
                        if inp['visible'] and inp['type'] in ('text', 'search'):
                            try:
                                selector = f"#{inp['id']}" if inp['id'] else f"input[placeholder='{inp['placeholder']}']"
                                field = page.locator(selector).first
                                field.click()
                                field.fill(CONTAINERS[0])
                                field.press("Enter")
                                time.sleep(8)
                                search_text = page.evaluate("() => document.body.innerText") or ""
                                page.screenshot(path=str(OUTPUT_DIR / f"dpworld_{name.replace(' ', '_').lower()}_search.png"), full_page=True)
                                site_result["search_result"] = search_text[:2000]
                                break
                            except Exception as e:
                                site_result["search_error"] = str(e)

            log.info(f"  {name}: title='{page_title}' blocked={site_result['blocked']} tracking={site_result['has_tracking']}")

        except Exception as e:
            log.warning(f"  {name}: {e}")
            results["sites_checked"].append({"name": name, "url": url, "error": str(e)})

    return results


def main():
    log.info("=== DP World / Dubai Trade Container Gate-Out Check ===")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = get_token()
    log.info("MLX authenticated")

    # Try with AE proxy first
    profile_id = create_profile(token, "dpworld-tracker", use_proxy=True)
    log.info(f"Profile created (AE proxy): {profile_id}")

    try:
        port = launch_profile(token, profile_id)
        log.info(f"Profile launched on CDP port {port}")

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            page = context.new_page()
            page.set_default_timeout(30000)

            results = check_dpworld_sites(page)
            page.close()

        # Save results
        with open(OUTPUT_DIR / "dpworld_check.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
        log.info(f"Results saved")

        # Summary
        print("\n=== DP World / Dubai Trade Site Check ===")
        for site in results["sites_checked"]:
            status = "BLOCKED" if site.get("blocked") else ("TRACKING" if site.get("has_tracking") else "NO TRACKING")
            print(f"  {site['name']}: {status}")
            if site.get("search_result"):
                print(f"    Search result preview: {site['search_result'][:200]}")

    finally:
        stop_profile(profile_id)
        delete_profile(token, profile_id)
        log.info("Profile stopped and deleted")


if __name__ == "__main__":
    main()
