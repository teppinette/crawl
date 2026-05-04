"""
DGFT IEC (Import-Export Code) verification via Multilogin + DGFT portal.

Uses Multilogin anti-detect browser with IN residential proxy to access
India's DGFT portal, solve the alphanumeric CAPTCHA via Claude Haiku,
and extract IEC details including status, branches, and directors.

All credentials from Azure Key Vault — nothing hardcoded.
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

log = logging.getLogger("crawl-gateway")

# ---------------------------------------------------------------------------
# Credentials (injected from keyvault via init())
# ---------------------------------------------------------------------------
_MLX_EMAIL = None
_MLX_PASSWORD = None
_MLX_FOLDER_ID = None
_MLX_PROXY_USER_IN = None
_MLX_PROXY_PASS_IN = None
_ANTHROPIC_KEY = None
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
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID
    global _MLX_PROXY_USER_IN, _MLX_PROXY_PASS_IN, _ANTHROPIC_KEY
    global _POOL_PROFILE_IDS

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    _ANTHROPIC_KEY = get_secret("anthropic-api-key")

    # Get IN proxy creds from Multilogin CLI
    try:
        result = subprocess.run(
            [str(_CLI_PATH), "proxy-get", "--country-code", "in",
             "--protocol", "http", "--type", "rotating"],
            capture_output=True, text=True, timeout=15,
        )
        if result.stdout.strip():
            parts = result.stdout.strip().split(":")
            _MLX_PROXY_USER_IN = parts[2]
            _MLX_PROXY_PASS_IN = parts[3]
    except Exception:
        pass

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    if _MLX_PASSWORD and _MLX_PROXY_USER_IN and _POOL_PROFILE_IDS:
        log.info("DGFT IEC ready: %d pool profiles, IN proxy configured", len(_POOL_PROFILE_IDS))
    else:
        log.warning("DGFT IEC not fully configured — missing credentials")


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


def _solve_captcha(img_data_b64: str) -> str:
    """Solve DGFT alphanumeric CAPTCHA (case-sensitive, 6 chars)."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 30,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": img_data_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Read the CAPTCHA text exactly. It has 6 characters: "
                                "mixed uppercase letters, lowercase letters, and digits. "
                                "Case matters. Reply with ONLY the 6 characters, nothing else."
                            ),
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
# Core lookup
# ---------------------------------------------------------------------------

def _do_dgft_lookup(port: int, iec: str, firm_prefix: str, profile_id: str) -> dict:
    """Run DGFT IEC lookup in a clean thread."""
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
                        "username": _MLX_PROXY_USER_IN,
                        "password": _MLX_PROXY_PASS_IN,
                    },
                    ignore_https_errors=True,
                )
                page = context.new_page()
                try:
                    result.update(_navigate_and_extract(page, iec, firm_prefix))
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
        log.error("DGFT lookup HUNG for IEC %s — force-stopping profile %s",
                  iec, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("DGFT lookup timed out (90s)")
    if error:
        raise error
    return result


def _navigate_and_extract(page, iec: str, firm_prefix: str) -> dict:
    """Navigate DGFT, fill form, solve CAPTCHA, extract results."""
    for attempt in range(3):
        page.goto(
            "https://www.dgft.gov.in/CP/?opt=view-any-ice",
            timeout=60000, wait_until="domcontentloaded",
        )
        page.click("text=View Any IEC", timeout=10000)
        time.sleep(3)

        page.fill("#iecNo", iec)
        page.fill("#entity", firm_prefix)

        # Get CAPTCHA by id
        captcha_el = page.locator("#captcha")
        captcha_el.screenshot(path="/tmp/dgft_captcha.png")
        with open("/tmp/dgft_captcha.png", "rb") as f:
            img_data = base64.b64encode(f.read()).decode()

        captcha_text = _solve_captcha(img_data)
        log.info("DGFT CAPTCHA solved: %s (IEC: %s, attempt %d)",
                 captcha_text, iec, attempt + 1)

        page.fill("#txt_Captcha", captcha_text)
        time.sleep(0.5)
        page.locator("button", has_text="View IEC").first.click()
        time.sleep(5)

        body = page.inner_text("body")
        if "valid captcha" in body.lower() or "enter valid" in body.lower():
            log.warning("DGFT CAPTCHA wrong (attempt %d), retrying...", attempt + 1)
            continue

        return _parse_dgft_result(iec, body)

    raise RuntimeError("DGFT CAPTCHA failed after 3 attempts")


def _parse_dgft_result(iec: str, body: str) -> dict:
    """Parse DGFT IEC result page into structured dict."""
    result = {
        "iec": iec,
        "source": "DGFT — Directorate General of Foreign Trade (dgft.gov.in)",
    }

    def _extract(label: str) -> str:
        match = re.search(rf"{label}\s*\n\s*(.+?)(?:\n|$)", body)
        return match.group(1).strip() if match else None

    iec_number = _extract("IEC Number")
    iec_status = _extract("IEC Status")
    del_status = _extract("DEL Status")
    issuance_date = _extract("IEC Issuance Date")
    firm_name = _extract("Firm Name")
    nature = _extract("Nature of concern/Firm")
    category = _extract("Category of Exporters")
    file_number = _extract("File Number")
    file_date = _extract("File Date")
    ra_office = _extract("DGFT RA Office")

    # Extract address (multiline)
    addr_match = re.search(r"Address\s*\n\s*(.+?)(?:\nBRANCH|$)", body, re.DOTALL)
    address = addr_match.group(1).strip().replace("\n", ", ") if addr_match else None

    # Extract branches
    branches = []
    branch_section = re.search(
        r"BRANCH DETAILS.*?(?:Branch Code.*?\n)(.*?)(?:DETAILS OF|Showing|\Z)",
        body, re.DOTALL
    )
    if branch_section:
        rows = branch_section.group(1).strip().split("\n")
        i = 0
        while i < len(rows):
            row = rows[i].strip()
            if row and row[0].isdigit():
                parts = []
                parts.append(row)
                # Next lines might be GSTIN and address
                while i + 1 < len(rows) and not rows[i + 1].strip()[:1].isdigit():
                    i += 1
                    parts.append(rows[i].strip())
                branch_text = " ".join(parts)
                # Try to extract branch code, GSTIN, address
                branch_match = re.match(r"(\d+)\s+(\S+)\s+(.*)", branch_text)
                if branch_match:
                    branches.append({
                        "branch_code": branch_match.group(1),
                        "gstin": branch_match.group(2),
                        "address": branch_match.group(3).strip(),
                    })
            i += 1

    # Extract directors/proprietors
    directors = []
    dir_section = re.search(
        r"DETAILS OF PROPRIETOR.*?(?:Sl\. No\..*?Name.*?PAN.*?\n)(.*?)(?:RCMC|Showing|\Z)",
        body, re.DOTALL
    )
    if dir_section:
        rows = dir_section.group(1).strip().split("\n")
        for row in rows:
            row = row.strip()
            if not row or row.startswith("Showing"):
                continue
            # Format: "1\tHARSHVARDHAN BOTHRA\tBYIXXXX6L"
            parts = re.split(r"\t+|\s{2,}", row)
            if len(parts) >= 3 and parts[0].isdigit():
                directors.append({
                    "name": parts[1].strip(),
                    "pan": parts[2].strip(),
                })
            elif len(parts) >= 2 and parts[0].isdigit():
                directors.append({"name": parts[1].strip()})

    if iec_status or firm_name:
        result["found"] = True
        result["iec_number"] = iec_number
        result["iec_status"] = iec_status
        result["del_status"] = del_status
        result["issuance_date"] = issuance_date
        result["firm_name"] = firm_name
        result["nature_of_concern"] = nature
        result["category_of_exporters"] = category
        result["file_number"] = file_number
        result["file_date"] = file_date
        result["ra_office"] = ra_office
        result["address"] = address
        result["branches"] = branches if branches else None
        result["directors"] = directors if directors else None
        result["validation_source"] = {
            "registry": "Directorate General of Foreign Trade (DGFT), Ministry of Commerce and Industry, Government of India",
            "url": "https://www.dgft.gov.in/CP/?opt=view-any-ice",
            "record_id": file_number,
            "iec_number": iec_number,
            "how_to_reproduce": (
                f"Visit https://www.dgft.gov.in/CP/?opt=view-any-ice → "
                f"Click 'View Any IEC' → Enter IEC: {iec} → "
                f"Enter Firm Name: first 3 chars of firm name → "
                f"Solve CAPTCHA → Click 'View IEC'"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    elif "not available" in body.lower() or "no record" in body.lower():
        result["found"] = False
        result["note"] = f"IEC {iec} not found in DGFT"
    else:
        raise RuntimeError("DGFT returned unexpected response")

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dgft_iec_verify(iec: str, entity_name: str = "", max_retries: int = 2) -> dict:
    """
    Verify IEC on DGFT portal. IEC = PAN for Indian companies.

    Returns dict with: iec, found, iec_status, del_status, firm_name,
    address, branches, directors, etc.
    """
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS or not _MLX_PROXY_USER_IN:
        return {
            "iec": iec,
            "found": False,
            "note": "Multilogin/DGFT not configured",
        }

    safe_iec = re.sub(r"[^A-Za-z0-9]", "", iec).upper()
    if len(safe_iec) != 10:
        return {"iec": iec, "found": False, "note": "IEC must be 10 characters (= PAN)"}

    # Firm prefix: first 3 chars of entity name, or "___" (DGFT requires >= 3)
    firm_prefix = entity_name[:3].upper() if len(entity_name) >= 3 else "___"

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {"iec": iec, "found": False, "note": "All profiles busy — try later"}

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return _do_dgft_lookup(port, safe_iec, firm_prefix, profile_id)
            except Exception as e:
                log.warning("DGFT attempt %d/%d failed (IEC %s): %s",
                            attempt + 1, max_retries, iec, e)
                if attempt == max_retries - 1:
                    return {
                        "iec": iec,
                        "found": False,
                        "error": str(e)[:200],
                        "note": "DGFT lookup failed after retries",
                    }
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)
