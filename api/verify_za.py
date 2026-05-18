"""
South Africa company verification via GLEIF LEI API (primary) + CIPC eServices HTML (secondary).

Primary source: GLEIF Global LEI Foundation (https://api.gleif.org/)
  - Free public REST API, no auth required
  - Covers listed SA companies (JSE, banks, subsidiaries of multinationals)
  - Returns: LEI, legal name, status, address, jurisdiction, legal form
  - Reproduce: https://search.gleif.org/#/record/<LEI>
  - Limitation: only covers companies with a Legal Entity Identifier (~200k ZA entities)

Secondary source: CIPC eServices enterprise-number search (https://eservices.cipc.co.za/Search.aspx)
  - Covers all CIPC-registered entities including SMEs (free, no auth for basic search)
  - Used only when CRN is provided and GLEIF returns no match
  - Scrapes via Multilogin browser (JavaScript-rendered tab panels)
  - Returns: enterprise number, name, type, status, registered/postal address

NOTE: BizPortal (/api/company/search) was decommissioned — the endpoint now
returns 301→404. BizPortal name search now requires login (POPIA compliance,
effective 2026). CIPC eServices name search redirects to BizPortal. Only the
enterprise-number lookup tab on Search.aspx still works without auth.

Company Registration Number (CRN) formats:
  - Private companies (Pty Ltd): YYYY/NNNNNN/07
  - Close corporations (CC):     YYYY/NNNNNN/23
  - Public companies:            YYYY/NNNNNN/06
  - Incorporated companies:      YYYY/NNNNNN/21
  - Non-profit companies:        YYYY/NNNNNN/08
  - External companies:          YYYY/NNNNNN/10

Input: entity_name (search by name) or crn (exact CRN lookup)
Returns: entity_name, country_code, found, lei, crn, legal_name, status, entity_type,
         registered_address, validation_source
"""

import logging
import re
import time

import requests

from mlx_http import mlx_navigate

log = logging.getLogger("verify-gateway")

# GLEIF public REST API — no auth required
_GLEIF_SEARCH_URL = "https://api.gleif.org/api/v1/lei-records"
_GLEIF_RECORD_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"

# CIPC eServices — enterprise number lookup tab (no login, JavaScript-rendered)
_CIPC_SEARCH_URL = "https://eservices.cipc.co.za/Search.aspx"

# CRN pattern: YYYY/NNNNNN/NN
_CRN_RE = re.compile(r"^\d{4}/\d{6}/\d{2}$")

# Entity type codes (last 2 digits of CRN)
_ENTITY_TYPE_MAP = {
    "06": "Public Company",
    "07": "Private Company (Pty Ltd)",
    "08": "Non-Profit Company",
    "09": "Personal Liability Company",
    "10": "External Company",
    "21": "Incorporated Company",
    "23": "Close Corporation (CC)",
}

# GLEIF entity status mapping
_GLEIF_STATUS_MAP = {
    "ACTIVE": "ACTIVE",
    "INACTIVE": "DEREGISTERED",
    "PENDING_TRANSFER": "PENDING",
    "PENDING_ARCHIVAL": "PENDING",
    "LAPSED": "LAPSED",
}

# GLEIF legal form codes for ZA entity types
_GLEIF_FORM_MAP = {
    "8888": "Unknown",
    "XE4Z": "Public Company",
    "CYSX": "Private Company (Pty Ltd)",
    "BU3S": "Close Corporation (CC)",
    "PWHD": "Non-Profit Company",
    "3B1C": "External Company",
}


def init(get_secret):
    log.info("ZA adapter ready — GLEIF LEI API (primary) + CIPC eServices (secondary)")


def cipc_verify(entity_name: str, crn: str = "") -> dict:
    """
    Verify a South African company.

    Strategy:
    1. GLEIF name search (free API, no auth) — works for all entity_name queries
    2. If CRN provided and GLEIF has no match: CIPC eServices enterprise-number
       lookup (mlx_navigate — JavaScript-rendered, scrapes the tab panel)

    GLEIF covers ~200k ZA entities (all listed companies, banks, insurers,
    large corporates). For SMEs not in GLEIF, CRN-based CIPC lookup is used.
    """
    if not entity_name and not crn:
        return {"found": False, "error": "entity_name or crn required"}

    try:
        # Step 1: GLEIF name search
        records = _gleif_search(entity_name) if entity_name else []

        if records:
            best, total = _rank_gleif(records, entity_name, crn)
            return _format_gleif(best, entity_name, crn, total, records[:5])

        # Step 2: CRN → CIPC eServices enterprise-number lookup (browser-based)
        if crn and _CRN_RE.match(crn.strip()):
            cipc_rec = _cipc_crn_lookup(crn.strip())
            if cipc_rec:
                return _format_cipc(cipc_rec, entity_name, crn)

        # Nothing found
        return {
            "entity_name": entity_name,
            "country_code": "ZA",
            "crn": crn or None,
            "found": False,
            "status": "NOT_FOUND",
            "source": "GLEIF LEI API; CIPC eServices, South Africa",
            "validation_source": _validation_source(entity_name or crn, crn=crn),
        }

    except Exception as e:
        log.error("ZA verify error for %s: %s", entity_name or crn, e)
        return {
            "entity_name": entity_name,
            "country_code": "ZA",
            "found": False,
            "error": str(e)[:300],
        }


# ---------------------------------------------------------------------------
# GLEIF search
# ---------------------------------------------------------------------------

def _gleif_search(name: str) -> list:
    """Search GLEIF by legal name, filtered to ZA jurisdiction."""
    try:
        params = {
            "filter[entity.legalName]": name,
            "filter[entity.legalAddress.country]": "ZA",
            "page[size]": 10,
        }
        resp = requests.get(
            _GLEIF_SEARCH_URL,
            params=params,
            headers={"Accept": "application/vnd.api+json"},
            timeout=20,
        )
        if not resp.ok:
            log.debug("GLEIF search returned %d for '%s'", resp.status_code, name[:30])
            return []
        data = resp.json()
        return data.get("data", [])

    except Exception as e:
        log.debug("GLEIF search failed for '%s': %s", name[:30], str(e)[:100])
        return []


def _rank_gleif(records: list, entity_name: str, crn: str) -> tuple[dict, int]:
    """Rank GLEIF records — exact name match first, then partial."""
    name_upper = entity_name.strip().upper() if entity_name else ""

    def score(r: dict) -> int:
        ent = r.get("attributes", {}).get("entity", {})
        name = (ent.get("legalName") or {}).get("name", "").strip().upper()
        other = [(n.get("name") or "").strip().upper()
                 for n in ent.get("otherNames", [])]
        all_names = [name] + other

        if name_upper and name == name_upper:
            return 0
        if name_upper and any(n == name_upper for n in all_names):
            return 1
        if name_upper and any(name_upper in n for n in all_names):
            return 2
        if name_upper and any(n.startswith(name_upper[:6]) for n in all_names if n):
            return 3
        return 4

    ranked = sorted(records, key=score)
    return ranked[0], len(records)


def _format_gleif(record: dict, query_name: str, crn_input: str,
                  total_matches: int, top_matches: list) -> dict:
    """Format GLEIF record into standard verification response."""
    lei = record.get("id", "")
    attr = record.get("attributes", {})
    ent = attr.get("entity", {})
    reg = attr.get("registration", {})

    legal_name = (ent.get("legalName") or {}).get("name", "") or query_name
    status_raw = ent.get("status", "")
    status = _GLEIF_STATUS_MAP.get(status_raw.upper(), status_raw.upper() if status_raw else "UNKNOWN")
    jurisdiction = ent.get("jurisdiction", "ZA")

    legal_form_code = (ent.get("legalForm") or {}).get("id", "")
    entity_type = _GLEIF_FORM_MAP.get(legal_form_code, legal_form_code or None)

    # Address
    addr_raw = ent.get("legalAddress") or ent.get("headquartersAddress") or {}
    addr_parts = [
        *[l for l in addr_raw.get("addressLines", []) if l],
        addr_raw.get("city", ""),
        addr_raw.get("region", ""),
        addr_raw.get("postalCode", ""),
        addr_raw.get("country", ""),
    ]
    registered_address = ", ".join(p for p in addr_parts if p) or None

    # Other matches summary
    other_matches = []
    for m in top_matches[1:]:
        m_ent = m.get("attributes", {}).get("entity", {})
        other_matches.append({
            "name": (m_ent.get("legalName") or {}).get("name"),
            "lei": m.get("id"),
            "status": m_ent.get("status"),
            "jurisdiction": m_ent.get("jurisdiction"),
        })

    return {
        "entity_name": legal_name,
        "country_code": "ZA",
        "found": True,
        "lei": lei or None,
        "crn": crn_input or None,
        "legal_name": legal_name or None,
        "status": status,
        "entity_type": entity_type,
        "registered_address": registered_address,
        "registration_date": None,   # GLEIF does not expose CIPC reg date
        "total_matches": total_matches,
        "other_matches": other_matches if other_matches else None,
        "directors_note": (
            "Directors and shareholders require a paid CIPC eServices account. "
            "See https://eservices.cipc.co.za/ for full company profile."
        ),
        "source": "GLEIF Global LEI Foundation, South Africa",
        "validation_source": _validation_source(
            query_name or crn_input, crn=crn_input, lei=lei, legal_name=legal_name
        ),
    }


# ---------------------------------------------------------------------------
# CIPC eServices enterprise-number lookup (browser-based, JS-rendered)
# ---------------------------------------------------------------------------

def _cipc_crn_lookup(crn: str) -> dict | None:
    """
    Scrape CIPC eServices Search.aspx enterprise-number tab for a known CRN.

    The page loads the result via ASP.NET AJAX (AjaxControlToolkit tabs).
    We use mlx_navigate to render the page, fill the input, click Search,
    then wait for the tab panel to populate.

    Returns a dict with keys: enterpriseNumber, enterpriseName, enterpriseType,
    enterpriseStatus, registeredAddress, postalAddress — or None on failure.
    """
    try:
        # Navigate and interact with the page via Playwright CDP
        result = mlx_navigate(
            _CIPC_SEARCH_URL,
            wait_s=4,
            timeout=90,
            country_code="za",
            js=f"""
                (async () => {{
                    // Fill enterprise number field
                    const inp = document.querySelector('input[id*="txtEntNo"]');
                    if (!inp) return 'INPUT_NOT_FOUND';
                    inp.value = '{crn}';

                    // Click the search button
                    const btn = document.querySelector('input[id*="btnEntNoSearch"]');
                    if (!btn) return 'BUTTON_NOT_FOUND';
                    btn.click();

                    // Wait up to 8s for the result panel to populate
                    for (let i = 0; i < 40; i++) {{
                        await new Promise(r => setTimeout(r, 200));
                        const nameEl = document.querySelector('[id*="lblEntName"]');
                        if (nameEl && nameEl.innerText && nameEl.innerText.trim()) {{
                            return 'OK';
                        }}
                    }}
                    return 'TIMEOUT';
                }})()
            """,
        )

        html = result.get("html", "")
        if not html:
            log.debug("CIPC CRN lookup: empty HTML for %s", crn)
            return None

        return _parse_cipc_html(html, crn)

    except Exception as e:
        log.debug("CIPC CRN lookup failed for %s: %s", crn, str(e)[:150])
        return None


def _parse_cipc_html(html: str, crn: str) -> dict | None:
    """Parse CIPC eServices Search.aspx result — enterprise details tab."""
    # Look for CRN in the rendered page
    crn_match = re.search(r"\b" + re.escape(crn) + r"\b", html)

    # Try label-based extraction (ASP.NET renders data into labelled spans)
    def extract_label(pattern: str) -> str:
        m = re.search(
            pattern + r"[^>]*>([^<]{1,200})<",
            html, re.IGNORECASE | re.DOTALL,
        )
        return m.group(1).strip() if m else ""

    ent_name = extract_label(r'id="[^"]*lblEntName[^"]*"')
    ent_type = extract_label(r'id="[^"]*lblEntType[^"]*"')
    ent_status = extract_label(r'id="[^"]*lblEntStatus[^"]*"')
    reg_addr = extract_label(r'id="[^"]*lblRegAddr[^"]*"')
    postal_addr = extract_label(r'id="[^"]*lblPostalAddr[^"]*"')
    ent_no = extract_label(r'id="[^"]*lblEntNo[^"]*"') or crn

    # Also try table-row extraction as fallback
    if not ent_name:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
        for row in rows:
            if crn in row:
                cells = re.findall(r"<td[^>]*>([^<]{1,150})</td>", row, re.IGNORECASE)
                cells = [c.strip() for c in cells if c.strip()]
                if len(cells) >= 2:
                    ent_name = ent_name or cells[1] if len(cells) > 1 else ""
                    break

    if not ent_name and not crn_match:
        log.debug("CIPC CRN lookup: no data found for %s", crn)
        return None

    # Infer entity type from CRN suffix if CIPC didn't return it
    if not ent_type and _CRN_RE.match(crn):
        suffix = crn.split("/")[-1]
        ent_type = _ENTITY_TYPE_MAP.get(suffix, f"Type-{suffix}")

    # Normalise status
    status_map = {
        "ACTIVE": "ACTIVE",
        "IN BUSINESS": "ACTIVE",
        "DEREGISTERED": "DEREGISTERED",
        "IN DEREGISTRATION": "IN DEREGISTRATION",
        "CONVERTED": "CONVERTED",
        "DISSOLVED": "DISSOLVED",
        "FINAL DEREGISTRATION": "DEREGISTERED",
        "IN LIQUIDATION": "IN LIQUIDATION",
    }
    status = status_map.get(ent_status.upper(), ent_status.upper() if ent_status else "UNKNOWN")

    return {
        "enterpriseNumber": ent_no,
        "enterpriseName": ent_name,
        "enterpriseType": ent_type,
        "enterpriseStatus": status,
        "registeredAddress": reg_addr,
        "postalAddress": postal_addr,
    }


def _format_cipc(record: dict, query_name: str, crn_input: str) -> dict:
    """Format CIPC eServices record into standard verification response."""
    legal_name = record.get("enterpriseName") or query_name
    crn = record.get("enterpriseNumber") or crn_input
    status = record.get("enterpriseStatus", "UNKNOWN")
    entity_type = record.get("enterpriseType")
    reg_addr = record.get("registeredAddress") or record.get("postalAddress")

    return {
        "entity_name": legal_name,
        "country_code": "ZA",
        "found": True,
        "lei": None,
        "crn": crn or None,
        "legal_name": legal_name or None,
        "status": status,
        "entity_type": entity_type,
        "registered_address": reg_addr or None,
        "registration_date": None,
        "directors_note": (
            "Directors and shareholders require a paid CIPC eServices account. "
            "See https://eservices.cipc.co.za/ for full company profile."
        ),
        "source": "CIPC eServices enterprise-number search, South Africa",
        "validation_source": _validation_source(
            query_name or crn_input, crn=crn, legal_name=legal_name
        ),
    }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _validation_source(query: str, crn: str = "", lei: str = "",
                        legal_name: str = "") -> dict:
    display_name = legal_name or query
    crn_display = crn.strip() if crn and _CRN_RE.match(crn.strip()) else ""
    lei_display = lei.strip() if lei else ""

    if lei_display:
        reproduce = (
            f"GLEIF search: https://search.gleif.org/#/record/{lei_display} "
            f"| Bulk search: https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]={display_name}"
        )
    elif crn_display:
        reproduce = (
            f"CIPC eServices: https://eservices.cipc.co.za/Search.aspx → "
            f"enter enterprise number: {crn_display} | "
            f"GLEIF: https://search.gleif.org/#/search?query={display_name}"
        )
    else:
        reproduce = (
            f"GLEIF: https://search.gleif.org/#/search?query={display_name} | "
            f"CIPC eServices (login required): https://bizportal.gov.za/"
        )

    return {
        "registry": "Companies and Intellectual Property Commission (CIPC), South Africa",
        "primary_url": "https://search.gleif.org/",
        "registry_url": "https://eservices.cipc.co.za/",
        "api": "https://api.gleif.org/api/v1/lei-records",
        "how_to_reproduce": reproduce,
        "limitations": (
            "GLEIF covers listed companies, banks, and large corporates with LEIs (~200k ZA entities). "
            "CIPC eServices enterprise-number lookup covers all CIPC-registered entities but "
            "requires a known CRN. BizPortal name search requires login (POPIA, effective 2026). "
            "Directors and shareholders require a paid CIPC eServices account."
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
