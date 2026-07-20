"""
Source: web_profile — official-website discovery + crawl for a company, using
ONLY the self-hosted free tooling (SearXNG for search, Crawl4AI for page fetch).
No paid web APIs (no Bright Data / Firecrawl / Tavily) are used here.

Given an entity name (+ optional country / domain hint) it:
  1. discovers the most likely official domain via SearXNG,
  2. fetches the home + about/contact pages via Crawl4AI,
  3. extracts a light corporate profile (description, products, leadership,
     contact, revenue/sales claims).

Hallucination guards:
  - if search finds no plausible domain, or the crawl returns nothing, we return
    found=False with a reason — the collector then writes an EMPTY-source
    evidence row (raw_content=None) so the empty-string sentinel holds and the
    synthesizers cannot cite it as corroboration.
  - a domain is pinned to the company via source_domain.name_matches_domain when
    the name is latin-scriptable; for CJK names (which can't token-match a latin
    domain) we surface name_match=false + the search provenance and keep the tier
    at OSINT / low confidence so nothing is over-trusted.

Base URLs (self-hosted, copapai-aux, NSG-locked to crawl egress):
  SEARXNG_URL   default http://104.209.153.42:8888
  CRAWL4AI_URL  default http://104.209.153.42:11235
"""

import logging
import os
import re
from urllib.parse import urlparse

import requests

import source_domain

log = logging.getLogger("crawl-gateway")

_UA = "Crawl-Research-Gateway/3.0 (+web-profile)"
_SEARXNG = os.environ.get("SEARXNG_URL", "http://104.209.153.42:8888").rstrip("/")
_CRAWL4AI = os.environ.get("CRAWL4AI_URL", "http://104.209.153.42:11235").rstrip("/")
_SEARCH_TIMEOUT = 20
_CRAWL_TIMEOUT = 45

# Hosts that are never a company's *own* site — don't accept them as the domain.
_NON_OFFICIAL = frozenset({
    "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "wikipedia.org", "bloomberg.com", "crunchbase.com",
    "opencorporates.com", "tianyancha.com", "qichacha.com", "qcc.com",
    "aiqicha.baidu.com", "baidu.com", "alibaba.com", "made-in-china.com",
    "1688.com", "amazon.com", "zhihu.com", "sohu.com", "sina.com.cn",
    "gov.cn", "org.cn", "dnb.com", "zoominfo.com", "importgenius.com",
    "panjiva.com", "volza.com",
    # Encyclopedias / news / directories / marketplaces — never a company's
    # own official site; keep them out of domain discovery.
    "britannica.com", "reuters.com", "bloomberglaw.com", "forbes.com",
    "globaltimes.cn", "scmp.com", "chinadaily.com.cn", "yellowpages.com",
    "kompass.com", "europages.com", "tradeindia.com", "indiamart.com",
    "ec21.com", "globalsources.com", "go4worldbusiness.com", "manta.com",
    "bloomberg.com",
})


def _registrable(host: str) -> str:
    host = (host or "").lower().strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_non_official(host: str) -> bool:
    h = _registrable(host)
    return any(h == bad or h.endswith("." + bad) for bad in _NON_OFFICIAL)


def _searxng(query: str, max_results: int = 10) -> list[dict]:
    """SearXNG JSON search. Returns [] on any failure (never raises)."""
    try:
        r = requests.get(
            f"{_SEARXNG}/search",
            params={"q": query, "format": "json", "safesearch": 0},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=_SEARCH_TIMEOUT,
        )
        if r.status_code >= 400:
            log.info("searxng %s for %r", r.status_code, query[:60])
            return []
        return (r.json() or {}).get("results", [])[:max_results]
    except Exception as e:
        log.info("searxng failed for %r: %s", query[:60], e)
        return []


import re as _re_inj
_INJECTION_PATTERNS = _re_inj.compile(
    r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|above|prior|earlier|all)\b"
    r"[^.\n]{0,40}\b(instruction|prompt|rule|context)s?\b"
    r"|\byou are now\b|\bnew instructions?\b|\bsystem prompt\b"
    r"|\b(mark|rate|report|classify|treat|consider)\b[^.\n]{0,30}\b(as )?(low[- ]?risk|cleared|safe|no risk|clean|not sanctioned)\b"
    r"|\bdo not (report|mention|flag|include|disclose)\b",
    _re_inj.IGNORECASE,
)


def neutralize_injection(text: str) -> str:
    """Defence-in-depth against prompt injection in UNTRUSTED scraped page text
    before it is fed to the LLM: redact lines that look like embedded instructions
    aimed at the model ('ignore previous instructions', 'mark as low risk', 'do not
    report…'). The subject of an investigation may control pages we crawl. The
    primary defence is still the system-prompt instruction hierarchy; this trims
    the obvious attacks. Returns text with matches replaced by a redaction marker."""
    if not text:
        return text
    return _INJECTION_PATTERNS.sub(" [redacted: possible-injection] ", text)


_CS_EP = (os.environ.get("CONTENT_SAFETY_ENDPOINT") or "").rstrip("/")
_CS_KEY = os.environ.get("CONTENT_SAFETY_KEY") or ""


def shield_prompt(documents: list, timeout: int = 8) -> list:
    """Azure Content Safety **Prompt Shields** — managed detection of prompt-
    injection / jailbreak attempts embedded in UNTRUSTED crawled evidence, layered
    ON TOP of the regex neutraliser above. Returns a list[bool] (attackDetected,
    one per input document, order-preserved). Fail-OPEN: returns [] when the
    service isn't configured or is unreachable, so the CIR never depends on it —
    the regex neutraliser + system-prompt hierarchy remain the floor."""
    if not (_CS_EP and _CS_KEY and documents):
        return []
    import json as _json
    out: list = []
    # API caps ~10 docs/call and per-doc length; batch conservatively.
    for i in range(0, len(documents), 10):
        chunk = [str(d)[:9000] for d in documents[i:i + 10]]
        body = {"userPrompt": "Analyze the following counterparty evidence.",
                "documents": chunk}
        try:
            r = requests.post(
                f"{_CS_EP}/contentsafety/text:shieldPrompt?api-version=2024-09-01",
                json=body, timeout=timeout,
                headers={"Ocp-Apim-Subscription-Key": _CS_KEY,
                         "Content-Type": "application/json"})
            if r.status_code >= 400:
                out.extend([False] * len(chunk))
                continue
            da = (r.json() or {}).get("documentsAnalysis") or []
            flags = [bool(a.get("attackDetected")) for a in da]
            # pad if the service returned fewer analyses than docs
            flags += [False] * (len(chunk) - len(flags))
            out.extend(flags[:len(chunk)])
        except Exception:
            out.extend([False] * len(chunk))
    return out


def searxng_images(query: str, max_results: int = 8) -> list[dict]:
    """Free image search via SearXNG (categories=images). Returns
    [{img_src, source_url, title}] — the direct image URL plus the page it was
    found on, so a photo can ALWAYS be shown with a citation and never asserted
    as verified identity. [] on any failure; never raises."""
    try:
        r = requests.get(
            f"{_SEARXNG}/search",
            params={"q": query, "format": "json", "safesearch": 1,
                    "categories": "images"},
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=_SEARCH_TIMEOUT,
        )
        if r.status_code >= 400:
            log.info("searxng images %s for %r", r.status_code, query[:60])
            return []
        out = []
        for res in (r.json() or {}).get("results", [])[:max_results]:
            img = res.get("img_src") or res.get("thumbnail_src") or ""
            if not img:
                continue
            out.append({"img_src": img,
                        "source_url": res.get("url") or "",
                        "title": (res.get("title") or "")[:200]})
        return out
    except Exception as e:
        log.info("searxng images failed for %r: %s", query[:60], e)
        return []


def _discover_domain(entity_name: str, country: str | None,
                     domain_hint: str | None) -> tuple[str | None, dict]:
    """Resolve the official domain. Prefer an explicit hint; else SearXNG."""
    prov = {"method": None, "candidates": []}
    if domain_hint:
        d = source_domain.extract_domain(domain_hint)
        if d and not _is_non_official(d):
            prov["method"] = "hint"
            return d, prov

    q = f'{entity_name} official website'
    if country:
        q += f' {country}'
    results = _searxng(q, max_results=10)
    prov["method"] = "searxng"
    # Rank candidate hosts by search order, skipping non-official platforms.
    for res in results:
        host = _registrable(urlparse(res.get("url", "")).netloc)
        if not host or _is_non_official(host) or source_domain.is_freemail(host):
            continue
        cand = {"host": host, "title": (res.get("title") or "")[:200],
                "url": res.get("url")}
        prov["candidates"].append(cand)
    if prov["candidates"]:
        return prov["candidates"][0]["host"], prov
    return None, prov


def _crawl(url: str) -> str | None:
    """Fetch one URL via Crawl4AI, return markdown/text. None on failure.

    The Crawl4AI server API has shifted across versions; parse defensively for
    markdown in the shapes we've seen ({results:[{markdown:{raw_markdown}}]},
    {markdown:...}, {results:[{markdown:"..."}]})."""
    try:
        r = requests.post(
            f"{_CRAWL4AI}/crawl",
            json={"urls": [url]},
            headers={"User-Agent": _UA, "Content-Type": "application/json"},
            timeout=_CRAWL_TIMEOUT,
        )
        if r.status_code >= 400:
            log.info("crawl4ai %s for %s", r.status_code, url)
            return None
        data = r.json()
    except Exception as e:
        log.info("crawl4ai failed for %s: %s", url, e)
        return None

    def _md_of(obj):
        if not isinstance(obj, dict):
            return None
        md = obj.get("markdown")
        if isinstance(md, dict):
            return md.get("raw_markdown") or md.get("fit_markdown")
        if isinstance(md, str):
            return md
        return obj.get("cleaned_html") or obj.get("extracted_content")

    text = None
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list) and results:
            text = _md_of(results[0])
        if not text:
            text = _md_of(data)
    if text:
        return re.sub(r"\n{3,}", "\n\n", text)[:60_000]
    return None


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s()]{6,}\d)")
_ROLE_HINTS = ("CEO", "Chairman", "Chairperson", "President", "Founder",
               "General Manager", "Managing Director", "总经理", "董事长",
               "法定代表人", "创始人", "总裁")
_REV_HINTS = ("revenue", "turnover", "annual sales", "annual output",
              "sales volume", "年产值", "营业收入", "年销售", "产能", "annual revenue")
_PRODUCT_HINTS = ("products", "product range", "we produce", "we manufacture",
                  "our products", "主营产品", "产品中心", "产品")


def _extract(pages: dict[str, str], entity_name: str) -> dict:
    """Heuristic profile extraction over the crawled text. Every field is
    optional — absent when not confidently found (no fabrication)."""
    joined = "\n".join(pages.values())
    out: dict = {}

    # Description = first substantial line that isn't nav/boilerplate.
    for line in joined.split("\n"):
        s = line.strip().lstrip("#*->| ").strip()
        if 40 <= len(s) <= 600 and not s.startswith("http") and " " in s:
            out["description"] = s[:600]
            break

    emails = sorted({e for e in _EMAIL_RE.findall(joined)
                     if not source_domain.is_freemail(e.split("@")[-1])})[:10]
    phones = sorted({re.sub(r"\s+", " ", p).strip()
                     for p in _PHONE_RE.findall(joined)
                     if len(re.sub(r"\D", "", p)) >= 8})[:10]
    if emails or phones:
        out["contact"] = {"emails": emails, "phones": phones}

    leadership = []
    for line in joined.split("\n"):
        if any(h in line for h in _ROLE_HINTS) and len(line) < 200:
            leadership.append(re.sub(r"\s+", " ", line.strip().lstrip("#*->| "))[:160])
        if len(leadership) >= 8:
            break
    if leadership:
        out["leadership"] = leadership

    revenue = []
    for line in joined.split("\n"):
        low = line.lower()
        if any(h in low or h in line for h in _REV_HINTS) and re.search(r"\d", line):
            revenue.append(re.sub(r"\s+", " ", line.strip())[:200])
        if len(revenue) >= 6:
            break
    if revenue:
        out["revenue_claims"] = revenue

    if any(h in joined.lower() or h in joined for h in _PRODUCT_HINTS):
        # capture a short products snippet near the first product hint
        m = re.search(r"(?i)(?:products?|主营产品|产品中心)[:：\s]*([^\n]{5,300})", joined)
        if m:
            out["products"] = re.sub(r"\s+", " ", m.group(1)).strip()[:300]

    return out


def check(entity_name: str, country: str | None = None,
          domain_hint: str | None = None) -> dict:
    """Discover + crawl the company's own website. Never raises. Returns the
    standard source shape; found=False (empty-source) when there is no site or
    the crawl yielded nothing."""
    base = {
        "source_id": "web_profile",
        "source_url": _SEARXNG,
        "found": False,
        "entity_name": entity_name,
    }
    domain, prov = _discover_domain(entity_name, country, domain_hint)
    base["discovery"] = prov
    if not domain:
        base["reason"] = "no plausible official website found via SearXNG"
        return base

    base["domain"] = domain
    base["source_url"] = f"https://{domain}"
    matched, hits = source_domain.name_matches_domain(entity_name, domain)
    base["name_match"] = matched
    base["name_hits"] = hits

    pages: dict[str, str] = {}
    for path in ("", "/about", "/about-us", "/en", "/contact"):
        url = f"https://{domain}{path}"
        txt = _crawl(url)
        if txt:
            pages[url] = txt
        if len(pages) >= 3:
            break
    if not pages:
        base["reason"] = f"domain {domain} found but no page content could be crawled"
        return base

    profile = _extract(pages, entity_name)
    base["found"] = True
    base["pages_crawled"] = list(pages.keys())
    base.update(profile)
    base["validation_source"] = {
        "primary": "Company website via SearXNG discovery + Crawl4AI fetch (free, self-hosted)",
        "primary_url": base["source_url"],
        "confidence": "medium" if matched else "low",
        "tier": "OSINT",
        "note": ("Domain pinned to company name" if matched else
                 "Domain NOT name-matched (e.g. CJK name vs latin domain) — "
                 "treat as OSINT lead, corroborate before relying on it"),
    }
    return base
