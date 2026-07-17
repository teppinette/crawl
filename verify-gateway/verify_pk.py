"""Pakistan SECP eServices verify — via Multilogin PK residential proxy.

Replaces the legacy Gulf-VM + Bright Data path (api/main.py:_secp_query_via_ssh).
The BD-via-Gulf-VM transport used `country-ae` exit which SECP refuses (TLS
RST / code=000); the FBR ATL pattern (Multilogin + PK residential profile)
works reliably.

What SECP eServices exposes publicly:
- ControllerServlet?request_id=SEARCH_NAME       → 8-col results table
  (idx, legal_name, status, CRO, reg_no, reg_date, form_ab_filed_upto,
   mandatory_filing). No officer or address data.
- ControllerServlet?request_id=CTC_SEARCH_COMPANY → setGridCellValue rows
  encoding name~~reg_no~Online/Offline~company_type~status~internal_ref~CRO.
  Adds company_type vs SEARCH_NAME but no extra ID-level fields.

There is no public detail / officer / address endpoint. CIPRA (the deeper
SECP repository) is auth-walled for member access. Anyone promising a
"deep SECP profile" is either reading the certified-true-copy receipts
($) or scraping CIPRA after login.

Same name-variant logic as the old Gulf-VM path:
  1. Strip "M/s." / "MESSRS." prefix (Pakistani correspondence form).
  2. Try full collapsed (AGROCHINAPAKISTAN), progressive partial collapses,
     original with spaces, suffix-stripped, and the largest content word
     ("PACKAGES" of "PACKAGES LIMITED").
  3. Each variant tried with searchOption=Beginning+With first
     (SECP default, strict) then Containing (broader). Containing
     results are word-overlap-filtered to drop noise.

Same response shape as the gateway-side path so the dispatcher doesn't
need to know which transport produced it.
"""

import logging
import re
from typing import Optional

import requests

from mlx_http import _get_country_proxy

log = logging.getLogger("verify-gateway")

_SECP_URL = "https://eservices.secp.gov.pk/eServices/ControllerServlet"
_SECP_REFERER_NS = "https://eservices.secp.gov.pk/eServices/NameSearch.jsp"
_SECP_REFERER_CTC = "https://eservices.secp.gov.pk/eServices/CTC_CompanySearch.jsp"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_CTC_TYPES = {
    "Private Limited Company", "Public Unlisted Company",
    "Public Listed Company", "Single Member Company",
    "Limited Liability Partnership", "Not For Profit Association",
    "Foreign Company", "Trade Organization",
}


def init(get_secret):
    """No-op — mlx_http handles credentials at module load."""
    log.info("verify_pk ready (Multilogin PK residential)")


def _build_variants(entity_name: str) -> list[str]:
    """Same logic as the legacy Gulf-VM path — SECP stores names
    inconsistently so we try multiple collapsed forms."""
    original = entity_name.strip().upper()
    # Strip Pakistani correspondence prefix that registered name never carries
    original = re.sub(r'^(M\s*/\s*S\.?|MESSRS\.?)\s+', '', original).strip()
    if not original:
        return []

    full_collapsed = re.sub(r'\s+', '', original)
    words = original.split()
    variants: list[str] = [full_collapsed]

    # Progressive collapses (first 2 joined, first 3 joined, ...)
    for n in range(2, len(words)):
        partial = "".join(words[:n]) + " " + " ".join(words[n:])
        partial = partial.strip()
        if partial not in variants:
            variants.append(partial)

    if original not in variants:
        variants.append(original)

    stripped = re.sub(
        r'\b(PVT|PRIVATE|LTD|LIMITED|INC|CORP|LLC|PLC|COMPANY|CO)\b\.?\s*',
        '', original,
    ).strip()
    if stripped and stripped not in variants:
        variants.append(stripped)

    content_words = [w for w in original.split() if w not in
                     ("PVT", "PRIVATE", "LTD", "LIMITED", "INC", "CORP",
                      "LLC", "PLC", "COMPANY", "CO", "THE", "AND", "OF")]
    if content_words and len(content_words[0]) >= 4:
        head = content_words[0]
        if head not in variants:
            variants.append(head)

    # Singular/plural variants — Onboarding's "POWER CHEMICALS INDUSTRIES
    # LTD." failed because the registered name is "POWER CHEMICAL
    # INDUSTRIES LIMITED" (CHEMICAL singular, INDUSTRIES kept plural).
    # English depluralization:
    #   -IES → -Y  (INDUSTRIES → INDUSTRY)
    #   -SES → -S  (BUSINESSES → BUSINESS)
    #   -S   →     (CHEMICALS → CHEMICAL)
    # We add TWO variants: aggressive (depluralize all words) and
    # conservative (depluralize only the 2nd content word, where
    # singular/plural mismatch most commonly surfaces in registered
    # names — e.g. CHEMICALS → CHEMICAL while keeping INDUSTRIES).
    def _depluralize_word(w: str) -> str:
        if len(w) < 5 or not w.endswith("S") or w.endswith(("SS", "US", "IS", "OS")):
            return w
        if w.endswith("IES"):
            return w[:-3] + "Y"
        if w.endswith("SES"):
            return w[:-2]
        return w[:-1]

    singular_all = " ".join(_depluralize_word(w) for w in original.split())
    if singular_all != original and singular_all not in variants:
        variants.append(singular_all)

    # Conservative: only depluralize the 2nd content word.
    if len(content_words) >= 2:
        first = content_words[0]
        second_dep = _depluralize_word(content_words[1])
        if second_dep != content_words[1]:
            v = f"{first} {second_dep}"
            if v not in variants:
                variants.append(v)

    return variants


def _meaningful_words(s: str) -> set:
    """Used to word-overlap-filter Containing search results.
    Also reused for the post-match name-match gate."""
    stop = {"PVT", "PRIVATE", "LTD", "LIMITED", "COMPANY", "CO",
            "THE", "AND", "OF", "PAKISTAN", "SMC", "PUBLIC",
            "GROUP", "HOLDINGS", "INTERNATIONAL", "INC", "CORP"}
    ws = re.findall(r"[A-Z]{3,}", (s or "").upper())
    return {w for w in ws if w not in stop}


def _name_match_score(query: str, returned: str) -> tuple[int, set]:
    """Returns (shared_word_count, shared_words). Used to reject results
    that share zero meaningful words with the queried name. Same pattern
    as the IN _india_tofler_lookup name-match gate."""
    q = _meaningful_words(query)
    r = _meaningful_words(returned)
    if not q or not r:
        return 0, set()
    shared = q & r
    return len(shared), shared


def _secp_proxies() -> Optional[dict]:
    proxy_info = _get_country_proxy("pk")
    if not proxy_info:
        return None
    proxy_url = (
        f"http://{proxy_info['username']}:{proxy_info['password']}"
        f"@{proxy_info['server'].replace('http://', '')}"
    )
    return {"http": proxy_url, "https": proxy_url}


def _new_secp_session() -> requests.Session:
    """Build a requests.Session with PK exit, then GET NameSearch.jsp to
    establish a JSESSIONID cookie.

    Critical detail discovered 2026-06-25: without an established session,
    SECP's ControllerServlet returns a generic Top-100-companies page
    REGARDLESS of the search query (51KB body, includes 'records were
    found according to given criteria' text — but the matches are
    unrelated entities like SHARM TRADING / CARRIER SERVICE / EXXON
    CHEMCAL repeated for every query). With a session cookie set, the
    query actually filters and returns relevant matches (e.g. PACKAGES
    CLUB / PACKAGES CONVERTORS for 'PACKAGES'). This bug was silent on
    the legacy Gulf-VM curl path because curl reused JSESSIONID across
    sequential requests within a single SSH command.
    """
    s = requests.Session()
    s.proxies = _secp_proxies() or {}
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        # Warm-up GET — sets JSESSIONID via Set-Cookie. Body is ignored.
        s.get(_SECP_REFERER_NS, timeout=20, verify=True)
    except requests.RequestException as e:
        log.warning("SECP session warm-up failed: %s", str(e)[:200])
        # Continue anyway — POST may still work in some sessions
    return s


def _secp_post(session: requests.Session, payload: dict,
               referer: str, timeout: int = 35) -> Optional[str]:
    """Form-POST to SECP via an existing PK Multilogin session."""
    try:
        r = session.post(
            _SECP_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": referer,
            },
            timeout=timeout,
            verify=True,
        )
        if r.status_code != 200:
            log.warning("SECP POST returned HTTP %s (referer=%s)",
                        r.status_code, referer.split('/')[-1])
            return None
        return r.text
    except requests.RequestException as e:
        log.warning("SECP POST failed (referer=%s): %s",
                    referer.split('/')[-1], str(e)[:200])
        return None


def _is_unfiltered_top100(html: str) -> bool:
    """Detect SECP's 'no session, no filter' Top-100 response. Marker:
    'The results display the top 100 records based on your search.'
    This is what comes back when JSESSIONID isn't established."""
    return ("display the top 100 records based on your search"
            in (html or ""))


def _parse_namesearch_rows(html: str, ctc_type: Optional[str]) -> list[dict]:
    """Parse SECP NameSearch results table. 8 cells per row:
    idx, legal_name, status, CRO, reg_no, reg_date, form_ab_filed_upto,
    mandatory_filing."""
    cells = re.findall(r'<TD class="tableText">([^<]*)', html)
    rows: list[dict] = []
    for i in range(0, len(cells), 8):
        row = cells[i:i + 8]
        if len(row) < 6:
            continue
        rows.append({
            "legal_name": row[1].strip(),
            "status": row[2].strip(),
            "cro": row[3].strip(),
            "registration_number": row[4].strip(),
            "registration_date": row[5].strip(),
            "form_ab_filed_upto": row[6].strip() if len(row) > 6 else None,
            "mandatory_filing": row[7].strip() if len(row) > 7 else None,
            "company_type": ctc_type,
        })
    return rows


def _parse_ctc_type(html: str) -> Optional[str]:
    """Parse the company_type from CTC_SEARCH onclick handler.
    Format: name~~reg_no~Online/Offline~company_type~status~internal_ref~CRO"""
    m = re.search(
        r'onclick="opener\.setGridCellValue\(&quot;([^"]+)&quot;\)', html,
    )
    if not m:
        return None
    parts = m.group(1).split("~")
    for p in parts:
        p = p.strip()
        if p in _CTC_TYPES:
            return p
    return None


def secp_namesearch(entity_name: str) -> dict:
    """SECP NameSearch via Multilogin PK. Returns:

    {
      "found": bool,
      "query": str,                  # variant + search option that hit
      "search_option": str,
      "results": [ { legal_name, status, cro, registration_number,
                     registration_date, form_ab_filed_upto,
                     mandatory_filing, company_type }, ... ],
      "tried_variants": [ { query, search_option }, ... ]
    }

    company_type only populated when CTC search also hit on the same
    variant (CTC has the type, NameSearch doesn't).
    """
    variants = _build_variants(entity_name)
    if not variants:
        return {"found": False, "results": [], "tried_variants": [],
                "error": "entity_name empty after normalization"}

    session = _new_secp_session()
    tried: list[dict] = []
    for name in variants:
        for search_option in ("Beginning With", "Containing"):
            tried.append({
                "query": name,
                "search_option": search_option,
            })

            ns_html = _secp_post(
                session,
                {"request_id": "SEARCH_NAME", "searchName": name,
                 "searchOption": search_option, "requesterProcess": ""},
                _SECP_REFERER_NS,
            )
            # ↑ requests.post(data=dict) URL-encodes spaces as "+".
            # SECP form-decodes "+" back to space — i.e. it sees
            # "Beginning With" verbatim. Sending "Beginning+With"
            # literally would be sent as %2B → SECP would see
            # "Beginning+With" with a literal plus, which it rejects
            # and falls back to the Top-100 default page.
            if not ns_html:
                continue
            # If SECP returned the generic Top-100 page (no session was
            # established), skip — these aren't filtered results. Belt-and-
            # suspenders alongside the session warm-up.
            if _is_unfiltered_top100(ns_html):
                log.warning("SECP returned Top-100 unfiltered page for '%s' "
                            "(%s) — JSESSIONID likely missing", name,
                            search_option)
                continue
            if "were found according to given criteria" not in ns_html:
                continue

            # Try CTC for company_type — same session, same variant
            ctc_type = None
            ctc_html = _secp_post(
                session,
                {"request_id": "CTC_SEARCH_COMPANY", "searchName": name,
                 "searchOption": search_option, "requesterProcess": "null"},
                _SECP_REFERER_CTC,
            )
            # same "+" vs " " caveat as the NameSearch POST above
            if ctc_html and not _is_unfiltered_top100(ctc_html):
                ctc_type = _parse_ctc_type(ctc_html)

            results = _parse_namesearch_rows(ns_html, ctc_type)
            if not results:
                continue

            # Containing → drop irrelevant hits via word-overlap with input
            if search_option == "Containing":
                q_words = _meaningful_words(entity_name)
                results = [r for r in results
                           if _meaningful_words(r["legal_name"]) & q_words]
                if not results:
                    continue

            return {
                "found": True,
                "query": name,
                "search_option": search_option.replace("+", " "),
                "results": results,
                "tried_variants": tried,
            }

    return {
        "found": False,
        "query": entity_name,
        "results": [],
        "tried_variants": tried,
    }


def secp_verify(entity_name: str) -> dict:
    """Top-level callable invoked by main.py PK route. Returns the
    final verify response shape (verified / legal_name / fields /
    validation_source / summary / tried_variants)."""
    if not entity_name:
        return {"verified": False, "found": False,
                "error": "entity_name required"}

    sr = secp_namesearch(entity_name)
    results = sr.get("results") or []
    found = bool(sr.get("found") and results)

    if not found:
        return {
            "verified": False,
            "found": False,
            "entity_name": entity_name,
            "country_code": "PK",
            "tried_variants": sr.get("tried_variants"),
            "note": (
                "SECP NameSearch did not return a match. Note SECP only "
                "indexes registered Pvt/Public Ltd / SMC / LLP / Foreign "
                "/ Trade Org / Not-For-Profit entities. Pakistani sole "
                "proprietorships and partnerships are NOT in SECP — they "
                "appear in FBR ATL by NTN instead. Pass an NTN for FBR "
                "lookup."
            ),
            "source": "SECP eServices NameSearch",
            "validation_source": {
                "registry": ("Securities and Exchange Commission of "
                             "Pakistan (SECP) — eServices NameSearch"),
                "url": _SECP_REFERER_NS,
                "transport": "Multilogin (PK sticky residential)",
                "how_to_reproduce": (
                    f"https://eservices.secp.gov.pk/eServices/NameSearch.jsp "
                    f"→ search '{entity_name}' Beginning With"
                ),
            },
        }

    # Name-match gate: a Beginning-With search of the content-word head
    # (e.g. "A.J." for "A.J. SYNTHETIC FOOTWARE INDUSTRIES (PVT.) LTD.")
    # can return an unrelated company that also starts with that prefix
    # ("A.J. DEVELOPERS (PRIVATE) LIMITED"). Reject the result when the
    # queried name and the top match share NO meaningful words. Conservative
    # — we surface the rejection in `rejected_candidate` so the caller can
    # audit the close-but-wrong record.
    top = results[0]
    score, shared = _name_match_score(entity_name, top["legal_name"])
    if score == 0:
        # All matches missed the name-overlap test → treat as not found
        return {
            "verified": False,
            "found": False,
            "entity_name": entity_name,
            "country_code": "PK",
            "tried_variants": sr.get("tried_variants"),
            "rejected_candidate": {
                "legal_name": top["legal_name"],
                "registration_number": top["registration_number"],
                "registration_date": top["registration_date"],
                "cro": top["cro"],
                "company_type": top.get("company_type"),
            },
            "note": (
                f"SECP returned '{top['legal_name']}' as the closest match "
                f"for '{entity_name}' but the names share no meaningful "
                f"words. Likely a false positive from a Beginning-With "
                f"search of a short prefix. Re-confirm the exact registered "
                f"name or pass an NTN for FBR lookup."
            ),
            "source": "SECP eServices NameSearch — rejected by name-match gate",
            "validation_source": {
                "registry": ("Securities and Exchange Commission of "
                             "Pakistan (SECP) — eServices NameSearch"),
                "url": _SECP_REFERER_NS,
                "transport": "Multilogin (PK sticky residential)",
            },
        }

    others = results[1:]
    summary = (
        f"{top['legal_name']} — Reg# {top['registration_number']} — "
        f"{top['status']} — {top.get('company_type') or 'company_type unknown'} "
        f"— Inc: {top['registration_date']}"
    )

    return {
        "verified": True,
        "found": True,
        "entity_name": entity_name,
        "country_code": "PK",
        "legal_name": top["legal_name"],
        "registration_number": top["registration_number"],
        "registration_date": top["registration_date"],
        "incorporation_date": top["registration_date"],   # generic alias
        "status": top["status"],
        "company_type": top.get("company_type"),
        "cro": top["cro"],
        "form_ab_filed_upto": top.get("form_ab_filed_upto"),
        "mandatory_filing": top.get("mandatory_filing"),
        "name_match": {
            "shared_meaningful_words": sorted(shared),
            "shared_count": score,
            # "high" if every queried word matches; "partial" otherwise.
            # GC/Onboarding can choose to require "high" for hard fills.
            "confidence": "high" if score >= len(
                _meaningful_words(entity_name)) else "partial",
        },
        "all_matches": results,
        "other_matches": others[:5] or None,
        "total_matches": len(results),
        "query": sr.get("query"),
        "search_option": sr.get("search_option"),
        "tried_variants": sr.get("tried_variants"),
        "source": "SECP eServices NameSearch + CTC_SEARCH_COMPANY",
        "summary": summary,
        "validation_source": {
            "registry": ("Securities and Exchange Commission of Pakistan "
                         "(SECP) — eServices NameSearch + CTC search"),
            "url": _SECP_REFERER_NS,
            "transport": "Multilogin (PK sticky residential)",
            "record_id": top["registration_number"],
            "how_to_reproduce": (
                f"https://eservices.secp.gov.pk/eServices/NameSearch.jsp "
                f"→ search '{sr.get('query')}' "
                f"{sr.get('search_option', 'Beginning With')}"
            ),
        },
    }
