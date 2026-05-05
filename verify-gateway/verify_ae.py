"""
UAE FTA TRN verification via Multilogin + AE residential proxy.

Uses Multilogin anti-detect browser with UAE proxy to access tax.gov.ae
TRN verification portal. Has image CAPTCHA solved by Claude Haiku.

Input: TRN (15-digit Tax Registration Number).
Returns: entity name, registration status.
"""

import base64
import hashlib
import json
import logging
import queue
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
_MLX_PROXY_USER_AE = None
_MLX_PROXY_PASS_AE = None
_ANTHROPIC_KEY = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

_pool: queue.Queue = queue.Queue()
_pool_initialized = False


def init(get_secret):
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID
    global _MLX_PROXY_USER_AE, _MLX_PROXY_PASS_AE, _ANTHROPIC_KEY, _POOL_PROFILE_IDS

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    _ANTHROPIC_KEY = get_secret("anthropic-api-key")

    # Get AE proxy creds from Multilogin CLI
    try:
        result = subprocess.run(
            [str(_CLI_PATH), "proxy-get", "--country-code", "ae",
             "--protocol", "http", "--type", "rotating"],
            capture_output=True, text=True, timeout=15,
        )
        if result.stdout.strip():
            parts = result.stdout.strip().split(":")
            _MLX_PROXY_USER_AE = parts[2]
            _MLX_PROXY_PASS_AE = parts[3]
    except Exception:
        pass

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    if _MLX_PASSWORD and _MLX_PROXY_USER_AE and _POOL_PROFILE_IDS:
        log.info("AE FTA TRN ready: %d pool profiles, AE proxy configured", len(_POOL_PROFILE_IDS))
    else:
        log.warning("AE FTA TRN not fully configured")


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


def _solve_captcha(img_data_b64: str) -> str:
    """Solve FTA image CAPTCHA using Claude Haiku vision."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 30,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data_b64}},
                    {"type": "text", "text": "Read the CAPTCHA text exactly. Reply with ONLY the characters shown, nothing else."},
                ],
            }],
        },
        headers={"x-api-key": _ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def _do_fta_lookup(port: int, trn: str, profile_id: str) -> dict:
    result = {}
    error = None

    def _run():
        nonlocal result, error
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.new_context(
                    proxy={
                        "server": "http://gate.multilogin.com:8080",
                        "username": _MLX_PROXY_USER_AE,
                        "password": _MLX_PROXY_PASS_AE,
                    },
                    ignore_https_errors=True,
                )
                page = context.new_page()
                try:
                    result.update(_navigate_and_extract(page, trn))
                finally:
                    page.close()
                    context.close()
                    browser.close()
        except Exception as e:
            error = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=120)

    if t.is_alive():
        log.error("FTA lookup HUNG for TRN %s — force-stopping profile %s", trn, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("FTA lookup timed out (120s)")
    if error:
        raise error
    return result


def _navigate_and_extract(page, trn: str) -> dict:
    """Navigate FTA TRN verification portal."""
    page.goto("https://tax.gov.ae/en/default.aspx", timeout=60000, wait_until="domcontentloaded")
    time.sleep(10)

    body = page.inner_text("body")

    # Look for TRN verification input
    # The FTA homepage has a TRN verification widget in the header
    trn_input = page.locator("input[id*='TRN' i], input[id*='trn' i], input[placeholder*='TRN']").first
    if trn_input.is_visible():
        trn_input.click()
        time.sleep(0.5)
        page.keyboard.type(trn, delay=80)
        time.sleep(1)
    else:
        # Try clicking "TRN verification" link first
        trn_link = page.locator("text=TRN").first
        if trn_link.is_visible():
            trn_link.click()
            time.sleep(3)
            trn_input = page.locator("input[type='text']").first
            trn_input.click()
            time.sleep(0.5)
            page.keyboard.type(trn, delay=80)
            time.sleep(1)

    # Look for CAPTCHA
    captcha_img = page.locator("img[id*='aptcha' i], img[alt*='aptcha' i], img[class*='aptcha' i]").first
    if captcha_img.is_visible():
        captcha_img.screenshot(path="/tmp/fta_captcha.png")
        with open("/tmp/fta_captcha.png", "rb") as f:
            img_data = base64.b64encode(f.read()).decode()
        captcha_text = _solve_captcha(img_data)
        log.info("FTA CAPTCHA solved: %s (TRN: %s)", captcha_text, trn)

        captcha_input = page.locator("input[id*='aptcha' i], input[id*='Code' i]").first
        if captcha_input.is_visible():
            captcha_input.click()
            time.sleep(0.5)
            page.keyboard.type(captcha_text, delay=80)

    time.sleep(1)

    # Click verify
    page.evaluate('''() => {
        const btns = document.querySelectorAll('button, input[type="submit"], a.btn, span[onclick]');
        for (const b of btns) {
            const txt = (b.textContent || b.value || '').toLowerCase();
            if (b.offsetParent !== null && (txt.includes('verify') || txt.includes('search') || txt.includes('submit'))) {
                b.click();
                return true;
            }
        }
        if (typeof doValidationVerifyTRN === 'function') { doValidationVerifyTRN(''); return true; }
        return false;
    }''')
    time.sleep(8)

    body = page.inner_text("body")
    return _parse_fta_result(trn, body)


def _parse_fta_result(trn: str, body: str) -> dict:
    result = {
        "trn": trn,
        "source": "Federal Tax Authority (FTA), United Arab Emirates",
    }

    # Extract entity name
    name_match = re.search(r"(?:Name|الاسم)\s*[:\n]\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    name = name_match.group(1).strip() if name_match else None

    if not name:
        trn_name_match = re.search(rf"{trn}\s*\n\s*(.+?)(?:\n|$)", body)
        name = trn_name_match.group(1).strip() if trn_name_match else None

    if name and name != trn:
        result["found"] = True
        result["legal_name"] = name
        result["validation_source"] = {
            "registry": "Federal Tax Authority (FTA), UAE",
            "url": "https://tax.gov.ae",
            "record_id": trn,
            "how_to_reproduce": f"Visit tax.gov.ae → TRN Verification → Enter TRN: {trn} → Solve CAPTCHA → Verify",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    else:
        result["found"] = False
        result["note"] = f"TRN {trn} — portal loaded but could not extract entity name"
        result["raw_snippet"] = body[:500]

    return result


def fta_trn_verify(trn: str, entity_name: str = "", max_retries: int = 2) -> dict:
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS or not _MLX_PROXY_USER_AE:
        return {"trn": trn, "found": False, "note": "Multilogin/AE proxy not configured"}

    safe_trn = re.sub(r"[^0-9]", "", trn)
    if len(safe_trn) != 15:
        return {"trn": trn, "found": False, "note": "TRN must be 15 digits"}

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {"trn": trn, "found": False, "note": "All profiles busy — try later"}

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return _do_fta_lookup(port, safe_trn, profile_id)
            except Exception as e:
                log.warning("FTA attempt %d/%d failed (TRN %s): %s", attempt + 1, max_retries, trn, e)
                if attempt == max_retries - 1:
                    return {"trn": trn, "found": False, "error": str(e)[:200], "note": "FTA lookup failed after retries"}
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)
