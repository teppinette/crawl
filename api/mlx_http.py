"""
Shared Multilogin HTTP helper for verify-gateway adapters.

Routes HTTP requests through Multilogin anti-detect browser profiles.
Uses Playwright CDP to execute fetch() inside the browser context,
ensuring all traffic goes through Multilogin's proxy infrastructure
with proper browser fingerprinting.

Usage:
    from mlx_http import mlx_get, mlx_post, mlx_navigate

    # JSON API call (returns parsed dict/list)
    data = mlx_get("https://api.example.com/data", params={"q": "test"})

    # POST JSON API
    data = mlx_post("https://api.example.com/submit", json={"key": "val"})

    # Website navigation (returns page body text)
    body = mlx_navigate("https://example.com/search?q=test")

All functions are blocking (synchronous). Profile pool is shared across
all adapters — max 5 concurrent requests (limited by Multilogin profiles).
"""

import hashlib
import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("verify-gateway")

# ---------------------------------------------------------------------------
# Credentials (injected from keyvault via init())
# ---------------------------------------------------------------------------
_MLX_EMAIL = None
_MLX_PASSWORD = None
_MLX_FOLDER_ID = None
_POOL_PROFILE_IDS = []
_CLI_PATH = Path("/home/copapadmin/mlx/deps/cli/xcli")

# Auth token cache
_token_lock = threading.Lock()
_cached_token = None
_token_expiry = 0

# Profile pool
_pool: queue.Queue = queue.Queue()
_pool_initialized = False
_init_lock = threading.Lock()

_ready = False

# Country proxy cache: {"pt": {"user": "...", "pass": "..."}, ...}
_proxy_cache: dict[str, dict] = {}
_proxy_cache_lock = threading.Lock()


def init(get_secret):
    """Initialize credentials from Key Vault. Call once at startup."""
    global _MLX_EMAIL, _MLX_PASSWORD, _MLX_FOLDER_ID, _POOL_PROFILE_IDS, _ready

    _MLX_EMAIL = get_secret("multilogin-email") or "teppinette@copap.com"
    _MLX_PASSWORD = get_secret("multilogin-password")
    _MLX_FOLDER_ID = get_secret("multilogin-folder-id")

    pool_json = get_secret("multilogin-pool-profiles")
    if pool_json:
        try:
            _POOL_PROFILE_IDS = json.loads(pool_json)
        except Exception:
            _POOL_PROFILE_IDS = []

    _ready = bool(_MLX_PASSWORD and _POOL_PROFILE_IDS)
    if _ready:
        log.info("mlx_http ready: %d pool profiles", len(_POOL_PROFILE_IDS))
    else:
        log.warning("mlx_http not configured — missing credentials or profiles")


def _get_country_proxy(country_code: str) -> dict | None:
    """
    Get Multilogin residential proxy credentials for a country.

    Uses xcli proxy-get to get gate.multilogin.com:8080 with country targeting.
    Returns {"server": "http://gate.multilogin.com:8080", "username": ..., "password": ...}
    or None if country proxy unavailable.

    Caches results — proxy creds don't change per-session.
    """
    cc = country_code.lower().strip()
    if not cc:
        return None

    with _proxy_cache_lock:
        if cc in _proxy_cache:
            return _proxy_cache[cc]

    try:
        result = subprocess.run(
            [str(_CLI_PATH), "proxy-get", "--country-code", cc,
             "--protocol", "http", "--type", "rotating"],
            capture_output=True, text=True, timeout=15,
        )
        if result.stdout.strip():
            # Format: host:port:username:password
            parts = result.stdout.strip().split(":")
            if len(parts) >= 4:
                proxy_info = {
                    "server": f"http://{parts[0]}:{parts[1]}",
                    "username": parts[2],
                    "password": parts[3],
                }
                with _proxy_cache_lock:
                    _proxy_cache[cc] = proxy_info
                log.info("mlx_http: cached %s proxy (%s)", cc.upper(), parts[0])
                return proxy_info
    except Exception as e:
        log.warning("mlx_http: failed to get %s proxy: %s", cc.upper(), str(e)[:100])

    return None


def is_ready() -> bool:
    return _ready


def _init_pool():
    global _pool_initialized
    with _init_lock:
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
# Core: execute fetch() inside Multilogin browser
# ---------------------------------------------------------------------------

def _do_fetch(url: str, method: str, headers: dict | None,
              json_body: dict | None, timeout_s: int,
              country_code: str) -> dict:
    """Execute HTTP request through Multilogin residential proxy.

    Uses requests + Multilogin proxy (gate.multilogin.com) for API calls.
    No browser needed — proxy masks VM IP with country-targeted exit.
    Browser is only needed for page scraping (see _do_navigate).
    """
    proxy_info = _get_country_proxy(country_code) if country_code else None

    proxies = None
    if proxy_info:
        proxy_url = (f"http://{proxy_info['username']}:{proxy_info['password']}"
                     f"@{proxy_info['server'].replace('http://', '')}")
        proxies = {"http": proxy_url, "https": proxy_url}

    req_headers = dict(headers or {})
    req_kwargs = {"headers": req_headers, "timeout": timeout_s, "verify": True}
    if proxies:
        req_kwargs["proxies"] = proxies

    if method.upper() == "GET":
        resp = requests.get(url, **req_kwargs)
    else:
        if json_body is not None:
            req_headers.setdefault("Content-Type", "application/json")
            req_kwargs["data"] = json.dumps(json_body)
        resp = requests.request(method.upper(), url, **req_kwargs)

    result = {
        "status_code": resp.status_code,
        "ok": resp.ok,
        "body": resp.text,
    }

    try:
        result["json"] = resp.json()
    except (json.JSONDecodeError, ValueError):
        result["json"] = None

    return result


def _do_navigate(port: int, url: str, wait_s: int,
                 timeout_s: int, country_code: str,
                 profile_id: str) -> str:
    """Navigate to URL and return page body text."""
    proxy_info = _get_country_proxy(country_code) if country_code else None
    result = {"body": ""}
    error = None

    def _run():
        nonlocal result, error
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                ctx_kwargs = {"ignore_https_errors": True}
                if proxy_info:
                    ctx_kwargs["proxy"] = proxy_info
                context = browser.new_context(**ctx_kwargs)
                page = context.new_page()
                try:
                    page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                    if wait_s > 0:
                        time.sleep(wait_s)
                    result["body"] = page.inner_text("body")
                    result["html"] = page.content()
                finally:
                    page.close()
                    context.close()
                    browser.close()
        except Exception as e:
            error = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_s + 30)

    if t.is_alive():
        log.error("mlx_http navigate HUNG for %s — force-stopping profile %s",
                  url[:60], profile_id[:8])
        _stop_profile(profile_id)
        t.join(timeout=10)
        raise RuntimeError(f"mlx_http navigate timed out ({timeout_s}s)")
    if error:
        raise error
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _with_profile(fn, *args, max_retries=2, **kwargs):
    """Acquire a profile, run fn, release profile. Retry on transient failures."""
    if not _ready:
        raise RuntimeError("mlx_http not initialized — call init() first")

    _init_pool()

    try:
        profile_id = _pool.get(timeout=120)
    except queue.Empty:
        raise RuntimeError("All Multilogin profiles busy — try later")

    try:
        for attempt in range(max_retries):
            try:
                token = _get_token()
                port = _launch_profile(token, profile_id)
                return fn(port, *args, profile_id=profile_id, **kwargs)
            except Exception as e:
                log.warning("mlx_http attempt %d/%d failed: %s",
                            attempt + 1, max_retries, str(e)[:200])
                if attempt == max_retries - 1:
                    raise
            finally:
                _stop_profile(profile_id)
    finally:
        _pool.put(profile_id)


def mlx_get(url: str, params: dict | None = None,
            headers: dict | None = None, timeout: int = 30,
            country_code: str = "") -> dict:
    """
    HTTP GET through Multilogin residential proxy.

    params: query string parameters (appended to URL)
    headers: custom HTTP headers
    timeout: seconds for the request (default 30)
    country_code: 2-letter ISO code for country-targeted exit IP (e.g. "pt", "eg")

    Returns dict with: status_code, ok, body (str), json (parsed or None)
    """
    if not _ready:
        raise RuntimeError("mlx_http not initialized — call init() first")
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    for attempt in range(2):
        try:
            return _do_fetch(url, "GET", headers, None, timeout, country_code)
        except Exception as e:
            log.warning("mlx_get attempt %d/2 failed: %s", attempt + 1, str(e)[:200])
            if attempt == 1:
                raise


def mlx_post(url: str, json_body: dict | None = None,
             headers: dict | None = None, timeout: int = 30,
             country_code: str = "") -> dict:
    """
    HTTP POST through Multilogin residential proxy.

    json_body: dict to send as JSON body
    headers: custom HTTP headers
    timeout: seconds for the request (default 30)
    country_code: 2-letter ISO code for country-targeted exit IP (e.g. "pt", "eg")

    Returns dict with: status_code, ok, body (str), json (parsed or None)
    """
    if not _ready:
        raise RuntimeError("mlx_http not initialized — call init() first")
    for attempt in range(2):
        try:
            return _do_fetch(url, "POST", headers, json_body, timeout, country_code)
        except Exception as e:
            log.warning("mlx_post attempt %d/2 failed: %s", attempt + 1, str(e)[:200])
            if attempt == 1:
                raise


def mlx_navigate(url: str, wait_s: int = 3, timeout: int = 30,
                 country_code: str = "") -> dict:
    """
    Navigate to URL in Multilogin browser and return page content.

    wait_s: seconds to wait after page load (for JS rendering)
    timeout: seconds for navigation timeout
    country_code: 2-letter ISO code for country-targeted exit IP (e.g. "pt", "eg")

    Returns dict with: body (inner text), html (full HTML)
    """
    return _with_profile(_do_navigate, url, wait_s, timeout, country_code)
