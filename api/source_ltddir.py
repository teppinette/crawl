"""
Source: ltddir.com — HK Companies Directory (ICRIS mirror, not Cloudflare-locked).

Fills the address + filing-currency gap that OpenCorporates HK leaves open.
Direct HTTP (no Multilogin needed — site doesn't anti-bot for plain GETs).

Returns extras the engine wrapper passes through unchanged. NOT a primary
source; used as enrichment after verify_hk.py confirms entity exists.
"""

import logging
import re

import requests

log = logging.getLogger("verify-gateway")

_BASE = "https://www.ltddir.com/companies"


def init(get_secret=None):
    log.info("source_ltddir ready (HK Companies Directory enrichment, direct HTTP)")


def _slugify(name: str) -> str:
    """Convert 'INTEX DEVELOPMENT COMPANY LIMITED' to 'intex-development-company-limited'."""
    s = re.sub(r"[^\w\s-]", "", (name or "").lower())
    s = re.sub(r"[\s]+", "-", s.strip())
    return s


def ltddir_enrich(entity_name: str, cr_number: str = "") -> dict:
    """
    Fetch HK Companies Directory page for an entity. Returns dict of extras
    or empty dict on failure.

    Tries the slug-based URL first; ltddir's primary URL pattern is
    /companies/<slug>/ where slug is the legal name lowercased + hyphenated.
    """
    if not entity_name:
        return {}
    slug = _slugify(entity_name)
    url = f"{_BASE}/{slug}/"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200 or len(r.text) < 800:
            return {}
        html = r.text
    except Exception as e:
        log.debug("ltddir fetch failed for %s: %s", entity_name, e)
        return {}

    # Strip tags for text extraction
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # The page lays out fields in fixed order — use the NEXT label as the stop
    # boundary so each value is captured cleanly without bleeding into the next.
    _LABELS_IN_ORDER = [
        ("Company Name:", "Chinese Company Name:"),
        ("Chinese Company Name:", "CR No."),
        ("CR No.", "Business Registration No."),
        ("Business Registration No.", "Date of Incorporation:"),
        ("Date of Incorporation:", "Company Type:"),
        ("Company Type:", "Company Status:"),
        ("Company Status:", "Date of Annual Examination:"),
        ("Date of Annual Examination:", "Last Annual Return Filed (NAR1):"),
        ("Last Annual Return Filed (NAR1):", "Register of Charges:"),
        ("Register of Charges:", "Name History:"),
    ]

    def _between(start: str, end: str) -> str | None:
        m = re.search(re.escape(start) + r"\s*(.+?)\s*" + re.escape(end), text)
        return m.group(1).strip() if m else None

    # CR No. has a parenthesised form in the page header that confuses the
    # generic between-labels approach. Pin it directly: 7-8 digit number after
    # "CR No.".
    cr_match = re.search(r"CR No\.\s+(\d{7,8})\b", text)

    extracted = {
        "ltddir_company_name": _between(*_LABELS_IN_ORDER[0]),
        "ltddir_chinese_name": _between(*_LABELS_IN_ORDER[1]),
        "ltddir_cr_no": cr_match.group(1) if cr_match else None,
        "ltddir_br_no": _between(*_LABELS_IN_ORDER[3]),
        "ltddir_incorporation_date": _between(*_LABELS_IN_ORDER[4]),
        "ltddir_company_type": _between(*_LABELS_IN_ORDER[5]),
        "ltddir_company_status": _between(*_LABELS_IN_ORDER[6]),
        "ltddir_annual_exam_window": _between(*_LABELS_IN_ORDER[7]),
        "ltddir_last_nar1_filed": _between(*_LABELS_IN_ORDER[8]),
        "ltddir_register_of_charges": _between(*_LABELS_IN_ORDER[9]),
    }

    # Registered office address — multiline-ish, special pattern
    addr_match = re.search(
        r"Registered Office Address[:\s]+([^|.]{10,300}?)(?:\s+updated on \d{4}-\d{2}-\d{2}|\s+Website)",
        text,
    )
    if addr_match:
        extracted["ltddir_registered_office"] = addr_match.group(1).strip()

    # Updated-on timestamp
    upd_match = re.search(r"updated on (\d{4}-\d{2}-\d{2})", text)
    if upd_match:
        extracted["ltddir_updated_on"] = upd_match.group(1)

    # Name history — pattern "DATE NAME (Chinese) DATE NAME ..."
    name_hist = re.search(
        r"Name History[:\s]+(.*?)(?:Registered Office Address|Website|Popular Companies)",
        text,
    )
    if name_hist:
        # Parse "DD-MMM-YYYY NAME 中文 DD-MMM-YYYY NAME..." entries
        entries = re.findall(
            r"(\d{1,2}-[A-Z]{3}-\d{4})\s+([A-Z][^\d]{2,120}?)(?=\s+\d{1,2}-[A-Z]{3}-\d{4}|\s*$)",
            name_hist.group(1)
        )
        if entries:
            extracted["ltddir_name_history"] = [
                {"date": d, "name": n.strip()} for d, n in entries
            ]

    # Strip empty values
    return {k: v for k, v in extracted.items() if v}
