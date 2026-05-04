"""
FBR Active Taxpayer List (ATL) verification via Multilogin + IRIS portal.

Uses a fixed pool of 5 permanent Multilogin profiles with PK residential
proxy to access FBR IRIS 2.0 portal.  Profiles are reused across lookups —
never created or deleted at runtime (Multilogin bans rapid create/delete).

Each lookup: acquire profile from pool → launch → Playwright CDP → IRIS →
solve CAPTCHA → extract result → stop profile → return to pool.

All credentials from Azure Key Vault — nothing hardcoded.
"""

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

log = logging.getLogger("crawl-gateway")

# ---------------------------------------------------------------------------
# Credentials (injected from keyvault at module load via init())
# ---------------------------------------------------------------------------
_MLX_EMAIL = None
_MLX_PASSWORD = None
_MLX_FOLDER_ID = None
_MLX_PROXY_USER = None
_MLX_PROXY_PASS = None
_ANTHROPIC_KEY = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

# Auth token cache
_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

# Profile pool — queue of available profile IDs
_pool: queue.Queue = queue.Queue()
_pool_initialized = False


def init(get_secret):
    """Initialize credentials from Key Vault. Call once at startup."""
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID
    global _MLX_PROXY_USER, _MLX_PROXY_PASS, _ANTHROPIC_KEY
    global _POOL_PROFILE_IDS

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    _MLX_PROXY_USER = get_secret("multilogin-proxy-user")
    _MLX_PROXY_PASS = get_secret("multilogin-proxy-pass")
    _ANTHROPIC_KEY = get_secret("anthropic-api-key")

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    if not _MLX_PASSWORD:
        log.warning("multilogin-password not in Key Vault — FBR ATL disabled")
    elif not _POOL_PROFILE_IDS:
        log.warning("multilogin-pool-profiles not set — FBR ATL disabled")
    else:
        log.info("FBR ATL ready: %d pool profiles", len(_POOL_PROFILE_IDS))


def _init_pool():
    """Load profile IDs into the queue (once)."""
    global _pool_initialized
    if _pool_initialized:
        return
    for pid in _POOL_PROFILE_IDS:
        _pool.put(pid)
    _pool_initialized = True


# ---------------------------------------------------------------------------
# Multilogin helpers
# ---------------------------------------------------------------------------

def _get_token() -> str:
    """Get a cached Multilogin auth token, refreshing if near expiry."""
    global _cached_token, _token_expiry
    with _token_lock:
        if time.monotonic() < _token_expiry and _cached_token:
            return _cached_token
        resp = requests.post(
            "https://api.multilogin.com/user/signin",
            json={
                "email": _MLX_EMAIL,
                "password": hashlib.md5(_MLX_PASSWORD.encode()).hexdigest(),
            },
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
    """Launch a Multilogin profile headless. Returns CDP port."""
    url = (
        f"https://launcher.mlx.yt:45001/api/v2/profile"
        f"/f/{_MLX_FOLDER_ID}/p/{profile_id}"
        f"/start?automation_type=playwright&headless_mode=true"
    )
    resp = requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        verify=False,
        timeout=60,
    )
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX launch failed: {data['status']['message']}")
    return int(data["data"]["port"])


def _stop_profile(profile_id: str):
    """Stop a running profile (but never delete it)."""
    try:
        subprocess.run(
            [str(_CLI_PATH), "profile-stop", "--profile-id", profile_id],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


def _solve_captcha(canvas_data_url: str) -> str:
    """Solve FBR numeric CAPTCHA using Claude Haiku vision."""
    img_data = canvas_data_url.split(",")[1]
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 20,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": "What are the digits shown in this CAPTCHA image? Reply with ONLY the digits, nothing else.",
                        },
                    ],
                }
            ],
        },
        headers={
            "x-api-key": _ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ---------------------------------------------------------------------------
# Core lookup — runs in a dedicated thread to avoid asyncio conflicts
# ---------------------------------------------------------------------------

def _do_fbr_lookup(port: int, ntn: str, profile_id: str) -> dict:
    """Run FBR IRIS lookup in a clean thread with its own Playwright.

    If the thread hangs, we force-stop the Multilogin profile which kills
    the browser process and unblocks everything.
    """
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
                        "username": _MLX_PROXY_USER,
                        "password": _MLX_PROXY_PASS,
                    },
                    ignore_https_errors=True,
                )
                page = context.new_page()
                try:
                    page.goto(
                        "https://iris.fbr.gov.pk/",
                        timeout=60000,
                        wait_until="domcontentloaded",
                    )
                    page.click('a:has-text("Verifications")', timeout=15000)
                    page.wait_for_selector(
                        "text=Active Taxpayer List", timeout=30000
                    )

                    # Select NTN
                    page.click("text=-- Select --", timeout=10000)
                    time.sleep(1)
                    page.locator('li:has-text("NTN")').first.click()
                    time.sleep(2)

                    # Fill NTN
                    page.fill("#regNo", ntn)

                    # Solve CAPTCHA
                    captcha_data = page.evaluate(
                        "() => { const c = document.querySelector('canvas'); "
                        "return c ? c.toDataURL('image/png') : null; }"
                    )
                    if not captcha_data:
                        raise RuntimeError("CAPTCHA canvas not found")

                    captcha_text = _solve_captcha(captcha_data)
                    log.info("FBR CAPTCHA solved: %s (NTN: %s)", captcha_text, ntn)

                    page.locator('input[placeholder="Enter "]').first.fill(
                        captcha_text
                    )
                    time.sleep(0.5)
                    page.locator("button", has_text="VERIFY").first.click()
                    time.sleep(4)

                    body = page.inner_text("body")
                    result.update(_parse_atl_result(ntn, body))
                finally:
                    page.close()
                    context.close()
                    browser.close()
        except Exception as e:
            error = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=90)  # 90s hard timeout (was 120 — tighter now)

    if t.is_alive():
        # Thread is stuck — force-kill the browser by stopping the profile.
        # This kills the Mimic process and unblocks the Playwright thread.
        log.error("FBR lookup HUNG for NTN %s (profile %s) — force-stopping",
                  ntn, profile_id[:8])
        _stop_profile(profile_id)
        # Give the thread a moment to die after browser is killed
        t.join(timeout=10)
        raise RuntimeError("FBR lookup timed out (90s) — profile force-stopped")
    if error:
        raise error
    return result


def _parse_atl_result(ntn: str, body: str) -> dict:
    """Parse the IRIS ATL result page into a structured dict."""
    result = {"ntn": ntn, "source": "FBR IRIS 2.0 — Active Taxpayer List (Income Tax)"}

    def _extract(label: str) -> str:
        match = re.search(rf"{label}:\s*(.+?)(?:\n|$)", body)
        return match.group(1).strip() if match else None

    reg_no = _extract("Registration No")
    name = _extract("Name")
    biz_name = _extract("Business Name")
    filing = _extract("Filing Status")
    check_date = _extract("Filing Status Checking Date")

    if filing:
        result["status"] = "ACTIVE" if "active" in filing.lower() else "INACTIVE"
        result["legal_name"] = name
        result["business_name"] = biz_name
        result["filing_status"] = filing
        result["checking_date"] = check_date
        result["registration_number"] = reg_no
        result["method"] = (
            "Multilogin anti-detect browser + PK residential proxy → "
            "FBR IRIS 2.0 ATL (Income Tax) with CAPTCHA solve"
        )
    elif "Invalid Captcha" in body or "invalid captcha" in body.lower():
        raise RuntimeError("CAPTCHA solve failed — retrying")
    else:
        result["status"] = "NOT_FOUND"
        result["note"] = "NTN not found in FBR Active Taxpayer List"

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fbr_atl_verify(ntn: str, max_retries: int = 2) -> dict:
    """
    Verify NTN on FBR Active Taxpayer List (Income Tax) via IRIS portal.

    Acquires a profile from the fixed pool (5 permanent profiles), launches
    it, runs the lookup, stops it, and returns the profile to the pool.
    Up to 5 concurrent lookups.

    Returns dict with: ntn, status, legal_name, business_name, filing_status,
    checking_date, source, method, note
    """
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS:
        return {
            "ntn": ntn,
            "status": "UNAVAILABLE",
            "note": "Multilogin not configured — FBR ATL disabled",
        }

    safe_ntn = re.sub(r"[^0-9]", "", ntn)
    if not safe_ntn:
        return {"ntn": ntn, "status": "ERROR", "note": "Invalid NTN format"}

    _init_pool()

    # Acquire a profile from the pool (wait up to 2 min)
    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {
            "ntn": ntn,
            "status": "ERROR",
            "note": "FBR lookup queue full — all 5 profiles busy, try again later",
        }

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                result = _do_fbr_lookup(port, safe_ntn, profile_id)
                return result
            except Exception as e:
                log.warning(
                    "FBR ATL attempt %d/%d failed (NTN %s, profile %s): %s",
                    attempt + 1, max_retries, ntn, profile_id[:8], e,
                )
                if attempt == max_retries - 1:
                    return {
                        "ntn": ntn,
                        "status": "ERROR",
                        "error": str(e)[:200],
                        "note": "FBR IRIS lookup failed after retries",
                    }
            finally:
                _stop_profile(profile_id)
    finally:
        # Always return profile to pool
        _pool.put(profile_id)
