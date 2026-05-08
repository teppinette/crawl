"""
Adverse Media Tool — multi-provider adverse media screening.

Providers:
  GDELT    — Global Database of Events, Language and Tone (65 languages, free)
  BING     — Bing News Search API (requires key, disabled until provisioned)
  SERPAPI  — SerpAPI Google News (requires key, disabled until provisioned)
  CRT_SH   — Certificate Transparency logs (shell company signal)
  WAYBACK  — Wayback Machine CDX API (domain age / capture history)

Contract: POST /tools/adverse_media — returns structured articles + shell signals.
GC owns classification + persistence; this module is I/O only.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, quote_plus

import httpx

from keyvault import get_secret

log = logging.getLogger("adverse-media")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = get_secret("anthropic-api-key")
BING_API_KEY = get_secret("bing-news-api-key")        # empty until provisioned
SERPAPI_KEY = get_secret("serpapi-api-key")             # empty until provisioned

VERSION = "1.0.0"

# Bright Data proxy for Bing/SerpAPI calls (GDELT, crt.sh, Wayback are geo-neutral)
_BD_PROXY = os.environ.get("BRIGHTDATA_PROXY", "")

# Country → default search languages (ISO 639-1)
COUNTRY_LANGUAGES = {
    "CN": ["en", "zh"], "HK": ["en", "zh"], "TW": ["en", "zh"],
    "RU": ["en", "ru"], "BY": ["en", "ru"], "UA": ["en", "uk", "ru"],
    "TR": ["en", "tr"],
    "AE": ["en", "ar"], "SA": ["en", "ar"], "EG": ["en", "ar"],
    "QA": ["en", "ar"], "BH": ["en", "ar"], "KW": ["en", "ar"],
    "OM": ["en", "ar"], "JO": ["en", "ar"], "IQ": ["en", "ar"],
    "PK": ["en", "ur"],
    "IN": ["en", "hi"],
    "JP": ["en", "ja"], "KR": ["en", "ko"],
    "DE": ["en", "de"], "FR": ["en", "fr"], "ES": ["en", "es"],
    "IT": ["en", "it"], "BR": ["en", "pt"], "MX": ["en", "es"],
    "CO": ["en", "es"], "AR": ["en", "es"], "CL": ["en", "es"],
    "NL": ["en", "nl"], "SE": ["en", "sv"], "NO": ["en", "no"],
    "PH": ["en"], "SG": ["en", "zh"], "MY": ["en", "ms"],
    "TH": ["en", "th"], "VN": ["en", "vi"], "ID": ["en", "id"],
}

# Tier → max total results (across all providers)
TIER_LIMITS = {"BASE": 10, "STANDARD": 20, "ENHANCED": 50}

# GDELT uses full language names in sourcelang: filter
_ISO_TO_GDELT = {
    "zh": "chinese", "ru": "russian", "tr": "turkish", "ar": "arabic",
    "ur": "urdu", "hi": "hindi", "ja": "japanese", "ko": "korean",
    "de": "german", "fr": "french", "es": "spanish", "it": "italian",
    "pt": "portuguese", "nl": "dutch", "sv": "swedish", "no": "norwegian",
    "uk": "ukrainian", "pl": "polish", "ms": "malay", "th": "thai",
    "vi": "vietnamese", "id": "indonesian",
}

_GDELT_TO_ISO = {v: k for k, v in _ISO_TO_GDELT.items()}
_GDELT_TO_ISO["english"] = "en"

# Adverse media keywords (English) — used in Bing/SerpAPI queries (NOT GDELT, which uses tone filter)
_ADVERSE_KEYWORDS = (
    "fraud OR corruption OR sanction OR lawsuit OR investigation "
    "OR penalty OR arrest OR scandal OR smuggling OR debarment"
)


# ---------------------------------------------------------------------------
# URL Canonicalization (dedup key for GC)
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = frozenset([
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "gclsrc", "ref", "source", "share",
    "mc_cid", "mc_eid", "msclkid", "yclid",
])


def _canonical_url(url: str) -> str:
    """Normalize URL for deduplication: lowercase host, strip tracking params, trailing slash."""
    try:
        p = urlparse(url)
        params = parse_qs(p.query, keep_blank_values=False)
        clean = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        query = urlencode(clean, doseq=True) if clean else ""
        path = p.path.rstrip("/") or "/"
        return f"{p.scheme.lower()}://{p.netloc.lower()}{path}" + (f"?{query}" if query else "")
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Translation (bilingual queries via Claude Haiku)
# ---------------------------------------------------------------------------

async def _translate(name: str, target_lang: str) -> Optional[str]:
    """Translate entity name to target language via Claude Haiku. Returns None on failure."""
    if target_lang == "en":
        return name
    if not ANTHROPIC_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Translate this company/entity name to {target_lang} (ISO 639-1 code). "
                            f"Return ONLY the translated name in the target script, nothing else.\n\n"
                            f"{name}"
                        ),
                    }],
                },
            )
            if resp.status_code == 200:
                text = resp.json()["content"][0]["text"].strip().strip('"').strip("'")
                if text and text != name:
                    return text
    except Exception as e:
        log.warning("Translation to %s failed: %s", target_lang, e)
    return None


# ---------------------------------------------------------------------------
# Provider: GDELT DOC 2.0 API
# ---------------------------------------------------------------------------

_GDELT_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"


async def _query_gdelt(company_name: str, country: str, languages: list[str],
                        days_back: int, max_results: int) -> dict:
    """Fan-out GDELT queries: English adverse + per-language + negative-tone."""
    t0 = time.monotonic()
    articles = []
    errors = []

    # Build query variants — GDELT allows 1 request per 5 seconds
    queries: list[tuple[str, str]] = []

    # 1) English: negative tone filter (catches all adverse coverage without keyword bloat)
    #    GDELT rejects queries with too many OR'd terms, so use tone<-3 as primary filter
    queries.append(("en", f'"{company_name}" tone<-3'))

    # 3) Translated queries for each non-English language
    translations = await asyncio.gather(
        *[_translate(company_name, lang) for lang in languages if lang != "en"]
    )
    for lang, translated in zip([l for l in languages if l != "en"], translations):
        if translated:
            gdelt_lang = _ISO_TO_GDELT.get(lang)
            if gdelt_lang:
                queries.append((lang, f'"{translated}" sourcelang:{gdelt_lang}'))
            else:
                queries.append((lang, f'"{translated}"'))

    # Timespan param
    if days_back <= 30:
        timespan = f"{days_back}d"
    elif days_back <= 84:
        timespan = f"{days_back // 7}w"
    else:
        timespan = "12w"  # GDELT max ~3 months

    per_query_max = min(max(max_results // max(len(queries), 1), 25), 250)

    async def _run_one(lang_key: str, query: str, delay: float = 0):
        if delay > 0:
            await asyncio.sleep(delay)
        for attempt in range(2):
            try:
                params = {
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": per_query_max,
                    "sort": "datedesc",
                    "timespan": timespan,
                }
                async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                    resp = await client.get(_GDELT_BASE, params=params)
                    if resp.status_code == 429:
                        if attempt == 0:
                            await asyncio.sleep(6)
                            continue
                        errors.append(f"GDELT {lang_key}: rate limited (429)")
                        return
                    if resp.status_code != 200:
                        errors.append(f"GDELT {lang_key}: HTTP {resp.status_code}")
                        return
                    # GDELT returns empty body or "{}" for zero-result queries
                    try:
                        data = resp.json()
                    except Exception:
                        return  # unparseable/empty response = 0 results
                    for art in data.get("articles") or []:
                        articles.append({
                            "title": (art.get("title") or "")[:1000],
                            "description": "",
                            "url": art.get("url", ""),
                            "source": art.get("domain", ""),
                            "author": None,
                            "published_at": _gdelt_parse_date(art.get("seendate")),
                            "language": _GDELT_TO_ISO.get((art.get("language") or "").lower(), "en"),
                            "source_provider": "GDELT",
                            "tone": None,
                            "themes": [],
                        })
                    return
            except Exception as e:
                errors.append(f"GDELT {lang_key}: {type(e).__name__}: {e}")
                return

    # GDELT rate limit: 1 request per 5 seconds — stagger accordingly
    tasks = []
    for i, (lk, q) in enumerate(queries):
        tasks.append(_run_one(lk, q, delay=i * 6))
    await asyncio.gather(*tasks)

    latency = int((time.monotonic() - t0) * 1000)
    # "ok" even with 0 results if queries succeeded; "error" only if all queries failed
    all_failed = len(errors) >= len(queries) and not articles
    return {
        "status": "error" if all_failed else "ok",
        "count": len(articles),
        "latency_ms": latency,
        "articles": articles,
        "error": "; ".join(errors) if errors else None,
    }


def _gdelt_parse_date(s: str) -> Optional[str]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Provider: Bing News Search API
# ---------------------------------------------------------------------------

_BING_URL = "https://api.bing.microsoft.com/v7.0/news/search"


async def _query_bing(company_name: str, country: str, languages: list[str],
                       days_back: int, max_results: int) -> dict:
    """Bing News search with country proxy routing."""
    if not BING_API_KEY:
        return {"status": "disabled", "count": 0, "latency_ms": 0, "articles": [],
                "error": "bing-news-api-key not provisioned"}

    t0 = time.monotonic()
    articles = []
    errors = []

    freshness = "Day" if days_back <= 1 else "Week" if days_back <= 7 else "Month"
    query = f'"{company_name}" AND ({_ADVERSE_KEYWORDS})'

    try:
        client_kwargs = {"timeout": 15, "follow_redirects": True}
        if _BD_PROXY:
            client_kwargs["proxy"] = _BD_PROXY

        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(
                _BING_URL,
                params={"q": query, "count": min(max_results, 100),
                        "freshness": freshness, "mkt": _country_to_bing_mkt(country),
                        "sortBy": "Date"},
                headers={"Ocp-Apim-Subscription-Key": BING_API_KEY},
            )
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
            else:
                for art in resp.json().get("value", []):
                    articles.append({
                        "title": (art.get("name") or "")[:1000],
                        "description": (art.get("description") or "")[:4000],
                        "url": art.get("url", ""),
                        "source": (art.get("provider") or [{}])[0].get("name", ""),
                        "author": None,
                        "published_at": art.get("datePublished"),
                        "language": languages[0] if languages else "en",
                        "source_provider": "BING",
                        "tone": None,
                        "themes": [],
                    })
    except Exception as e:
        errors.append(str(e))

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "status": "ok" if not errors or articles else "error",
        "count": len(articles),
        "latency_ms": latency,
        "articles": articles,
        "error": "; ".join(errors) if errors else None,
    }


def _country_to_bing_mkt(cc: str) -> str:
    """Map country code to Bing market code."""
    return {
        "US": "en-US", "GB": "en-GB", "DE": "de-DE", "FR": "fr-FR",
        "CN": "zh-CN", "JP": "ja-JP", "KR": "ko-KR", "IN": "en-IN",
        "AE": "ar-AE", "SA": "ar-SA", "TR": "tr-TR", "RU": "ru-RU",
        "BR": "pt-BR", "MX": "es-MX", "ES": "es-ES", "IT": "it-IT",
        "PK": "en-PK", "NL": "nl-NL",
    }.get(cc, "en-US")


# ---------------------------------------------------------------------------
# Provider: SerpAPI (Google News)
# ---------------------------------------------------------------------------

_SERPAPI_URL = "https://serpapi.com/search.json"


async def _query_serpapi(company_name: str, country: str, languages: list[str],
                          days_back: int, max_results: int) -> dict:
    """SerpAPI Google News — disabled until key provisioned."""
    if not SERPAPI_KEY:
        return {"status": "disabled", "count": 0, "latency_ms": 0, "articles": [],
                "error": "serpapi-api-key not provisioned"}

    t0 = time.monotonic()
    articles = []
    errors = []

    query = f'"{company_name}" fraud OR corruption OR scandal OR sanction OR lawsuit'
    tbs = f"qdr:d" if days_back <= 1 else f"qdr:w" if days_back <= 7 else f"qdr:m"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                _SERPAPI_URL,
                params={
                    "q": query, "tbm": "nws", "api_key": SERPAPI_KEY,
                    "gl": country.lower(), "num": min(max_results, 100),
                    "tbs": tbs,
                },
            )
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
            else:
                for art in resp.json().get("news_results", []):
                    articles.append({
                        "title": (art.get("title") or "")[:1000],
                        "description": (art.get("snippet") or "")[:4000],
                        "url": art.get("link", ""),
                        "source": art.get("source", ""),
                        "author": None,
                        "published_at": None,
                        "language": languages[0] if languages else "en",
                        "source_provider": "SERPAPI",
                        "tone": None,
                        "themes": [],
                    })
    except Exception as e:
        errors.append(str(e))

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "status": "ok" if not errors or articles else "error",
        "count": len(articles),
        "latency_ms": latency,
        "articles": articles,
        "error": "; ".join(errors) if errors else None,
    }


# ---------------------------------------------------------------------------
# Provider: crt.sh (Certificate Transparency)
# ---------------------------------------------------------------------------

_CRTSH_URL = "https://crt.sh/"


async def _query_crtsh(domain: str) -> dict:
    """Query crt.sh for SSL certificates. Returns shell_signals, not articles."""
    if not domain:
        return {"status": "skipped", "count": 0, "latency_ms": 0,
                "certs": [], "error": "no domain provided"}

    t0 = time.monotonic()
    certs = []
    errors = []

    # Clean domain
    domain = domain.lower().replace("http://", "").replace("https://", "").split("/")[0]

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(_CRTSH_URL, params={"q": f"%.{domain}", "output": "json"})
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
            else:
                data = resp.json()
                for cert in data[:200]:  # cap at 200
                    certs.append({
                        "id": cert.get("id"),
                        "issuer": cert.get("issuer_name", ""),
                        "common_name": cert.get("common_name", ""),
                        "not_before": cert.get("not_before"),
                        "not_after": cert.get("not_after"),
                        "entry_timestamp": cert.get("entry_timestamp"),
                    })
    except Exception as e:
        errors.append(str(e))

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "status": "ok" if not errors else "error",
        "count": len(certs),
        "latency_ms": latency,
        "certs": certs,
        "error": "; ".join(errors) if errors else None,
    }


# ---------------------------------------------------------------------------
# Provider: Wayback Machine CDX API
# ---------------------------------------------------------------------------

_WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"


async def _query_wayback(domain: str) -> dict:
    """Query Wayback Machine for domain capture history. Returns shell_signals."""
    if not domain:
        return {"status": "skipped", "count": 0, "latency_ms": 0,
                "captures": [], "error": "no domain provided"}

    t0 = time.monotonic()
    captures = []
    errors = []

    domain = domain.lower().replace("http://", "").replace("https://", "").split("/")[0]

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                _WAYBACK_CDX,
                params={
                    "url": f"{domain}/*",
                    "output": "json",
                    "fl": "timestamp,statuscode,mimetype",
                    "limit": 500,
                    "collapse": "timestamp:6",  # one per month
                },
            )
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
            else:
                data = resp.json()
                # First row is header
                for row in data[1:] if len(data) > 1 else []:
                    if len(row) >= 3:
                        captures.append({
                            "timestamp": row[0],
                            "status": row[1],
                            "mimetype": row[2],
                        })
    except Exception as e:
        errors.append(str(e))

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "status": "ok" if not errors else "error",
        "count": len(captures),
        "latency_ms": latency,
        "captures": captures,
        "error": "; ".join(errors) if errors else None,
    }


# ---------------------------------------------------------------------------
# Shell Company Signals (assembled from crt.sh + Wayback)
# ---------------------------------------------------------------------------

def _build_shell_signals(crtsh_result: dict, wayback_result: dict) -> Optional[dict]:
    """Combine crt.sh + Wayback into shell_signals block."""
    certs = crtsh_result.get("certs", [])
    captures = wayback_result.get("captures", [])

    if not certs and not captures:
        return None

    # Earliest cert date
    earliest_cert = None
    for c in certs:
        nb = c.get("not_before")
        if nb:
            try:
                dt = datetime.fromisoformat(nb.replace("Z", "+00:00")) if "T" in nb else datetime.strptime(nb, "%Y-%m-%d")
                if earliest_cert is None or dt < earliest_cert:
                    earliest_cert = dt
            except Exception:
                pass

    # Earliest Wayback capture
    first_capture = None
    for cap in captures:
        ts = cap.get("timestamp", "")
        if len(ts) >= 8:
            try:
                dt = datetime.strptime(ts[:8], "%Y%m%d")
                if first_capture is None or dt < first_capture:
                    first_capture = dt
                break  # Already sorted chronologically
            except Exception:
                pass

    # Domain age from earliest known date
    earliest_known = min(filter(None, [earliest_cert, first_capture]), default=None)
    domain_age_days = None
    if earliest_known:
        domain_age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - earliest_known.replace(tzinfo=None)).days

    return {
        "cert_count": len(certs),
        "earliest_cert_date": earliest_cert.strftime("%Y-%m-%d") if earliest_cert else None,
        "wayback_first_capture": first_capture.strftime("%Y-%m-%d") if first_capture else None,
        "wayback_total_captures": len(captures),
        "domain_age_days": domain_age_days,
    }


# ---------------------------------------------------------------------------
# Orchestrator — fan-out, dedup, assemble
# ---------------------------------------------------------------------------

def _extract_domain(company_name: str, website: str = None) -> str:
    """Best-effort domain extraction from website URL or company name."""
    if website:
        d = website.lower().replace("http://", "").replace("https://", "").split("/")[0]
        if "." in d:
            return d
    # Fallback: try to guess from company name (very rough)
    return ""


async def scan(
    company_name: str,
    country: str,
    entity_id: int = 0,
    languages: list[str] = None,
    tier: str = "STANDARD",
    days_back: int = 7,
    max_results: int = None,
    website: str = None,
) -> dict:
    """
    Main entry point. Fans out to all providers, deduplicates articles,
    assembles response per contract schema.
    """
    t0 = time.monotonic()

    # Defaults
    if not languages:
        languages = COUNTRY_LANGUAGES.get(country, ["en"])
    if max_results is None:
        max_results = TIER_LIMITS.get(tier, 20)

    domain = _extract_domain(company_name, website)

    # Fan-out: article providers + shell signal providers
    gdelt_task = _query_gdelt(company_name, country, languages, days_back, max_results)
    bing_task = _query_bing(company_name, country, languages, days_back, max_results)
    serpapi_task = _query_serpapi(company_name, country, languages, days_back, max_results)
    crtsh_task = _query_crtsh(domain)
    wayback_task = _query_wayback(domain)

    gdelt_r, bing_r, serpapi_r, crtsh_r, wayback_r = await asyncio.gather(
        gdelt_task, bing_task, serpapi_task, crtsh_task, wayback_task
    )

    # Collect all articles
    all_articles = []
    all_articles.extend(gdelt_r.get("articles", []))
    all_articles.extend(bing_r.get("articles", []))
    all_articles.extend(serpapi_r.get("articles", []))

    # Deduplicate by canonical URL
    seen_urls = set()
    deduped = []
    for art in all_articles:
        url = art.get("url", "")
        if not url:
            continue
        canon = _canonical_url(url)
        if canon in seen_urls:
            continue
        seen_urls.add(canon)
        # Enforce field length limits
        art["url"] = url[:2000]
        art["title"] = (art.get("title") or "")[:1000]
        art["description"] = (art.get("description") or "")[:4000]
        art["source"] = (art.get("source") or "")[:200]
        if art.get("author"):
            art["author"] = art["author"][:255]
        deduped.append(art)

    # Trim to max_results
    deduped = deduped[:max_results]

    # Shell signals
    shell_signals = _build_shell_signals(crtsh_r, wayback_r)

    # Provider status block
    providers = {}
    for name, result in [("GDELT", gdelt_r), ("BING", bing_r), ("SERPAPI", serpapi_r),
                          ("CRT_SH", crtsh_r), ("WAYBACK", wayback_r)]:
        entry = {
            "status": result["status"],
            "count": result["count"],
            "latency_ms": result.get("latency_ms", 0),
        }
        if result.get("error"):
            entry["error"] = result["error"]
        providers[name] = entry

    # Overall status
    has_error = any(r["status"] == "error" for r in [gdelt_r, bing_r, serpapi_r])
    has_data = len(deduped) > 0
    if has_error and not has_data:
        status = "error"
    elif has_error and has_data:
        status = "partial"
    else:
        status = "complete"

    duration_ms = int((time.monotonic() - t0) * 1000)

    result = {
        "status": status,
        "duration_ms": duration_ms,
        "providers": providers,
        "articles": deduped,
    }
    if shell_signals:
        result["shell_signals"] = shell_signals

    return result


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def health() -> dict:
    """Quick provider reachability check."""
    checks = {}

    # GDELT — connectivity check only (don't waste rate limit on test query)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.head(_GDELT_BASE)
            checks["GDELT"] = "up" if resp.status_code in (200, 302, 405) else f"down ({resp.status_code})"
    except Exception as e:
        checks["GDELT"] = f"down ({e})"

    # Bing
    checks["BING"] = "disabled" if not BING_API_KEY else "configured"

    # SerpAPI
    checks["SERPAPI"] = "disabled" if not SERPAPI_KEY else "configured"

    # crt.sh
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_CRTSH_URL, params={"q": "example.com", "output": "json"})
            checks["CRT_SH"] = "up" if resp.status_code == 200 else f"down ({resp.status_code})"
    except Exception as e:
        checks["CRT_SH"] = f"down ({e})"

    # Wayback
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_WAYBACK_CDX, params={"url": "example.com", "output": "json", "limit": 1})
            checks["WAYBACK"] = "up" if resp.status_code == 200 else f"down ({resp.status_code})"
    except Exception as e:
        checks["WAYBACK"] = f"down ({e})"

    return {"providers": checks, "version": VERSION}
