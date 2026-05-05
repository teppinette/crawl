"""
Turkey GIB e-Fatura VKN verification via Playwright.

Uses GIB e-Belge portal (sorgu.efatura.gov.tr) to verify VKN registration
in the e-Fatura system. Has image CAPTCHA (JPEG, 6 hex chars) solved by
Claude Haiku. Requires human-like typing delays to pass anti-bot check.

Input: VKN (Vergi Kimlik Numarası) — 10-digit tax ID.
Returns: registration status (registered/not found).

No Multilogin needed — direct Playwright headless works.
No proxy needed — accessible from any IP.
"""

import base64
import logging
import re
import time

import requests

log = logging.getLogger("verify-gateway")

_ANTHROPIC_KEY = None


def init(get_secret):
    global _ANTHROPIC_KEY
    _ANTHROPIC_KEY = get_secret("anthropic-api-key")
    if _ANTHROPIC_KEY:
        log.info("TR GIB VKN ready (direct Playwright, no Multilogin)")


def _solve_captcha(img_b64: str) -> str:
    """Solve GIB CAPTCHA (6 hex chars, JPEG)."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 20,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": "Read the CAPTCHA text exactly. It is 6 hex characters (0-9, a-f only). Reply with ONLY the 6 characters."},
                ],
            }],
        },
        headers={"x-api-key": _ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def gib_vkn_verify(vkn: str, entity_name: str = "", max_retries: int = 3) -> dict:
    """Verify VKN on GIB e-Fatura registered users portal."""
    if not _ANTHROPIC_KEY:
        return {"vkn": vkn, "found": False, "note": "Anthropic key not configured"}

    safe_vkn = re.sub(r"[^0-9]", "", vkn)
    if len(safe_vkn) != 10:
        return {"vkn": vkn, "found": False, "note": "VKN must be 10 digits"}

    import threading

    result = {}
    error = None

    def _run():
        nonlocal result, error
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page()

                # Intercept CAPTCHA image from page load
                captcha_data = {}

                def handle_response(response):
                    if "img.php" in response.url:
                        try:
                            captcha_data["body"] = response.body()
                        except Exception:
                            pass

                page.on("response", handle_response)

                for attempt in range(max_retries):
                    captcha_data.clear()
                    page.goto(
                        "https://sorgu.efatura.gov.tr/kullanicilar/xliste.php",
                        timeout=60000, wait_until="networkidle",
                    )
                    time.sleep(5)

                    if not captcha_data.get("body"):
                        log.warning("TR CAPTCHA image not intercepted (attempt %d)", attempt + 1)
                        continue

                    img_b64 = base64.b64encode(captcha_data["body"]).decode()
                    captcha_text = _solve_captcha(img_b64)
                    log.info("TR CAPTCHA solved: %s (VKN: %s, attempt %d)", captcha_text, safe_vkn, attempt + 1)

                    # Human-like interaction (required to pass anti-bot)
                    page.mouse.move(300, 200)
                    time.sleep(0.5)

                    vkn_input = page.locator("input[name='search_string']")
                    vkn_input.click()
                    time.sleep(0.5)
                    page.keyboard.type(safe_vkn, delay=100)
                    time.sleep(1)

                    captcha_input = page.locator("input[name='captcha_code']")
                    captcha_input.click()
                    time.sleep(0.5)
                    page.keyboard.type(captcha_text, delay=80)
                    time.sleep(1)

                    page.locator("#ara").click()
                    time.sleep(5)

                    body = page.inner_text("body")

                    if "kayıtlıdır" in body:
                        result.update({
                            "vkn": safe_vkn,
                            "found": True,
                            "status": "REGISTERED",
                            "note": "VKN is registered in the e-Fatura system (Mükellef kayıtlıdır)",
                            "source": "GIB e-Belge — Gelir İdaresi Başkanlığı, Republic of Turkey",
                            "validation_source": {
                                "registry": "Gelir İdaresi Başkanlığı (GIB), Republic of Turkey",
                                "url": "https://sorgu.efatura.gov.tr/kullanicilar/xliste.php",
                                "record_id": safe_vkn,
                                "how_to_reproduce": (
                                    f"Visit https://ebelge.gib.gov.tr/efaturakayitlikullanicilar.html → "
                                    f"Enter VKN: {safe_vkn} → Solve CAPTCHA → Click Ara"
                                ),
                                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            },
                        })
                        browser.close()
                        return

                    elif "bulunamadı" in body:
                        result.update({
                            "vkn": safe_vkn,
                            "found": False,
                            "status": "NOT_REGISTERED",
                            "note": "VKN not found in e-Fatura registry (kayıt bulunamadı)",
                            "source": "GIB e-Belge",
                        })
                        browser.close()
                        return

                    elif "güvenlik" in body.lower():
                        log.warning("TR CAPTCHA rejected (attempt %d)", attempt + 1)
                        continue

                result.update({
                    "vkn": safe_vkn,
                    "found": False,
                    "note": "GIB CAPTCHA failed after retries",
                })
                browser.close()

        except Exception as e:
            error = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=90)

    if t.is_alive():
        return {"vkn": safe_vkn, "found": False, "error": "GIB lookup timed out (90s)"}
    if error:
        return {"vkn": safe_vkn, "found": False, "error": str(error)[:200]}
    return result
