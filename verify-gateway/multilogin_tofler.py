"""
IN MCA21 corporate registry lookup via Tofler (which republishes MCA21
master data) using Multilogin anti-detect browser + IN residential exit.

Tofler has no CAPTCHA — just navigates to a CIN-keyed URL and parses the
rendered page. Replaces the dead BD-Unlocker `pakistan` zone path that
used to live in crawldevvm:_india_tofler_lookup.

Source attribution preserved: "Tofler.in (MCA21 data), Ministry of
Corporate Affairs (MCA21), Government of India".
"""

import logging
import re

from mlx_http import mlx_navigate

log = logging.getLogger("verify-gateway")

_TOFLER_BASE = "https://www.tofler.in"


def _slug(name: str) -> str:
    s = name.strip().lower().replace(".", "").replace(",", "")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    return s


def _build_url(entity_name: str, cin: str) -> str:
    return f"{_TOFLER_BASE}/{_slug(entity_name)}/company/{cin}"


def _parse(body: str, html: str, source_url: str) -> dict:
    out = {"found": False, "source": "Tofler.in (MCA21 data)", "source_url": source_url}
    blob = (body or "") + "\n" + (html or "")

    cin_match = re.search(r'([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})', blob)
    if cin_match:
        out["cin"] = cin_match.group(1)
        out["found"] = True

    name_match = re.search(
        r'# ([A-Z][A-Z\s\.\&\(\)]+(?:PRIVATE|PUBLIC|LLP|LIMITED|COMPANY)[A-Z\s\.\(\)]*)',
        blob,
    )
    if name_match:
        out["legal_name"] = name_match.group(1).strip()
    else:
        # H1 / title fallback
        h1 = re.search(r'<h1[^>]*>([^<]+)</h1>', html or "", re.IGNORECASE)
        if h1:
            out["legal_name"] = h1.group(1).strip()

    if re.search(r'\bActive\b', blob):
        out["status"] = "Active"
    elif re.search(r'\bStruck Off\b', blob, re.IGNORECASE):
        out["status"] = "Struck Off"
    elif re.search(r'\bDormant\b', blob, re.IGNORECASE):
        out["status"] = "Dormant"
    elif re.search(r'Under Liquidation', blob, re.IGNORECASE):
        out["status"] = "Under Liquidation"

    inc_match = re.search(r'incorporated on (\d{1,2}\s+\w+,?\s+\d{4})', blob, re.IGNORECASE)
    if not inc_match:
        inc_match = re.search(r'Date of Incorporation[:\s]+(\d{1,2}[\-/\s]\w+[\-/\s]\d{4})', blob, re.IGNORECASE)
    if inc_match:
        out["incorporation_date"] = inc_match.group(1).strip()

    for label, val in [
        ("private limited", "Private Limited Company"),
        ("public limited", "Public Limited Company"),
        ("limited liability", "Limited Liability Partnership"),
        ("one person", "One Person Company"),
    ]:
        if label in blob.lower():
            out["company_type"] = val
            break

    addr_match = re.search(r'registered address is (?:at )?(.*?\d{6})', blob, re.IGNORECASE | re.DOTALL)
    if not addr_match:
        addr_match = re.search(r'Registered Address[:\s]+(.*?\d{6})', blob, re.DOTALL)
    if addr_match:
        out["registered_address"] = re.sub(r'\s+', ' ', addr_match.group(1).strip()).rstrip(",. ")

    dir_match = re.search(
        r'(?:has\s+\w+\s+directors?\s*[-–—]\s*|directors?\s*[-–—:]\s*|directors?\s+(?:are|includes?|consists?\s+of)\s*[-–—:]?\s*)(.*?)(?:\.\s|$)',
        blob, re.IGNORECASE,
    )
    if dir_match:
        names = re.split(r',\s*(?:and\s+)?|\s+and\s+', dir_match.group(1))
        directors = [n.strip() for n in names if n.strip() and len(n.strip()) > 2 and len(n.strip()) < 80]
        if directors:
            out["directors"] = directors[:30]

    if not out.get("directors"):
        # Fallback: Tofler director list page often renders a table with names + DINs
        din_blocks = re.findall(r'([A-Z][A-Za-z\.\s]{3,60})\s*\n?\s*(?:DIN|Director)\s*[:\s]*(\d{8})', blob)
        if din_blocks:
            out["directors"] = [{"name": n.strip(), "din": d} for n, d in din_blocks[:30]]

    auth_m = re.search(r'authorized share capital is INR ([\d,\.]+\s*(?:lac|cr|lakh|crore)?)', blob, re.IGNORECASE)
    paid_m = re.search(r'paid-up capital is INR ([\d,\.]+\s*(?:lac|cr|lakh|crore)?)', blob, re.IGNORECASE)
    if auth_m:
        out["authorized_capital"] = auth_m.group(1).strip()
    if paid_m:
        out["paidup_capital"] = paid_m.group(1).strip()

    return out


def tofler_cin_verify(entity_name: str, cin: str) -> dict:
    """
    Look up Indian company on Tofler by CIN via Multilogin + IN residential exit.

    Returns dict shaped like the legacy crawldevvm:_india_tofler_lookup so the
    caller in main.py can wrap it without changes.
    """
    entity_name = (entity_name or "").strip()
    cin = (cin or "").strip().upper()
    if not cin:
        return {"found": False, "error": "cin required for IN MCA21/Tofler verification"}

    url = _build_url(entity_name or "company", cin)
    log.info("Tofler CIN lookup: %s (entity=%s)", url, entity_name)

    try:
        nav = mlx_navigate(url, wait_s=4, timeout=45, country_code="in")
    except Exception as e:
        log.warning("Tofler mlx_navigate failed for %s: %s", cin, e)
        return {"found": False, "error": f"Tofler navigate failed: {str(e)[:200]}", "source_url": url}

    body = nav.get("body") or ""
    html = nav.get("html") or ""

    if len(body) + len(html) < 400:
        return {"found": False, "error": "Empty page from Tofler", "source_url": url}

    return _parse(body, html, url)
