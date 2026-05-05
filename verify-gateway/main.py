"""
Verify Gateway — Centralized government registry verification service.

Single-purpose service: validates entity data against authoritative gov registries
for any supported country. Uses Multilogin anti-detect browser, proxies,
and direct HTTP as needed per country.

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

# Add parent api/ dir for keyvault module
sys.path.insert(0, "/home/copapadmin/crawl/api")
from keyvault import get_secret

import multilogin_fbr
import multilogin_dgft
import multilogin_bizfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verify-gateway")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = get_secret("cir-api-key")
VERSION = "1.0.0"

# Thread pool for blocking lookups
_pool = ThreadPoolExecutor(max_workers=5)

# Supported countries
SUPPORTED_COUNTRIES = {
    "PK": "FBR IRIS ATL (Active Taxpayer List) — NTN verification",
    "IN": "DGFT IEC (Import-Export Code) — PAN/IEC verification",
    "SG": "ACRA Bizfile — UEN, status, address (directors require paid profile)",
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
    multilogin_dgft.init(get_secret)
    multilogin_bizfile.init(get_secret)
    log.info("Verify Gateway v%s started — %d countries supported", VERSION, len(SUPPORTED_COUNTRIES))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "supported_countries": SUPPORTED_COUNTRIES,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/verify")
async def verify(request: Request, _key: str = Depends(verify_api_key)):
    """
    Verify entity against government registry.

    Body: {
        "entity_name": "Company Name",
        "country_code": "PK",        // PK, IN, SG
        "ntn": "1234567-8",           // Pakistan NTN (optional for PK)
        "iec": "ABCDE1234F",          // India IEC/PAN (optional for IN)
        "uen": "201733771N"           // Singapore UEN (optional for SG)
    }
    """
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    country_code = body.get("country_code", "").strip().upper()

    if not entity_name and not body.get("ntn") and not body.get("iec") and not body.get("uen"):
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
    if country_code == "PK":
        ntn = body.get("ntn", "").strip()
        if not ntn:
            raise HTTPException(status_code=422, detail="ntn required for PK verification")
        result = await loop.run_in_executor(_pool, multilogin_fbr.fbr_atl_verify, ntn)
        return result

    # --------------- INDIA ---------------
    if country_code == "IN":
        iec = body.get("iec", "").strip().upper()
        if not iec:
            raise HTTPException(status_code=422, detail="iec (PAN) required for IN verification")
        result = await loop.run_in_executor(
            _pool, multilogin_dgft.dgft_iec_verify, iec, entity_name
        )
        return result

    # --------------- SINGAPORE ---------------
    if country_code == "SG":
        uen = body.get("uen", "").strip()
        result = await loop.run_in_executor(
            _pool, multilogin_bizfile.bizfile_verify, entity_name, uen
        )
        return result
