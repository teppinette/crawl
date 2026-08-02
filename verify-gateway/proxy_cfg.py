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

from keyvault import get_secret

log = logging.getLogger("verify-gateway")

# Credentials come from crawl-kv, never from this file. They used to be hardcoded
# here, which meant a rotation had to be made in source and redeployed, and the
# passwords sat in git history. get_secret() falls back to the env var of the same
# name upper-cased with dashes as underscores, so a container can still override.
#
# Bright Data residential (global megapool)
_BRD_RES_USER = get_secret("brightdata-proxy-user")
_BRD_RES_PASS = get_secret("brightdata-proxy-pass")

# Bright Data datacenter — static KR IP (103.252.109.79)
_BRD_DC_USER = get_secret("brightdata-dc-proxy-user")
_BRD_DC_PASS = get_secret("brightdata-dc-proxy-pass")

# not a secret — the public proxy endpoint
_BRD_HOST = "brd.superproxy.io:33335"

# Every outbound request in this service goes through the proxy, so empty
# credentials mean total failure. Say so at import instead of emitting
# 'http://:@brd.superproxy.io:33335' and failing per-request with no clue why.
for _n, _v in (("brightdata-proxy-user", _BRD_RES_USER), ("brightdata-proxy-pass", _BRD_RES_PASS),
               ("brightdata-dc-proxy-user", _BRD_DC_USER), ("brightdata-dc-proxy-pass", _BRD_DC_PASS)):
    if not _v:
        log.error("proxy_cfg: secret '%s' is empty — proxied requests will fail. "
                  "Check crawl-kv and the VM's managed-identity access.", _n)


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
