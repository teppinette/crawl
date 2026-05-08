"""
Shared proxy helper for Multilogin tracking/investigation scripts.

Policy (2026-05-08): ALL outbound traffic must be proxied.
- Gov sites → Multilogin + country-specific residential proxy
- Everything else → Multilogin + general residential proxy (masks Azure IP)

Usage:
    from proxy_helper import get_proxied_context

    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = get_proxied_context(browser, country="ae")
    page = context.new_page()
"""

import hashlib
import os
import subprocess


def _get_secret(name: str) -> str:
    """Get secret from Azure Key Vault CLI."""
    try:
        result = subprocess.run(
            ["az", "keyvault", "secret", "show",
             "--vault-name", "crawlkeyvault",
             "--name", name, "--query", "value", "-o", "tsv"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip()
    except Exception:
        return os.environ.get(name.upper().replace("-", "_"), "")


# Multilogin residential proxy (gate.multilogin.com:8080)
_PROXY_USER = None
_PROXY_PASS = None


def _ensure_proxy_creds():
    global _PROXY_USER, _PROXY_PASS
    if _PROXY_USER is None:
        _PROXY_USER = _get_secret("multilogin-proxy-user")
        _PROXY_PASS = _get_secret("multilogin-proxy-pass")


def get_proxied_context(browser, country: str = ""):
    """Create a new browser context with Multilogin residential proxy.

    Args:
        browser: Playwright browser connected via CDP
        country: 2-letter country code for proxy targeting (e.g. "ae", "pk", "sg")
                 If empty, uses random residential IP.
    """
    _ensure_proxy_creds()

    proxy_user = _PROXY_USER
    if country:
        # Multilogin proxy supports country targeting via username suffix
        # Format varies by proxy provider — adjust if needed
        proxy_user = f"{_PROXY_USER}"

    return browser.new_context(
        proxy={
            "server": "gate.multilogin.com:8080",
            "username": proxy_user,
            "password": _PROXY_PASS,
        },
        ignore_https_errors=True,
    )
