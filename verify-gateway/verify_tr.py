"""
Turkey GIB VKN (Tax ID) verification via Playwright.

Uses GIB (Gelir İdaresi Başkanlığı / Revenue Administration) taxpayer
verification portal. No CAPTCHA, no proxy needed. Requires Playwright
for JS SPA rendering.

Input: VKN (Vergi Kimlik Numarası) — 10-digit tax identification number.
Returns: company name, tax office, registration status.
"""

import json
import logging
import queue
import hashlib
import re
import subprocess
import threading
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("verify-gateway")

_MLX_EMAIL = None
_MLX_PASSWORD = None
_MLX_FOLDER_ID = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

_pool: queue.Queue = queue.Queue()
_pool_initialized = False


def init(get_secret):
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID, _POOL_PROFILE_IDS
    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []
    if _MLX_PASSWORD and _POOL_PROFILE_IDS:
        log.info("TR GIB VKN ready: %d pool profiles", len(_POOL_PROFILE_IDS))


def _init_pool():
    global _pool_initialized
    if _pool_initialized:
        return
    for pid in _POOL_PROFILE_IDS:
        _pool.put(pid)
    _pool_initialized = True


def _get_token() -> str:
    global _cached_token, _token_expiry
    with _token_lock:
        if time.monotonic() < _token_expiry and _cached_token:
            return _cached_token
        resp = requests.post(
            "https://api.multilogin.com/user/signin",
            json={"email": _MLX_EMAIL, "password": hashlib.md5(_MLX_PASSWORD.encode()).hexdigest()},
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
        f"/f/{_MLX_FOLDER_ID}/p/{profile_id}"
        f"/start?automation_type=playwright&headless_mode=true"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, verify=False, timeout=60)
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX launch failed: {data['status']['message']}")
    return int(data["data"]["port"])


def _stop_profile(profile_id: str):
    try:
        subprocess.run([str(_CLI_PATH), "profile-stop", "--profile-id", profile_id],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def _do_gib_lookup(port: int, vkn: str, profile_id: str) -> dict:
    result = {}
    error = None

    def _run():
        nonlocal result, error
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                try:
                    result.update(_navigate_and_extract(page, vkn))
                finally:
                    page.close()
                    context.close()
                    browser.close()
        except Exception as e:
            error = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=90)

    if t.is_alive():
        log.error("GIB lookup HUNG for VKN %s — force-stopping profile %s", vkn, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("GIB lookup timed out (90s)")
    if error:
        raise error
    return result


def _navigate_and_extract(page, vkn: str) -> dict:
    """Navigate GIB taxpayer verification and extract result."""
    page.goto(
        "https://ivd.gib.gov.tr/tvd_side/main.jsp?gession=&ESSION=&ESSION_ID=",
        timeout=60000, wait_until="domcontentloaded",
    )
    time.sleep(5)

    # Try the VKN verification page
    page.goto(
        "https://sorgu.gib.gov.tr/mukellef-bilgileri-dogrulama",
        timeout=60000, wait_until="domcontentloaded",
    )
    time.sleep(8)

    body = page.inner_text("body")

    # Look for input fields
    inputs = page.evaluate('''() => Array.from(document.querySelectorAll('input')).map(e =>
        ({id: e.id, name: e.name, type: e.type, placeholder: e.placeholder, visible: e.offsetParent !== null}))''')

    # Find the VKN input field
    vkn_input = None
    for inp in inputs:
        if inp.get("visible") and inp.get("type") == "text":
            vkn_input = inp
            break

    if vkn_input:
        selector = f"#{vkn_input['id']}" if vkn_input.get("id") else f"input[name='{vkn_input['name']}']"
        page.locator(selector).fill(vkn)
        time.sleep(1)

        # Find and click submit/query button
        page.evaluate('''() => {
            const btns = document.querySelectorAll('button, input[type="submit"]');
            for (const b of btns) {
                if (b.offsetParent !== null && (b.textContent.includes('Sorgula') || b.textContent.includes('Doğrula') || b.type === 'submit')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }''')
        time.sleep(8)

        body = page.inner_text("body")

    return _parse_gib_result(vkn, body)


def _parse_gib_result(vkn: str, body: str) -> dict:
    result = {
        "vkn": vkn,
        "source": "GIB — Gelir İdaresi Başkanlığı (Revenue Administration), Republic of Turkey",
    }

    # Try to extract company/taxpayer name
    name_match = re.search(r"(?:Unvan|Adı Soyadı|Mükellef|Ticaret Unvanı)\s*[:\n]\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    name = name_match.group(1).strip() if name_match else None

    # Tax office
    office_match = re.search(r"(?:Vergi Dairesi|Tax Office)\s*[:\n]\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    tax_office = office_match.group(1).strip() if office_match else None

    # Status
    status_match = re.search(r"(?:Durum|Status|Mükellefiyet)\s*[:\n]\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    status = status_match.group(1).strip() if status_match else None

    if name or tax_office or status:
        result["found"] = True
        result["legal_name"] = name
        result["tax_office"] = tax_office
        result["status"] = status
        result["validation_source"] = {
            "registry": "Gelir İdaresi Başkanlığı (GIB), Republic of Turkey",
            "url": "https://sorgu.gib.gov.tr/mukellef-bilgileri-dogrulama",
            "record_id": vkn,
            "how_to_reproduce": f"Visit GIB portal → Mükellef Bilgileri Doğrulama → Enter VKN: {vkn}",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    else:
        result["found"] = False
        result["note"] = f"VKN {vkn} not found or GIB portal did not return structured data"
        result["raw_snippet"] = body[:500]

    return result


def gib_vkn_verify(vkn: str, entity_name: str = "", max_retries: int = 2) -> dict:
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS:
        return {"vkn": vkn, "found": False, "note": "Multilogin not configured"}

    safe_vkn = re.sub(r"[^0-9]", "", vkn)
    if len(safe_vkn) != 10:
        return {"vkn": vkn, "found": False, "note": "VKN must be 10 digits"}

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {"vkn": vkn, "found": False, "note": "All profiles busy — try later"}

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return _do_gib_lookup(port, safe_vkn, profile_id)
            except Exception as e:
                log.warning("GIB attempt %d/%d failed (VKN %s): %s", attempt + 1, max_retries, vkn, e)
                if attempt == max_retries - 1:
                    return {"vkn": vkn, "found": False, "error": str(e)[:200], "note": "GIB lookup failed after retries"}
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)
