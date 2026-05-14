"""
Dark Web Research Gateway v4.0
Runs on crawl-darkweb VM (port 8450, West Europe / Netherlands)
Receives sanitized entity queries from crawldevvm, routes through Tor.
NO internal references, NO customer data — only entity names + country.

Sources (33 total, 32 free + 1 paid):
  --- Tor Search Engines ---
  1.  Ahmia — Tor hidden service search engine
  2.  Torch — Tor search engine (clearnet mirror)
  3.  Haystak — largest Tor search index (~1.5B pages)   [NEW]
  4.  DuckDuckGo via Tor — anonymous web search
  5.  DuckDuckGo adverse — targeted fraud/sanctions/leak keyword queries
  --- Breach / Credential Databases ---
  6.  Dehashed ($15/mo) — breach records with passwords/emails
  7.  LeakCheck — breach lookup by domain/email
  8.  BreachDirectory — breach search via RapidAPI
  9.  HIBP (Have I Been Pwned) — breach notification (free API)   [NEW]
  --- Leak / Exposure ---
  10. Psbdmp — Pastebin dump aggregator
  11. LeakIX — exposed services & data leaks
  12. HudsonRock Cavalier — infostealer/credential exposure by domain
  13. JustPaste.it — popular paste site for data dumps   [NEW]
  14. GitHub/Gist code search — leaked credentials in code repos   [NEW]
  --- Ransomware ---
  15. Ransomlook — ransomware group victim lists
  --- Investigative Databases ---
  16. OCCRP Aleph — organized crime & corruption project
  17. ICIJ Offshore Leaks — Panama/Paradise/Pandora Papers
  18. OpenSanctions — global sanctions & PEP database
  19. OpenCorporates — 200M+ company records   [NEW]
  --- Document / Archive ---
  20. WikiLeaks — cables & leaked documents (exact phrase match)
  21. Telegram (TGStat) — public channel/group search
  22. Web Archive — removed/changed web content
  23. Court records — legal filings via Tor-routed DDG
  24. Reddit — darknet/fraud subreddit mentions   [NEW]
  --- Threat Intelligence ---
  25. PulseDive — threat intel, IOCs, passive DNS
  26. FullHunt — attack surface / exposed subdomains
  27. Greynoise — IP reputation / scanner detection
  28. Shodan (free tier) — internet-connected devices   [NEW]
  29. VirusTotal (free tier) — domain/IP/file reputation   [NEW]
  30. AlienVault OTX — open threat exchange   [NEW]
  31. AbuseIPDB (free tier) — IP abuse reports   [NEW]
  --- Tor Directory ---
  32. Onion.live — Tor .onion directory / uptime   [NEW]
  33. IntelligenceX — paste/leak/darknet archive (free scrape)
"""

import asyncio
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
from bs4 import BeautifulSoup

app = FastAPI(title="Dark Web Research Gateway", version="4.1")

API_KEY = os.environ.get("DARKWEB_API_KEY", "dwk_crawl_2026Q2_f8a3b7e1d9c4")

# Paid API keys (set via env vars or config)
INTELX_API_KEY = ""  # Dropped — free trial expired, not worth $400/mo
DEHASHED_API_KEY = os.environ.get("DEHASHED_API_KEY", "")
DEHASHED_EMAIL = os.environ.get("DEHASHED_EMAIL", "")

# Free API keys (set via env vars)
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")
SHODAN_API_KEY = os.environ.get("SHODAN_API_KEY", "")
ABUSEIPDB_API_KEY = os.environ.get("ABUSEIPDB_API_KEY", "")

TOR_PROXY = "socks5://127.0.0.1:9050"

# Bright Data residential proxy for clearnet API calls (masks Azure VM IP)
# Set via systemd env: BRIGHTDATA_PROXY=http://user:pass@brd.superproxy.io:33335
BRIGHTDATA_PROXY = os.environ.get("BRIGHTDATA_PROXY", "")

# Blob upload config
BLOB_SAS_TOKEN_FILE = os.path.expanduser("~/crawl/config/blob_sas_token")
BLOB_ACCOUNT = "stcrawlosint"
BLOB_CONTAINER = "osint-staging"

# Limit concurrent Tor requests to prevent circuit exhaustion
_tor_semaphore: asyncio.Semaphore = None

@app.on_event("startup")
async def _init_semaphore():
    global _tor_semaphore
    _tor_semaphore = asyncio.Semaphore(4)

# Blocked terms — HARD FAIL if any appear in request
_BLOCKED_TERMS = {
    "copap", "copapadmin", "copap ai", "global compliance",
    "gc app", "crawldevvm", "crawl-americas", "crawl-europe",
    "crawl-gulf", "crawl-china", "crawl-india", "crawl-darkweb",
    "osint-staging", "stcrawlosint",
}

# Standard headers for Tor-routed requests
_TOR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class ResearchRequest(BaseModel):
    entity_name: str
    country: Optional[str] = None
    owners: Optional[list[str]] = None  # Key individuals / UBOs to also search
    domain: Optional[str] = None        # Company domain for breach checks
    search_domains: Optional[list[str]] = None  # Subset of sources to use
    depth: str = "medium"  # light | medium | heavy


JOBS_DIR = os.path.expanduser("~/crawl/output")
os.makedirs(JOBS_DIR, exist_ok=True)


def _sanitize_check(text: str) -> bool:
    lower = text.lower()
    for term in _BLOCKED_TERMS:
        if term in lower:
            return False
    return True


def _save_job(job: dict):
    path = os.path.join(JOBS_DIR, f"{job['job_id']}.json")
    with open(path, "w") as f:
        json.dump(job, f, indent=2, default=str)


def _load_job(job_id: str) -> Optional[dict]:
    path = os.path.join(JOBS_DIR, f"{job_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_snake(name: str) -> str:
    """Convert entity name to snake_case for blob paths."""
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def _upload_to_blob(local_path: str, blob_name: str) -> bool:
    """Upload a file to Azure blob storage via REST API (no az CLI needed)."""
    try:
        if not os.path.exists(BLOB_SAS_TOKEN_FILE):
            return False
        with open(BLOB_SAS_TOKEN_FILE) as f:
            sas_token = f.read().strip()
        if not sas_token:
            return False
        # Use curl with Azure Blob REST API
        url = f"https://{BLOB_ACCOUNT}.blob.core.windows.net/{BLOB_CONTAINER}/{blob_name}?{sas_token}"
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "PUT",
             "-H", "x-ms-blob-type: BlockBlob",
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{local_path}",
             url],
            capture_output=True, text=True, timeout=30,
        )
        status_code = result.stdout.strip()
        return status_code in ("201", "200")
    except Exception:
        return False


async def _tor_fetch(url: str, timeout: float = 30.0, json_mode: bool = False) -> Optional[str]:
    """Fetch URL through Tor SOCKS proxy (semaphore-limited to prevent circuit exhaustion)."""
    async with _tor_semaphore:
        try:
            async with httpx.AsyncClient(
                proxy=TOR_PROXY,
                timeout=timeout,
                follow_redirects=True,
                headers=_TOR_HEADERS,
            ) as client:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    return f"ERROR: HTTP {resp.status_code}"
                return resp.text
        except Exception as e:
            return f"ERROR: {str(e)}"


async def _tor_fetch_json(url: str, timeout: float = 30.0, headers: dict = None) -> Optional[dict]:
    """Fetch URL through Tor, parse as JSON (semaphore-limited)."""
    async with _tor_semaphore:
        try:
            hdrs = {**_TOR_HEADERS, **(headers or {})}
            async with httpx.AsyncClient(
                proxy=TOR_PROXY,
                timeout=timeout,
                follow_redirects=True,
                headers=hdrs,
            ) as client:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    return None
                return resp.json()
        except Exception:
            return None


async def _direct_fetch_json(url: str, timeout: float = 20.0, headers: dict = None) -> Optional[dict]:
    """Fetch URL via Bright Data proxy for APIs that block Tor exits."""
    try:
        hdrs = {**_TOR_HEADERS, **(headers or {})}
        client_kwargs = dict(
            timeout=timeout,
            follow_redirects=True,
            headers=hdrs,
        )
        if BRIGHTDATA_PROXY:
            client_kwargs["proxy"] = BRIGHTDATA_PROXY
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return None
            return resp.json()
    except Exception:
        return None


async def _direct_fetch_text(url: str, timeout: float = 20.0, headers: dict = None) -> Optional[str]:
    """Fetch URL via Bright Data proxy, return text."""
    try:
        hdrs = {**_TOR_HEADERS, **(headers or {})}
        client_kwargs = dict(
            timeout=timeout,
            follow_redirects=True,
            headers=hdrs,
        )
        if BRIGHTDATA_PROXY:
            client_kwargs["proxy"] = BRIGHTDATA_PROXY
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return None
            return resp.text
    except Exception:
        return None


# ==========================================================================
# SOURCE 1: Ahmia — Tor hidden service search
# ==========================================================================
async def _search_ahmia(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        html = await _tor_fetch(f"https://ahmia.fi/search/?q={q}", timeout=45)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for sel in ["li.result", ".search-result", "article"]:
                results = soup.select(sel)
                if results:
                    break
            for r in (results or [])[:15]:
                title_el = r.select_one("a")
                desc_el = r.select_one("p") or r.select_one(".description")
                if title_el:
                    findings.append({
                        "source": "ahmia",
                        "type": "dark_web_mention",
                        "title": title_el.get_text(strip=True)[:200],
                        "url": title_el.get("href", "")[:300],
                        "snippet": desc_el.get_text(strip=True)[:500] if desc_el else "",
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "ahmia", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 2: Torch — Tor search engine (clearnet mirror)
# ==========================================================================
async def _search_torch(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        html = await _tor_fetch(f"https://torsearch.se/search?q={q}", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select("a[href*='.onion']")[:15]:
                findings.append({
                    "source": "torch",
                    "type": "dark_web_mention",
                    "title": r.get_text(strip=True)[:200],
                    "url": r.get("href", "")[:300],
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "torch", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 3: Haystak — largest Tor search engine [NEW]
# ==========================================================================
async def _search_haystak(entity: str) -> list[dict]:
    """Search Haystak — largest .onion index (~1.5B pages)."""
    findings = []
    try:
        q = quote_plus(entity)
        # Haystak .onion address (via Tor)
        html = await _tor_fetch(
            f"http://haystak5njsmn2hqkewecpaxetahtwhsbsa64jom2k22z5afxhnpxfid.onion/?q={q}",
            timeout=45,
        )
        if not html or "ERROR:" in html or len(html) < 200:
            # Fallback to clearnet mirror
            html = await _tor_fetch(f"https://haystak.xyz/?q={q}", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select(".result a, .search-result a, h4 a, a[href*='.onion']")[:15]:
                title = r.get_text(strip=True)[:200]
                if title and len(title) > 3:
                    findings.append({
                        "source": "haystak",
                        "type": "dark_web_mention",
                        "title": title,
                        "url": r.get("href", "")[:300],
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "haystak", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 4: DuckDuckGo via Tor — anonymous clearnet search
# ==========================================================================
async def _search_ddg_tor(entity: str, country: str = "") -> list[dict]:
    findings = []
    search_term = f"{entity} {country}".strip()
    try:
        q = quote_plus(search_term)
        html = await _tor_fetch(
            f"https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/html/?q={q}",
            timeout=40,
        )
        if not html or "ERROR:" in html or len(html) < 500:
            html = await _tor_fetch(f"https://duckduckgo.com/html/?q={q}", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select(".result__a")[:15]:
                findings.append({
                    "source": "duckduckgo_tor",
                    "type": "web_mention",
                    "title": r.get_text(strip=True)[:200],
                    "url": r.get("href", "")[:300],
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "duckduckgo_tor", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 5: DuckDuckGo — targeted adverse/fraud/leak searches
# ==========================================================================
async def _search_ddg_adverse(entity: str) -> list[dict]:
    """Targeted searches for fraud, sanctions evasion, leaks, lawsuits."""
    findings = []
    queries = [
        f'"{entity}" fraud OR scam OR lawsuit OR investigation',
        f'"{entity}" sanctions OR evasion OR money laundering',
        f'"{entity}" leak OR breach OR hack OR exposed',
        f'"{entity}" shell company OR offshore OR nominee',
    ]
    for query in queries:
        try:
            q = quote_plus(query)
            html = await _tor_fetch(
                f"https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/html/?q={q}",
                timeout=40,
            )
            if not html or "ERROR:" in html or len(html) < 500:
                html = await _tor_fetch(f"https://duckduckgo.com/html/?q={q}", timeout=25)
            if html and "ERROR:" not in html:
                soup = BeautifulSoup(html, "html.parser")
                for r in soup.select(".result__a")[:5]:
                    title = r.get_text(strip=True)[:200]
                    if not any(f.get("title") == title for f in findings):
                        findings.append({
                            "source": "duckduckgo_adverse",
                            "type": "adverse_media",
                            "title": title,
                            "url": r.get("href", "")[:300],
                            "query": query[:100],
                            "retrieved_at": _now(),
                        })
        except Exception:
            continue
    return findings


# ==========================================================================
# SOURCE 6: Dehashed — breach/credential database ($15/mo)
# ==========================================================================
async def _search_dehashed(entity: str, domain: str = "") -> list[dict]:
    """Search Dehashed breach database (v2 API). Requires DEHASHED_API_KEY."""
    findings = []
    if not DEHASHED_API_KEY:
        return findings
    try:
        headers = {
            "DeHashed-Api-Key": DEHASHED_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Use exact-phrase match for entity name to avoid substring false positives.
        # Domain searched as-is (already specific).
        queries = [f'"{entity}"']
        if domain:
            queries.append(domain)
        dh_kwargs = dict(timeout=25)
        if BRIGHTDATA_PROXY:
            dh_kwargs["proxy"] = BRIGHTDATA_PROXY
        async with httpx.AsyncClient(**dh_kwargs) as client:
            for query in queries:
                resp = await client.post(
                    "https://api.dehashed.com/v2/search",
                    json={"query": query, "size": 50, "de_dupe": True},
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for entry in data.get("entries", [])[:50]:
                        email = entry.get("email", "")
                        if isinstance(email, list):
                            email = email[0] if email else ""
                        findings.append({
                            "source": "dehashed",
                            "type": "breach_record",
                            "email": email,
                            "username": entry.get("username", ""),
                            "name": entry.get("name", ""),
                            "database_name": entry.get("database_name", ""),
                            "hashed_password": bool(entry.get("hashed_password", "")),
                            "phone": entry.get("phone", ""),
                            "address": entry.get("address", ""),
                            "ip_address": entry.get("ip_address", ""),
                            "query_used": query,
                            "retrieved_at": _now(),
                        })
    except Exception as e:
        findings.append({"source": "dehashed", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 7: LeakCheck — breach lookup (free API, 100/day)
# ==========================================================================
async def _search_leakcheck(entity: str, domain: str = "") -> list[dict]:
    """Search LeakCheck for breach records by domain or email patterns."""
    findings = []
    targets = []
    if domain:
        targets.append(("domain", domain))
    # Exact phrase to avoid substring false positives on common words
    targets.append(("keyword", f'"{entity}"'))
    try:
        for search_type, query in targets:
            q = quote_plus(query)
            data = await _direct_fetch_json(
                f"https://leakcheck.io/api/public?check={q}",
                timeout=20,
            )
            if data and isinstance(data, dict):
                sources = data.get("sources", [])
                if isinstance(sources, list):
                    for src in sources[:20]:
                        findings.append({
                            "source": "leakcheck",
                            "type": "breach_record",
                            "database_name": src.get("name", "") if isinstance(src, dict) else str(src),
                            "breach_date": src.get("date", "") if isinstance(src, dict) else "",
                            "query_used": query,
                            "search_type": search_type,
                            "retrieved_at": _now(),
                        })
                elif data.get("found"):
                    findings.append({
                        "source": "leakcheck",
                        "type": "breach_record",
                        "found": True,
                        "total": data.get("total", 0),
                        "query_used": query,
                        "search_type": search_type,
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "leakcheck", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 8: BreachDirectory — breach search (free API)
# ==========================================================================
async def _search_breachdirectory(entity: str, domain: str = "") -> list[dict]:
    """Search BreachDirectory for leaked credentials by domain."""
    findings = []
    if not domain:
        return findings
    try:
        data = await _direct_fetch_json(
            f"https://breachdirectory.p.rapidapi.com/?func=auto&term={quote_plus(domain)}",
            timeout=20,
            headers={"X-RapidAPI-Host": "breachdirectory.p.rapidapi.com"},
        )
        if data and data.get("success"):
            for entry in data.get("result", [])[:20]:
                findings.append({
                    "source": "breachdirectory",
                    "type": "breach_record",
                    "email": entry.get("email", ""),
                    "has_password": entry.get("has_password", False),
                    "password": entry.get("password", "")[:3] + "***" if entry.get("password") else "",
                    "sha1": entry.get("sha1", ""),
                    "database_name": entry.get("sources", ""),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "breachdirectory", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 9: HIBP (Have I Been Pwned) — breach notification [NEW]
# ==========================================================================
async def _search_hibp(domain: str) -> list[dict]:
    """Check HIBP for breaches associated with a domain (free API)."""
    findings = []
    if not domain:
        return findings
    try:
        # HIBP v3 API — search breaches by domain (no key needed for breach list)
        data = await _direct_fetch_json(
            f"https://haveibeenpwned.com/api/v3/breaches?domain={quote_plus(domain)}",
            timeout=20,
            headers={
                "User-Agent": "CrawlDarkWebGateway/4.0",
                "Accept": "application/json",
            },
        )
        if data and isinstance(data, list):
            for breach in data[:20]:
                findings.append({
                    "source": "hibp",
                    "type": "breach_record",
                    "breach_name": breach.get("Name", ""),
                    "breach_title": breach.get("Title", ""),
                    "domain": breach.get("Domain", ""),
                    "breach_date": breach.get("BreachDate", ""),
                    "pwn_count": breach.get("PwnCount", 0),
                    "data_classes": breach.get("DataClasses", []),
                    "is_verified": breach.get("IsVerified", False),
                    "is_sensitive": breach.get("IsSensitive", False),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "hibp", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 10: Psbdmp — Pastebin dump aggregator
# ==========================================================================
async def _search_psbdmp(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        resp = await _tor_fetch(f"https://psbdmp.ws/api/v3/search/{q}", timeout=30)
        if resp and "ERROR:" not in resp:
            try:
                data = json.loads(resp)
                if isinstance(data, list):
                    for item in data[:15]:
                        findings.append({
                            "source": "psbdmp",
                            "type": "paste_dump",
                            "paste_id": item.get("id", ""),
                            "content_preview": str(item.get("text", ""))[:500],
                            "retrieved_at": _now(),
                        })
            except json.JSONDecodeError:
                pass
    except Exception as e:
        findings.append({"source": "psbdmp", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 11: LeakIX — exposed services & data leaks
# ==========================================================================
async def _search_leakix(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        data = await _direct_fetch_json(
            f"https://leakix.net/search?scope=leak&q={q}",
            headers={"Accept": "application/json"},
        )
        if data and isinstance(data, list):
            for item in data[:15]:
                findings.append({
                    "source": "leakix",
                    "type": "exposed_service",
                    "title": item.get("summary", item.get("event_type", ""))[:200],
                    "ip": item.get("ip", ""),
                    "port": item.get("port", ""),
                    "protocol": item.get("protocol", ""),
                    "country": item.get("geoip", {}).get("country_name", ""),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "leakix", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 12: HudsonRock Cavalier — breach/infostealer data (free API)
# ==========================================================================
async def _search_hudsonrock(domain: str) -> list[dict]:
    """Check if company domain appears in infostealer logs."""
    findings = []
    if not domain:
        return findings
    try:
        data = await _direct_fetch_json(
            f"https://cavalier.hudsonrock.com/api/json/v2/osint-tools/search-by-domain?domain={domain}",
            timeout=20,
        )
        if data:
            stealers = data.get("stealers", [])
            findings.append({
                "source": "hudsonrock_cavalier",
                "type": "infostealer_exposure",
                "domain": domain,
                "total_stealers": len(stealers),
                "sample_entries": [
                    {
                        "computer_name": s.get("computer_name", ""),
                        "date_compromised": s.get("date_compromised", ""),
                        "malware_path": s.get("malware_path", ""),
                    }
                    for s in stealers[:10]
                ],
                "retrieved_at": _now(),
            })
    except Exception as e:
        findings.append({"source": "hudsonrock_cavalier", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 13: JustPaste.it — paste site search [NEW]
# ==========================================================================
async def _search_justpaste(entity: str) -> list[dict]:
    """Search JustPaste.it for entity mentions in paste dumps."""
    findings = []
    try:
        q = quote_plus(entity)
        # JustPaste.it has a search page
        html = await _tor_fetch(f"https://justpaste.it/search?q={q}", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select("a.article-link, .search-result a, .result a, h3 a")[:15]:
                title = r.get_text(strip=True)[:200]
                href = r.get("href", "")
                if title and len(title) > 3:
                    findings.append({
                        "source": "justpaste",
                        "type": "paste_dump",
                        "title": title,
                        "url": href if href.startswith("http") else f"https://justpaste.it{href}",
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "justpaste", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 14: GitHub/Gist code search — leaked credentials [NEW]
# ==========================================================================
async def _search_github(entity: str, domain: str = "") -> list[dict]:
    """Search GitHub code for leaked credentials, keys, or entity mentions."""
    findings = []
    queries = [f'"{entity}"']
    if domain:
        queries.append(f'"{domain}" password OR secret OR key OR token')
    try:
        for query in queries:
            q = quote_plus(query)
            data = await _direct_fetch_json(
                f"https://api.github.com/search/code?q={q}&per_page=10",
                timeout=20,
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "CrawlDarkWebGateway/4.0",
                },
            )
            if data and isinstance(data, dict):
                for item in data.get("items", [])[:10]:
                    repo = item.get("repository", {})
                    findings.append({
                        "source": "github_code",
                        "type": "code_leak",
                        "filename": item.get("name", ""),
                        "path": item.get("path", ""),
                        "repo_name": repo.get("full_name", ""),
                        "repo_url": repo.get("html_url", ""),
                        "file_url": item.get("html_url", ""),
                        "query_used": query[:100],
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "github_code", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 15: Ransomlook — ransomware victim search
# ==========================================================================
async def _search_ransomlook(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        data = await _direct_fetch_json(
            f"https://www.ransomlook.io/api/search?query={q}",
            timeout=20,
        )
        if data and isinstance(data, list):
            for item in data[:10]:
                findings.append({
                    "source": "ransomlook",
                    "type": "ransomware_victim",
                    "group": item.get("group_name", ""),
                    "victim": item.get("post_title", item.get("victim", ""))[:200],
                    "date": item.get("discovered", item.get("date", "")),
                    "url": item.get("post_url", ""),
                    "retrieved_at": _now(),
                })
        elif data and isinstance(data, dict):
            for group, victims in data.items():
                if isinstance(victims, list):
                    for v in victims[:5]:
                        findings.append({
                            "source": "ransomlook",
                            "type": "ransomware_victim",
                            "group": group,
                            "victim": str(v.get("post_title", v) if isinstance(v, dict) else v)[:200],
                            "date": v.get("discovered", "") if isinstance(v, dict) else "",
                            "retrieved_at": _now(),
                        })
    except Exception as e:
        findings.append({"source": "ransomlook", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 16: OCCRP Aleph — organized crime & corruption data
# ==========================================================================
async def _search_occrp(entity: str) -> list[dict]:
    findings = []
    try:
        # Exact phrase match to avoid substring false positives
        q = quote_plus(f'"{entity}"')
        data = await _direct_fetch_json(
            f"https://aleph.occrp.org/api/2/search?q={q}&limit=20",
            timeout=25,
        )
        if data and "results" in data:
            for item in data["results"][:15]:
                props = item.get("properties", {})
                findings.append({
                    "source": "occrp_aleph",
                    "type": "organized_crime_data",
                    "title": (props.get("name", [""])[0] if isinstance(props.get("name"), list)
                              else props.get("name", ""))[:200],
                    "schema": item.get("schema", ""),
                    "dataset": item.get("collection", {}).get("label", ""),
                    "countries": props.get("country", []),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "occrp_aleph", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 17: ICIJ Offshore Leaks — Panama/Paradise/Pandora Papers
# ==========================================================================
async def _search_icij(entity: str) -> list[dict]:
    findings = []
    try:
        # Exact phrase match to avoid substring false positives
        q = quote_plus(f'"{entity}"')
        data = await _direct_fetch_json(
            f"https://offshoreleaks.icij.org/api/v1/search?q={q}&limit=20",
            timeout=25,
        )
        if data and isinstance(data, dict):
            for item in data.get("results", data.get("data", []))[:15]:
                findings.append({
                    "source": "icij_offshore_leaks",
                    "type": "offshore_entity",
                    "title": (item.get("name", "") or item.get("node_id", ""))[:200],
                    "jurisdiction": item.get("jurisdiction", ""),
                    "dataset": item.get("sourceID", item.get("source", "")),
                    "linked_to": item.get("linked_to", ""),
                    "incorporation_date": item.get("incorporation_date", ""),
                    "status": item.get("status", ""),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "icij_offshore_leaks", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 18: OpenSanctions — global sanctions & PEP database
# ==========================================================================
async def _search_opensanctions(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        data = await _direct_fetch_json(
            f"https://api.opensanctions.org/search/default?q={q}&limit=20",
            timeout=25,
        )
        if data and "results" in data:
            for item in data["results"][:15]:
                # Filter low-confidence fuzzy matches (score 0-100)
                if item.get("score", 0) < 70:
                    continue
                props = item.get("properties", {})
                findings.append({
                    "source": "opensanctions",
                    "type": "sanctions_pep",
                    "title": (props.get("name", [""])[0] if isinstance(props.get("name"), list)
                              else str(props.get("name", "")))[:200],
                    "schema": item.get("schema", ""),
                    "datasets": item.get("datasets", []),
                    "countries": props.get("country", []),
                    "score": item.get("score", 0),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "opensanctions", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 19: OpenCorporates — 200M+ company records [NEW]
# ==========================================================================
async def _search_opencorporates(entity: str, country: str = "") -> list[dict]:
    """Search OpenCorporates for company registration data."""
    findings = []
    try:
        q = quote_plus(entity)
        url = f"https://api.opencorporates.com/v0.4/companies/search?q={q}&per_page=10"
        if country:
            url += f"&jurisdiction_code={country.lower()}"
        data = await _direct_fetch_json(url, timeout=20)
        if data and isinstance(data, dict):
            results = data.get("results", {})
            companies = results.get("companies", [])
            for item in companies[:10]:
                company = item.get("company", {})
                findings.append({
                    "source": "opencorporates",
                    "type": "corporate_record",
                    "company_name": company.get("name", "")[:200],
                    "company_number": company.get("company_number", ""),
                    "jurisdiction": company.get("jurisdiction_code", ""),
                    "status": company.get("current_status", ""),
                    "incorporation_date": company.get("incorporation_date", ""),
                    "dissolution_date": company.get("dissolution_date", ""),
                    "company_type": company.get("company_type", ""),
                    "registered_address": company.get("registered_address_in_full", ""),
                    "url": company.get("opencorporates_url", ""),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "opencorporates", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 20: WikiLeaks — cables/docs search
# ==========================================================================
async def _search_wikileaks(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(f'"{entity}"')
        html = await _tor_fetch(f"https://search.wikileaks.org/?q={q}", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            entity_lower = entity.lower()
            entity_words = [w for w in entity_lower.split() if len(w) > 3]
            for r in soup.select(".result a, .search-result a, h4 a")[:15]:
                title = r.get_text(strip=True)
                title_lower = title.lower()
                if title and len(title) > 5 and any(w in title_lower for w in entity_words):
                    findings.append({
                        "source": "wikileaks",
                        "type": "leaked_document",
                        "title": title[:200],
                        "url": r.get("href", "")[:300],
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "wikileaks", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 21: Telegram search — public channels/groups
# ==========================================================================
async def _search_telegram(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        html = await _tor_fetch(f"https://tgstat.com/search?q={q}&type=posts", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select(".post-card, .post-body, .post-text, .channel-card")[:15]:
                link = r.select_one("a[href*='t.me']") or r.select_one("a")
                text = r.get_text(strip=True)[:300]
                if text and len(text) > 20 and (
                    entity.lower().split()[0] in text.lower()
                    or len(text) > 50
                ):
                    findings.append({
                        "source": "telegram_search",
                        "type": "telegram_mention",
                        "title": text[:200],
                        "url": link.get("href", "") if link else "",
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "telegram_search", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 22: Google Cache / Web Archive via Tor — find removed content
# ==========================================================================
async def _search_web_archive(entity: str) -> list[dict]:
    findings = []
    try:
        q = quote_plus(entity)
        data = await _direct_fetch_json(
            f"https://web.archive.org/cdx/search/cdx?url=*&matchType=host&output=json&limit=10&fl=original,timestamp,statuscode&filter=statuscode:200&q={q}",
            timeout=20,
        )
        html = await _tor_fetch(
            f"https://web.archive.org/web/*/https://www.google.com/search?q={q}",
            timeout=20,
        )
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select("a[href*='web.archive.org/web/']")[:10]:
                findings.append({
                    "source": "web_archive",
                    "type": "archived_page",
                    "title": r.get_text(strip=True)[:200],
                    "url": r.get("href", "")[:300],
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "web_archive", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 23: Court/legal record search via Tor
# ==========================================================================
async def _search_court_records(entity: str, country: str = "") -> list[dict]:
    """Search for court records, legal filings, regulatory actions."""
    findings = []
    queries = [
        f'"{entity}" site:gov.uk filetype:pdf',
        f'"{entity}" court OR tribunal OR judgment',
        f'"{entity}" regulatory action OR enforcement OR fine',
    ]
    for query in queries[:2]:
        try:
            q = quote_plus(query)
            html = await _tor_fetch(
                f"https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/html/?q={q}",
                timeout=40,
            )
            if not html or "ERROR:" in html or len(html) < 500:
                html = await _tor_fetch(f"https://duckduckgo.com/html/?q={q}", timeout=25)
            if html and "ERROR:" not in html:
                soup = BeautifulSoup(html, "html.parser")
                for r in soup.select(".result__a")[:5]:
                    title = r.get_text(strip=True)[:200]
                    if not any(f.get("title") == title for f in findings):
                        findings.append({
                            "source": "court_records_tor",
                            "type": "legal_record",
                            "title": title,
                            "url": r.get("href", "")[:300],
                            "query": query[:100],
                            "retrieved_at": _now(),
                        })
        except Exception:
            continue
    return findings


# ==========================================================================
# SOURCE 24: Reddit — darknet/fraud subreddit search [NEW]
# ==========================================================================
async def _search_reddit(entity: str) -> list[dict]:
    """Search Reddit for entity mentions in relevant subreddits."""
    findings = []
    try:
        q = quote_plus(entity)
        # Reddit JSON search API (no auth needed)
        data = await _direct_fetch_json(
            f"https://www.reddit.com/search.json?q={q}&sort=relevance&limit=15",
            timeout=20,
            headers={"User-Agent": "CrawlDarkWebGateway/4.0"},
        )
        if data and isinstance(data, dict):
            posts = data.get("data", {}).get("children", [])
            for post in posts[:15]:
                pdata = post.get("data", {})
                subreddit = pdata.get("subreddit", "")
                findings.append({
                    "source": "reddit",
                    "type": "social_mention",
                    "title": pdata.get("title", "")[:200],
                    "subreddit": subreddit,
                    "author": pdata.get("author", ""),
                    "score": pdata.get("score", 0),
                    "num_comments": pdata.get("num_comments", 0),
                    "url": f"https://reddit.com{pdata.get('permalink', '')}",
                    "created_utc": pdata.get("created_utc", ""),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "reddit", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 25: PulseDive — threat intel / IOCs (free community API)
# ==========================================================================
async def _search_pulsedive(entity: str, domain: str = "") -> list[dict]:
    """Search PulseDive threat intel for domain/entity indicators."""
    findings = []
    targets = []
    if domain:
        targets.append(domain)
    targets.append(entity)
    try:
        for target in targets:
            q = quote_plus(target)
            data = await _direct_fetch_json(
                f"https://pulsedive.com/api/search.php?value={q}&limit=20&pretty=1",
                timeout=20,
            )
            if data and isinstance(data, dict):
                results = data.get("results", [])
                if isinstance(results, list):
                    for r in results[:10]:
                        findings.append({
                            "source": "pulsedive",
                            "type": "threat_intel",
                            "indicator": r.get("indicator", ""),
                            "indicator_type": r.get("type", ""),
                            "risk": r.get("risk", ""),
                            "summary": r.get("summary", "")[:300],
                            "threats": r.get("threats", []),
                            "query_used": target,
                            "retrieved_at": _now(),
                        })
            elif data and isinstance(data, list):
                for r in data[:10]:
                    findings.append({
                        "source": "pulsedive",
                        "type": "threat_intel",
                        "indicator": r.get("indicator", str(r)[:100]),
                        "risk": r.get("risk", ""),
                        "query_used": target,
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "pulsedive", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 26: FullHunt — attack surface / exposed assets (free tier)
# ==========================================================================
async def _search_fullhunt(domain: str) -> list[dict]:
    """Search FullHunt for exposed subdomains and services."""
    findings = []
    if not domain:
        return findings
    try:
        data = await _direct_fetch_json(
            f"https://fullhunt.io/api/v1/domain/{quote_plus(domain)}/subdomains",
            timeout=20,
        )
        if data and isinstance(data, dict):
            hosts = data.get("hosts", data.get("subdomains", []))
            if isinstance(hosts, list):
                for host in hosts[:20]:
                    if isinstance(host, str):
                        findings.append({
                            "source": "fullhunt",
                            "type": "exposed_asset",
                            "subdomain": host,
                            "domain": domain,
                            "retrieved_at": _now(),
                        })
                    elif isinstance(host, dict):
                        findings.append({
                            "source": "fullhunt",
                            "type": "exposed_asset",
                            "subdomain": host.get("host", ""),
                            "ip": host.get("ip", ""),
                            "port": host.get("port", ""),
                            "technology": host.get("technology", []),
                            "domain": domain,
                            "retrieved_at": _now(),
                        })
            metadata = data.get("metadata", {})
            if metadata:
                findings.append({
                    "source": "fullhunt",
                    "type": "domain_intel",
                    "domain": domain,
                    "total_subdomains": metadata.get("total_results", len(hosts)),
                    "technologies": metadata.get("technologies", []),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "fullhunt", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 27: Greynoise — IP reputation (free community API)
# ==========================================================================
async def _search_greynoise(entity: str) -> list[dict]:
    """Search Greynoise for entity mentions in scanner/noise data."""
    findings = []
    try:
        q = quote_plus(entity)
        data = await _direct_fetch_json(
            f"https://api.greynoise.io/v3/community/{q}",
            timeout=15,
        )
        if data and isinstance(data, dict) and data.get("seen"):
            findings.append({
                "source": "greynoise",
                "type": "ip_reputation",
                "ip": data.get("ip", ""),
                "noise": data.get("noise", False),
                "riot": data.get("riot", False),
                "classification": data.get("classification", ""),
                "name": data.get("name", ""),
                "link": data.get("link", ""),
                "retrieved_at": _now(),
            })
    except Exception:
        pass  # Greynoise is supplementary, don't error on miss
    return findings


# ==========================================================================
# SOURCE 28: Shodan — internet-connected devices (free tier) [NEW]
# ==========================================================================
async def _search_shodan(entity: str, domain: str = "") -> list[dict]:
    """Search Shodan for exposed infrastructure (free API tier)."""
    findings = []
    target = domain or entity
    try:
        if SHODAN_API_KEY:
            # Authenticated search
            q = quote_plus(target)
            data = await _direct_fetch_json(
                f"https://api.shodan.io/shodan/host/search?key={SHODAN_API_KEY}&query={q}&page=1",
                timeout=20,
            )
            if data and isinstance(data, dict):
                for match in data.get("matches", [])[:15]:
                    findings.append({
                        "source": "shodan",
                        "type": "exposed_infrastructure",
                        "ip": match.get("ip_str", ""),
                        "port": match.get("port", ""),
                        "org": match.get("org", ""),
                        "product": match.get("product", ""),
                        "version": match.get("version", ""),
                        "os": match.get("os", ""),
                        "country": match.get("location", {}).get("country_name", ""),
                        "hostnames": match.get("hostnames", []),
                        "retrieved_at": _now(),
                    })
        else:
            # Free tier — search without key (limited)
            q = quote_plus(target)
            data = await _direct_fetch_json(
                f"https://api.shodan.io/shodan/host/search?query={q}",
                timeout=20,
            )
            # Also try DNS resolve for domain
            if domain:
                dns_data = await _direct_fetch_json(
                    f"https://api.shodan.io/dns/resolve?hostnames={domain}",
                    timeout=15,
                )
                if dns_data and isinstance(dns_data, dict):
                    ip = dns_data.get(domain)
                    if ip:
                        findings.append({
                            "source": "shodan",
                            "type": "dns_resolve",
                            "domain": domain,
                            "ip": ip,
                            "retrieved_at": _now(),
                        })
    except Exception as e:
        findings.append({"source": "shodan", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 29: VirusTotal — domain/IP reputation (free tier) [NEW]
# ==========================================================================
async def _search_virustotal(domain: str) -> list[dict]:
    """Check VirusTotal for domain reputation and detections."""
    findings = []
    if not domain or not VIRUSTOTAL_API_KEY:
        return findings
    try:
        data = await _direct_fetch_json(
            f"https://www.virustotal.com/api/v3/domains/{domain}",
            timeout=20,
            headers={"x-apikey": VIRUSTOTAL_API_KEY},
        )
        if data and isinstance(data, dict):
            attrs = data.get("data", {}).get("attributes", {})
            analysis = attrs.get("last_analysis_stats", {})
            findings.append({
                "source": "virustotal",
                "type": "domain_reputation",
                "domain": domain,
                "malicious": analysis.get("malicious", 0),
                "suspicious": analysis.get("suspicious", 0),
                "harmless": analysis.get("harmless", 0),
                "undetected": analysis.get("undetected", 0),
                "reputation": attrs.get("reputation", 0),
                "categories": attrs.get("categories", {}),
                "registrar": attrs.get("registrar", ""),
                "creation_date": attrs.get("creation_date", ""),
                "whois": attrs.get("whois", "")[:500],
                "retrieved_at": _now(),
            })
    except Exception as e:
        findings.append({"source": "virustotal", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 30: AlienVault OTX — open threat exchange [NEW]
# ==========================================================================
async def _search_alienvault(entity: str, domain: str = "") -> list[dict]:
    """Search AlienVault OTX for threat intelligence (free, no key needed)."""
    findings = []
    try:
        # Search pulses by keyword
        q = quote_plus(entity)
        data = await _direct_fetch_json(
            f"https://otx.alienvault.com/api/v1/search/pulses?q={q}&page=1&limit=10",
            timeout=20,
        )
        if data and isinstance(data, dict):
            for pulse in data.get("results", [])[:10]:
                findings.append({
                    "source": "alienvault_otx",
                    "type": "threat_intel",
                    "pulse_name": pulse.get("name", "")[:200],
                    "description": pulse.get("description", "")[:300],
                    "author": pulse.get("author", {}).get("username", ""),
                    "created": pulse.get("created", ""),
                    "tags": pulse.get("tags", [])[:10],
                    "adversary": pulse.get("adversary", ""),
                    "targeted_countries": pulse.get("targeted_countries", []),
                    "indicators_count": len(pulse.get("indicators", [])),
                    "retrieved_at": _now(),
                })

        # Also check domain-specific indicators
        if domain:
            dom_data = await _direct_fetch_json(
                f"https://otx.alienvault.com/api/v1/indicators/domain/{domain}/general",
                timeout=20,
            )
            if dom_data and isinstance(dom_data, dict):
                pulse_count = dom_data.get("pulse_info", {}).get("count", 0)
                if pulse_count > 0:
                    findings.append({
                        "source": "alienvault_otx",
                        "type": "domain_threat_intel",
                        "domain": domain,
                        "pulse_count": pulse_count,
                        "whois": dom_data.get("whois", "")[:300],
                        "alexa_rank": dom_data.get("alexa", ""),
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "alienvault_otx", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 31: AbuseIPDB — IP abuse reports (free tier) [NEW]
# ==========================================================================
async def _search_abuseipdb(domain: str) -> list[dict]:
    """Check AbuseIPDB for abuse reports against domain/IP."""
    findings = []
    if not domain or not ABUSEIPDB_API_KEY:
        return findings
    try:
        # First resolve domain to IP, then check
        dns_html = await _direct_fetch_text(
            f"https://dns.google/resolve?name={domain}&type=A",
            timeout=10,
        )
        ip = None
        if dns_html:
            try:
                dns_data = json.loads(dns_html)
                answers = dns_data.get("Answer", [])
                for ans in answers:
                    if ans.get("type") == 1:  # A record
                        ip = ans.get("data")
                        break
            except json.JSONDecodeError:
                pass

        if ip:
            data = await _direct_fetch_json(
                f"https://api.abuseipdb.com/api/v2/check?ipAddress={ip}&maxAgeInDays=90",
                timeout=15,
                headers={
                    "Key": ABUSEIPDB_API_KEY,
                    "Accept": "application/json",
                },
            )
            if data and isinstance(data, dict):
                abuse_data = data.get("data", {})
                if abuse_data.get("totalReports", 0) > 0:
                    findings.append({
                        "source": "abuseipdb",
                        "type": "ip_abuse",
                        "ip": ip,
                        "domain": domain,
                        "abuse_confidence": abuse_data.get("abuseConfidenceScore", 0),
                        "total_reports": abuse_data.get("totalReports", 0),
                        "country": abuse_data.get("countryCode", ""),
                        "isp": abuse_data.get("isp", ""),
                        "usage_type": abuse_data.get("usageType", ""),
                        "is_whitelisted": abuse_data.get("isWhitelisted", False),
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "abuseipdb", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 32: Onion.live — Tor .onion directory [NEW]
# ==========================================================================
async def _search_onion_live(entity: str) -> list[dict]:
    """Search Onion.live for .onion sites mentioning the entity."""
    findings = []
    try:
        q = quote_plus(entity)
        html = await _tor_fetch(f"https://onion.live/search?q={q}", timeout=30)
        if html and "ERROR:" not in html:
            soup = BeautifulSoup(html, "html.parser")
            for r in soup.select(".search-result, .result, .card, tr")[:15]:
                link = r.select_one("a")
                text = r.get_text(strip=True)[:300]
                if link and text and len(text) > 10:
                    findings.append({
                        "source": "onion_live",
                        "type": "dark_web_directory",
                        "title": text[:200],
                        "url": link.get("href", "")[:300],
                        "retrieved_at": _now(),
                    })
    except Exception as e:
        findings.append({"source": "onion_live", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 33: IntelligenceX — paste/leak/darknet archive (free scrape)
# ==========================================================================
async def _search_intelx(entity: str) -> list[dict]:
    """Search IntelligenceX. Uses Pro API if INTELX_API_KEY is set, else free scrape."""
    findings = []
    try:
        if INTELX_API_KEY:
            search_body = {"term": entity, "maxresults": 20, "media": 0, "sort": 2, "terminate": []}
            ix_kwargs = dict(timeout=30)
            if BRIGHTDATA_PROXY:
                ix_kwargs["proxy"] = BRIGHTDATA_PROXY
            async with httpx.AsyncClient(**ix_kwargs) as client:
                resp = await client.post(
                    "https://free.intelx.io/intelligent/search",
                    json=search_body,
                    headers={"x-key": INTELX_API_KEY, "Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    search_id = resp.json().get("id", "")
                    await asyncio.sleep(2)
                    resp2 = await client.get(
                        f"https://free.intelx.io/intelligent/search/result?id={search_id}&limit=20",
                        headers={"x-key": INTELX_API_KEY},
                    )
                    if resp2.status_code == 200:
                        records = resp2.json().get("records", [])
                        for rec in records[:20]:
                            sys_id = rec.get("systemid", "")
                            bucket = rec.get("bucket", "")
                            preview = ""
                            if sys_id:
                                try:
                                    resp3 = await client.get(
                                        f"https://free.intelx.io/file/preview?type=0&storageid={sys_id}&bucket={bucket}",
                                        headers={"x-key": INTELX_API_KEY},
                                    )
                                    if resp3.status_code == 200:
                                        preview = resp3.text[:2000]
                                except Exception:
                                    pass
                            findings.append({
                                "source": "intelx_pro",
                                "type": "leak_archive",
                                "title": rec.get("name", "")[:200],
                                "media_type": rec.get("mediah", ""),
                                "date": rec.get("date", ""),
                                "size": rec.get("size", 0),
                                "bucket": bucket,
                                "full_content": preview if preview else None,
                                "retrieved_at": _now(),
                            })
        else:
            q = quote_plus(entity)
            html = await _tor_fetch(f"https://intelx.io/?s={q}", timeout=30)
            if html and "ERROR:" not in html:
                soup = BeautifulSoup(html, "html.parser")
                for r in soup.select(".result, .card, tr[data-id]")[:15]:
                    text = r.get_text(strip=True)[:500]
                    link = r.select_one("a")
                    if text and len(text) > 30 and entity.lower().split()[0] in text.lower():
                        findings.append({
                            "source": "intelx",
                            "type": "leak_archive",
                            "title": text[:200],
                            "url": link.get("href", "") if link else "",
                            "retrieved_at": _now(),
                        })
    except Exception as e:
        findings.append({"source": "intelx", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 34: crt.sh — Certificate Transparency logs [NEW]
# ==========================================================================
async def _search_crtsh(domain: str) -> list[dict]:
    """Search crt.sh for all SSL certificates issued for a domain.
    Reveals subdomains, shadow infrastructure, linked domains."""
    findings = []
    if not domain:
        return findings
    try:
        data = await _direct_fetch_json(
            f"https://crt.sh/?q=%25.{quote_plus(domain)}&output=json",
            timeout=25,
        )
        if data and isinstance(data, list):
            # Deduplicate by common_name
            seen_names = set()
            for cert in data[:50]:
                cn = cert.get("common_name", "")
                if cn and cn not in seen_names:
                    seen_names.add(cn)
                    # Parse SAN names for extra domains
                    name_value = cert.get("name_value", "")
                    san_domains = [n.strip() for n in name_value.split("\n") if n.strip()] if name_value else []
                    findings.append({
                        "source": "crtsh",
                        "type": "certificate_transparency",
                        "common_name": cn,
                        "san_domains": san_domains[:10],
                        "issuer": cert.get("issuer_name", ""),
                        "not_before": cert.get("not_before", ""),
                        "not_after": cert.get("not_after", ""),
                        "serial_number": cert.get("serial_number", ""),
                        "cert_id": cert.get("id", ""),
                        "entry_timestamp": cert.get("entry_timestamp", ""),
                        "retrieved_at": _now(),
                    })
            # Summary finding
            if findings:
                all_sans = set()
                for f in findings:
                    all_sans.update(f.get("san_domains", []))
                findings.insert(0, {
                    "source": "crtsh",
                    "type": "certificate_summary",
                    "domain": domain,
                    "total_certificates": len(data),
                    "unique_common_names": len(seen_names),
                    "all_subdomains": sorted(list(all_sans))[:50],
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "crtsh", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# SOURCE 35: Interpol Red Notices — wanted persons [NEW]
# ==========================================================================
async def _search_interpol(entity: str, owners: list[str] = None) -> list[dict]:
    """Search Interpol Red Notices for wanted persons. Checks entity name
    and all provided owner/director names."""
    findings = []
    # Build search targets: entity name words + individual owners
    search_names = []
    if owners:
        search_names.extend(owners)
    # Also try entity name as person (some entities are named after individuals)
    search_names.append(entity)

    for name in search_names[:6]:
        try:
            parts = name.strip().split()
            if len(parts) < 2:
                continue
            # Interpol API uses forename + name (surname)
            forename = parts[0]
            surname = " ".join(parts[1:])
            data = await _direct_fetch_json(
                f"https://ws-public.interpol.int/notices/v1/red?forename={quote_plus(forename)}&name={quote_plus(surname)}&resultPerPage=20",
                timeout=20,
            )
            if data and isinstance(data, dict):
                notices = data.get("_embedded", {}).get("notices", [])
                for notice in notices[:10]:
                    links = notice.get("_links", {})
                    detail_url = links.get("self", {}).get("href", "")
                    thumbnail = links.get("thumbnail", {}).get("href", "")
                    findings.append({
                        "source": "interpol_red_notice",
                        "type": "wanted_person",
                        "forename": notice.get("forename", ""),
                        "name": notice.get("name", ""),
                        "date_of_birth": notice.get("date_of_birth", ""),
                        "nationalities": notice.get("nationalities", []),
                        "entity_id": notice.get("entity_id", ""),
                        "detail_url": detail_url,
                        "charge": "",
                        "searched_name": name,
                        "retrieved_at": _now(),
                    })
                    # Fetch detail page for charges
                    if detail_url:
                        try:
                            detail = await _direct_fetch_json(detail_url, timeout=10)
                            if detail:
                                findings[-1]["charge"] = detail.get("charge", "")[:500]
                                findings[-1]["weight"] = detail.get("weight", "")
                                findings[-1]["height"] = detail.get("height", "")
                                findings[-1]["eyes_color"] = detail.get("eyes_colors_id", [])
                                findings[-1]["hair_color"] = detail.get("hairs_id", [])
                                findings[-1]["place_of_birth"] = detail.get("place_of_birth", "")
                                findings[-1]["country_of_birth"] = detail.get("country_of_birth_id", "")
                        except Exception:
                            pass
            # Also search Yellow Notices (missing persons) and UN Notices
            un_data = await _direct_fetch_json(
                f"https://ws-public.interpol.int/notices/v1/un?forename={quote_plus(forename)}&name={quote_plus(surname)}&resultPerPage=10",
                timeout=15,
            )
            if un_data and isinstance(un_data, dict):
                un_notices = un_data.get("_embedded", {}).get("notices", [])
                for notice in un_notices[:5]:
                    findings.append({
                        "source": "interpol_un_notice",
                        "type": "un_sanctions_notice",
                        "forename": notice.get("forename", ""),
                        "name": notice.get("name", ""),
                        "date_of_birth": notice.get("date_of_birth", ""),
                        "nationalities": notice.get("nationalities", []),
                        "reference": notice.get("un_reference", ""),
                        "searched_name": name,
                        "retrieved_at": _now(),
                    })
        except Exception as e:
            findings.append({"source": "interpol_red_notice", "type": "error",
                             "detail": str(e)[:300], "searched_name": name})
    return findings


# ==========================================================================
# SOURCE 36: World Bank Debarment List [NEW]
# ==========================================================================
async def _search_worldbank_debarment(entity: str, owners: list[str] = None) -> list[dict]:
    """Search World Bank Group debarment list for sanctioned firms/individuals.
    Also checks ADB, EBRD, AfDB, IDB cross-debarment."""
    findings = []
    search_terms = [entity]
    if owners:
        search_terms.extend(owners[:3])

    for term in search_terms:
        try:
            # World Bank API — search debarred firms and individuals
            q = quote_plus(term)
            data = await _direct_fetch_json(
                f"https://search.worldbank.org/api/v2/debarr?format=json&qterm={q}&per_page=20",
                timeout=20,
            )
            if data and isinstance(data, dict):
                total = data.get("total", 0)
                rows = data.get("rows", [])
                if isinstance(rows, dict):
                    rows = list(rows.values())
                for row in (rows or [])[:15]:
                    if isinstance(row, dict):
                        findings.append({
                            "source": "worldbank_debarment",
                            "type": "debarment_record",
                            "firm_name": row.get("firm_name", ""),
                            "address": row.get("address", ""),
                            "country": row.get("country", ""),
                            "from_date": row.get("from_date", ""),
                            "to_date": row.get("to_date", ""),
                            "grounds": row.get("grounds", ""),
                            "sanction_type": row.get("sanction_type", ""),
                            "searched_term": term,
                            "total_matches": total,
                            "retrieved_at": _now(),
                        })
        except Exception as e:
            findings.append({"source": "worldbank_debarment", "type": "error",
                             "detail": str(e)[:300], "searched_term": term})

    # Also check ADB (Asian Development Bank) sanctions
    try:
        adb_html = await _direct_fetch_text(
            "https://www.adb.org/who-we-are/integrity/sanctions",
            timeout=15,
        )
        if adb_html and entity.lower().split()[0] in adb_html.lower():
            findings.append({
                "source": "adb_sanctions",
                "type": "debarment_record",
                "note": f"Potential match for '{entity}' found on ADB sanctions page — manual review needed",
                "url": "https://www.adb.org/who-we-are/integrity/sanctions",
                "retrieved_at": _now(),
            })
    except Exception:
        pass

    return findings


# ==========================================================================
# SOURCE 37: URLScan.io — website scanning [NEW]
# ==========================================================================
async def _search_urlscan(domain: str) -> list[dict]:
    """Search URLScan.io for previous scans of a domain. Shows tech stack,
    connected domains, trackers, redirects, and page content."""
    findings = []
    if not domain:
        return findings
    try:
        # Search for existing scans of this domain
        data = await _direct_fetch_json(
            f"https://urlscan.io/api/v1/search/?q=domain:{quote_plus(domain)}&size=10",
            timeout=20,
            headers={"User-Agent": "CrawlDarkWebGateway/4.0"},
        )
        if data and isinstance(data, dict):
            results = data.get("results", [])
            for scan in results[:10]:
                page = scan.get("page", {})
                task = scan.get("task", {})
                stats = scan.get("stats", {})
                findings.append({
                    "source": "urlscan",
                    "type": "website_scan",
                    "url": page.get("url", ""),
                    "domain": page.get("domain", ""),
                    "ip": page.get("ip", ""),
                    "country": page.get("country", ""),
                    "server": page.get("server", ""),
                    "title": page.get("title", "")[:200],
                    "status_code": page.get("status", ""),
                    "asn": page.get("asn", ""),
                    "asnname": page.get("asnname", ""),
                    "scan_time": task.get("time", ""),
                    "screenshot_url": scan.get("screenshot", ""),
                    "result_url": scan.get("result", ""),
                    "unique_ips": stats.get("uniqIPs", 0),
                    "requests": stats.get("requests", 0),
                    "malicious": stats.get("malicious", 0),
                    "retrieved_at": _now(),
                })

            # Summary
            if results:
                all_ips = set()
                all_countries = set()
                for scan in results:
                    p = scan.get("page", {})
                    if p.get("ip"):
                        all_ips.add(p["ip"])
                    if p.get("country"):
                        all_countries.add(p["country"])
                findings.insert(0, {
                    "source": "urlscan",
                    "type": "domain_scan_summary",
                    "domain": domain,
                    "total_scans_found": len(results),
                    "unique_ips_seen": sorted(list(all_ips)),
                    "countries": sorted(list(all_countries)),
                    "retrieved_at": _now(),
                })
    except Exception as e:
        findings.append({"source": "urlscan", "type": "error", "detail": str(e)[:300]})
    return findings


# ==========================================================================
# RESEARCH ORCHESTRATOR
# ==========================================================================

async def _run_research(job_id: str, entity: str, country: str, depth: str,
                        owners: list[str] = None, domain: str = None):
    job = _load_job(job_id)
    if not job:
        return

    job["status"] = "running"
    _save_job(job)

    all_findings = []
    circuits = 0
    source_status = {}

    async def _run_source(name: str, coro):
        nonlocal circuits
        try:
            results = await coro
            circuits += 1
            source_status[name] = {"status": "ok", "count": len(results)}
            return results
        except Exception as e:
            source_status[name] = {"status": "error", "detail": str(e)[:200]}
            return [{"source": name, "type": "error", "detail": str(e)[:200]}]

    try:
        # Phase 1: Dark web search engines (Tor-routed) — 4 sources
        phase1 = await asyncio.gather(
            _run_source("ahmia", _search_ahmia(entity)),
            _run_source("torch", _search_torch(entity)),
            _run_source("haystak", _search_haystak(entity)),
            _run_source("ddg_tor", _search_ddg_tor(entity, country)),
            _run_source("ddg_adverse", _search_ddg_adverse(entity)),
            _run_source("onion_live", _search_onion_live(entity)),
            return_exceptions=True,
        )
        for result in phase1:
            if isinstance(result, list):
                all_findings.extend(result)

        # Phase 2: Leak/breach databases (paid + free) — 9 sources
        phase2 = await asyncio.gather(
            _run_source("dehashed", _search_dehashed(entity, domain or "")),
            _run_source("hibp", _search_hibp(domain or "")),
            _run_source("leakcheck", _search_leakcheck(entity, domain or "")),
            _run_source("breachdirectory", _search_breachdirectory(entity, domain or "")),
            _run_source("psbdmp", _search_psbdmp(entity)),
            _run_source("justpaste", _search_justpaste(entity)),
            _run_source("leakix", _search_leakix(entity)),
            _run_source("hudsonrock", _search_hudsonrock(domain or "")),
            _run_source("ransomlook", _search_ransomlook(entity)),
            return_exceptions=True,
        )
        for result in phase2:
            if isinstance(result, list):
                all_findings.extend(result)

        # Phase 3: Investigative & compliance databases — 7 sources
        phase3 = await asyncio.gather(
            _run_source("occrp", _search_occrp(entity)),
            _run_source("icij", _search_icij(entity)),
            _run_source("opensanctions", _search_opensanctions(entity)),
            _run_source("opencorporates", _search_opencorporates(entity, country)),
            _run_source("interpol", _search_interpol(entity, owners)),
            _run_source("worldbank", _search_worldbank_debarment(entity, owners)),
            return_exceptions=True,
        )
        for result in phase3:
            if isinstance(result, list):
                all_findings.extend(result)

        # Phase 4: Document, archive & social search — 7 sources
        phase4 = await asyncio.gather(
            _run_source("wikileaks", _search_wikileaks(entity)),
            _run_source("telegram", _search_telegram(entity)),
            _run_source("web_archive", _search_web_archive(entity)),
            _run_source("court_records", _search_court_records(entity, country)),
            _run_source("reddit", _search_reddit(entity)),
            _run_source("github", _search_github(entity, domain or "")),
            _run_source("intelx", _search_intelx(entity)),
            return_exceptions=True,
        )
        for result in phase4:
            if isinstance(result, list):
                all_findings.extend(result)

        # Phase 5: Threat intelligence & infrastructure — 10 sources
        phase5 = await asyncio.gather(
            _run_source("pulsedive", _search_pulsedive(entity, domain or "")),
            _run_source("fullhunt", _search_fullhunt(domain or "")),
            _run_source("greynoise", _search_greynoise(entity)),
            _run_source("shodan", _search_shodan(entity, domain or "")),
            _run_source("virustotal", _search_virustotal(domain or "")),
            _run_source("alienvault", _search_alienvault(entity, domain or "")),
            _run_source("abuseipdb", _search_abuseipdb(domain or "")),
            _run_source("crtsh", _search_crtsh(domain or "")),
            _run_source("urlscan", _search_urlscan(domain or "")),
            return_exceptions=True,
        )
        for result in phase5:
            if isinstance(result, list):
                all_findings.extend(result)

        # Phase 6: Search owners/individuals if provided (medium/heavy depth)
        if owners and depth in ("medium", "heavy"):
            for owner in owners[:5]:
                if not _sanitize_check(owner):
                    continue
                owner_tasks = await asyncio.gather(
                    _run_source(f"opensanctions_{owner[:20]}", _search_opensanctions(owner)),
                    _run_source(f"occrp_{owner[:20]}", _search_occrp(owner)),
                    _run_source(f"icij_{owner[:20]}", _search_icij(owner)),
                    _run_source(f"ddg_adverse_{owner[:20]}", _search_ddg_adverse(owner)),
                    return_exceptions=True,
                )
                for result in owner_tasks:
                    if isinstance(result, list):
                        for f in result:
                            f["searched_individual"] = owner
                        all_findings.extend(result)

        job["findings"] = all_findings
        job["tor_circuits_used"] = circuits
        job["source_status"] = source_status
        job["status"] = "completed"
        job["completed_at"] = _now()

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["completed_at"] = _now()

    # Summary stats
    job["summary"] = {
        "total_findings": len(all_findings),
        "by_source": {},
        "by_type": {},
        "sources_searched": len(source_status),
        "sources_with_results": sum(1 for s in source_status.values() if s.get("count", 0) > 0),
    }
    for f in all_findings:
        src = f.get("source", "unknown")
        typ = f.get("type", "unknown")
        job["summary"]["by_source"][src] = job["summary"]["by_source"].get(src, 0) + 1
        job["summary"]["by_type"][typ] = job["summary"]["by_type"].get(typ, 0) + 1

    _save_job(job)

    # Upload to blob storage for persistence
    try:
        entity_snake = _to_snake(entity)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        blob_name = f"dark-web/{entity_snake}_{date_str}.json"
        local_path = os.path.join(JOBS_DIR, f"{job_id}.json")
        uploaded = _upload_to_blob(local_path, blob_name)
        if uploaded:
            job["blob_path"] = f"osint-staging/{blob_name}"
            _save_job(job)
    except Exception:
        pass  # Blob upload is best-effort


# ==========================================================================
# API ENDPOINTS
# ==========================================================================

_ALL_SOURCES = [
    # Tor search engines
    "ahmia", "torch", "haystak", "duckduckgo_tor", "duckduckgo_adverse", "onion_live",
    # Breach/credential databases
    "dehashed", "hibp", "leakcheck", "breachdirectory",
    # Leak/exposure
    "psbdmp", "justpaste", "leakix", "hudsonrock_cavalier", "github_code",
    # Ransomware
    "ransomlook",
    # Investigative & compliance
    "occrp_aleph", "icij_offshore_leaks", "opensanctions", "opencorporates",
    "interpol_red_notice", "worldbank_debarment",
    # Document/archive/social
    "wikileaks", "telegram_search", "web_archive", "court_records", "reddit",
    # Threat intelligence & infrastructure
    "pulsedive", "fullhunt", "greynoise", "shodan", "virustotal",
    "alienvault_otx", "abuseipdb", "crtsh", "urlscan", "intelx",
]


@app.get("/health")
async def health():
    try:
        ip_check = await _tor_fetch("https://check.torproject.org/api/ip", timeout=10)
        tor_data = json.loads(ip_check) if ip_check and "ERROR:" not in ip_check else {}
        return {
            "status": "healthy" if tor_data.get("IsTor") else "degraded",
            "tor_connected": tor_data.get("IsTor", False),
            "tor_exit_ip": tor_data.get("IP", "unknown"),
            "sources": _ALL_SOURCES,
            "source_count": len(_ALL_SOURCES),
            "version": "4.1",
            "paid_sources": {
                "dehashed": bool(DEHASHED_API_KEY),
            },
            "optional_api_keys": {
                "virustotal": bool(VIRUSTOTAL_API_KEY),
                "shodan": bool(SHODAN_API_KEY),
                "abuseipdb": bool(ABUSEIPDB_API_KEY),
            },
            "timestamp": _now(),
        }
    except Exception as e:
        return {"status": "degraded", "error": str(e)}


@app.post("/api/v1/research")
async def submit_research(req: ResearchRequest, x_api_key: str = Header()):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # HARD FAIL on blocked terms
    combined = f"{req.entity_name} {req.country or ''} {' '.join(req.owners or [])}"
    if not _sanitize_check(combined):
        raise HTTPException(status_code=400, detail="BLOCKED: request contains prohibited terms")

    job_id = str(uuid.uuid4())[:12]
    job = {
        "job_id": job_id,
        "entity_name": req.entity_name,
        "country": req.country,
        "owners": req.owners,
        "domain": req.domain,
        "depth": req.depth,
        "status": "queued",
        "started_at": _now(),
        "completed_at": None,
        "findings": [],
        "tor_circuits_used": 0,
        "source_status": {},
        "summary": None,
        "error": None,
    }
    _save_job(job)

    await _run_research(
        job_id, req.entity_name, req.country or "", req.depth,
        owners=req.owners, domain=req.domain,
    )
    job = _load_job(job_id)

    return {
        "job_id": job_id,
        "status": job.get("status", "completed"),
        "findings_count": len(job.get("findings", [])),
        "summary": job.get("summary"),
        "blob_path": job.get("blob_path"),
    }


@app.get("/api/v1/research/{job_id}")
async def get_research(job_id: str, x_api_key: str = Header()):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    job = _load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/v1/jobs")
async def list_jobs(x_api_key: str = Header()):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    jobs = []
    for f in sorted(os.listdir(JOBS_DIR), reverse=True)[:50]:
        if f.endswith(".json"):
            job = _load_job(f.replace(".json", ""))
            if job:
                jobs.append({
                    "job_id": job["job_id"],
                    "entity_name": job["entity_name"],
                    "status": job["status"],
                    "started_at": job["started_at"],
                    "findings_count": len(job.get("findings", [])),
                    "summary": job.get("summary"),
                    "blob_path": job.get("blob_path"),
                })
    return jobs


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8450, workers=1)
