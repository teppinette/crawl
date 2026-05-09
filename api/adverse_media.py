"""
Adverse Media Tool — multi-provider adverse media screening.

Providers:
  GDELT       — Global Database of Events, Language and Tone (65 languages, free)
  BD_SERP     — Bright Data SERP API: Google News search (paid, per-request)
  BD_DISCOVER — Bright Data Discover API: AI-ranked adverse media search (paid)
  CRT_SH      — Certificate Transparency logs (shell company signal)
  WAYBACK     — Wayback Machine CDX API (domain age / capture history)

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

# Bright Data API key (for SERP API + Discover API)
_BD_API_KEY = get_secret("brightdata-api-key") or "a5327ce4-3832-42a8-86b7-96bf0dd1950c"
_BD_SERP_ZONE = os.environ.get("BD_SERP_ZONE", "serp_api1")

VERSION = "2.0.0"

# Bright Data residential proxy — ALL outbound goes through this
_BD_PROXY = os.environ.get("BRIGHTDATA_PROXY", "")
if not _BD_PROXY:
    _BD_PROXY = "http://brd-customer-hl_7bf69e76-zone-pk_residental:o6nw1d0jrol0@brd.superproxy.io:33335"

# Combined CA bundle: system CAs + Bright Data proxy CA (required for httpx through proxy)
_BD_CA_BUNDLE = "/home/copapadmin/crawl/config/ca-bundle-with-bd.crt"
if not os.path.exists(_BD_CA_BUNDLE):
    _BD_CA_BUNDLE = True  # fall back to system CA only

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
                # GDELT is a free public API — route DIRECT, not through proxy
                # (Bright Data proxy gets blocked by GDELT's CDN, causing timeouts)
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
# Provider: Bright Data SERP API (Google News)
# ---------------------------------------------------------------------------

_BD_SERP_URL = "https://api.brightdata.com/request"


async def _query_bd_serp(company_name: str, country: str, languages: list[str],
                          days_back: int, max_results: int) -> dict:
    """Bright Data SERP API — Google News search. Requires serp_api1 zone."""
    if not _BD_API_KEY:
        return {"status": "disabled", "count": 0, "latency_ms": 0, "articles": [],
                "error": "brightdata-api-key not configured"}

    t0 = time.monotonic()
    articles = []
    errors = []

    # Build adverse media query
    query = f'"{company_name}" fraud OR corruption OR scandal OR sanction OR lawsuit OR penalty OR investigation'
    tbs = "qdr:d" if days_back <= 1 else "qdr:w" if days_back <= 7 else "qdr:m"
    gl = country.lower() if country else "us"

    google_url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=nws&tbs={tbs}&gl={gl}&num={min(max_results, 20)}"

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(
                _BD_SERP_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {_BD_API_KEY}",
                },
                json={
                    "zone": _BD_SERP_ZONE,
                    "url": google_url,
                    "format": "raw",
                },
            )
            if resp.status_code == 400 and "not found" in resp.text.lower():
                return {"status": "disabled", "count": 0, "latency_ms": 0, "articles": [],
                        "error": f"SERP zone '{_BD_SERP_ZONE}' not created — create at brightdata.com/cp/zones"}
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                data = resp.json()
                for art in data.get("news", []):
                    articles.append({
                        "title": (art.get("title") or "")[:1000],
                        "description": (art.get("description") or art.get("snippet") or "")[:4000],
                        "url": art.get("link", ""),
                        "source": art.get("source", ""),
                        "author": None,
                        "published_at": art.get("date"),
                        "language": languages[0] if languages else "en",
                        "source_provider": "BD_SERP",
                        "tone": None,
                        "themes": [],
                    })
    except Exception as e:
        errors.append(f"BD_SERP: {type(e).__name__}: {e}")

    latency = int((time.monotonic() - t0) * 1000)
    return {
        "status": "ok" if not errors or articles else "error",
        "count": len(articles),
        "latency_ms": latency,
        "articles": articles,
        "error": "; ".join(errors) if errors else None,
    }


# ---------------------------------------------------------------------------
# Provider: Bright Data Discover API (AI-ranked adverse media)
# ---------------------------------------------------------------------------

_BD_DISCOVER_URL = "https://api.brightdata.com/discover"


async def _query_bd_discover(company_name: str, country: str, languages: list[str],
                              days_back: int, max_results: int) -> dict:
    """Bright Data Discover API — AI-ranked web search with adverse media intent."""
    if not _BD_API_KEY:
        return {"status": "disabled", "count": 0, "latency_ms": 0, "articles": [],
                "error": "brightdata-api-key not configured"}

    t0 = time.monotonic()
    articles = []
    errors = []

    # Build intent-driven query for adverse media screening
    intent = (
        "Find news articles, regulatory actions, court filings, and investigative reports "
        "about this company involving fraud, corruption, sanctions violations, money laundering, "
        "smuggling, debarment, penalties, lawsuits, criminal investigations, or financial scandals. "
        "Exclude press releases, marketing content, and neutral business coverage. "
        "Prioritize articles from reputable news organizations and government sources."
    )

    # Date filtering
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    payload = {
        "query": f"{company_name} fraud corruption scandal sanction",
        "mode": "fast",
        "intent": intent,
        "num_results": min(max_results, 20),
        "start_date": start_date,
        "end_date": end_date,
    }
    if country:
        payload["country"] = country.lower()
    if languages and languages[0] != "en":
        payload["language"] = languages[0]

    try:
        # Step 1: Submit task
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _BD_DISCOVER_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {_BD_API_KEY}",
                },
                json=payload,
            )
            if resp.status_code != 200:
                errors.append(f"Discover submit: HTTP {resp.status_code}")
                latency = int((time.monotonic() - t0) * 1000)
                return {"status": "error", "count": 0, "latency_ms": latency, "articles": [],
                        "error": "; ".join(errors)}
            task_id = resp.json().get("task_id")

        # Step 2: Poll for results (max 25s, 2s intervals)
        result_data = None
        for _ in range(12):
            await asyncio.sleep(2)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_BD_DISCOVER_URL}?task_id={task_id}",
                    headers={"Authorization": f"Bearer {_BD_API_KEY}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "done":
                        result_data = data
                        break
                    elif data.get("status") == "failed":
                        errors.append(f"Discover task failed: {data.get('error', 'unknown')}")
                        break

        if result_data:
            for item in result_data.get("results", []):
                articles.append({
                    "title": (item.get("title") or "")[:1000],
                    "description": (item.get("description") or "")[:4000],
                    "url": item.get("link", ""),
                    "source": urlparse(item.get("link", "")).netloc,
                    "author": None,
                    "published_at": None,
                    "language": languages[0] if languages else "en",
                    "source_provider": "BD_DISCOVER",
                    "tone": None,
                    "themes": [],
                    "relevance_score": item.get("relevance_score"),
                })
        elif not errors:
            errors.append("Discover: timed out waiting for results")

    except Exception as e:
        errors.append(f"BD_DISCOVER: {type(e).__name__}: {e}")

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
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                       proxy=_BD_PROXY, verify=_BD_CA_BUNDLE) as client:
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
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                       proxy=_BD_PROXY, verify=_BD_CA_BUNDLE) as client:
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
    bd_serp_task = _query_bd_serp(company_name, country, languages, days_back, max_results)
    bd_discover_task = _query_bd_discover(company_name, country, languages, days_back, max_results)
    crtsh_task = _query_crtsh(domain)
    wayback_task = _query_wayback(domain)

    # Per-provider timeout — one hung provider must never block the whole response
    _PROVIDER_TIMEOUT = 35  # seconds (Discover polls up to 25s internally)

    async def _safe(coro, name):
        try:
            return await asyncio.wait_for(coro, timeout=_PROVIDER_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Adverse media provider %s timed out after %ds", name, _PROVIDER_TIMEOUT)
            return {"status": "error", "count": 0, "articles": [], "error": f"{name}: timed out ({_PROVIDER_TIMEOUT}s)"}

    gdelt_r, bd_serp_r, bd_discover_r, crtsh_r, wayback_r = await asyncio.gather(
        _safe(gdelt_task, "GDELT"),
        _safe(bd_serp_task, "BD_SERP"),
        _safe(bd_discover_task, "BD_DISCOVER"),
        _safe(crtsh_task, "CRT_SH"),
        _safe(wayback_task, "WAYBACK"),
    )

    # Collect all articles
    all_articles = []
    all_articles.extend(gdelt_r.get("articles", []))
    all_articles.extend(bd_serp_r.get("articles", []))
    all_articles.extend(bd_discover_r.get("articles", []))

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
    for name, result in [("GDELT", gdelt_r), ("BD_SERP", bd_serp_r),
                          ("BD_DISCOVER", bd_discover_r),
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
    has_error = any(r["status"] == "error" for r in [gdelt_r, bd_serp_r, bd_discover_r])
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
# Health check (cached — GDELT rate-limits at 1 req / 5s)
# ---------------------------------------------------------------------------

_health_cache = {"result": None, "expires": 0}
_HEALTH_CACHE_TTL = 60  # seconds


async def health() -> dict:
    """Quick provider reachability check. Cached for 60s to avoid GDELT rate limits."""
    now = time.time()
    if _health_cache["result"] and now < _health_cache["expires"]:
        return _health_cache["result"]

    checks = {}

    async def _check(name, coro):
        try:
            return await asyncio.wait_for(coro, timeout=8)
        except asyncio.TimeoutError:
            return f"down (timeout 8s)"
        except Exception as e:
            return f"down ({e})"

    async def _gdelt_check():
        # Don't hit GDELT for health checks — they rate-limit at 1 req / 5s
        # and health check HEAD requests burn quota. Just report "available".
        return "available (rate-limited: 1 req / 5s)"

    async def _crtsh_check():
        async with httpx.AsyncClient(timeout=6, proxy=_BD_PROXY, verify=_BD_CA_BUNDLE) as client:
            resp = await client.get(_CRTSH_URL, params={"q": "example.com", "output": "json"})
            return "up" if resp.status_code == 200 else f"down ({resp.status_code})"

    async def _wayback_check():
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(_WAYBACK_CDX, params={"url": "example.com", "output": "json", "limit": 1})
            return "up" if resp.status_code == 200 else f"down ({resp.status_code})"

    async def _bd_serp_check():
        # Just verify zone exists by checking API connectivity
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.post(
                _BD_SERP_URL,
                headers={"Authorization": f"Bearer {_BD_API_KEY}", "Content-Type": "application/json"},
                json={"zone": _BD_SERP_ZONE, "url": "https://www.google.com/search?q=test&tbm=nws", "format": "raw"},
            )
            if resp.status_code == 400 and "not found" in resp.text.lower():
                return f"disabled (zone '{_BD_SERP_ZONE}' not created)"
            return "up" if resp.status_code == 200 else f"down ({resp.status_code})"

    async def _bd_discover_check():
        return "up" if _BD_API_KEY else "disabled (no API key)"

    gdelt_r, bd_serp_r, bd_discover_r, crtsh_r, wayback_r = await asyncio.gather(
        _check("GDELT", _gdelt_check()),
        _check("BD_SERP", _bd_serp_check()),
        _check("BD_DISCOVER", _bd_discover_check()),
        _check("CRT_SH", _crtsh_check()),
        _check("WAYBACK", _wayback_check()),
    )

    checks["GDELT"] = gdelt_r
    checks["BD_SERP"] = bd_serp_r
    checks["BD_DISCOVER"] = bd_discover_r
    checks["CRT_SH"] = crtsh_r
    checks["WAYBACK"] = wayback_r

    result = {"providers": checks, "version": VERSION}
    _health_cache["result"] = result
    _health_cache["expires"] = time.time() + _HEALTH_CACHE_TTL
    return result
