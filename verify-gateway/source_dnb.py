"""
Source: D&B Business Directory profile scrape via Multilogin.

Free tier exposes: Key Principal (1 named director), industry classification,
address, website. Other principals + full financials behind paywall.

Used as ENRICHMENT after a primary source (OC/ltddir/gov) confirms entity
exists. D&B profile URLs are deterministic when known; for discovery we
search D&B's directory via Multilogin (search page is reCAPTCHA-gated, so
prefer caller-provided profile_url when possible).
"""

import logging
import re

import mlx_http

log = logging.getLogger("verify-gateway")


def init(get_secret=None):
    log.info("source_dnb ready (D&B Business Directory enrichment via Multilogin)")


def dnb_enrich(profile_url: str = "", entity_name: str = "",
               country_code: str = "") -> dict:
    """
    Pull D&B Business Directory profile. Returns dict of extras or {} on failure.

    profile_url: deterministic /business-directory/company-profiles.<slug>.<hash>.html
    entity_name + country_code: fallback for D&B search if no profile_url given
                                (search uses reCAPTCHA, often fails; profile_url
                                is the reliable path)
    """
    if not profile_url:
        # D&B search is reCAPTCHA-gated. Without a known profile URL we can't
        # reliably reach the data. Caller should populate profile_url when known.
        return {"dnb_status": "no_profile_url_provided"}

    cc_lower = (country_code or "").lower() or "us"

    try:
        r = mlx_http.mlx_navigate(
            url=profile_url, wait_s=14,
            country_code=cc_lower, timeout=110,
        )
    except Exception as e:
        log.debug("dnb fetch failed for %s: %s", profile_url, e)
        return {"dnb_status": f"fetch_error: {str(e)[:120]}"}

    body = r.get("body", "") or ""
    html = r.get("html", "") or ""

    lower_html = html.lower()
    # Real Cloudflare/CAPTCHA challenge page markers — tight match so we don't
    # false-trigger on D&B's "This site is protected by reCAPTCHA" footer text.
    challenge_markers = (
        "just a moment", "verify you are human",
        "g-recaptcha-response", "challenge-platform",
        "cf_chl_opt",
    )
    is_real_challenge = any(m in lower_html for m in challenge_markers) and len(body) < 2000
    if is_real_challenge:
        return {"dnb_status": f"captcha (body_len={len(body)})"}

    # Tolerate short body if INTEX / Key Principal / Director markers present.
    has_relevant = bool(
        (entity_name and entity_name.upper() in body.upper())
        or "Key Principal" in body
        or "Director" in body
        or "Principal" in body
    )
    if not has_relevant:
        return {"dnb_status": f"no_relevant_markers (body_len={len(body)})"}

    extracted: dict = {"dnb_profile_url": profile_url}

    # Key Principal (the first-named director — D&B free tier shows one)
    kp_match = re.search(
        r"Key Principal[:\s]+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ \-,.'’]+?)\s+(?:See|Industry|Director|Contact)",
        body,
    )
    if kp_match:
        extracted["dnb_key_principal"] = kp_match.group(1).strip()

    # Industry
    ind_match = re.search(
        r"Industry[:\s]+([^.\n]{5,300}?)(?:See other industries|Popular Search|Address|Phone|Website)",
        body,
    )
    if ind_match:
        extracted["dnb_industry"] = re.sub(r"\s+", " ", ind_match.group(1)).strip()

    # Address line
    addr_match = re.search(
        r"Address[:\s]+([^\n]{8,250}?)(?:See Other Location|Phone|Website|Employees)",
        body,
    )
    if addr_match:
        extracted["dnb_address"] = re.sub(r"\s+", " ", addr_match.group(1)).strip()

    # Website (D&B lists the publicly-known website)
    web_match = re.search(r"Website[:\s]+([a-zA-Z0-9.\-/]+\.[a-zA-Z]{2,})", body)
    if web_match:
        extracted["dnb_website"] = web_match.group(1).strip()

    # Contacts hint — D&B shows count even when names are paywalled
    contacts_match = re.search(r"Get in Touch with (\d+) Principal", body)
    if contacts_match:
        extracted["dnb_principal_count"] = int(contacts_match.group(1))

    # Other principal names that ARE visible (D&B sometimes shows 1, sometimes more)
    other_principals = re.findall(
        r"\b([A-Z][A-Z][A-Z\-]+\s+[A-Z][A-Z\-]+(?:\s+[A-Z][A-Z\-]+)?)\b\s+(?:Director|Officer|Principal|Owner|CEO|Chairman)",
        body,
    )
    if other_principals:
        # Deduplicate while preserving order
        seen = set()
        names = []
        for n in other_principals:
            n_clean = n.strip()
            if n_clean not in seen and len(n_clean) > 4:
                names.append(n_clean)
                seen.add(n_clean)
        if names:
            extracted["dnb_visible_principals"] = names[:10]

    return extracted
