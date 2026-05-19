"""
Saudi Arabia commercial registration verification via mc.gov.sa.

Source: https://mc.gov.sa/ar/eservices/Pages/Commercial-data.aspx
Public page, no login required. BotDetect CAPTCHA solved by Claude Haiku.
Uses Multilogin anti-detect browser with SA residential proxy.

Input: CR number (10 digits) or company name
Returns: company name, CR status, activities, capital, owners/managers.
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
_MLX_PROXY_USER_SA = None
_MLX_PROXY_PASS_SA = None
_ANTHROPIC_KEY = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

_pool: queue.Queue = queue.Queue()
_pool_initialized = False

_MC_URL = "https://mc.gov.sa/ar/eservices/Pages/Commercial-data.aspx"


def init(get_secret):
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID
    global _MLX_PROXY_USER_SA, _MLX_PROXY_PASS_SA, _ANTHROPIC_KEY, _POOL_PROFILE_IDS

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")
    _ANTHROPIC_KEY = get_secret("anthropic-api-key")

    # Get SA proxy creds from Multilogin CLI
    try:
        result = subprocess.run(
            [str(_CLI_PATH), "proxy-get", "--country-code", "sa",
             "--protocol", "http", "--type", "rotating"],
            capture_output=True, text=True, timeout=15,
        )
        if result.stdout.strip():
            parts = result.stdout.strip().split(":")
            _MLX_PROXY_USER_SA = parts[2]
            _MLX_PROXY_PASS_SA = parts[3]
    except Exception:
        pass

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    if _MLX_PASSWORD and _MLX_PROXY_USER_SA and _POOL_PROFILE_IDS:
        log.info("SA MCI CR ready: %d pool profiles, SA proxy configured", len(_POOL_PROFILE_IDS))
    else:
        missing = []
        if not _MLX_PASSWORD:
            missing.append("mlx-password")
        if not _MLX_PROXY_USER_SA:
            missing.append("SA-proxy")
        if not _POOL_PROFILE_IDS:
            missing.append("pool-profiles")
        log.warning("SA MCI CR not fully configured (missing: %s)", ", ".join(missing))


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
    """Solve BotDetect image CAPTCHA using Claude Sonnet 4.6 vision (better OCR than Haiku on noisy CAPTCHAs)."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 30,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_data_b64}},
                    {"type": "text", "text": (
                        "This is a BotDetect CAPTCHA with distorted alphanumeric characters "
                        "(typically 5-6 characters, mixed upper-case letters and digits, "
                        "obscured by random lines and noise). "
                        "Read the characters carefully and reply with ONLY the characters in order — "
                        "no spaces, no explanation, no punctuation."
                    )},
                ],
            }],
        },
        headers={"x-api-key": _ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    clean = re.sub(r"[^A-Za-z0-9]", "", text)
    if len(clean) > 10:
        clean = clean[:8]
    return clean


def _do_mci_lookup(port: int, query: str, profile_id: str) -> dict:
    """Run the MCI CR lookup via Playwright on the Multilogin browser."""
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
                        "username": _MLX_PROXY_USER_SA,
                        "password": _MLX_PROXY_PASS_SA,
                    },
                    ignore_https_errors=True,
                )
                page = context.new_page()
                try:
                    result.update(_navigate_and_extract(page, query))
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
        log.error("MCI lookup HUNG for %s — force-stopping profile %s", query, profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError("MCI lookup timed out (120s)")
    if error:
        raise error
    return result


def _reload_captcha(page, img_id: str, reload_link_id: str, wait_ms: int = 8000):
    """Force-reload the BotDetect CAPTCHA image and wait until the src token changes."""
    try:
        prev_src = page.evaluate(f"document.getElementById('{img_id}')?.src || ''")
        # Click reload via the BotDetect onclick handler (bypasses any overlay)
        page.evaluate(f"document.getElementById('{reload_link_id}')?.click()")
        # Wait for the src to change (BotDetect appends a new &t=... token)
        page.wait_for_function(
            f"""(prev) => {{
                const i = document.getElementById('{img_id}');
                return i && i.src && i.src !== prev
                    && i.src.indexOf('BotDetectCaptcha.ashx?get=image') !== -1;
            }}""",
            arg=prev_src,
            timeout=wait_ms,
        )
        # Then wait for the image to actually finish loading
        page.wait_for_function(
            f"""() => {{
                const i = document.getElementById('{img_id}');
                return i && i.complete && i.naturalWidth > 50;
            }}""",
            timeout=wait_ms,
        )
    except Exception as e:
        log.info("MCI: reload_captcha wait failed (%s) — continuing", str(e)[:80])
    time.sleep(1)


def _navigate_and_extract(page, query: str) -> dict:
    """Navigate mc.gov.sa commercial registration inquiry page."""
    _SEARCH_ID = "ctl00_ctl71_g_97660838_4b9f_499e_b870_135f6e7060ef_ctl00_txtCRName"
    _CAPTCHA_INPUT_ID = "ctl00_ctl71_g_97660838_4b9f_499e_b870_135f6e7060ef_ctl00_CaptchaCodeTextBox"
    _CAPTCHA_IMG_ID = "c__catalogs_masterpage_innerpage_ctl00_ctl71_g_97660838_4b9f_499e_b870_135f6e7060ef_ctl00_examplecaptcha_CaptchaImage"
    _RELOAD_LINK_ID = "c__catalogs_masterpage_innerpage_ctl00_ctl71_g_97660838_4b9f_499e_b870_135f6e7060ef_ctl00_examplecaptcha_ReloadLink"
    _BTN_ID = "ctl00_ctl71_g_97660838_4b9f_499e_b870_135f6e7060ef_ctl00_btnSearch"

    def _dismiss_overlays():
        page.evaluate("""() => {
            // Cookie banner — click any 'متابعة' button
            document.querySelectorAll('button, a, span, div').forEach(b => {
                const t = (b.textContent || '').trim();
                if (t === 'متابعة!' || t === 'متابعة' || t === 'موافق') { try { b.click(); } catch(e){} }
            });
            // Hide any sticky cookie/overlay containers
            ['cookieConsent', 'cookie-banner', 'cookiePolicy', 'cookieNotice'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.style.display = 'none';
            });
            document.querySelectorAll('[class*="cookie" i], [id*="cookie" i]').forEach(el => {
                if (el.tagName !== 'BODY' && el.tagName !== 'HTML') el.style.display = 'none';
            });
        }""")

    page.goto(_MC_URL, timeout=60000, wait_until="domcontentloaded")
    time.sleep(5)

    try:
        page.locator(f"#{_SEARCH_ID}").wait_for(state="visible", timeout=30000)
        log.info("MCI: search form loaded")
    except Exception:
        log.warning("MCI: search form not visible — reloading page")
        page.reload(wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)
        page.locator(f"#{_SEARCH_ID}").wait_for(state="visible", timeout=30000)

    _dismiss_overlays()
    time.sleep(1)

    body = ""
    for captcha_try in range(4):
        # Always dismiss overlays at the start of each iteration — they re-render after postback
        _dismiss_overlays()
        time.sleep(0.5)

        # Fill the search field (re-fill — postback may have cleared it)
        page.locator(f"#{_SEARCH_ID}").fill(query)
        time.sleep(0.5)

        # Use the specific CAPTCHA image ID — not the class — to avoid multi-match with other LBD imgs
        captcha_img = page.locator(f"#{_CAPTCHA_IMG_ID}")
        try:
            captcha_img.wait_for(state="visible", timeout=15000)
        except Exception:
            log.warning("MCI: CAPTCHA image not visible (try %d)", captcha_try + 1)
            break

        # Wait until src is a BotDetect image URL (avoids capturing transient placeholders)
        try:
            page.wait_for_function(
                f"""() => {{
                    const i = document.getElementById('{_CAPTCHA_IMG_ID}');
                    return i && i.src && i.src.indexOf('BotDetectCaptcha.ashx?get=image') !== -1
                        && i.complete && i.naturalWidth > 50;
                }}""",
                timeout=15000,
            )
        except Exception:
            log.warning("MCI: CAPTCHA src never settled (try %d)", captcha_try + 1)

        captcha_img.scroll_into_view_if_needed()
        _dismiss_overlays()
        time.sleep(1)

        box = captcha_img.bounding_box()
        if not box or box["width"] < 50 or box["height"] < 20:
            log.warning("MCI: CAPTCHA box too small (%s) — skipping", box)
            break

        captcha_img.screenshot(path=f"/tmp/mci_captcha_{captcha_try}.png")
        with open(f"/tmp/mci_captcha_{captcha_try}.png", "rb") as f:
            img_data = base64.b64encode(f.read()).decode()
        captcha_text = _solve_captcha(img_data)
        log.info("MCI CAPTCHA try %d: %s (query: %s)", captcha_try + 1, captcha_text, query[:20])

        if not captcha_text:
            # OCR returned empty — force reload and retry
            log.info("MCI: empty OCR — clicking reload icon")
            _reload_captcha(page, _CAPTCHA_IMG_ID, _RELOAD_LINK_ID)
            continue

        page.locator(f"#{_CAPTCHA_INPUT_ID}").fill(captcha_text)
        time.sleep(0.5)

        page.evaluate(f"document.getElementById('{_BTN_ID}').click()")
        time.sleep(10)

        body = page.inner_text("body")

        if "رمز التحقق غير صحيح" in body:
            log.info("MCI: CAPTCHA incorrect (try %d/4) — clicking reload + retrying", captcha_try + 1)
            _reload_captcha(page, _CAPTCHA_IMG_ID, _RELOAD_LINK_ID)
            continue

        if "لا يوجد سجلات" in body:
            if "السجلات التجارية" in body:
                break
            log.info("MCI: no records found for %s", query[:20])
            break

        if "السجلات التجارية" in body or "تفاصيل السجل" in body:
            log.info("MCI: data found for %s", query[:20])
            break

        break

    # Screenshot final result
    try:
        page.screenshot(path="/tmp/mci_result.png", full_page=True)
    except Exception:
        pass

    return _parse_mci_result(query, body)


def _parse_mci_result(query: str, body: str) -> dict:
    """Parse MCI commercial registration result from page text."""
    result = {
        "query": query,
        "source": "Ministry of Commerce (MCI), Saudi Arabia",
    }

    # Check for "no results" message
    if "لا توجد نتائج" in body or "لم يتم العثور" in body or "لا يوجد سجلات" in body or "No results" in body.lower():
        result["found"] = False
        result["status"] = "NOT_FOUND"
        result["validation_source"] = {
            "registry": "Ministry of Commerce (MCI), Kingdom of Saudi Arabia",
            "url": _MC_URL,
            "how_to_reproduce": f"Visit mc.gov.sa → Commercial Data Inquiry → Search: {query}",
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return result

    # MCI result page uses tab-separated table rows:
    # الكيان التجاري\tCompanyName\tحالة السجل\tنشط\tمدة المنشأة\t99
    # رقم السجل التجاري\t1010000096\t...\tرأس المال\t40000000000
    # Parse all tab-separated key-value pairs (keep empty values to preserve alignment)
    fields = {}
    for line in body.split("\n"):
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 2:
            continue
        # Process pairs: key, value, key, value, ...
        i = 0
        while i < len(parts) - 1:
            key = parts[i]
            val = parts[i + 1]
            if key:  # only store if key is non-empty
                fields[key] = val
            i += 2

    # Extract company name — appears as standalone line after "السجلات التجارية"
    entity_name = ""
    name_match = re.search(r"السجلات التجارية\n(.+?)(?:\n|$)", body)
    if name_match:
        entity_name = name_match.group(1).strip()

    # Also try from tab-separated fields
    if not entity_name:
        entity_name = fields.get("الكيان التجاري", "")

    # CR number
    cr_number = fields.get("رقم السجل التجاري", "")
    if not cr_number:
        cr_match = re.search(r"\b(\d{10})\b", body)
        cr_number = cr_match.group(1) if cr_match else ""

    # National unified number
    unified_number = ""
    unified_match = re.search(r"الرقم الوطني الموحد للمنشأة\s*:?\s*(\d+)", body)
    if unified_match:
        unified_number = unified_match.group(1)

    # Status
    status = fields.get("حالة السجل", "")

    # Capital
    capital = fields.get("رأس المال", "")

    # Registration date
    reg_date = fields.get("تاريخ اصدار السجل", "")

    # Duration
    duration = fields.get("مدة المنشأة", "")

    # Phone
    phone = fields.get("هاتف", "")

    # Website
    website = fields.get("الموقع الالكترونى", "")

    # E-store link
    estore = fields.get("رابط المتجر الإلكتروني", "")

    # Activities — long text after النشاط التجاري
    activities_raw = fields.get("النشاط التجاري", "")
    activities = []
    if activities_raw:
        # Split by " - " separator
        acts = [a.strip() for a in activities_raw.split(" - ") if a.strip()]
        activities = acts

    found = bool(entity_name or cr_number)

    result.update({
        "found": found,
        "entity_name": entity_name or None,
        "cr_number": cr_number or (query if re.match(r"^\d{10}$", query) else None),
        "unified_number": unified_number or None,
        "status": status.upper() if status else ("FOUND" if found else "NOT_FOUND"),
        "capital": capital or None,
        "registration_date": reg_date or None,
        "duration_years": duration or None,
        "phone": phone or None,
        "website": website or estore or None,
        "activities": activities if activities else None,
        "validation_source": {
            "registry": "Ministry of Commerce (MCI), Kingdom of Saudi Arabia",
            "url": _MC_URL,
            "record_id": cr_number or query,
            "how_to_reproduce": (
                f"Visit mc.gov.sa → e-Services → Commercial Data Inquiry → "
                f"Search: {query} → Solve CAPTCHA → View results"
            ),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    })

    return result


def wathq_verify(entity_name: str, cr_number: str = "") -> dict:
    """
    Verify a Saudi company via MCI commercial registration lookup.

    Uses Multilogin anti-detect browser to access mc.gov.sa public inquiry page.
    BotDetect CAPTCHA solved by Claude Haiku vision.
    """
    if not _MLX_PASSWORD or not _POOL_PROFILE_IDS or not _MLX_PROXY_USER_SA:
        return {
            "entity_name": entity_name,
            "cr_number": cr_number,
            "found": False,
            "note": "Multilogin/SA proxy not configured",
        }

    query = cr_number if cr_number else entity_name
    if not query:
        return {"found": False, "error": "entity_name or cr_number required"}

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        return {"entity_name": entity_name, "found": False, "note": "All profiles busy — try later"}

    try:
        for attempt in range(3):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return _do_mci_lookup(port, query, profile_id)
            except Exception as e:
                log.warning("MCI attempt %d/3 failed (%s): %s", attempt + 1, query[:20], e)
                if attempt == 2:
                    return {
                        "entity_name": entity_name,
                        "cr_number": cr_number,
                        "found": False,
                        "error": str(e)[:200],
                        "note": "MCI lookup failed after retries",
                    }
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)
