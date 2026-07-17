"""
Verify Gateway — Centralized government registry verification service.

Single-purpose service: validates entity data against authoritative gov registries
for any supported country. Uses Multilogin anti-detect browser, proxies,
and direct HTTP as needed per country.

Also provides GLEIF LEI lookup for corporate hierarchy mapping (cross-country).

Port: 8460
Auth: X-API-Key header (from Azure Key Vault)
"""

import asyncio
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import APIKeyHeader

import os


def get_secret(name: str) -> str:
    """Get secret from environment variable."""
    env_name = name.replace("-", "_").upper()
    return os.environ.get(env_name, "") or os.environ.get(name, "")

import multilogin_fbr
import multilogin_dgft
import multilogin_bizfile
import multilogin_tofler
import verify_pk
import verify_tr
import verify_ae
import verify_cn
import verify_uk
import verify_br
import verify_lei
import verify_kr
import verify_us
import verify_sa
import verify_cl
import verify_co
import verify_pe
import verify_mx
import verify_il
import verify_ca
import verify_no
import verify_nz
import verify_dk
import verify_cz
import verify_fi
import verify_lv
import verify_lt
import icris3ep_officers
import verify_fr
import verify_tw
import verify_ec
import verify_hk
import verify_ch
import verify_au
import verify_jp
import verify_nl
import verify_it
import verify_ar
import verify_eg
import verify_ma
import verify_es
import verify_de
import verify_be
import verify_pt
import verify_za
import verify_pl
import verify_gr
import mlx_http

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify-gateway")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = get_secret("cir-api-key")
VERSION = "1.4.0"

# Thread pool for blocking lookups
_pool = ThreadPoolExecutor(max_workers=5)

# Supported countries
SUPPORTED_COUNTRIES = {
    "GR": "GEMI (Γ.Ε.ΜΗ.) — GEMI number, ΑΦΜ, status, legal form, address (Multilogin) + VIES/EL confirmation",
    "PK": "FBR IRIS ATL (Active Taxpayer List) — NTN verification",
    "IN": "DGFT IEC (Import-Export Code) — PAN/IEC verification",
    "SG": "ACRA Bizfile — UEN, status, address (directors require paid profile)",
    "TR": "GIB VKN (Tax ID) verification — company name, tax office, status",
    "AE": "FTA TRN (Tax Registration Number) verification — entity name, status",
    "CN": "SAMR/GSXT via crawl-china VM — company name, USCC, legal rep, status",
    "GB": "Companies House — company name, number, status, address, SIC codes (free API)",
    "BR": "Receita Federal CNPJ — company name, status, address, partners, CNAE (free API)",
    "US": "SEC EDGAR — CIK, entity type, SIC, EIN, tickers, addresses (free API)",
    "KR": "DART (FSS) — company name, CEO, stock code, BRN, address, industry (free API key)",
    "SA": "Wathq (MCI) — commercial registration, owners, managers, capital (free API)",
    "CL": "SII RUT — taxpayer status, economic activities, address (free, no CAPTCHA)",
    "CO": "RUES — commercial registration, NIT, legal form, chamber of commerce (free)",
    "PE": "SUNAT RUC — company name, status, condition, address, economic activity (free API)",
    "MX": "DENUE (INEGI) — establishment name, legal name, activity, address, employee size (free API)",
    "IL": "ICA (data.gov.il) — company name (HE+EN), number, type, status, address (free CKAN API)",
    "CA": "BC OrgBook — entity name, BN, status, type, registration date, jurisdiction (free API)",
    "NO": "Brønnøysundregistrene (Enhetsregisteret) — org-nr, legal name, status, address, industry (free JSON API)",
    "NZ": "NZ Companies Office — NZBN, legal name, status, entity type, registered address (free public search)",
    "DK": "cvrapi.dk (Danish CVR wrapper) — CVR, legal name, status, address, industry (free public API)",
    "CZ": "ARES (Justice/Finance Ministry) — IČO, legal name, legal form, NACE, address, status (free JSON API)",
    "FI": "PRH Avoindata (Patentti- ja rekisterihallitus) — Business ID, legal name, legal form, industry, address (free JSON API)",
    "LV": "Latvian Register of Enterprises (data.gov.lv) — regcode, legal name, type, address, status (free JSON API)",
    "LT": "JADIS (Registry Center) — LT company code, legal name, status (form-based search via Multilogin)",
    "FR": "Registre National des Entreprises (INSEE/INPI) — SIREN, directors, legal form, activity, address (free API)",
    "TW": "GCIS Open Data (MOEA) — UBN, company name, status, capital, address, responsible person (free JSON API)",
    "EC": "SRI (Servicio de Rentas Internas) — RUC, legal name, status, economic activity, address (free API)",
    "HK": "ICRIS (Companies Registry) — CR number, company name, status, type (free public search)",
    "CH": "Zefix (FOSC) — UID, legal name, status, legal form, purpose, canton, address (free REST API)",
    "AU": "ABR (Australian Business Register) — ABN, ACN, legal name, entity type, status, GST (free JSONP API)",
    "JP": "Houjin Bangou (NTA) — corporate number, legal name (JP+EN), kind, status, address (free API)",
    "NL": "KvK (Kamer van Koophandel) — KVK number, legal name, status, legal form, address (free public search)",
    "IT": "VIES (EU VAT) — P.IVA, legal name, status, address (free EU tax validation)",
    "AR": "AFIP (CUIT) — CUIT, legal name, tax status, address, economic activities (free API)",
    "EG": "GLEIF LEI Registry — LEI, legal name (AR+EN), status, commercial reg, address (free API)",
    "MA": "GLEIF LEI Registry — LEI, legal name, status, address (~250 entities). Smaller MA entities not in GLEIF.",
    "ES": "VIES (EU VAT) — CIF, legal name, status, address (free EU tax validation)",
    "DE": "VIES (EU VAT) — USt-IdNr, legal name, status, address (free EU tax validation)",
    "BE": "VIES (EU VAT) + KBO/BCE — CBE number, legal name, status, address (free REST API)",
    "PT": "VIES (EU VAT) — NIPC/NIF, legal name, status, address (free EU tax validation)",
    "ZA": "GLEIF LEI API (primary) + CIPC eServices enterprise-number (secondary) — legal name, LEI, status, address",
    "PL": "KRS (Ministry of Justice) — KRS, NIP, REGON, legal form, address, representatives (free API)",
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str = Depends(_api_key_header)):
    if not API_KEY:
        return key
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Verify Gateway", version=VERSION)


@app.on_event("startup")
async def startup():
    """Initialize all verification modules."""
    multilogin_fbr.init(get_secret)
    mlx_http.init(get_secret)
    multilogin_dgft.init(get_secret)
    multilogin_bizfile.init(get_secret)
    verify_tr.init(get_secret)
    verify_ae.init(get_secret)
    verify_cn.init(get_secret)
    verify_uk.init(get_secret)
    verify_br.init(get_secret)
    verify_lei.init(get_secret)
    verify_kr.init(get_secret)
    verify_us.init(get_secret)
    verify_sa.init(get_secret)
    verify_cl.init(get_secret)
    verify_co.init(get_secret)
    verify_pe.init(get_secret)
    verify_mx.init(get_secret)
    verify_il.init(get_secret)
    verify_pk.init(get_secret)
    verify_ca.init(get_secret)
    verify_no.init(get_secret)
    verify_nz.init(get_secret)
    verify_dk.init(get_secret)
    verify_cz.init(get_secret)
    verify_fi.init(get_secret)
    verify_lv.init(get_secret)
    verify_lt.init(get_secret)
    icris3ep_officers.init(get_secret)
    verify_fr.init(get_secret)
    verify_tw.init(get_secret)
    verify_ec.init(get_secret)
    verify_hk.init(get_secret)
    verify_ch.init(get_secret)
    verify_au.init(get_secret)
    verify_jp.init(get_secret)
    verify_nl.init(get_secret)
    verify_it.init(get_secret)
    verify_ar.init(get_secret)
    verify_eg.init(get_secret)
    verify_ma.init(get_secret)
    verify_es.init(get_secret)
    verify_de.init(get_secret)
    verify_gr.init(get_secret)
    verify_be.init(get_secret)
    verify_pt.init(get_secret)
    verify_za.init(get_secret)
    verify_pl.init(get_secret)
    log.info("Verify Gateway v%s started — %d countries + LEI supported", VERSION, len(SUPPORTED_COUNTRIES))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "supported_countries": SUPPORTED_COUNTRIES,
        "enrichment_sources": {
            "GLEIF_LEI": "Corporate hierarchy lookup — parent/ultimate parent (2.6M+ entities, free)",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/verify")
async def verify(request: Request, _key: str = Depends(verify_api_key)):
    """
    Verify entity against government registry.

    Body: {
        "entity_name": "Company Name",
        "country_code": "PK",              // PK, IN, SG, TR, AE, CN, GB, BR
        "ntn": "1234567-8",                 // Pakistan NTN (for PK)
        "iec": "ABCDE1234F",                // India IEC/PAN (for IN)
        "uen": "201733771N",                // Singapore UEN (for SG)
        "vkn": "1234567890",                // Turkey VKN tax ID (for TR)
        "trn": "100330886100003",           // UAE TRN (for AE)
        "uscc": "91110...",                 // China USCC (for CN, optional)
        "company_number": "12345678",       // UK company number (for GB, optional)
        "cnpj": "00.000.000/0001-00",       // Brazil CNPJ (for BR)
        "cr_number": "1010000096",          // Saudi CR number (for SA)
        "rut": "76123456-7",                // Chile RUT (for CL)
        "nit": "123456789",                 // Colombia NIT (for CO)
        "ruc": "20100030595",               // Peru RUC (for PE)
    }
    """
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    country_code = body.get("country_code", "").strip().upper()

    if not entity_name and not any(body.get(k) for k in ("ntn", "iec", "uen", "company_number", "cnpj", "cik", "ticker", "cr_number", "rut", "nit", "ruc")):
        raise HTTPException(status_code=422, detail="At least entity_name or an ID field required")
    if not country_code:
        raise HTTPException(status_code=422, detail="country_code required")
    if country_code not in SUPPORTED_COUNTRIES:
        raise HTTPException(
            status_code=422,
            detail=f"Country {country_code} not supported. Supported: {', '.join(sorted(SUPPORTED_COUNTRIES))}",
        )

    loop = asyncio.get_event_loop()

    # --------------- PAKISTAN ---------------
    # Two paths:
    #   1. NTN supplied (or reg_number alias)  → FBR ATL via Multilogin PK
    #      (preserved — existing path covering Pakistani sole props /
    #       partnerships which only show up in FBR).
    #   2. Entity-name only / SECP id          → SECP NameSearch + CTC via
    #      Multilogin PK residential (verify_pk.secp_verify, added
    #      2026-06-25 — replaces the old gateway-side Gulf-VM + Bright
    #      Data path which used wrong country exit and timed out).
    if country_code == "PK":
        ntn = (body.get("ntn") or "").strip()
        reg_number = (body.get("reg_number") or "").strip()
        # NTN is numeric digits / dash; SECP reg_no is also numeric but
        # typically zero-padded 7-digit. Disambiguate by explicit field.
        if ntn:
            result = await loop.run_in_executor(_pool, multilogin_fbr.fbr_atl_verify, ntn)
            return result
        # No NTN — try SECP NameSearch by entity_name. SECP only indexes
        # registered companies (Pvt/Public/SMC/LLP/Foreign Co/etc.); sole
        # proprietors fall through to a clear NOT_FOUND with the FBR-by-
        # NTN escalation explained in the note.
        if not entity_name:
            raise HTTPException(
                status_code=422,
                detail="ntn or entity_name required for PK verification",
            )
        result = await loop.run_in_executor(_pool, verify_pk.secp_verify, entity_name)
        return result

    # --------------- INDIA ---------------
    if country_code == "IN":
        # Accept reg_number as alias for CIN — GC and onboarding send it as the
        # generic registry-id field.
        cin = (body.get("cin") or body.get("reg_number") or "").strip().upper()
        iec = body.get("iec", "").strip().upper()

        # CIN → Tofler (MCA21 republished data) via Multilogin + IN exit.
        # Returns directors / incorporation / address / capital — the corporate
        # registry shape GC needs.
        if cin:
            result = await loop.run_in_executor(
                _pool, multilogin_tofler.tofler_cin_verify, entity_name, cin
            )
            return result

        # IEC → DGFT IRIS (import-export code, different lookup)
        if iec:
            result = await loop.run_in_executor(
                _pool, multilogin_dgft.dgft_iec_verify, iec, entity_name
            )
            return result

        raise HTTPException(
            status_code=422,
            detail="cin (or reg_number) or iec required for IN verification",
        )

    # --------------- SINGAPORE ---------------
    if country_code == "SG":
        uen = body.get("uen", "").strip()
        result = await loop.run_in_executor(
            _pool, multilogin_bizfile.bizfile_verify, entity_name, uen
        )
        return result

    # --------------- TURKEY ---------------
    if country_code == "TR":
        vkn = body.get("vkn", "").strip() or body.get("reg_number", "").strip()
        if vkn:
            result = await loop.run_in_executor(
                _pool, verify_tr.gib_vkn_verify, vkn, entity_name
            )
        else:
            # No VKN: name-only path via GLEIF + OpenCorporates
            result = await loop.run_in_executor(
                _pool, verify_tr.gleif_oc_verify, entity_name, ""
            )
        return result

    # --------------- UAE ---------------
    if country_code == "AE":
        trn = body.get("trn", "").strip() or body.get("reg_number", "").strip()
        if trn:
            # TRN-supplied path: original Multilogin + FTA TRN verify
            result = await loop.run_in_executor(
                _pool, verify_ae.fta_trn_verify, trn, entity_name
            )
        else:
            # No TRN: name-only path via GLEIF (primary) + OpenCorporates (secondary).
            # Onboarding-style name lookups land here.
            result = await loop.run_in_executor(
                _pool, verify_ae.gleif_oc_verify, entity_name, ""
            )
        return result

    # --------------- CHINA ---------------
    if country_code == "CN":
        uscc = body.get("uscc", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_cn.cn_verify, entity_name, uscc
        )
        return result

    # --------------- UK ---------------
    if country_code == "GB":
        company_number = body.get("company_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_uk.companies_house_verify, entity_name, company_number
        )
        return result

    # --------------- BRAZIL ---------------
    if country_code == "BR":
        cnpj = body.get("cnpj", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_br.cnpj_verify, entity_name, cnpj
        )
        return result




    # --------------- USA ---------------
    if country_code == "US":
        cik = body.get("cik", "").strip()
        ticker = body.get("ticker", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_us.edgar_verify, entity_name, cik, ticker
        )
        return result

    # --------------- SOUTH KOREA ---------------
    if country_code == "KR":
        corp_code = body.get("corp_code", "").strip()
        brn = body.get("brn", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_kr.dart_verify, entity_name, corp_code, brn
        )
        return result

    # --------------- SAUDI ARABIA ---------------
    if country_code == "SA":
        cr_number = body.get("cr_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_sa.wathq_verify, entity_name, cr_number
        )
        return result

    # --------------- CHILE ---------------
    if country_code == "CL":
        rut = body.get("rut", "").strip()
        if not rut:
            raise HTTPException(status_code=422, detail="rut (Chilean RUT number) required for CL verification")
        result = await loop.run_in_executor(
            _pool, verify_cl.sii_rut_verify, entity_name, rut
        )
        return result

    # --------------- COLOMBIA ---------------
    if country_code == "CO":
        nit = body.get("nit", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_co.rues_verify, entity_name, nit
        )
        return result

    # --------------- PERU ---------------
    if country_code == "PE":
        ruc = body.get("ruc", "").strip()
        if not ruc:
            raise HTTPException(status_code=422, detail="ruc (11-digit RUC) required for PE verification")
        result = await loop.run_in_executor(
            _pool, verify_pe.sunat_ruc_verify, entity_name, ruc
        )
        return result

    # --------------- MEXICO ---------------
    if country_code == "MX":
        rfc = body.get("rfc", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_mx.denue_verify, entity_name, rfc
        )
        return result

    # --------------- ISRAEL ---------------
    if country_code == "IL":
        company_number = body.get("company_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_il.ica_verify, entity_name, company_number
        )
        return result

    if country_code == "CA":
        bn = body.get("business_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_ca.orgbook_verify, entity_name, bn
        )
        return result

    if country_code == "NO":
        org_number = body.get("org_number", body.get("organisasjonsnummer", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_no.brreg_verify, entity_name, org_number
        )
        return result

    if country_code == "NZ":
        nzbn = body.get("nzbn", body.get("company_number", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_nz.companies_office_verify, entity_name, nzbn
        )
        return result

    if country_code == "DK":
        cvr = body.get("cvr", body.get("vat", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_dk.cvr_verify, entity_name, cvr
        )
        return result

    if country_code == "CZ":
        ico = body.get("ico", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_cz.ares_verify, entity_name, ico
        )
        return result

    if country_code == "FI":
        business_id = body.get("business_id", body.get("y_tunnus", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_fi.prh_verify, entity_name, business_id
        )
        return result

    if country_code == "LV":
        regcode = body.get("regcode", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_lv.lursoft_verify, entity_name, regcode
        )
        return result

    if country_code == "LT":
        company_code = body.get("company_code", body.get("imones_kodas", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_lt.jadis_verify, entity_name, company_code
        )
        return result


    if country_code == "FR":
        siren = body.get("siren", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_fr.entreprises_verify, entity_name, siren
        )
        return result



    # --------------- TAIWAN ---------------
    if country_code == "TW":
        ubn = body.get("ubn", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_tw.moea_verify, entity_name, ubn
        )
        return result

    # --------------- ECUADOR ---------------
    if country_code == "EC":
        ruc = body.get("ruc", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_ec.supercias_verify, entity_name, ruc
        )
        return result

    # --------------- HONG KONG ---------------
    if country_code == "HK":
        cr_number = body.get("cr_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_hk.icris_verify, entity_name, cr_number
        )
        return result

    # --------------- SWITZERLAND ---------------
    if country_code == "CH":
        uid = body.get("uid", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_ch.zefix_verify, entity_name, uid
        )
        return result

    # --------------- AUSTRALIA ---------------
    if country_code == "AU":
        abn = body.get("abn", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_au.abr_verify, entity_name, abn
        )
        return result

    # --------------- JAPAN ---------------
    if country_code == "JP":
        corp_number = body.get("corp_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_jp.houjin_verify, entity_name, corp_number
        )
        return result

    # --------------- GREECE (GEMI + VIES/EL) ---------------
    if country_code == "GR":
        gemi_number = (body.get("gemi_number") or body.get("reg_number") or "").strip()
        afm = (body.get("afm") or body.get("vat_id") or "").strip()
        result = await loop.run_in_executor(
            _pool, verify_gr.gemi_verify, entity_name, gemi_number, afm
        )
        return result

    # --------------- NETHERLANDS ---------------
    if country_code == "NL":
        kvk_number = body.get("kvk_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_nl.kvk_verify, entity_name, kvk_number
        )
        return result

    # --------------- ITALY ---------------
    if country_code == "IT":
        partita_iva = body.get("partita_iva", body.get("vat_id", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_it.registroimprese_verify, entity_name, partita_iva
        )
        return result

    # --------------- ARGENTINA ---------------
    if country_code == "AR":
        cuit = body.get("cuit", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_ar.afip_verify, entity_name, cuit
        )
        return result

    # --------------- EGYPT ---------------
    if country_code == "EG":
        commercial_reg = body.get("commercial_reg", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_eg.gafi_verify, entity_name, commercial_reg
        )
        return result

    # --------------- MOROCCO ---------------
    if country_code == "MA":
        ompic_number = body.get("ompic_number", "").strip() or body.get("reg_number", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_ma.verify, entity_name, ompic_number
        )
        return result

    # --------------- SPAIN ---------------
    if country_code == "ES":
        cif = body.get("cif", body.get("vat_id", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_es.borme_verify, entity_name, cif
        )
        return result

    # --------------- GERMANY ---------------
    if country_code == "DE":
        vat_id = body.get("vat_id", body.get("ust_id", "")).strip()
        result = await loop.run_in_executor(
            _pool, verify_de.handelsregister_verify, entity_name, "", vat_id
        )
        return result

    # --------------- BELGIUM ---------------
    if country_code == "BE":
        cbe_number = body.get("cbe_number", body.get("cbe", body.get("vat_id", ""))).strip()
        result = await loop.run_in_executor(
            _pool, verify_be.kbo_verify, entity_name, cbe_number
        )
        return result

    # --------------- PORTUGAL ---------------
    if country_code == "PT":
        nipc = body.get("nipc", body.get("nif", body.get("vat_id", ""))).strip()
        result = await loop.run_in_executor(
            _pool, verify_pt.mj_verify, entity_name, nipc
        )
        return result

    # --------------- SOUTH AFRICA ---------------
    if country_code == "ZA":
        crn = body.get("crn", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_za.cipc_verify, entity_name, crn
        )
        return result

    # --------------- POLAND ---------------
    if country_code == "PL":
        krs = body.get("krs", "").strip()
        nip = body.get("nip", "").strip()
        result = await loop.run_in_executor(
            _pool, verify_pl.krs_verify, entity_name, krs, nip
        )
        return result

@app.post("/verify/lei")
async def verify_lei_endpoint(request: Request, _key: str = Depends(verify_api_key)):
    """
    GLEIF LEI lookup — corporate hierarchy mapping.

    Body: {
        "entity_name": "Company Name",     // Search by name
        "lei": "5493001KJTIIGC8Y1R12",     // Or direct LEI lookup
        "country_code": "GB",              // Optional country filter for name search
    }

    Returns: LEI, entity details, direct parent, ultimate parent, legal form.
    """
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    lei = body.get("lei", "").strip()
    country_code = body.get("country_code", "").strip()

    if not entity_name and not lei:
        raise HTTPException(status_code=422, detail="entity_name or lei required")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _pool, verify_lei.lei_lookup, entity_name, lei, country_code
    )
    return result


@app.post("/verify/officers")
async def verify_officers(request: Request):
    """Paid HK officers lookup via ICRIS3EP (cache-first)."""
    body = await request.json()
    country_code = (body.get("country_code") or "").upper()
    brn = (body.get("brn") or "").strip()
    cr_number = (body.get("cr_number") or "").strip()
    entity_name = (body.get("entity_name") or "").strip()
    refresh = bool(body.get("refresh", False))
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _pool, icris3ep_officers.fetch_officers,
        country_code, brn, cr_number, entity_name, refresh,
    )
