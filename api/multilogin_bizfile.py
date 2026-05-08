"""
Singapore Bizfile (ACRA) company verification via Multilogin.

Uses Multilogin anti-detect browser to search bizfile.gov.sg.
No CAPTCHA, no proxy needed (SG not geo-blocked).

Free search returns: company name, UEN, status, former name, industry, address.
Directors/shareholders require paid Business Profile (SGD $5.50) — not available here.

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
# Credentials (injected from keyvault via init())
# ---------------------------------------------------------------------------
_MLX_EMAIL = None
_MLX_PASSWORD = None
_MLX_FOLDER_ID = None
_MLX_PROXY_USER = None
_MLX_PROXY_PASS = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

# Auth token cache
_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

# Profile pool
_pool: queue.Queue = queue.Queue()
_pool_initialized = False


def init(get_secret):
    """Initialize credentials from Key Vault. Call once at startup."""
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID, _MLX_PROXY_USER, _MLX_PROXY_PASS, _POOL_PROFILE_IDS

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    _MLX_PROXY_USER = get_secret("multilogin-proxy-user")
    _MLX_PROXY_PASS = get_secret("multilogin-proxy-pass")

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    if _MLX_PASSWORD and _POOL_PROFILE_IDS:
        log.info("Bizfile SG ready: %d pool profiles", len(_POOL_PROFILE_IDS))
    else:
        log.warning("Bizfile SG not configured — missing credentials")


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
    url = (
        f"https://launcher.mlx.yt:45001/api/v2/profile"
        f"/f/{_MLX_FOLDER_ID}/p/{profile_id}"
        f"/start?automation_type=playwright&headless_mode=true"
    )
    resp = requests.get(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        verify=False, timeout=60,
    )
    data = resp.json()
    if data["status"]["http_code"] != 200:
        raise RuntimeError(f"MLX launch failed: {data['status']['message']}")
    return int(data["data"]["port"])


def _stop_profile(profile_id: str):
    try:
        subprocess.run(
            [str(_CLI_PATH), "profile-stop", "--profile-id", profile_id],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core lookup
# ---------------------------------------------------------------------------

def _do_bizfile_lookup(port: int, entity_name: str, uen: str, profile_id: str) -> dict:
    """Run Bizfile lookup in a clean thread."""
    result = {}
    error = None

    def _run():
        nonlocal result, error
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                ctx_kwargs = {"ignore_https_errors": True}
                if _MLX_PROXY_USER and _MLX_PROXY_PASS:
                    ctx_kwargs["proxy"] = {
                        "server": "gate.multilogin.com:8080",
                        "username": _MLX_PROXY_USER,
                        "password": _MLX_PROXY_PASS,
                    }
                context = browser.new_context(**ctx_kwargs)
                page = context.new_page()
                try:
                    result.update(_navigate_and_extract(page, entity_name, uen))
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
        log.error("Bizfile lookup HUNG for '%s' — force-stopping profile %s",
                  entity_name, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("Bizfile lookup timed out (90s)")
    if error:
        raise error
    return result


def _navigate_and_extract(page, entity_name: str, uen: str) -> dict:
    """Navigate Bizfile, search, extract results.

    Bizfile uses reCAPTCHA v3 (invisible) — must execute grecaptcha.execute()
    before the search will fire. React input requires native value setter
    to update component state.
    """
    search_term = uen if uen else entity_name

    page.goto(
        "https://www.bizfile.gov.sg",
        timeout=60000, wait_until="domcontentloaded",
    )
    # Wait for page + reCAPTCHA scripts to fully load
    time.sleep(12)

    # Wait for grecaptcha to be available (up to 15s)
    page.wait_for_function("typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'", timeout=15000)

    # Set React input state via native value setter + dispatch events
    page.evaluate(f'''() => {{
        const input = document.getElementById('input-search-bar');
        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        nativeInputValueSetter.call(input, {json.dumps(search_term)});
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }}''')
    time.sleep(2)

    # Execute reCAPTCHA v3 to get token (required for search to work)
    recaptcha_result = page.evaluate('''async () => {
        try {
            await grecaptcha.execute(
                '6LfIEuIqAAAAAPGiSbBEzmpmoZvlNX50t2rtUiow', {action: 'search'});
            return {success: true};
        } catch(e) {
            return {success: false, error: e.toString()};
        }
    }''')

    if not recaptcha_result.get("success"):
        raise RuntimeError(f"reCAPTCHA failed: {recaptcha_result.get('error')}")

    # Click search button (force=True — button is in filter section)
    page.locator("#federated-search-dropdown-bottom-search-btn").click(force=True)
    time.sleep(12)

    body = page.inner_text("body")
    return _parse_bizfile_result(entity_name, uen, body)


def _parse_bizfile_result(entity_name: str, uen: str, body: str) -> dict:
    """Parse Bizfile search result page into structured dict.

    Actual results format (from Bizfile SPA):
        INTERASIA ENERGY PTE. LTD.
        Formerly known as
        CLIFF CAPITAL PARTNERS PTE. LTD.
        UEN
        201733771N
        Entity Status
        Live Company
        Industry (SSIC)
        Wholesale of fuels and related products - 46610
        Address
        3 Coleman Street, #03-24, ...
    """
    result = {
        "entity_name": entity_name,
        "source": "ACRA Bizfile — Accounting and Corporate Regulatory Authority (bizfile.gov.sg)",
    }

    # Extract UEN (format: 9 digits + 1 letter, or T + 2 digits + 2 letters + 4 digits + 1 letter)
    uen_match = re.search(r"\b(\d{9}[A-Z]|T\d{2}[A-Z]{2}\d{4}[A-Z])\b", body)
    found_uen = uen_match.group(1) if uen_match else uen

    # Extract entity status (appears after "Entity Status\n")
    status_match = re.search(r"Entity Status\n(.+?)(?:\n|$)", body)
    status = status_match.group(1).strip() if status_match else None

    # Extract former name (appears after "Formerly known as\n")
    former_match = re.search(r"Formerly known as\n(.+?)(?:\n|$)", body)
    former_name = former_match.group(1).strip() if former_match else None

    # Extract industry/SSIC (appears after "Industry (SSIC)\n")
    ssic_match = re.search(r"Industry \(SSIC\)\n(.+?)(?:\n|$)", body)
    industry = ssic_match.group(1).strip() if ssic_match else None

    # Extract address (appears after "Address\n", ends before "More information")
    addr_match = re.search(r"Address\n(.+?)(?:\s*View Map|\nMore information|$)", body)
    address = addr_match.group(1).strip().replace(" View Map", "") if addr_match else None

    # Extract legal name — first result name, appears in caps before "Formerly" or "UEN"
    # It's the company name that appears at the start of the result block
    name_match = re.search(
        r"(?:Unable to find entity\?\n|item\(s\)\n)(.+?)(?:\nFormerly|\nUEN)",
        body,
    )
    if not name_match:
        # Try: name appears right before "Formerly known as" or "UEN"
        name_match = re.search(r"\n([A-Z][A-Z\s&.,()'-]+(?:PTE|LTD|INC|CORP|LLC|CO)[A-Z\s.,()'-]*)\n", body)
    legal_name = name_match.group(1).strip() if name_match else None

    if found_uen or status or legal_name:
        result["found"] = True
        result["uen"] = found_uen
        result["legal_name"] = legal_name
        result["status"] = status
        result["former_name"] = former_name
        result["industry"] = industry
        result["address"] = address
        result["directors_available"] = False
        result["directors_note"] = (
            "Directors/shareholders require ACRA Business Profile (SGD $5.50). "
            "Free search provides company status, UEN, and address only."
        )
        result["validation_source"] = {
            "registry": "Accounting and Corporate Regulatory Authority (ACRA), Government of Singapore",
            "url": "https://www.bizfile.gov.sg",
            "record_id": found_uen,
            "how_to_reproduce": (
                f"Visit https://www.bizfile.gov.sg → Search for "
                f"'{uen or entity_name}' → View entity details"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    elif "0 item" in body.lower() or "unable to find" in body.lower():
        result["found"] = False
        result["note"] = f"'{entity_name}' not found in ACRA Bizfile"
    else:
        result["found"] = False
        result["note"] = "Bizfile search did not return expected results"
        result["raw_snippet"] = body[:500]

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bizfile_verify(entity_name: str, uen: str = "", max_retries: int = 2) -> dict:
    """
    Verify company on Singapore Bizfile (ACRA).

    Returns dict with: entity_name, found, uen, legal_name, status,
    former_name, entity_type, industry, address, validation_source.
    Directors NOT available (paid feature).
    """
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS:
        return {
            "entity_name": entity_name,
            "found": False,
            "note": "Multilogin not configured — Bizfile SG disabled",
        }

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {
            "entity_name": entity_name,
            "found": False,
            "note": "All profiles busy — try later",
        }

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return _do_bizfile_lookup(port, entity_name, uen, profile_id)
            except Exception as e:
                log.warning("Bizfile attempt %d/%d failed ('%s'): %s",
                            attempt + 1, max_retries, entity_name, e)
                if attempt == max_retries - 1:
                    return {
                        "entity_name": entity_name,
                        "found": False,
                        "error": str(e)[:200],
                        "note": "Bizfile lookup failed after retries",
                    }
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)
