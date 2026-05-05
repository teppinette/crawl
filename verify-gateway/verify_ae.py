"""
UAE FTA TRN (Tax Registration Number) verification via Playwright.

Uses UAE Federal Tax Authority portal (tax.gov.ae) TRN verification.
Has image CAPTCHA — solved via Claude Haiku vision OCR.

Input: TRN (15-digit Tax Registration Number).
Returns: entity name, registration status.
"""

import base64
import hashlib
import json
import logging
import os
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
_ANTHROPIC_KEY = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

_pool: queue.Queue = queue.Queue()
_pool_initialized = False


def init(get_secret):
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID, _ANTHROPIC_KEY, _POOL_PROFILE_IDS
    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    _ANTHROPIC_KEY = get_secret("anthropic-api-key")
    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []
    if _MLX_PASSWORD and _POOL_PROFILE_IDS:
        log.info("AE FTA TRN ready: %d pool profiles", len(_POOL_PROFILE_IDS))


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
                context = browser.new_context(ignore_https_errors=True)
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
    t.join(timeout=90)

    if t.is_alive():
        log.error("FTA lookup HUNG for TRN %s — force-stopping profile %s", trn, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("FTA lookup timed out (90s)")
    if error:
        raise error
    return result


def _navigate_and_extract(page, trn: str) -> dict:
    """Navigate FTA TRN verification portal."""
    for attempt in range(3):
        page.goto("https://tax.gov.ae/en/default.aspx", timeout=60000, wait_until="domcontentloaded")
        time.sleep(8)

        # Look for TRN verification section — it's a modal/popup triggered by header link
        # Try clicking "TRN verification" link
        trn_link = page.locator("text=TRN verification").first
        if trn_link.is_visible():
            trn_link.click()
            time.sleep(3)

        # Fill TRN input
        trn_input = page.locator("input[id*='TRN'], input[id*='trn'], input[placeholder*='TRN']").first
        if not trn_input.is_visible():
            # Try alternative: look for any visible text input near CAPTCHA
            trn_input = page.locator("input[type='text']").first

        trn_input.fill(trn)
        time.sleep(1)

        # Find and solve CAPTCHA
        captcha_img = page.locator("img[id*='aptcha'], img[id*='RadCaptcha'], img[alt*='aptcha']").first
        if captcha_img.is_visible():
            captcha_img.screenshot(path="/tmp/fta_captcha.png")
            with open("/tmp/fta_captcha.png", "rb") as f:
                img_data = base64.b64encode(f.read()).decode()
            captcha_text = _solve_captcha(img_data)
            log.info("FTA CAPTCHA solved: %s (TRN: %s, attempt %d)", captcha_text, trn, attempt + 1)

            # Fill CAPTCHA
            captcha_input = page.locator("input[id*='aptcha'], input[id*='Code']").first
            if captcha_input.is_visible():
                captcha_input.fill(captcha_text)

        time.sleep(1)

        # Click verify/submit
        page.evaluate('''() => {
            const btns = document.querySelectorAll('button, input[type="submit"], a.btn');
            for (const b of btns) {
                const txt = b.textContent || b.value || '';
                if (b.offsetParent !== null && (txt.includes('Verify') || txt.includes('Search') || txt.includes('Submit'))) {
                    b.click();
                    return true;
                }
            }
            // Try calling the validation function directly
            if (typeof doValidationVerifyTRN === 'function') {
                doValidationVerifyTRN('');
                return true;
            }
            return false;
        }''')
        time.sleep(5)

        body = page.inner_text("body")

        if "invalid" in body.lower() and "captcha" in body.lower():
            log.warning("FTA CAPTCHA wrong (attempt %d), retrying...", attempt + 1)
            continue

        return _parse_fta_result(trn, body)

    raise RuntimeError("FTA CAPTCHA failed after 3 attempts")


def _parse_fta_result(trn: str, body: str) -> dict:
    result = {
        "trn": trn,
        "source": "Federal Tax Authority (FTA), United Arab Emirates",
    }

    # Extract entity name from verification result
    name_match = re.search(r"(?:Name|الاسم)\s*[:\n]\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    name = name_match.group(1).strip() if name_match else None

    # Also try: TRN verification shows name in a result div
    if not name:
        # Look for pattern: TRN followed by name
        trn_name_match = re.search(rf"{trn}\s*\n\s*(.+?)(?:\n|$)", body)
        name = trn_name_match.group(1).strip() if trn_name_match else None

    if name:
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
        result["note"] = f"TRN {trn} not found or FTA portal did not return structured data"
        result["raw_snippet"] = body[:500]

    return result


def fta_trn_verify(trn: str, entity_name: str = "", max_retries: int = 2) -> dict:
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS:
        return {"trn": trn, "found": False, "note": "Multilogin not configured"}

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
