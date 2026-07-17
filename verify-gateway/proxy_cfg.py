"""
Bright Data proxy configuration for verify-gateway modules.

All outbound HTTP must go through proxy. No direct access.

Two zones available:
  - pk_residental: Global residential megapool, country targeting, rotating/sticky
  - southkorea_datacenter: Static KR IP (103.252.109.79), for DART IP whitelist

Residential is preferred for country-targeted requests (GB, BR, etc.)
but falls back to datacenter if residential is unavailable.
"""

import logging

log = logging.getLogger("verify-gateway")

# Bright Data residential (global megapool)
_BRD_RES_USER = "brd-customer-hl_7bf69e76-zone-pk_residental"
_BRD_RES_PASS = "o6nw1d0jrol0"

# Bright Data datacenter — static KR IP (103.252.109.79)
_BRD_DC_USER = "brd-customer-hl_7bf69e76-zone-southkorea_datacenter"
_BRD_DC_PASS = "43cxhit61ilr"

_BRD_HOST = "brd.superproxy.io:33335"


def get_proxy(country_code: str = "") -> str:
    """
    Get Bright Data residential proxy URL with optional country targeting.

    country_code: 2-letter ISO (gb, br, kr, etc.) or empty for random megapool.
    Returns: http://user:pass@host:port format for curl_cffi proxy= parameter.
    """
    user = _BRD_RES_USER
    if country_code:
        user = f"{_BRD_RES_USER}-country-{country_code.lower()}"
    return f"http://{user}:{_BRD_RES_PASS}@{_BRD_HOST}"


def get_dc_proxy() -> str:
    """
    Get Bright Data datacenter proxy — static KR IP (103.252.109.79).
    Use for DART and as fallback when residential whitelist is pending.
    """
    return f"http://{_BRD_DC_USER}:{_BRD_DC_PASS}@{_BRD_HOST}"


def get_proxy_with_session(country_code: str = "", session_id: str = "") -> str:
    """
    Get Bright Data residential proxy with sticky session (same IP for session duration).
    Session stays alive ~10 min after last request (residential).
    """
    user = _BRD_RES_USER
    if country_code:
        user = f"{user}-country-{country_code.lower()}"
    if session_id:
        user = f"{user}-session-{session_id}"
    return f"http://{user}:{_BRD_RES_PASS}@{_BRD_HOST}"
