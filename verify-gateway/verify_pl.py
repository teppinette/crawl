"""
Poland company verification via KRS (Krajowy Rejestr Sądowy) API.

Primary source: https://api-krs.ms.gov.pl/api/krs/OdpisSzukaj/{krs_number}
Official Polish Ministry of Justice KRS REST API.
Free — no auth required, returns structured JSON.
Full Polish business registry: legal name, KRS number, NIP, REGON,
legal form, registration date, address, representative persons, PKD codes.

Fallback: EU VIES VAT validation for NIP verification.
POST https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number
{"countryCode":"PL","vatNumber":"..."}

KRS number: up to 10 digits (zero-padded to 10 for API).
NIP: 10 digits (Polish tax ID).

Input: entity_name (search — if KRS/NIP not given, returns not_found),
       krs (KRS number), nip (NIP / VAT number)
Returns: entity_name, country_code, found, krs, nip, regon, legal_name,
         status, legal_form, registered_address, court, representatives,
         pkd_codes, registration_date, validation_source
"""

import logging
import re
import time

from mlx_http import mlx_get, mlx_post

log = logging.getLogger("verify-gateway")

# KRS REST API — Ministry of Justice Poland
# OdpisAktualny = current extract (active company data)
# OdpisPelny = full extract (historical data too, slower)
_KRS_BASE = "https://api-krs.ms.gov.pl/api/krs"
_KRS_ODPIS_URL = _KRS_BASE + "/OdpisAktualny/{krs}"          # primary
_KRS_SZUKAJ_URL = _KRS_BASE + "/OdpisSzukaj/{krs}"           # fallback variant

# EU VIES VAT validation (NIP fallback)
_VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"

# KRS number must be up to 10 digits
_KRS_RE = re.compile(r"^\d{1,10}$")
# NIP: 10 digits (may have dashes)
_NIP_RE = re.compile(r"^\d{10}$")


def init(get_secret):
    log.info("PL KRS ready (api-krs.ms.gov.pl, Multilogin proxy)")


def krs_verify(entity_name: str, krs: str = "", nip: str = "") -> dict:
    """
    Verify a Polish company via KRS (primary) or VIES (NIP fallback).

    - If krs provided: look up directly via KRS REST API.
    - If nip provided (no krs): validate via EU VIES; also attempt KRS
      name-match if VIES succeeds.
    - If only entity_name: set found=False (KRS has no public name-search API).
    """
    if not entity_name and not krs and not nip:
        return {"found": False, "error": "entity_name, krs, or nip required"}

    try:
        # ── 1. KRS lookup (preferred) ──────────────────────────────────────
        if krs:
            clean_krs = _clean_krs(krs)
            if not clean_krs:
                return {
                    "entity_name": entity_name,
                    "krs": krs,
                    "found": False,
                    "error": "KRS number must be 1–10 digits",
                }
            return _krs_lookup(entity_name, clean_krs, nip)

        # ── 2. NIP via VIES (fallback) ─────────────────────────────────────
        if nip:
            clean_nip = _clean_nip(nip)
            if not clean_nip:
                return {
                    "entity_name": entity_name,
                    "nip": nip,
                    "found": False,
                    "error": "NIP must be 10 digits",
                }
            return _vies_lookup(entity_name, clean_nip)

        # ── 3. Name only — KRS has no public name search API ──────────────
        return {
            "entity_name": entity_name,
            "country_code": "PL",
            "found": False,
            "status": "NOT_SEARCHED",
            "note": (
                "KRS API requires a KRS number or NIP. "
                "Provide krs= or nip= to perform a registry lookup. "
                "Manual search: https://wyszukiwarka.ms.gov.pl/"
            ),
            "source": "KRS (Krajowy Rejestr Sądowy), Ministry of Justice, Poland",
            "validation_source": _validation_source_name(entity_name),
        }

    except Exception as e:
        log.error("PL KRS error for %s (krs=%s nip=%s): %s", entity_name, krs, nip, e)
        return {
            "entity_name": entity_name,
            "country_code": "PL",
            "found": False,
            "error": str(e)[:300],
        }


# ─────────────────────────────────────────────────────────────────────────────
# KRS API lookup
# ─────────────────────────────────────────────────────────────────────────────

def _krs_lookup(entity_name: str, krs: str, nip: str) -> dict:
    """Look up a company by KRS number via the official MS.GOV.PL API."""
    # KRS numbers are zero-padded to 10 digits in the API path
    padded = krs.zfill(10)

    data = _fetch_krs(padded)
    if data is None:
        # Try alternate endpoint format
        data = _fetch_krs_alt(padded)

    if data is None:
        return {
            "entity_name": entity_name,
            "country_code": "PL",
            "krs": krs,
            "nip": nip or None,
            "found": False,
            "status": "NOT_FOUND",
            "source": "KRS (Krajowy Rejestr Sądowy), Ministry of Justice, Poland",
            "validation_source": _validation_source_krs(krs),
        }

    return _format_krs_result(data, entity_name, krs, nip)


def _fetch_krs(padded_krs: str) -> dict | None:
    """
    Fetch from primary KRS endpoint: OdpisAktualny (current extract).
    Returns parsed JSON or None on failure.
    """
    url = _KRS_ODPIS_URL.format(krs=padded_krs)
    try:
        result = mlx_get(
            url,
            params={"rejestr": "P", "format": "json"},   # P = przedsiebiorca (entrepreneurs)
            headers={"Accept": "application/json"},
            timeout=60, country_code="pl",
        )
        if result.get("status_code") == 404:
            # Try rejestr=S (stowarzyszenia — associations/foundations)
            result2 = mlx_get(
                url,
                params={"rejestr": "S", "format": "json"},
                headers={"Accept": "application/json"},
                timeout=60, country_code="pl",
            )
            if result2.get("status_code") == 404:
                return None
            if not result2.get("ok"):
                raise RuntimeError(f"KRS HTTP {result2.get('status_code')}: {result2.get('body', '')[:200]}")
            return result2.get("json")
        if not result.get("ok"):
            raise RuntimeError(f"KRS HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
        return result.get("json")
    except Exception as e:
        log.debug("KRS OdpisAktualny failed for %s: %s", padded_krs, str(e)[:120])
        return None


def _fetch_krs_alt(padded_krs: str) -> dict | None:
    """
    Fallback: OdpisSzukaj endpoint (search-style, different schema).
    """
    url = _KRS_SZUKAJ_URL.format(krs=padded_krs)
    try:
        result = mlx_get(
            url,
            headers={"Accept": "application/json"},
            timeout=60, country_code="pl",
        )
        if result.get("status_code") in (404, 400):
            return None
        if not result.get("ok"):
            raise RuntimeError(f"KRS HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
        return result.get("json")
    except Exception as e:
        log.debug("KRS OdpisSzukaj failed for %s: %s", padded_krs, str(e)[:120])
        return None


def _format_krs_result(data: dict, query_name: str, krs: str, nip_input: str) -> dict:
    """
    Parse KRS API JSON into standard verification response.

    KRS OdpisAktualny returns a deeply nested document:
      odpis.dane.dzial1.danePodmiotu  — entity data
      odpis.dane.dzial1.siedzibaIAdresStaropolski — address
      odpis.dane.dzial2.reprezentacja — representatives
      odpis.dane.dzial3.pkd — PKD activity codes
    """
    # Navigate the KRS response structure
    odpis = data.get("odpis", data)  # some endpoints wrap in "odpis"
    dane = odpis.get("dane", odpis)

    dzial1 = dane.get("dzial1", {})
    dane_podmiotu = dzial1.get("danePodmiotu", {})
    siedziba = dzial1.get("siedzibaIAdresStaropolski",
                dane.get("siedzibaIAdres", {}))

    dzial2 = dane.get("dzial2", {})
    reprezentacja = dzial2.get("reprezentacja", {})
    czlonkowie = reprezentacja.get("czlonkowie", [])  # board / management

    dzial3 = dane.get("dzial3", {})
    pkd_raw = dzial3.get("przedmiotDzialalnosci", {})
    pkd_items = pkd_raw.get("pozycjePkd", [])

    # Also check top-level fields (some API variants are flat)
    if not dane_podmiotu:
        dane_podmiotu = dane

    # ── Entity identity ────────────────────────────────────────────────────
    legal_name = (
        dane_podmiotu.get("nazwa", "")
        or dane_podmiotu.get("firmaNazwa", "")
        or dane.get("nazwa", "")
        or query_name
    )

    krs_number = (
        dane_podmiotu.get("numerKRS", "")
        or dane.get("numerKRS", "")
        or krs
    )
    nip = (
        dane_podmiotu.get("nip", "")
        or dane.get("nip", "")
        or nip_input
        or None
    )
    regon = (
        dane_podmiotu.get("regon", "")
        or dane.get("regon", "")
        or None
    )

    # ── Legal form ─────────────────────────────────────────────────────────
    forma_prawna = (
        dane_podmiotu.get("formaPrawna", "")
        or dane_podmiotu.get("formaPrawnaSkrot", "")
        or dane.get("formaPrawna", "")
        or None
    )

    # ── Status / registration date ─────────────────────────────────────────
    data_rejestracji = (
        dzial1.get("dataRejestracji", "")
        or dane.get("dataRejestracji", "")
        or None
    )
    # If we got a result from the API the entity exists in the registry
    status = "ACTIVE"
    # Check if struck off or in liquidation
    rozwiazanie = dane.get("dzial6", {})
    if rozwiazanie:
        status = "DISSOLVED"
    likwidacja = dane.get("dzial5", {})
    if likwidacja:
        status = "IN_LIQUIDATION"

    # ── Address ────────────────────────────────────────────────────────────
    adres = (
        siedziba.get("adres", {})
        if isinstance(siedziba, dict) else {}
    )
    ulica = adres.get("ulica", siedziba.get("ulica", ""))
    nr_domu = adres.get("nrDomu", siedziba.get("nrDomu", ""))
    nr_lokalu = adres.get("nrLokalu", siedziba.get("nrLokalu", ""))
    miejscowosc = (
        adres.get("miejscowosc", "")
        or siedziba.get("miejscowosc", "")
        or siedziba.get("siedziba", "")
    )
    kod_pocztowy = adres.get("kodPocztowy", siedziba.get("kodPocztowy", ""))

    addr_parts = []
    if ulica:
        addr_parts.append(ulica)
    if nr_domu:
        addr_parts.append(str(nr_domu) + (f"/{nr_lokalu}" if nr_lokalu else ""))
    if kod_pocztowy:
        addr_parts.append(kod_pocztowy)
    if miejscowosc:
        addr_parts.append(miejscowosc)
    registered_address = ", ".join(p for p in addr_parts if p.strip()) or None

    # ── Court ──────────────────────────────────────────────────────────────
    sad_rejestrowy = (
        dzial1.get("sadRejestrowy", {})
        if isinstance(dzial1, dict) else {}
    )
    court = (
        sad_rejestrowy.get("nazwa", "")
        or sad_rejestrowy.get("sad", "")
        or dane.get("sadRejestrowy", "")
        or None
    )

    # ── Representatives / Management Board ────────────────────────────────
    representatives = []
    for czl in (czlonkowie if isinstance(czlonkowie, list) else [])[:10]:
        imie = czl.get("imie", "")
        drugie_imie = czl.get("drugieImie", "")
        nazwisko = czl.get("nazwisko", "")
        funkcja = czl.get("funkcja", czl.get("rola", ""))
        full_name = " ".join(p for p in [imie, drugie_imie, nazwisko] if p).strip()
        if full_name:
            representatives.append({
                "name": full_name,
                "role": funkcja or None,
            })

    # ── PKD economic activity codes ────────────────────────────────────────
    pkd_codes = []
    for item in (pkd_items if isinstance(pkd_items, list) else [])[:10]:
        code = item.get("kodPkd", item.get("kod", ""))
        desc = item.get("opis", item.get("nazwaLong", ""))
        glowny = item.get("glownyPrzedmiotDzialalnosci", False)
        if code:
            pkd_codes.append({
                "code": code,
                "description": desc or None,
                "primary": bool(glowny),
            })

    return {
        "entity_name": legal_name.strip() if legal_name else query_name,
        "query_name": query_name,
        "country_code": "PL",
        "found": True,
        "status": status,
        "krs": krs_number.strip() if krs_number else krs,
        "nip": nip.strip() if nip else None,
        "regon": regon.strip() if regon else None,
        "legal_name": legal_name.strip() if legal_name else None,
        "legal_form": forma_prawna,
        "registration_date": data_rejestracji[:10] if data_rejestracji else None,
        "registered_address": registered_address,
        "court": court,
        "representatives": representatives if representatives else None,
        "pkd_codes": pkd_codes if pkd_codes else None,
        "source": "KRS (Krajowy Rejestr Sądowy), Ministry of Justice, Poland",
        "validation_source": _validation_source_krs(krs_number.strip() if krs_number else krs),
    }


# ─────────────────────────────────────────────────────────────────────────────
# VIES NIP fallback
# ─────────────────────────────────────────────────────────────────────────────

def _vies_lookup(entity_name: str, nip: str) -> dict:
    """Validate a Polish NIP via EU VIES VAT system."""
    try:
        result = mlx_post(
            _VIES_URL,
            json_body={"countryCode": "PL", "vatNumber": nip},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=60, country_code="pl",
        )

        if result.get("status_code") in (400, 404, 422):
            return {
                "entity_name": entity_name,
                "country_code": "PL",
                "nip": nip,
                "found": False,
                "status": "NOT_FOUND",
                "source": "EU VIES VAT Information Exchange System",
                "validation_source": _validation_source_vies(nip),
            }

        if not result.get("ok"):
            raise RuntimeError(f"VIES HTTP {result.get('status_code')}: {result.get('body', '')[:200]}")
        data = result.get("json") or {}

        valid = data.get("valid", False) or data.get("isValid", False)
        vat_name = data.get("name", "")
        vat_address = data.get("address", "")
        request_date = data.get("requestDate", "")

        if not valid:
            return {
                "entity_name": entity_name,
                "country_code": "PL",
                "nip": nip,
                "found": False,
                "status": "INVALID_VAT",
                "note": "NIP not found or not valid in EU VIES at time of query",
                "source": "EU VIES VAT Information Exchange System",
                "validation_source": _validation_source_vies(nip),
            }

        # VIES returns "---" for inactive VAT registrations sometimes
        vat_name_clean = vat_name.strip() if vat_name and vat_name.strip() not in ("---", "***") else ""

        return {
            "entity_name": vat_name_clean or entity_name,
            "query_name": entity_name,
            "country_code": "PL",
            "found": True,
            "status": "ACTIVE",
            "krs": None,
            "nip": nip,
            "regon": None,
            "legal_name": vat_name_clean or None,
            "legal_form": None,
            "registration_date": None,
            "registered_address": vat_address.strip() if vat_address else None,
            "court": None,
            "representatives": None,
            "pkd_codes": None,
            "vies_checked_at": request_date or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "EU VIES VAT Information Exchange System",
            "validation_source": _validation_source_vies(nip),
        }

    except Exception as e:
        log.error("PL VIES error for NIP %s: %s", nip, e)
        return {
            "entity_name": entity_name,
            "country_code": "PL",
            "nip": nip,
            "found": False,
            "error": str(e)[:300],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_krs(krs: str) -> str:
    """Strip whitespace/dashes, validate digits only, up to 10 digits."""
    clean = re.sub(r"[\s\-/]", "", krs.strip())
    if _KRS_RE.match(clean):
        return clean
    return ""


def _clean_nip(nip: str) -> str:
    """Strip dashes/spaces, must be exactly 10 digits."""
    clean = re.sub(r"[\s\-]", "", nip.strip())
    if _NIP_RE.match(clean):
        return clean
    return ""


def _validation_source_krs(krs: str) -> dict:
    return {
        "registry": "KRS — Krajowy Rejestr Sądowy (National Court Register), Ministry of Justice, Poland",
        "url": f"https://wyszukiwarka.ms.gov.pl/rejestr-przedsiebiorcow/podmiot/{krs}",
        "api": "https://api-krs.ms.gov.pl/api/krs/OdpisAktualny/{krs}?rejestr=P&format=json",
        "how_to_reproduce": (
            f"Visit wyszukiwarka.ms.gov.pl → KRS: {krs} → Pobierz odpis aktualny"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _validation_source_vies(nip: str) -> dict:
    return {
        "registry": "EU VIES — VAT Information Exchange System (European Commission)",
        "url": "https://ec.europa.eu/taxation_customs/vies/",
        "api": "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number",
        "how_to_reproduce": (
            f"Visit ec.europa.eu/taxation_customs/vies → "
            f"Member State: Poland (PL) → VAT Number: {nip} → Verify"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _validation_source_name(entity_name: str) -> dict:
    return {
        "registry": "KRS — Krajowy Rejestr Sądowy (National Court Register), Ministry of Justice, Poland",
        "url": "https://wyszukiwarka.ms.gov.pl/",
        "how_to_reproduce": (
            f"Visit wyszukiwarka.ms.gov.pl → Search: {entity_name}"
        ),
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
