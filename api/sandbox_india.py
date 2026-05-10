"""
India PAN & GSTIN verification via Sandbox.co.in API.

Replaces broken Attestr integration on GC/Onboarding side.
14-day trial active as of 2026-05-10 — evaluate for paid plan.

Endpoints:
    PAN:   POST https://api.sandbox.co.in/kyc/pan/verify
    GSTIN: POST https://api.sandbox.co.in/gst/compliance/public/gstin/search

Auth: JWT token (24h) from POST /authenticate with x-api-key + x-api-secret.
"""

import logging
import os
import re
import time
import threading
from datetime import datetime, timezone

import httpx

from keyvault import get_secret
import raw_store

log = logging.getLogger("sandbox_india")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_API_KEY = get_secret("sandbox-api-key") or os.environ.get("SANDBOX_API_KEY", "")
_API_SECRET = get_secret("sandbox-api-secret") or os.environ.get("SANDBOX_API_SECRET", "")

_AUTH_URL = "https://api.sandbox.co.in/authenticate"
_PAN_URL = "https://api.sandbox.co.in/kyc/pan/verify"
_GSTIN_URL = "https://api.sandbox.co.in/gst/compliance/public/gstin/search"

# PAN format: 5 alpha + 4 digits + 1 alpha (e.g. AAACR5055K)
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# GSTIN format: 2-digit state + 10-char PAN + 1 entity + Z + check digit
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

# JWT token cache (24h validity, refresh at 23h)
_token_cache = {"token": "", "expires": 0}
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _get_token() -> str:
    """Get or refresh Sandbox JWT token (24h validity)."""
    now = time.time()
    with _token_lock:
        if _token_cache["token"] and now < _token_cache["expires"]:
            return _token_cache["token"]

    if not _API_KEY or not _API_SECRET:
        raise RuntimeError("Sandbox API key/secret not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_AUTH_URL, headers={
            "x-api-key": _API_KEY,
            "x-api-secret": _API_SECRET,
            "x-api-version": "1.0.0",
            "Content-Type": "application/json",
        })
        resp.raise_for_status()
        token = resp.json().get("access_token", "")
        if not token:
            raise RuntimeError("Sandbox auth returned no token")

    with _token_lock:
        _token_cache["token"] = token
        _token_cache["expires"] = now + 23 * 3600  # refresh 1h before expiry

    log.info("Sandbox token refreshed (valid 24h)")
    return token


def _auth_headers(token: str) -> dict:
    return {
        "authorization": token,
        "x-api-key": _API_KEY,
        "x-api-version": "1.0.0",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# PAN Verification
# ---------------------------------------------------------------------------

async def verify_pan(pan: str, name: str = "", dob: str = "") -> dict:
    """
    Verify an Indian PAN number.

    Args:
        pan: 10-character PAN (e.g. AAACR5055K)
        name: Name as per PAN (optional but improves match)
        dob: Date of birth DD/MM/YYYY (optional but improves match)

    Returns:
        {
            "verified": true/false,
            "pan": "AAACR5055K",
            "status": "valid" | "invalid" | "deactivated",
            "category": "company" | "individual" | "trust" | ...,
            "name_match": true/false | null,
            "dob_match": true/false | null,
            "aadhaar_linked": "y" | "n" | "na",
            "validation_source": {...},
            "latency_ms": 1234,
        }
    """
    pan = pan.strip().upper()
    if not _PAN_RE.match(pan):
        return {"verified": False, "pan": pan, "error": f"Invalid PAN format (expected XXXXX9999X, got {pan})"}

    t0 = time.monotonic()
    try:
        token = await _get_token()
        body = {
            "@entity": "in.co.sandbox.kyc.pan_verification.request",
            "pan": pan,
            "consent": "Y",
            "reason": "Counterparty verification for compliance onboarding",
        }
        # Sandbox requires name + DOB — both mandatory
        if not name or not dob:
            return {"verified": False, "pan": pan,
                    "error": "name and dob (DD/MM/YYYY) required for PAN verification"}
        body["name_as_per_pan"] = name
        body["date_of_birth"] = dob

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_PAN_URL, headers=_auth_headers(token), json=body)

        latency = int((time.monotonic() - t0) * 1000)

        raw_store.store(
            source="SANDBOX_PAN", entity_name=name or pan,
            request_method="POST", request_url=_PAN_URL,
            request_params={"pan": pan},
            request_headers={"authorization": "***", "x-api-key": "***"},
            response_status=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text, duration_ms=latency,
        )

        if resp.status_code == 422:
            return {"verified": False, "pan": pan, "error": resp.json().get("message", "Invalid PAN"),
                    "latency_ms": latency}
        if resp.status_code == 503:
            return {"verified": False, "pan": pan, "error": "Source unavailable (gov portal down)",
                    "latency_ms": latency}
        resp.raise_for_status()

        data = resp.json().get("data", {})
        status = data.get("status", "unknown")
        now = datetime.now(timezone.utc).isoformat()

        return {
            "verified": status == "valid",
            "pan": data.get("pan", pan),
            "status": status,
            "category": data.get("category"),
            "name_match": data.get("name_as_per_pan_match"),
            "dob_match": data.get("date_of_birth_match"),
            "aadhaar_linked": data.get("aadhaar_seeding_status"),
            "validation_source": {
                "registry": "Income Tax Department, Government of India (via Sandbox.co.in)",
                "how_to_reproduce": f"Visit https://eportal.incometax.gov.in → Verify PAN {pan}",
                "verified_at": now,
            },
            "latency_ms": latency,
        }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        log.warning("PAN verify failed for %s: %s", pan, e)
        return {"verified": False, "pan": pan, "error": f"{type(e).__name__}: {e}",
                "latency_ms": latency}


# ---------------------------------------------------------------------------
# GSTIN Verification
# ---------------------------------------------------------------------------

async def verify_gstin(gstin: str) -> dict:
    """
    Verify an Indian GSTIN number.

    Args:
        gstin: 15-character GSTIN (e.g. 27AAACR5055K1Z7)

    Returns:
        {
            "verified": true/false,
            "gstin": "27AAACR5055K1Z7",
            "legal_name": "RELIANCE INDUSTRIES LIMITED",
            "trade_name": "RELIANCE",
            "status": "Active" | "Cancelled" | "Provisional",
            "taxpayer_type": "Regular",
            "constitution": "Public Limited Company",
            "registration_date": "01/07/2017",
            "business_activities": [...],
            "address": {...},
            "state_jurisdiction": "...",
            "center_jurisdiction": "...",
            "validation_source": {...},
            "latency_ms": 1234,
        }
    """
    gstin = gstin.strip().upper()
    if not _GSTIN_RE.match(gstin):
        return {"verified": False, "gstin": gstin,
                "error": f"Invalid GSTIN format (expected 15 chars like 27AAACR5055K1Z7, got {gstin})"}

    t0 = time.monotonic()
    try:
        token = await _get_token()

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(_GSTIN_URL, headers=_auth_headers(token),
                                     json={"gstin": gstin})

        latency = int((time.monotonic() - t0) * 1000)

        raw_store.store(
            source="SANDBOX_GSTIN", entity_name=gstin,
            request_method="POST", request_url=_GSTIN_URL,
            request_params={"gstin": gstin},
            request_headers={"authorization": "***", "x-api-key": "***"},
            response_status=resp.status_code,
            response_headers=dict(resp.headers),
            response_body=resp.text, duration_ms=latency,
        )

        if resp.status_code == 422:
            return {"verified": False, "gstin": gstin, "error": resp.json().get("message", "Invalid GSTIN"),
                    "latency_ms": latency}
        resp.raise_for_status()

        outer = resp.json().get("data", {})
        # Check for error response (status_cd=0)
        if outer.get("error") or outer.get("status_cd") == "0":
            err_msg = outer.get("error", {}).get("message", "No records found")
            return {"verified": False, "gstin": gstin, "error": err_msg, "latency_ms": latency}

        data = outer.get("data", {})
        if not data:
            return {"verified": False, "gstin": gstin, "error": "Empty response", "latency_ms": latency}

        status = data.get("sts", "Unknown")
        now = datetime.now(timezone.utc).isoformat()

        # Extract address
        pradr = data.get("pradr", {}).get("addr", {})
        address = {}
        if pradr:
            address = {
                "building": f"{pradr.get('bno', '')} {pradr.get('bnm', '')}".strip(),
                "street": pradr.get("st", ""),
                "location": pradr.get("loc", ""),
                "district": pradr.get("dst", ""),
                "state": pradr.get("stcd", ""),
                "pincode": pradr.get("pncd", ""),
            }

        return {
            "verified": status.lower() in ("active", "provisional"),
            "gstin": data.get("gstin", gstin),
            "legal_name": data.get("lgnm"),
            "trade_name": data.get("tradeNam"),
            "status": status,
            "taxpayer_type": data.get("dty"),
            "constitution": data.get("ctb"),
            "registration_date": data.get("rgdt"),
            "cancellation_date": data.get("cxdt") or None,
            "last_updated": data.get("lstupdt"),
            "business_activities": data.get("nba", []),
            "einvoice_status": data.get("einvoiceStatus"),
            "address": address,
            "state_jurisdiction": data.get("stj"),
            "center_jurisdiction": data.get("ctj"),
            "validation_source": {
                "registry": "Goods and Services Tax Network (GSTN), Government of India (via Sandbox.co.in)",
                "how_to_reproduce": f"Visit https://services.gst.gov.in/services/searchtp → Search GSTIN {gstin}",
                "verified_at": now,
            },
            "latency_ms": latency,
        }

    except Exception as e:
        latency = int((time.monotonic() - t0) * 1000)
        log.warning("GSTIN verify failed for %s: %s", gstin, e)
        return {"verified": False, "gstin": gstin, "error": f"{type(e).__name__}: {e}",
                "latency_ms": latency}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def health() -> dict:
    """Check Sandbox API availability."""
    if not _API_KEY or not _API_SECRET:
        return {"status": "disabled", "error": "sandbox-api-key/secret not configured"}
    try:
        token = await _get_token()
        return {"status": "up", "has_token": bool(token)}
    except Exception as e:
        return {"status": "down", "error": str(e)}
