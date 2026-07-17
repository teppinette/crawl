"""
Lithuania verify — runs on the generic engine via T_MLX_FORM.

Source: JADIS (Registry Center JURIDICAL DATABASE) at registrucentras.lt.
The site is Cloudflare-protected and uses a classic HTML form for search.
Multilogin browser bypasses Cloudflare; the form-submit transport handles
the fill+click+results flow that direct GET can't.
"""

import logging
import re

import verify_engine as eng

log = logging.getLogger("verify-gateway")

_FORM_URL = "https://www.registrucentras.lt/jar/p/index.php"


def init(get_secret=None):
    log.info("LT verify ready (engine) — JADIS via Multilogin browser form submit")


def _build_form_fields(entity_name: str, ids: dict) -> dict:
    code = (ids.get("company_code") or "").strip()
    # JADIS form has fields: pav (name), kodas (code), and a hidden p=1 set by Submit
    return {
        "input[name='pav']":   entity_name if not code else "",
        "input[name='kodas']": code,
    }


def _parse_lt(raw: dict, entity_name: str, ids: dict) -> dict:
    html = raw.get("html") or ""
    body = raw.get("body") or ""
    src = html + "\n" + body

    if not src.strip():
        return {"found": False, "error": "empty_response"}

    # JADIS results page: results are in a table. The row contains the company
    # code (9-digit), the legal name (in quotes — Lithuanian uses „...""), and
    # status. Look for the specific results-table cells.
    #
    # JADIS-specific table-cell pattern targeting search-result rows:
    #   <td>NAME</td><td>CODE</td><td>STATUS</td>
    # NOTE: This regex is provisional — JADIS HTML is undocumented and shifts
    # without notice. Status quo (2026-06-11): some matches still pick up UI
    # widgets like "Šrifto dydis" (accessibility font-size control). Parser
    # needs targeted refinement when a stable JADIS DOM probe is available.

    # First — find ALL 9-digit codes that look like company codes
    codes = re.findall(r"\b(\d{9})\b", src)
    if not codes:
        codes = re.findall(r"\b(\d{8})\b", src)
    if not codes:
        return {"found": False, "note": "JADIS: no result code found in page"}

    # JADIS results: each row has code adjacent to name. Find a <tr>...</tr>
    # block containing one of the codes.
    code = None
    name = None
    for candidate_code in codes:
        # Match a <tr> containing this code, then extract the cell with the entity name
        tr_pat = re.compile(
            r"<tr[^>]*>(.*?" + re.escape(candidate_code) + r".*?)</tr>",
            re.DOTALL | re.IGNORECASE,
        )
        tr_match = tr_pat.search(src)
        if not tr_match:
            continue
        tr_html = tr_match.group(1)
        # Strip tags from each cell
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr_html, re.DOTALL)
        cleaned = [re.sub(r"<[^>]+>", " ", c).strip() for c in cells]
        # Find a cell containing a quoted Lithuanian/Latin name (length > 4)
        for cell in cleaned:
            cell_text = cell.strip("\"„""'" + " \t\n\r")
            if len(cell_text) > 4 and re.search(r"[A-ZĄČĘĖĮŠŲŪŽ]", cell_text) \
               and not any(h in cell_text.lower() for h in (
                   "paieška", "rezultatai", "kodas", "pavadinimas", "šrifto",
                   "registruotos", "statusas",
               )):
                name = cell_text
                code = candidate_code
                break
        if name:
            break
    if not code:
        code = codes[0]

    # Status — look for common JADIS status strings
    status = "UNKNOWN"
    for status_kw, mapped in [
        ("Registruotas", "ACTIVE"),
        ("Likviduojam", "IN_LIQUIDATION"),
        ("Bankrutuoj", "BANKRUPT"),
        ("Išregistruotas", "DISSOLVED"),
        ("Likviduotas", "DISSOLVED"),
    ]:
        if status_kw in src:
            status = mapped
            break

    return {
        "found": True,
        "legal_name": name or entity_name,
        "business_registration_number": code,
        "is_listed": False,
        # LT-specific extras
        "company_code": code,
        "status": status,
        "summary": f"{name or entity_name} — JADIS {code} — {status}",
    }


LT_CONFIG = eng.CountryConfig(
    country_code="LT",
    source_name="JADIS (Registry Center), Republic of Lithuania",
    transport=eng.T_MLX_FORM,
    primary_url=_FORM_URL,
    parser=_parse_lt,
    form_fields_builder=_build_form_fields,
    submit_selector="",
    wait_after_submit_s=4,
    timeout=75,
    how_to_reproduce_template=(
        "Visit https://www.registrucentras.lt/jar/p/ → search '{entity}'"
    ),
)


def jadis_verify(entity_name: str, company_code: str = "") -> dict:
    """LT verify entry point — backward compat with main.py routing."""
    code = re.sub(r"\D", "", company_code or "")
    if code and len(code) in (8, 9):
        return eng.run(LT_CONFIG, code, {"company_code": code})
    return eng.run(LT_CONFIG, entity_name, {})
