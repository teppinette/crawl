"""
Source: domain_trust — registry-of-record (RDAP) + website check for a
company's own domain, with a name-match verdict.

Reusable Tier-1 verification capability (NOT an LLM agent): given a company
name plus any of {domain, website, emails}, it derives the corporate
domain(s), queries the public domain registry over RDAP (the ICANN-mandated
WHOIS replacement — HTTP/JSON, works without a key), probes the website, and
returns a per-domain verdict:

  VERIFIED   — a real *registered* domain whose second-level label matches the
               company name. Age / status / registrar are supporting signals;
               an unreachable website is a SOFT flag only (many legitimate
               sites are geo-blocked / behind the GFW).
  REVIEW     — registered but the name doesn't clearly match, or a risk flag
               (registry hold, <30 days old).
  UNVERIFIED — no registration record found (domain may not exist).

Consumed by onboarding (Verification tab), CIR/deepdive, score-gateway
entity_risk, and copapllm as a tool. Registrant is GDPR-redacted for gTLDs,
so we rely on registrar / events / status, not the registrant contact.

Free public APIs (rdap.org bootstrap + direct HTTP) — direct requests, like
the GLEIF / OpenSanctions / OFSI source adapters.
"""

import logging
import re
import socket
from datetime import date, datetime

import requests

log = logging.getLogger("crawl-gateway")

_UA = "Crawl-Research-Gateway/3.0 (+domain-trust)"
_RDAP = "https://rdap.org/domain/{domain}"
_RDAP_TIMEOUT = 15
_WEB_TIMEOUT = 10
_TITLE_RE = re.compile(r"<title[^>]*>([^<]{1,500})</title>", re.IGNORECASE | re.DOTALL)

# Legal-form / generic words to ignore when matching a name to a domain label.
_STOPWORDS = {
    "inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation",
    "company", "co", "plc", "sa", "ag", "gmbh", "bv", "nv", "spa", "srl",
    "pty", "pte", "holdings", "group", "international", "global", "worldwide",
    "the", "and", "of", "for",
}

# Public / free webmail — an email here says nothing about a company domain.
_FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me", "gmx.com",
    "gmx.net", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
    "qq.com", "163.com", "126.com", "sina.com", "sina.cn", "sohu.com",
    "foxmail.com", "yeah.net", "139.com", "aliyun.com", "21cn.com",
    "naver.com", "hanmail.net", "daum.net", "nate.com",
    "rediffmail.com", "hotmail.co.uk",
})


def extract_domain(website):
    if not website:
        return None
    raw = website.strip().lower()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        from urllib.parse import urlparse
        host = urlparse(raw).netloc or ""
    except Exception:
        return None
    host = host.split("@")[-1].split(":")[0].strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def domain_from_email(email):
    if not email or "@" not in email:
        return None
    return (email.rsplit("@", 1)[1] or "").strip().lower().strip(".") or None


def is_freemail(domain):
    return (domain or "").lower() in _FREEMAIL


def _name_tokens(legal_name):
    if not legal_name:
        return []
    toks = re.findall(r"[A-Za-z][A-Za-z0-9'&]{2,}", legal_name.lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) >= 3]


def name_matches_domain(legal_name, domain):
    if not legal_name or not domain:
        return False, []
    core = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    if not core:
        return False, []
    hits = [t for t in _name_tokens(legal_name) if len(t) >= 3 and t in core]
    if not hits and len(core) >= 4 and core in re.sub(r"[^a-z0-9]", "", legal_name.lower()):
        hits = [core]
    return bool(hits), hits


def _parse_date(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(iso_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


def fetch_rdap(domain):
    out = {"registrar": None, "created": None, "expires": None, "status": [],
           "nameservers": [], "raw_available": False}
    try:
        r = requests.get(_RDAP.format(domain=domain),
                         headers={"Accept": "application/rdap+json", "User-Agent": _UA},
                         timeout=_RDAP_TIMEOUT)
    except requests.RequestException as e:
        log.info("RDAP fetch failed for %s: %s", domain, e)
        return out
    if r.status_code >= 400:
        return out
    try:
        data = r.json()
    except ValueError:
        return out
    out["raw_available"] = True
    for ev in data.get("events") or []:
        action = (ev.get("eventAction") or "").lower()
        if action == "registration":
            out["created"] = ev.get("eventDate")
        elif action == "expiration":
            out["expires"] = ev.get("eventDate")
    out["status"] = data.get("status") or []
    for ns in data.get("nameservers") or []:
        n = (ns.get("ldhName") or "").lower().strip(".")
        if n:
            out["nameservers"].append(n)
    for ent in data.get("entities") or []:
        if "registrar" not in [x.lower() for x in (ent.get("roles") or [])]:
            continue
        vcard = ent.get("vcardArray") or []
        if len(vcard) == 2 and isinstance(vcard[1], list):
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and (item[0] or "").lower() == "fn":
                    out["registrar"] = item[3]
    return out


def fetch_website_meta(domain):
    out = {"alive": False, "status_code": None, "final_url": None, "title": None}
    try:
        r = requests.get("http://" + domain,
                         headers={"User-Agent": _UA, "Accept": "text/html,*/*"},
                         timeout=_WEB_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return out
    out["status_code"] = r.status_code
    out["final_url"] = r.url
    out["alive"] = 200 <= r.status_code < 400
    m = _TITLE_RE.search(r.text[:200_000] if r.text else "")
    if m:
        out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:500]
    return out


def _dns_resolve(domain):
    """Return the A-record IP if the domain resolves, else None. Free proof
    that a domain EXISTS even when its ccTLD registry isn't in RDAP (e.g. .tw)."""
    try:
        return socket.gethostbyname(domain)
    except Exception:
        return None


def _whoisxml(domain):
    """Full WHOIS via WhoisXML API — covers every TLD including ccTLDs that
    RDAP (rdap.org) does not (.tw, .cn ccTLD variants, many others). Returns a
    dict shaped like fetch_rdap() + registrant, or None if unconfigured/failed.
    Key: crawl-kv secret 'whoisxml-api-key' (paid; free tier ~500/mo)."""
    try:
        from keyvault import get_secret
        key = get_secret("whoisxml-api-key")
    except Exception:
        key = None
    if not key:
        return None
    try:
        r = requests.get(
            "https://www.whoisxmlapi.com/whoisserver/WhoisService",
            params={"apiKey": key, "domainName": domain, "outputFormat": "JSON"},
            headers={"User-Agent": _UA}, timeout=_RDAP_TIMEOUT,
        )
        if r.status_code >= 400:
            log.info("whoisxml %s for %s", r.status_code, domain)
            return None
        rec = (r.json() or {}).get("WhoisRecord") or {}
    except Exception as e:
        log.info("whoisxml failed for %s: %s", domain, e)
        return None
    reg = rec.get("registryData") or rec
    registrant = (rec.get("registrant") or {}).get("organization") \
        or (reg.get("registrant") or {}).get("organization")
    status = reg.get("status") or rec.get("status") or ""
    if isinstance(status, str):
        status = [s for s in re.split(r"[,\s]+", status) if s][:8]
    return {
        "registrar": rec.get("registrarName") or reg.get("registrarName"),
        "created": rec.get("createdDate") or reg.get("createdDate")
                   or rec.get("creationDate"),
        "expires": rec.get("expiresDate") or reg.get("expiresDate"),
        "status": status or [],
        "registrant": registrant,
        "nameservers": [],
        "raw_available": True,
        "via": "whoisxml",
    }


def fetch_registry(domain):
    """Registry-of-record for a domain: RDAP first (free), WhoisXML fallback
    (paid, all TLDs) when RDAP returns no registration date. Tags the source
    it used in 'via'."""
    rdap = fetch_rdap(domain)
    rdap["via"] = "rdap"
    if rdap.get("created"):
        return rdap
    wx = _whoisxml(domain)
    if wx and wx.get("created"):
        return wx
    # neither had a registration date — keep whatever RDAP gave (usually empty)
    return wx or rdap


def _domain_verdict(legal_name, domain, rdap, web, resolved_ip=None):
    today = date.today()
    created = _parse_date(rdap.get("created"))
    age_days = (today - created).days if created else None
    statuses = [s.lower() for s in (rdap.get("status") or [])]
    hold = any(k in s for s in statuses for k in ("hold", "redemption", "pendingdelete"))
    very_new = age_days is not None and age_days < 30
    matched, hits = name_matches_domain(legal_name, domain)

    base = {
        "domain": domain, "registered": created.isoformat() if created else None,
        "age_days": age_days, "expires": rdap.get("expires"),
        "registrar": rdap.get("registrar"), "registrant": rdap.get("registrant"),
        "status": rdap.get("status"), "site_alive": web.get("alive"),
        "site_title": web.get("title"), "name_match": matched, "name_hits": hits,
        "registry_source": rdap.get("via"),
    }
    if not created:
        # No registry record. If the domain RESOLVES it still EXISTS — the
        # normal case for a ccTLD not in RDAP (.tw) when no paid WHOIS key is
        # configured. DNS resolution is proof of existence.
        if resolved_ip:
            reasons = ["Registry record unavailable (ccTLD not in RDAP / no WHOIS "
                       "key) but the domain RESOLVES via DNS to %s" % resolved_ip]
            if matched:
                reasons.insert(0, "Domain name matches the company (%s)" % ", ".join(hits))
                base["verdict"] = "VERIFIED"
            else:
                reasons.append("Domain name does not clearly match the company name")
                base["verdict"] = "REVIEW"
            base["reasons"] = reasons
            return base
        base["verdict"] = "UNVERIFIED"
        base["reasons"] = ["No registration record and the domain did not resolve "
                           "(may not exist)"]
        return base

    reasons = ["Domain name matches the company (%s)" % ", ".join(hits)
               if matched else "Domain name does not clearly match the company name"]
    if rdap.get("via") == "whoisxml":
        reasons.append("Registry data via WhoisXML (full ccTLD coverage)")
    if hold:
        reasons.append("Flag: registry hold/dispute")
    if very_new:
        reasons.append("Flag: registered <30 days ago")
    if not web.get("alive"):
        reasons.append("Soft flag: website did not resolve (does not block verification)")
    base["verdict"] = "VERIFIED" if (matched and not hold and not very_new) else "REVIEW"
    base["reasons"] = reasons
    return base


def _geolocate_ip(ip):
    """Free IP geolocation (ip-api.com, no key). Returns
    {country, country_code, region, city, isp, org, lat, lon} or None.
    Feeds the domain-location comprehension agent: does where the domain is
    hosted line up with where the entity says it operates?"""
    if not ip:
        return None
    try:
        r = requests.get(
            "http://ip-api.com/json/%s" % ip,
            params={"fields": "status,country,countryCode,regionName,city,isp,org,lat,lon"},
            headers={"User-Agent": _UA}, timeout=8,
        )
        j = r.json()
    except Exception as e:
        log.info("geolocate failed for %s: %s", ip, e)
        return None
    if (j.get("status") or "").lower() != "success":
        return None
    return {
        "country": j.get("country"), "country_code": j.get("countryCode"),
        "region": j.get("regionName"), "city": j.get("city"),
        "isp": j.get("isp"), "org": j.get("org"),
        "lat": j.get("lat"), "lon": j.get("lon"),
    }


def _rank(v):
    return {"VERIFIED": 0, "REVIEW": 1, "UNVERIFIED": 2}.get(v, 3)


def check(entity_name, domain=None, website=None, emails=None):
    """Derive corporate domains from {domain, website, emails}, RDAP + website
    check each, return per-domain verdicts + an overall verdict. Never raises."""
    domains = {}   # domain -> {'sources': set, 'emails': set}
    freemail = []

    def _add(dom, source, email=None):
        if not dom or is_freemail(dom):
            if email:
                freemail.append(email)
            return
        rec = domains.setdefault(dom, {"sources": set(), "emails": set()})
        rec["sources"].add(source)
        if email:
            rec["emails"].add(email)

    if domain:
        _add(domain.strip().lower().strip("."), "explicit")
    wd = extract_domain(website) if website else None
    if wd:
        _add(wd, "website")
    for em in emails or []:
        _add(domain_from_email(em), "email", email=em)

    results = []
    for dom, meta in domains.items():
        try:
            registry = fetch_registry(dom)          # RDAP → WhoisXML fallback
            web = fetch_website_meta(dom)
            ip = _dns_resolve(dom)                   # proof of existence + IP
            geo = _geolocate_ip(ip) if ip else None  # where it's hosted
            v = _domain_verdict(entity_name or "", dom, registry, web, resolved_ip=ip)
            v["resolved_ip"] = ip
            v["ip_geolocation"] = geo
        except Exception as e:
            log.warning("domain_trust check failed for %s: %s", dom, e)
            v = {"domain": dom, "verdict": "UNVERIFIED", "reasons": [str(e)[:160]],
                 "resolved_ip": None, "ip_geolocation": None}
        v["sources"] = sorted(meta["sources"])
        v["emails"] = sorted(meta["emails"])
        results.append(v)

    results.sort(key=lambda v: _rank(v.get("verdict")))
    if not results:
        overall = "NO_DOMAIN"
    else:
        overall = results[0]["verdict"]

    email_domains = {d for d, m in domains.items() if "email" in m["sources"]}
    website_matches_email = (wd in email_domains) if (wd and email_domains) else None

    return {
        "source_id": "domain_trust",
        "source_url": "https://rdap.org",
        "found": bool(results and any(r.get("registered") or r.get("resolved_ip")
                                      for r in results)),
        "verdict": overall,
        "domains": results,
        "freemail_emails": sorted(set(freemail)),
        "website_matches_email": website_matches_email,
    }
