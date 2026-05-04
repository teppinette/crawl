"""
Crawl Research Gateway v3.0 — Scenario-based OSINT research API.

Accepts research requests via scenario-specific payloads, sanitizes ALL data
to remove any identifying information about the requesting organization,
routes to regional crawl VMs, and returns results via Azure blob staging.

HARD RULES:
  1. NO customer/supplier names, org identity, or internal IDs ever reach OpenClaw
  2. Only crawldevvm can SSH to regional VMs (NSG enforced)
  3. OpenClaw receives ONLY: entity name, country, and research parameters
  4. The word "COPAP" must NEVER appear in any prompt sent to OpenClaw

Usage:
    uvicorn main:app --host 0.0.0.0 --port 8400 --workers 4

Scenarios:
    cir            Counterparty Intelligence Report (DD research)
    product-intel  Product market intelligence (pricing, sourcing, competitors)
"""

import asyncio
import collections
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import paramiko
from fastapi import FastAPI, HTTPException, Security, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, model_validator

from keyvault import get_secret, load_vm_tokens
from event_log import log_job_event, log_api_access
import multilogin_fbr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crawl-gateway")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = get_secret("cir-api-key")
MAX_RETRIES = 1
API_VERSION = "3.0.0"

JURISDICTION_MAP = {
    "US": "americas", "CA": "americas", "CO": "americas", "BR": "americas",
    "MX": "americas", "CL": "americas", "PE": "americas", "AR": "americas",
    "PA": "americas", "EC": "americas", "VE": "americas", "DO": "americas",
    "GT": "americas", "HN": "americas", "CR": "americas", "UY": "americas",
    "PY": "americas", "BO": "americas", "NI": "americas", "SV": "americas",
    "TR": "europe", "RU": "europe", "BY": "europe", "RS": "europe",
    "NG": "europe", "UA": "europe", "BG": "europe", "DE": "europe",
    "NL": "europe", "GB": "europe", "FR": "europe", "IT": "europe",
    "ES": "europe", "CH": "europe", "SE": "europe", "NO": "europe",
    "AE": "gulf", "EG": "gulf", "PK": "gulf", "IQ": "gulf",
    "SA": "gulf", "QA": "gulf", "BH": "gulf", "KW": "gulf",
    "OM": "gulf", "JO": "gulf",
    "CN": "china", "HK": "china", "VN": "china", "MM": "china",
    "TW": "china", "KR": "china", "JP": "china", "SG": "china",
    "TH": "china", "MY": "china", "PH": "china", "ID": "china",
    "IN": "india",
}

_vm_tokens = load_vm_tokens()
VM_CONFIG = {
    "americas": {"ip": "172.206.2.41", "user": "copapadmin", "token": _vm_tokens["americas"]},
    "europe":   {"ip": "172.189.56.218", "user": "copapadmin", "token": _vm_tokens["europe"]},
    "gulf":     {"ip": "20.233.46.58", "user": "copadmin", "token": _vm_tokens["gulf"]},
    "china":    {"ip": "10.0.0.4", "user": "copapadmin", "token": _vm_tokens["china"]},
    "india":    {"ip": "20.193.150.43", "user": "copapadmin", "token": _vm_tokens["india"]},
}

DARKWEB_VM = {
    "ip": "20.86.161.6",
    "port": 8450,
    "api_key": get_secret("darkweb-api-key"),
}

SSH_KEY_PATH = os.path.expanduser("~/.ssh/crawldevvm_key.pem")
SSH_KNOWN_HOSTS = os.path.expanduser("~/.ssh/crawl_known_hosts")
BLOB_ACCOUNT = "stcrawlosint"
BLOB_CONTAINER = "osint-staging"
SAS_TOKEN_PATH = Path(os.path.expanduser("~/crawl/config/blob_sas_token"))
_BLOB_SAS_TOKEN = get_secret("blob-sas-token") or (
    SAS_TOKEN_PATH.read_text().strip() if SAS_TOKEN_PATH.exists() else ""
)
LOCAL_OUTPUT_DIR = Path(os.path.expanduser("~/crawl/output"))
LOCAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR = Path("/home/copapadmin/crawl/api/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Thread pool for blocking SSH work — keeps the async event loop free
_ssh_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ssh")
_MAX_QUEUED_JOBS = 20  # Reject new jobs if this many are already running/queued

# Per-job locks to prevent concurrent SSH threads from clobbering each other's
# blob_paths when writing to the same job file (fan-out race condition fix).
_job_locks: dict[str, threading.Lock] = {}
_job_locks_guard = threading.Lock()


_JOB_LOCKS_MAX = 200  # Trigger cleanup when this many locks accumulate


def _get_job_lock(job_id: str) -> threading.Lock:
    """Get or create a lock for a specific job (thread-safe)."""
    with _job_locks_guard:
        if job_id not in _job_locks:
            _job_locks[job_id] = threading.Lock()
        if len(_job_locks) > _JOB_LOCKS_MAX:
            _cleanup_job_locks()
        return _job_locks[job_id]


def _cleanup_job_locks():
    """Remove locks for jobs that are no longer running (completed/failed/unknown)."""
    to_remove = []
    for jid in list(_job_locks.keys()):
        job_file = JOBS_DIR / f"{jid}.json"
        if not job_file.exists():
            to_remove.append(jid)
            continue
        try:
            status = json.loads(job_file.read_text()).get("status", "")
            if status in ("completed", "failed"):
                to_remove.append(jid)
        except Exception:
            pass
    for jid in to_remove:
        _job_locks.pop(jid, None)
    if to_remove:
        log.info("Cleaned up %d stale job locks (%d remaining)", len(to_remove), len(_job_locks))


def update_job_fields(job_id: str, updates: dict) -> dict:
    """
    Thread-safe read-modify-write on a job file.
    `updates` can include simple key-value pairs and special list-append keys:
      "_append_blob_path": str  — appends to blob_paths[]
      "_set_region_status": (region, status, error|None) — sets region_status[region]
      "_append_error": str — appends to errors[] list (never overwrites)
    Returns the updated job dict.
    """
    lock = _get_job_lock(job_id)
    with lock:
        job = load_job(job_id)

        # Handle list-append operations
        if "_append_blob_path" in updates:
            if not job.get("blob_paths"):
                job["blob_paths"] = []
            job["blob_paths"].append(updates.pop("_append_blob_path"))

        # Handle per-region status tracking
        if "_set_region_status" in updates:
            region, rstatus, rerror = updates.pop("_set_region_status")
            if not job.get("region_status"):
                job["region_status"] = {}
            job["region_status"][region] = {"status": rstatus, "error": rerror}

        # Handle error append (never overwrites previous region errors)
        if "_append_error" in updates:
            err = updates.pop("_append_error")
            if not job.get("errors"):
                job["errors"] = []
            job["errors"].append(err)
            # Also keep a single "error" field with the latest for backward compat
            updates["error"] = err

        # Apply simple field updates
        job.update(updates)
        save_job(job)
        return job

# ---------------------------------------------------------------------------
# DATA SANITIZATION — HARD RULE: Nothing identifying reaches OpenClaw
# ---------------------------------------------------------------------------

# Terms that must NEVER appear in any prompt sent to OpenClaw.
# Add any org name, product name, internal term that could identify the requester.
_BLOCKED_TERMS = [
    "copap", "copapadmin", "copap ai", "copap trading",
    "global compliance", "gc app",
    # Internal system names
    "cir-api", "crawldevvm", "osint-staging",
    # Add customer/supplier names here as they become known
    # The sanitizer also blocks any field tagged as internal
]

# Fields that are NEVER forwarded to OpenClaw (stripped before prompt building)
_INTERNAL_FIELDS = {
    "copap_relationship", "copap_products", "copap_incoterms",
    "source_report", "priority", "workstreams",
    "requesting_app", "internal_ref", "requester_org",
}


def sanitize_text(text: str) -> str:
    """Remove all blocked terms from text. Case-insensitive."""
    sanitized = text
    for term in _BLOCKED_TERMS:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def sanitize_payload(data: dict) -> dict:
    """
    Deep-sanitize a payload dict before it becomes an OpenClaw prompt.

    1. Remove all _INTERNAL_FIELDS entirely
    2. Scan all string values for _BLOCKED_TERMS
    3. Raise ValueError if a blocked term is found (hard fail, don't silently redact)
    """
    cleaned = {}
    violations = []

    for key, value in data.items():
        # Strip internal fields completely
        if key in _INTERNAL_FIELDS:
            continue

        if isinstance(value, str):
            lower = value.lower()
            for term in _BLOCKED_TERMS:
                if term in lower:
                    violations.append(f"Field '{key}' contains blocked term '{term}'")
            cleaned[key] = value
        elif isinstance(value, list):
            cleaned[key] = _sanitize_list(value, key, violations)
        elif isinstance(value, dict):
            cleaned[key] = sanitize_payload(value)
        else:
            cleaned[key] = value

    if violations:
        raise ValueError(
            f"DATA SANITIZATION FAILURE — blocked terms detected in payload: "
            f"{'; '.join(violations)}. This data must NEVER reach OpenClaw."
        )

    return cleaned


def _sanitize_list(items: list, parent_key: str, violations: list) -> list:
    """Sanitize list items recursively."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(sanitize_payload(item))
        elif isinstance(item, str):
            lower = item.lower()
            for term in _BLOCKED_TERMS:
                if term in lower:
                    violations.append(f"List '{parent_key}' contains blocked term '{term}'")
            result.append(item)
        else:
            result.append(item)
    return result


def verify_prompt_clean(prompt: str) -> str:
    """
    Final gate before any prompt is sent to OpenClaw.
    Scans the assembled prompt for blocked terms. Hard fail if found.
    """
    lower = prompt.lower()
    for term in _BLOCKED_TERMS:
        if term in lower:
            raise ValueError(
                f"PROMPT SANITIZATION FAILURE — blocked term '{term}' found in "
                f"assembled prompt. Aborting dispatch. This is a critical error."
            )
    return prompt


# ---------------------------------------------------------------------------
# Models — Shared
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    partial_success = "partial_success"
    failed = "failed"


class ScenarioType(str, Enum):
    cir = "cir"
    product_intel = "product-intel"
    dark_web = "dark-web"


class Individual(BaseModel):
    name: str
    title: Optional[str] = None


class Affiliate(BaseModel):
    entity_name: str
    country: str
    relationship: Optional[str] = None


class Supplier(BaseModel):
    entity_name: str
    country: Optional[str] = None


class ReviewRequest(BaseModel):
    reviewer: str = Field(..., description="Analyst name or email")
    score: int = Field(..., ge=1, le=5, description="1=reject, 2=needs-work, 3=acceptable, 4=good, 5=excellent")
    notes: Optional[str] = Field(None, description="Reviewer comments")


class JobResponse(BaseModel):
    job_id: str
    scenario: str
    status: JobStatus
    request_id: Optional[str] = None
    region: Optional[str] = None
    regions: Optional[list[str]] = None
    region_status: Optional[dict] = None
    entity_name: Optional[str] = None
    country: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None
    blob_path: Optional[str] = None
    blob_paths: Optional[list[str]] = None
    report_summary: Optional[str] = None
    error: Optional[str] = None
    errors: Optional[list[str]] = None
    retry_count: int = 0
    review: Optional[dict] = None
    dark_web: Optional[dict] = None


# ---------------------------------------------------------------------------
# Models — CIR Scenario
# ---------------------------------------------------------------------------

class CIRRequest(BaseModel):
    """Counterparty Intelligence Report — seed data for DD research."""
    entity_legal_name: str = Field(..., description="Full legal name as registered")
    entity_trade_names: Optional[str] = Field(None, description="DBAs, trade names")
    entity_country: str = Field(..., description="ISO 2-letter country code", min_length=2, max_length=2)
    entity_jurisdiction: Optional[str] = Field(None, description="State/province/region")
    entity_address: Optional[str] = Field(None, description="Registered address")
    entity_website: Optional[str] = Field(None, description="URL")
    entity_type: Optional[str] = Field(None, description="Public/Private, legal form")
    entity_industry: Optional[str] = Field(None, description="Sector description")
    entity_tax_id: Optional[str] = Field(None, description="Tax ID / registration number")
    key_individuals: Optional[list[Individual]] = Field(default_factory=list)
    known_affiliates: Optional[list[Affiliate]] = Field(default_factory=list)
    known_suppliers: Optional[list[Supplier]] = Field(default_factory=list)
    # --- Internal fields (stripped before OpenClaw, never forwarded) ---
    copap_relationship: Optional[str] = Field(None, description="INTERNAL: stripped before dispatch")
    copap_products: Optional[str] = Field(None, description="INTERNAL: stripped before dispatch")
    copap_incoterms: Optional[str] = Field(None, description="INTERNAL: stripped before dispatch")
    source_report: Optional[str] = Field(None, description="INTERNAL: stripped before dispatch")
    workstreams: Optional[list[str]] = Field(None, description="Specific workstreams (e.g., ['1A','2A']). Null = all.")
    priority: Optional[str] = Field("standard", description="INTERNAL: immediate / high / standard")


# ---------------------------------------------------------------------------
# Models — Product Intel Scenario
# ---------------------------------------------------------------------------

class ProductSpec(BaseModel):
    """Product identification block."""
    generic_name: str = Field(..., description="Product common name (e.g., Linear Alkyl Benzene)")
    grade_code: Optional[str] = Field(None, description="Grade/spec code (e.g., C10-C13, 96%)")
    commodity_family: Optional[str] = Field(None, description="Commodity family (e.g., surfactants, aromatics)")


class ProductIntelRequest(BaseModel):
    """
    Product market intelligence contract.

    Matches the productintel team's contract:
      Send: request_id, product.generic_name, product.grade_code,
            product.commodity_family, region_hint, lookback_days, signal_types[]
      Receive: signals array + coverage_score
    """
    request_id: Optional[str] = Field(None, description="Idempotency key — if resubmitted, returns existing job")
    product: ProductSpec = Field(..., description="Product identification")
    region_hint: Optional[str] = Field(None, description="Region hint (e.g., 'gulf', 'asia'). Auto-derived from target_markets if omitted.")
    target_markets: Optional[list[str]] = Field(None, description="ISO 2-letter country codes. If omitted, derived from region_hint.")
    lookback_days: int = Field(30, description="Lookback period in days (7, 30, 90, 365)")
    signal_types: list[str] = Field(
        default_factory=lambda: ["news", "price_index", "freight", "supply_disruption", "geopolitical"],
        description="Signal types to collect: news, price_index, freight, supply_disruption, geopolitical"
    )
    # Legacy fields (still accepted for backward compat)
    product_name: Optional[str] = Field(None, description="DEPRECATED: use product.generic_name")
    intel_type: Optional[str] = Field(None, description="DEPRECATED: use signal_types")
    time_horizon: Optional[str] = Field(None, description="DEPRECATED: use lookback_days")
    known_producers: Optional[list[str]] = Field(None, description="Known producers to track")
    specific_questions: Optional[list[str]] = Field(None, description="Specific research questions")
    # --- Internal fields ---
    priority: Optional[str] = Field("standard", description="INTERNAL: stripped before dispatch")


# ---------------------------------------------------------------------------
# Models — Dark Web Scenario
# ---------------------------------------------------------------------------

class DarkWebRequest(BaseModel):
    """Dark web intelligence request — entity + optional owners/domain."""
    entity_name: str = Field(..., description="Entity legal name to search")
    country: Optional[str] = Field(None, description="ISO 2-letter country code")
    owners: Optional[list[str]] = Field(default_factory=list, description="Key individuals / UBOs to also search")
    domain: Optional[str] = Field(None, description="Company domain for breach/infostealer checks")
    depth: str = Field("medium", description="light | medium | heavy")
    # --- Internal fields ---
    priority: Optional[str] = Field("standard", description="INTERNAL: stripped before dispatch")


# Region hint mapping — when caller says "gulf" instead of specific country codes
# Keys are normalized to lowercase for matching
REGION_HINT_MAP = {
    "gulf": ["AE", "SA", "QA", "BH", "KW", "OM", "JO"],
    "asia": ["CN", "IN", "JP", "KR", "SG", "TH", "MY", "VN", "PH", "ID"],
    "se asia": ["SG", "TH", "MY", "VN", "PH", "ID", "MM"],
    "southeast asia": ["SG", "TH", "MY", "VN", "PH", "ID", "MM"],
    "east asia": ["CN", "HK", "JP", "KR", "TW"],
    "south asia": ["IN", "PK"],
    "europe": ["DE", "NL", "GB", "FR", "IT", "ES", "TR"],
    "americas": ["US", "CA", "BR", "MX", "CO"],
    "india": ["IN"],
    "china": ["CN", "HK"],
    "mena": ["AE", "SA", "EG", "IQ", "QA", "BH", "KW", "OM", "JO"],
    "global": ["US", "DE", "AE", "CN", "IN"],  # one VM per region for broad coverage
}


# ---------------------------------------------------------------------------
# Models — Generic Job Envelope
# ---------------------------------------------------------------------------

class JobRequest(BaseModel):
    """Generic job request envelope. Routes to scenario-specific handler."""
    scenario: ScenarioType = Field(..., description="Research scenario: cir | product-intel")
    payload: dict = Field(..., description="Scenario-specific payload")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
):
    """
    Accept auth via either:
      - X-API-Key: <key>  (our standard)
      - Authorization: Bearer <key>  (productintel team's preference)
    """
    key = api_key
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
    if not key or key != API_KEY:
        client_ip = request.client.host if request.client else "unknown"
        log.warning("AUTH FAIL from %s (key prefix: %s)", client_ip, (key or "")[:8] or "none")
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Crawl Research Gateway",
    version=API_VERSION,
    description=(
        "Scenario-based OSINT research gateway. Routes research requests to "
        "regional crawl VMs via SSH. All data is sanitized before dispatch — "
        "no identifying information about the requesting organization ever "
        "reaches the research agents."
    ),
)

# Initialize Multilogin FBR module (credentials from Key Vault)
multilogin_fbr.init(get_secret)


# ---------------------------------------------------------------------------
# Rate limiting — per-IP sliding window
# ---------------------------------------------------------------------------

_RATE_LIMIT = 30        # max requests per window
_RATE_WINDOW = 60       # window in seconds
_rate_hits: dict[str, collections.deque] = {}
_rate_lock = threading.Lock()


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Reject requests if a single IP exceeds the rate limit."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        if client_ip not in _rate_hits:
            _rate_hits[client_ip] = collections.deque()
        dq = _rate_hits[client_ip]
        # Evict old entries
        while dq and dq[0] < now - _RATE_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded ({_RATE_LIMIT} req/{_RATE_WINDOW}s)"},
            )
        dq.append(now)
    return await call_next(request)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """Log every API request to the database for traceability."""
    start = time.monotonic()
    response = await call_next(request)
    duration = int((time.monotonic() - start) * 1000)

    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    # Extract job_id from path if present (e.g. /api/v1/jobs/abc-123)
    job_id = None
    parts = path.strip("/").split("/")
    if len(parts) >= 4 and parts[2] in ("jobs", "research"):
        candidate = parts[3] if len(parts) > 3 else None
        if candidate and len(candidate) > 8:
            job_id = candidate

    log_api_access(
        client_ip=client_ip,
        method=request.method,
        path=path,
        status_code=response.status_code,
        duration_ms=duration,
        job_id=job_id,
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    return response


# ---------------------------------------------------------------------------
# Helpers — Job persistence
# ---------------------------------------------------------------------------

def save_job(job: dict):
    path = JOBS_DIR / f"{job['job_id']}.json"
    with open(path, "w") as f:
        json.dump(job, f, indent=2, default=str)


def load_job(job_id: str) -> dict:
    path = JOBS_DIR / f"{job_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    with open(path) as f:
        return json.load(f)


def list_jobs(limit: int = 50, scenario: str = None) -> list[dict]:
    jobs = []
    for p in sorted(JOBS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        with open(p) as f:
            j = json.load(f)
        if scenario and j.get("scenario") != scenario:
            continue
        jobs.append(j)
        if len(jobs) >= limit:
            break
    return jobs


# ---------------------------------------------------------------------------
# Helpers — Region routing
# ---------------------------------------------------------------------------

# Common country code mistakes — auto-correct before routing
_COUNTRY_CODE_FIXES = {
    # Mistake → Correct (with reason)
    "PA": None,  # PA = Panama (legitimate), but check entity name for Pakistan
    "UK": "GB",  # UK is not ISO 3166 — GB is
    "EN": "GB",  # England → Great Britain
    "RS": "RS",  # Serbia — correct, already mapped
}

# Country name keywords → correct ISO code (for entity-name-based correction)
_COUNTRY_NAME_TO_CODE = {
    "pakistan": "PK", "pakistani": "PK",
    "india": "IN", "indian": "IN",
    "china": "CN", "chinese": "CN",
    "turkey": "TR", "turkish": "TR", "turkiye": "TR",
    "russia": "RU", "russian": "RU",
    "brazil": "BR", "brazilian": "BR",
    "nigeria": "NG", "nigerian": "NG",
    "egypt": "EG", "egyptian": "EG",
    "iraq": "IQ", "iraqi": "IQ",
    "iran": "IR", "iranian": "IR",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "vietnam": "VN", "viet nam": "VN",
    "myanmar": "MM",
    "korea": "KR",
    "japan": "JP",
    "singapore": "SG",
    "thailand": "TH",
    "malaysia": "MY",
    "indonesia": "ID",
    "philippines": "PH",
    "taiwan": "TW",
    "hong kong": "HK",
}


def _fix_country_code(country_code: str, entity_name: str = "") -> tuple[str, str]:
    """
    Auto-correct common country code mistakes.
    Returns (corrected_code, warning_or_empty).
    Uses entity name to detect mismatches (e.g. 'PAKISTAN PVT LTD' with code 'PA').
    """
    cc = country_code.upper().strip()
    entity_lower = entity_name.lower()
    warning = ""

    # Check if entity name mentions a country that contradicts the code
    for keyword, correct_code in _COUNTRY_NAME_TO_CODE.items():
        if keyword in entity_lower and cc != correct_code:
            mapped_region = JURISDICTION_MAP.get(cc, "americas")
            correct_region = JURISDICTION_MAP.get(correct_code, "americas")
            if mapped_region != correct_region:
                warning = (
                    f"Country code '{cc}' corrected to '{correct_code}' — "
                    f"entity name contains '{keyword}' but code was "
                    f"'{cc}' ({mapped_region}), not '{correct_code}' ({correct_region})"
                )
                log.warning("COUNTRY CODE FIX: %s", warning)
                cc = correct_code
                break

    # Direct fixes (UK→GB etc)
    if cc in _COUNTRY_CODE_FIXES and _COUNTRY_CODE_FIXES[cc] is not None:
        fixed = _COUNTRY_CODE_FIXES[cc]
        if not warning:
            warning = f"Country code '{country_code}' corrected to '{fixed}'"
            log.warning("COUNTRY CODE FIX: %s", warning)
        cc = fixed

    return cc, warning


def get_region(country_code: str) -> str:
    return JURISDICTION_MAP.get(country_code.upper(), "americas")


def get_regions_for_markets(country_codes: list[str]) -> list[str]:
    """Map multiple country codes to unique regions (for fan-out scenarios)."""
    regions = list(dict.fromkeys(get_region(cc) for cc in country_codes))
    return regions


# ---------------------------------------------------------------------------
# Prompt Builders — each scenario has its own
# ---------------------------------------------------------------------------

def _get_country_instructions(country: str, clean: dict) -> str:
    """Return country-specific research instructions for CIR prompts."""
    reg_num = clean.get("registration_number", clean.get("entity_tax_id", ""))
    entity = clean.get("entity_legal_name", "")
    instructions = {
        "PK": (
            f"Pakistan entity — use region-gulf skill Pakistan section:\n"
            f"1. SECP: Search eservices.secp.gov.pk for company registration. Retry 3x if portal is slow.\n"
            f"2. FBR NTN: Verify {'NTN ' + reg_num + ' on' if reg_num else 'NTN on'} e.fbr.gov.pk Active Taxpayer List. Report VERIFIED_ACTIVE, VERIFIED_INACTIVE, or UNVERIFIED.\n"
            f"3. NAB: Check nab.gov.pk for entity + ALL directors individually against corruption cases.\n"
            f"4. SBP: Check sbp.org.pk sanctions watchlist.\n"
            f"5. Courts: Search Lahore HC, Sindh HC, Islamabad HC, Supreme Court for entity + directors.\n"
            f"6. Media: Search dawn.com, thenews.com.pk, geo.tv, brecorder.com for adverse coverage.\n"
            f"7. Trade: Check en.nbd.ltd for China-Pakistan import records.\n"
            f"8. DRAP: If chemical/pharma/agro — verify product registration at drap.gov.pk.\n"
            f"9. Include ntn_number, ntn_status, secp_number, nab_screening fields in output."
        ),
        "AE": (
            f"UAE entity — use region-gulf skill UAE section:\n"
            f"1. Identify which free zone / emirate — check JAFZA, DMCC, ADGM, DIFC, RAKEZ, DAFZA, Dubai DED.\n"
            f"2. CBUAE: Check centralbank.ae for licensed financial institution status.\n"
            f"3. DIFC Courts: Search difccourts.ae for judgments involving entity + directors.\n"
            f"4. Free zone opacity: Flag if UBO cannot be traced through free zone registration.\n"
            f"5. Iran evasion patterns: Check for shared registered agents, virtual office addresses, recent incorporation.\n"
            f"6. Media: Search gulfnews.com, khaleejtimes.com, thenationalnews.com.\n"
            f"7. If RAK/Ajman free zone — flag as higher risk (easier incorporation)."
        ),
        "TR": (
            f"Turkey entity — use region-europe skill Turkey section:\n"
            f"1. MERSIS: Search mersis.gtb.gov.tr for commercial registration.\n"
            f"2. TTSG: Check ilan.gov.tr (Trade Gazette) for official announcements.\n"
            f"3. KAP: If listed company, check kap.org.tr for disclosures.\n"
            f"4. UYAP: Check uyap.gov.tr for court records (limited public access).\n"
            f"5. SPK/CMB: Check spk.gov.tr for capital markets enforcement.\n"
            f"6. MASAK: Turkey's FIU — check for AML enforcement actions.\n"
            f"7. BDDK: Banking regulation — check bddk.org.tr if financial entity.\n"
            f"8. Media: Search dailysabah.com, hurriyetdailynews.com, bianet.org for adverse coverage.\n"
            f"9. Russia/Iran routing: Flag trade connections via Turkish free zones."
        ),
        "SA": (
            f"Saudi Arabia entity — use region-gulf skill Saudi section:\n"
            f"1. MCI: Search mc.gov.sa for commercial registration.\n"
            f"2. ZATCA: Check zatca.gov.sa for tax registration.\n"
            f"3. Tadawul: Check if listed on tadawul.com.sa.\n"
            f"4. SAMA: Check if licensed financial institution."
        ),
        "IN": (
            f"India entity — use region-india skill:\n"
            f"1. MCA21: Check company registration, directors, charges.\n"
            f"2. GST Portal: Verify GST registration.\n"
            f"3. DGFT IEC: Verify import-export code.\n"
            f"4. eCourts: Search for litigation.\n"
            f"5. SEBI/RBI: Check regulatory actions if financial entity."
        ),
        "CN": (
            f"China entity — use region-china skill:\n"
            f"1. NECIPS/GSXT: Check company registration.\n"
            f"2. Qichacha/Tianyancha: Search for corporate details.\n"
            f"3. UFLPA Entity List: Check forced labor concerns.\n"
            f"4. BIS MEU: Check military end-user list."
        ),
    }
    return instructions.get(country, "")


def build_cir_prompt(payload: dict) -> str:
    """Build CIR research prompt from SANITIZED payload."""
    # Sanitize first — hard fail if blocked terms detected
    clean = sanitize_payload(payload)

    lines = [
        f"Research: {clean['entity_legal_name']}, {clean['entity_country'].upper()}",
        "",
        "SUBJECT IDENTIFICATION BLOCK:",
        f"- ENTITY_LEGAL_NAME: {clean['entity_legal_name']}",
    ]

    field_map = {
        "entity_trade_names": "ENTITY_TRADE_NAMES",
        "entity_country": "ENTITY_COUNTRY",
        "entity_jurisdiction": "ENTITY_JURISDICTION",
        "entity_address": "ENTITY_ADDRESS",
        "entity_website": "ENTITY_WEBSITE",
        "entity_type": "ENTITY_TYPE",
        "entity_industry": "ENTITY_INDUSTRY",
        "entity_tax_id": "ENTITY_TAX_ID",
    }

    for field, label in field_map.items():
        val = clean.get(field)
        if val:
            if field == "entity_country":
                val = val.upper()
            lines.append(f"- {label}: {val}")

    if clean.get("key_individuals"):
        lines.append("")
        lines.append("KEY_INDIVIDUALS:")
        for i, ind in enumerate(clean["key_individuals"], 1):
            title = f" - {ind.get('title', '')}" if ind.get("title") else ""
            lines.append(f"  {i}. {ind['name']}{title}")

    if clean.get("known_affiliates"):
        lines.append("")
        lines.append("KNOWN_AFFILIATES:")
        for i, aff in enumerate(clean["known_affiliates"], 1):
            rel = f" - {aff.get('relationship', '')}" if aff.get("relationship") else ""
            lines.append(f"  {i}. {aff['entity_name']} - {aff['country']}{rel}")

    if clean.get("known_suppliers"):
        lines.append("")
        lines.append("KNOWN_SUPPLIERS:")
        for i, sup in enumerate(clean["known_suppliers"], 1):
            country = f" - {sup.get('country', '')}" if sup.get("country") else ""
            lines.append(f"  {i}. {sup['entity_name']}{country}")

    # Registration number / tax ID
    reg_num = clean.get("registration_number", clean.get("entity_tax_id", ""))
    if reg_num:
        lines.append(f"- REGISTRATION_NUMBER: {reg_num}")

    # Research focus from caller
    focus = clean.get("research_focus", "")
    if focus:
        lines.append("")
        lines.append(f"RESEARCH FOCUS: {focus}")

    workstreams = payload.get("workstreams")  # from original (internal field)
    if workstreams:
        lines.append("")
        lines.append(f"WORKSTREAMS TO EXECUTE: {', '.join(workstreams)}")
    else:
        lines.append("")
        lines.append("Execute ALL workstreams (1A through 8A) per the counterparty_research skill.")

    # Country-specific instructions — tell agent to use the deep sources in region skill
    country = clean.get("entity_country", "").upper()
    country_instructions = _get_country_instructions(country, clean)
    if country_instructions:
        lines.append("")
        lines.append("COUNTRY-SPECIFIC REQUIREMENTS:")
        lines.append(country_instructions)

    lines.append("")
    lines.append("Save JSON output to ~/crawl/output/ when complete.")

    prompt = "\n".join(lines)
    return verify_prompt_clean(prompt)


def build_product_intel_prompt(payload: dict, region: str, region_markets: list[str]) -> str:
    """
    Build product intelligence prompt from SANITIZED payload.
    One prompt per region, scoped to the markets in that region.
    Supports both new contract (product.generic_name, signal_types) and legacy fields.
    """
    clean = sanitize_payload(payload)

    # Resolve product name from new or legacy format
    product = clean.get("product", {})
    product_name = product.get("generic_name") if isinstance(product, dict) else None
    product_name = product_name or clean.get("product_name", "Unknown Product")
    grade_code = product.get("grade_code", "") if isinstance(product, dict) else ""
    commodity_family = product.get("commodity_family", "") if isinstance(product, dict) else ""

    # Resolve signal types from new or legacy format
    signal_types = clean.get("signal_types", [])
    if not signal_types:
        # Map legacy intel_type to signal_types
        legacy = clean.get("intel_type", "pricing")
        if legacy == "all":
            signal_types = ["news", "price_index", "freight", "supply_disruption", "geopolitical"]
        else:
            signal_types = [legacy]

    lookback = clean.get("lookback_days", 30)
    # Legacy fallback
    if "time_horizon" in clean and not clean.get("lookback_days"):
        th = clean["time_horizon"]
        lookback = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}.get(th, 30)

    market_str = ", ".join(cc.upper() for cc in region_markets)

    lines = [
        f"Product Intelligence: {product_name}",
        f"Markets: {market_str}",
        "",
        "RESEARCH PARAMETERS:",
        f"- PRODUCT: {product_name}",
    ]

    if grade_code:
        lines.append(f"- GRADE: {grade_code}")
    if commodity_family:
        lines.append(f"- COMMODITY_FAMILY: {commodity_family}")

    lines.append(f"- TARGET_MARKETS: {market_str}")
    lines.append(f"- LOOKBACK_DAYS: {lookback}")
    lines.append(f"- SIGNAL_TYPES: {', '.join(signal_types)}")

    if clean.get("known_producers"):
        lines.append("")
        lines.append("KNOWN PRODUCERS TO TRACK:")
        for i, p in enumerate(clean["known_producers"], 1):
            lines.append(f"  {i}. {p}")

    if clean.get("specific_questions"):
        lines.append("")
        lines.append("SPECIFIC QUESTIONS:")
        for i, q in enumerate(clean["specific_questions"], 1):
            lines.append(f"  {i}. {q}")

    lines.append("")
    lines.append("RESEARCH INSTRUCTIONS BY SIGNAL TYPE:")
    lines.append("CRITICAL: Only include data within the lookback window. Do NOT include data older than the lookback period in signals[].")
    lines.append("CRITICAL: Every signal MUST have a real URL — never leave url empty.")
    lines.append("CRITICAL: Generate a signal_id for each signal: <type>-<YYYYMMDD>-<8char hash of headline/event>")

    if "news" in signal_types:
        lines.append("- NEWS: Find recent news, headlines, sentiment (positive/negative/neutral)")
        lines.append("  Include source name, publication date, URL (REQUIRED), summary")

    if "price_index" in signal_types:
        lines.append("- PRICE_INDEX: Find current spot/contract prices")
        lines.append("  IMPORTANT: Separate price LEVELS from price CHANGES:")
        lines.append("    value_type='level' for absolute prices (e.g., $7200/ton)")
        lines.append("    value_type='delta' for price changes (e.g., +$50/ton)")
        lines.append("    value_type='pct' for percentage changes (e.g., +5%)")
        lines.append("  Use numeric fields: price_low, price_high, price_mid (numbers, not strings)")
        lines.append("  Include basis (FOB/CFR/CIF/DAP/EXW), port, currency, unit, URL")
        lines.append("  Check ICIS, Platts, ChemOrbis, Chemanalyst, commodity exchanges")

    if "freight" in signal_types:
        lines.append("- FREIGHT: Current shipping rates for relevant routes")
        lines.append("  Use sub_type to distinguish: 'base_rate', 'surcharge', or 'all_in'")
        lines.append("  rate must be numeric, include rate_unit ('40ft container', '20ft container', 'MT')")
        lines.append("  Include route, mode (container/bulk/tanker), transit_days, URL")

    if "supply_disruption" in signal_types:
        lines.append("- SUPPLY_DISRUPTION: Plant outages, force majeure, capacity changes")
        lines.append("  Include affected producer, expected duration, capacity impact, URL")

    if "geopolitical" in signal_types:
        lines.append("- GEOPOLITICAL: Trade policy changes, tariffs, sanctions, export bans")
        lines.append("  Include country, policy, effective date, impact assessment, URL")

    lines.append("")
    lines.append("OUTPUT FORMAT: Save JSON to ~/crawl/output/ with this structure:")
    lines.append("  {")
    lines.append("    product_name, grade_code, commodity_family,")
    lines.append(f"    target_markets[], research_date, research_region, lookback_days: {lookback},")
    lines.append("    signals: [")
    lines.append("      {signal_id, type: 'news', headline, sentiment, source, date, url, summary},")
    lines.append("      {signal_id, type: 'price_index', value_type: 'level|delta|pct', market, price_low, price_high, price_mid, currency, unit, basis, port, date, url, source},")
    lines.append("      {signal_id, type: 'freight', sub_type: 'base_rate|surcharge|all_in', route, rate (numeric), currency, rate_unit, mode, transit_days, date, url, source},")
    lines.append("      {signal_id, type: 'supply_disruption', producer, country, event, duration, capacity_impact, date, url, source},")
    lines.append("      {signal_id, type: 'geopolitical', country, policy, effective_date, impact, url, source}")
    lines.append("    ],")
    lines.append("    coverage_score: 0-100 (% of requested signal_types with data found),")
    lines.append("    sources: [{name, url (REQUIRED), accessed_date, data_quality}]")
    lines.append("  }")

    prompt = "\n".join(lines)
    return verify_prompt_clean(prompt)


# ---------------------------------------------------------------------------
# SSH Dispatch
# ---------------------------------------------------------------------------

def _inject_request_id(local_file: Path, request_id: str) -> bool:
    """Inject request_id into a downloaded blob JSON before uploading.
    Returns True if injection succeeded."""
    if not request_id or not local_file.exists():
        return False
    try:
        with open(local_file) as f:
            data = json.load(f)
        data["request_id"] = request_id
        with open(local_file, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return True
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to inject request_id into %s: %s", local_file, e)
        return False


def _run_ssh_research(job_id: str, region: str, prompt: str, scenario: str, attempt: int = 0):
    """Synchronous SSH dispatch — runs in thread pool, never blocks event loop."""
    vm = VM_CONFIG[region]
    # Read initial job state (non-critical read — just for entity_snake + request_id)
    job = load_job(job_id)
    request_id = job.get("request_id")
    ssh = None

    try:
        update_job_fields(job_id, {
            "status": "running",
            "retry_count": attempt,
            "_set_region_status": (region, "running", None),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        log.info("Job %s [%s/%s] attempt %d: SSH to %s@%s",
                 job_id[:8], scenario, region, attempt, vm["user"], vm["ip"])
        _ssh_start = time.monotonic()
        log_job_event(job_id, "dispatched", scenario=scenario, region=region, status="running",
                      details={"attempt": attempt, "vm_ip": vm["ip"]})

        ssh = paramiko.SSHClient()
        ssh.load_host_keys(SSH_KNOWN_HOSTS)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(
            hostname=vm["ip"],
            username=vm["user"],
            key_filename=SSH_KEY_PATH,
            timeout=15,
        )

        # Get full gateway token
        _, stdout, _ = ssh.exec_command(
            "python3 -c \"import json; print(json.load(open("
            f"'/home/{vm['user']}/.openclaw/openclaw.json'))['gateway']['auth']['token'])\""
        )
        full_token = stdout.read().decode().strip()

        # Build the openclaw agent command
        safe_prompt = prompt.replace("'", "'\\''")
        session_id = f"{scenario}-{job_id[:8]}"

        cmd = (
            f"source ~/.bashrc 2>/dev/null; "
            f"export OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1 && "
            f"export OPENCLAW_GATEWAY_TOKEN={full_token} && "
            f"openclaw agent --agent main --session-id {session_id} "
            f"--thinking high --timeout 900 --json "
            f"--message '{safe_prompt}'"
        )

        _, stdout, stderr = ssh.exec_command(cmd, timeout=960)
        result_raw = stdout.read().decode()
        error_raw = stderr.read().decode()

        # Guard: empty response from agent (agent crashed, timed out, or never started)
        if not result_raw.strip():
            err_msg = (
                f"{region}: agent returned empty response "
                f"(stderr: {error_raw[:300].strip() or 'none'})"
            )
            log.error("Job %s [%s/%s]: %s", job_id[:8], scenario, region, err_msg)
            update_job_fields(job_id, {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "_set_region_status": (region, "failed", err_msg),
                "_append_error": err_msg,
            })
            return load_job(job_id)

        # Parse result
        try:
            result = json.loads(result_raw)
            status = result.get("status", "unknown")
            payloads = result.get("result", {}).get("payloads", [])
            summary = payloads[-1].get("text", "") if payloads else ""

            # Build filenames — strip ALL non-alphanumeric chars (except underscores)
            # so our expected name matches what OpenClaw agents actually write.
            # Agents typically strip parens, brackets, etc. and may append country code.
            def _to_snake(name: str) -> str:
                s = name.lower().replace(" ", "_")
                s = re.sub(r"[^a-z0-9_]", "", s)  # drop parens, dots, commas, etc.
                s = re.sub(r"_+", "_", s).strip("_")  # collapse multiple underscores
                return s

            if scenario == "cir":
                entity_snake = _to_snake(job["entity_name"])
            else:
                entity_snake = _to_snake(job.get("product_name", "product"))

            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            remote_filename = f"{entity_snake}_{date_str}.json"
            blob_name = f"{scenario}/{region}/{remote_filename}"

            # SFTP the report from remote VM to crawldevvm
            local_file = LOCAL_OUTPUT_DIR / f"{region}_{remote_filename}"
            remote_path = f"/home/{vm['user']}/crawl/output/{remote_filename}"

            sftp = ssh.open_sftp()
            try:
                sftp.get(remote_path, str(local_file))
            except FileNotFoundError:
                # Fuzzy match — normalize both sides so "sinochem_(singapore)"
                # matches "sinochem_singapore_sg" that agents actually write.
                remote_files = sftp.listdir(f"/home/{vm['user']}/crawl/output/")

                # First pass: today-dated files, normalized comparison
                match = [
                    f for f in remote_files
                    if entity_snake in _to_snake(f) and date_str in f
                ]
                if not match:
                    # Second pass: any date, normalized — warn about staleness
                    match = [f for f in remote_files if entity_snake in _to_snake(f)]
                    if match:
                        match.sort(reverse=True)
                        log.warning(
                            "Job %s [%s/%s]: no today-dated file found, "
                            "using best match: %s (may be stale)",
                            job_id[:8], scenario, region, match[0]
                        )
                if match:
                    sftp.get(f"/home/{vm['user']}/crawl/output/{match[0]}", str(local_file))
                    blob_name = f"{scenario}/{region}/{match[0]}"
                else:
                    log.error(
                        "Job %s [%s/%s]: no output file found on remote VM. "
                        "Available files: %s",
                        job_id[:8], scenario, region,
                        [f for f in remote_files if f.endswith(".json")][:10]
                    )
            sftp.close()
            _ssh_dur = int((time.monotonic() - _ssh_start) * 1000)
            log_job_event(job_id, "ssh_complete", scenario=scenario, region=region,
                          duration_ms=_ssh_dur,
                          details={"tokens": payloads[-1].get("usage", {}).get("totalTokens", 0) if payloads else 0,
                                   "file": str(local_file.name) if local_file.exists() else None,
                                   "file_size": local_file.stat().st_size if local_file.exists() else 0})

            # Guard: 0-byte SFTP result means the report wasn't actually
            # retrieved — don't upload an empty blob and call it success.
            if local_file.exists() and local_file.stat().st_size == 0:
                log.error(
                    "Job %s [%s/%s]: SFTPed file is 0 bytes — report not "
                    "retrieved. Removing empty file.",
                    job_id[:8], scenario, region,
                )
                local_file.unlink()  # remove so downstream treats as missing

            # Inject request_id into the blob body so it's self-contained
            if local_file.exists():
                _inject_request_id(local_file, request_id)

            # Upload to blob from crawldevvm
            blob_error = None
            if local_file.exists() and _BLOB_SAS_TOKEN:
                upload_result = subprocess.run(
                    [
                        "az", "storage", "blob", "upload",
                        "--account-name", BLOB_ACCOUNT,
                        "--container-name", BLOB_CONTAINER,
                        "--name", blob_name,
                        "--file", str(local_file),
                        "--sas-token", _BLOB_SAS_TOKEN,
                        "--overwrite",
                    ],
                    capture_output=True, text=True, timeout=60,
                )
                if upload_result.returncode != 0:
                    blob_error = f"Blob upload failed: {upload_result.stderr[:200]}"
            elif not local_file.exists():
                blob_error = f"Report file not found on remote VM ({region})"
            elif not _BLOB_SAS_TOKEN:
                blob_error = "SAS token not configured (check Key Vault)"

            this_blob = f"{BLOB_CONTAINER}/{blob_name}" if not blob_error else None
            region_status = "completed" if status == "ok" else "failed"
            if this_blob:
                log_job_event(job_id, "blob_uploaded", scenario=scenario, region=region,
                              details={"blob_path": this_blob,
                                       "size_bytes": local_file.stat().st_size if local_file.exists() else 0})
            elif blob_error:
                log_job_event(job_id, "blob_failed", scenario=scenario, region=region,
                              error=blob_error)

            # Thread-safe update — per-region status + atomic blob_path append
            updates = {
                "report_summary": summary,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "_set_region_status": (region, region_status, blob_error),
            }
            if blob_error:
                updates["_append_error"] = f"{region}: {blob_error}"

            # For single-region jobs, set blob_path directly.
            # For fan-out jobs, use atomic append to blob_paths[].
            if job.get("regions"):
                if this_blob:
                    updates["_append_blob_path"] = this_blob
                # Don't set overall status here — dispatch_fanout handles it
            else:
                updates["status"] = region_status
                updates["blob_path"] = this_blob

            update_job_fields(job_id, updates)

            _total_dur = int((time.monotonic() - _ssh_start) * 1000)
            log_job_event(job_id, "region_complete", scenario=scenario, region=region,
                          status=region_status, duration_ms=_total_dur,
                          details={"blob_path": this_blob, "attempt": attempt})

            log.info("Job %s [%s/%s] attempt %d: %s (blob: %s)",
                     job_id[:8], scenario, region, attempt, region_status, this_blob)

        except json.JSONDecodeError:
            err_msg = f"Failed to parse response from {region}: {result_raw[:300]}"
            log.error("Job %s [%s/%s]: %s", job_id[:8], scenario, region, err_msg)
            log_job_event(job_id, "parse_error", scenario=scenario, region=region,
                          status="failed", error=err_msg[:500])
            updates = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "_set_region_status": (region, "failed", err_msg),
                "_append_error": err_msg,
            }
            if not job.get("regions"):
                updates["status"] = "failed"
            update_job_fields(job_id, updates)

    except Exception as e:
        err_msg = f"{region}: {str(e)[:400]}"
        log.error("Job %s [%s/%s] attempt %d failed: %s", job_id[:8], scenario, region, attempt, e)
        log_job_event(job_id, "dispatch_error", scenario=scenario, region=region,
                      status="failed", error=str(e)[:500],
                      details={"attempt": attempt, "exception_type": type(e).__name__})
        updates = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "_set_region_status": (region, "failed", str(e)[:400]),
            "_append_error": err_msg,
        }
        if not job.get("regions"):
            updates["status"] = "failed"
        update_job_fields(job_id, updates)
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

    return load_job(job_id)


def _run_darkweb_enrichment(job_id: str, entity_name: str, country: str,
                            owners: list[str] = None, domain: str = None) -> dict:
    """
    Synchronous call to the dark-web VM gateway. Returns dark-web findings dict.
    Called from thread pool — never blocks event loop.
    Injects findings into the CIR blob JSON on disk before blob upload.
    """
    vm = DARKWEB_VM
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.load_host_keys(SSH_KNOWN_HOSTS)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(
            hostname=vm["ip"],
            username="copapadmin",
            key_filename=SSH_KEY_PATH,
            timeout=15,
        )

        req_body = json.dumps({
            "entity_name": entity_name,
            "country": country,
            "owners": owners or [],
            "domain": domain or "",
            "depth": "heavy",
        })
        safe_body = req_body.replace("'", "'\\''")

        cmd = (
            f"curl -s -X POST http://127.0.0.1:{vm['port']}/api/v1/research "
            f"-H 'Content-Type: application/json' "
            f"-H 'X-API-Key: {vm['api_key']}' "
            f"-d '{safe_body}'"
        )

        _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
        result_raw = stdout.read().decode()

        if not result_raw.strip():
            log.warning("Job %s: dark-web enrichment returned empty", job_id[:8])
            return {"status": "failed", "findings": [], "error": "empty response"}

        dw_result = json.loads(result_raw)
        dw_job_id = dw_result.get("job_id", "")

        # Fetch the full result file
        _, stdout2, _ = ssh.exec_command(
            f"cat /home/copapadmin/crawl/output/{dw_job_id}.json", timeout=30
        )
        full_raw = stdout2.read().decode()

        if full_raw.strip():
            full_result = json.loads(full_raw)
            log.info("Job %s: dark-web enrichment completed — %d findings from %d sources",
                     job_id[:8],
                     full_result.get("summary", {}).get("total_findings", 0),
                     full_result.get("summary", {}).get("sources_searched", 0))
            return full_result

        return {"status": "completed", "findings": [], "summary": dw_result.get("summary", {})}

    except Exception as e:
        log.error("Job %s: dark-web enrichment failed: %s", job_id[:8], e)
        return {"status": "failed", "findings": [], "error": str(e)[:300]}
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


def _inject_darkweb_into_blob(local_file: Path, darkweb_data: dict) -> bool:
    """
    Inject dark-web findings into a CIR blob JSON — prominently.

    Adds:
      1. dark_web_screening section (same level as sanctions_screening, adverse_media)
      2. Appends dark web risk line to executive_summary
      3. Appends dark web flag to risk_assessment
      4. Keeps raw dark_web_intelligence for analyst drill-down
    """
    if not local_file.exists() or local_file.stat().st_size == 0:
        return False
    try:
        with open(local_file) as f:
            blob = json.load(f)

        summary = darkweb_data.get("summary", {})
        findings = darkweb_data.get("findings", [])
        total = summary.get("total_findings", 0)
        sources_searched = summary.get("sources_searched", 0)
        sources_hit = summary.get("sources_with_results", 0)
        by_source = summary.get("by_source", {})
        by_type = summary.get("by_type", {})

        # --- Classify risk level ---
        has_breach = any(f.get("type") in (
            "infostealer_exposure", "paste_dump", "exposed_service", "ransomware_victim"
        ) for f in findings)
        has_darknet = any(f.get("type") == "dark_web_mention" for f in findings)
        has_offshore = any(f.get("type") == "offshore_entity" for f in findings)
        has_sanctions = any(f.get("type") == "sanctions_pep" for f in findings)
        has_occrp = any(f.get("type") == "organized_crime_data" for f in findings)
        has_wikileaks = any(f.get("type") == "leaked_document" for f in findings)
        has_adverse = any(f.get("type") == "adverse_media" for f in findings)
        has_interpol = any(f.get("type") == "wanted_person" for f in findings)
        has_un_notice = any(f.get("type") == "un_sanctions_notice" for f in findings)
        has_debarment = any(f.get("type") == "debarment_record" for f in findings)
        has_code_leak = any(f.get("type") == "code_leak" for f in findings)

        if has_darknet or has_breach or has_sanctions or has_occrp or has_interpol or has_un_notice or has_debarment:
            risk_level = "CRITICAL"
        elif has_offshore or has_wikileaks or has_adverse or has_code_leak or total > 15:
            risk_level = "HIGH"
        elif total > 5:
            risk_level = "MEDIUM"
        elif total > 0:
            risk_level = "LOW"
        else:
            risk_level = "CLEAN"

        # --- Build key findings list (most important first) ---
        key_findings = []
        type_labels = {
            "wanted_person": "INTERPOL RED NOTICE",
            "un_sanctions_notice": "INTERPOL UN NOTICE",
            "debarment_record": "WORLD BANK DEBARMENT",
            "infostealer_exposure": "CREDENTIAL COMPROMISE",
            "ransomware_victim": "RANSOMWARE VICTIM",
            "dark_web_mention": "DARK WEB MENTION",
            "sanctions_pep": "SANCTIONS/PEP HIT",
            "organized_crime_data": "OCCRP HIT",
            "offshore_entity": "OFFSHORE ENTITY (ICIJ)",
            "leaked_document": "LEAKED DOCUMENT",
            "code_leak": "CODE/CREDENTIAL LEAK (GITHUB)",
            "paste_dump": "PASTE/LEAK DUMP",
            "exposed_service": "EXPOSED SERVICE",
            "adverse_media": "ADVERSE MEDIA",
            "telegram_mention": "TELEGRAM MENTION",
            "legal_record": "LEGAL RECORD",
            "breach_record": "BREACH RECORD",
            "certificate_transparency": "SSL CERTIFICATE",
            "corporate_record": "CORPORATE RECORD",
            "website_scan": "WEBSITE SCAN",
            "ip_abuse": "IP ABUSE REPORT",
            "social_mention": "SOCIAL MEDIA MENTION",
            "domain_reputation": "DOMAIN REPUTATION",
            "threat_intel": "THREAT INTELLIGENCE",
        }
        # Priority order for key findings
        priority_types = [
            "wanted_person", "un_sanctions_notice", "debarment_record",
            "infostealer_exposure", "ransomware_victim", "dark_web_mention",
            "sanctions_pep", "organized_crime_data", "offshore_entity",
            "leaked_document", "code_leak", "paste_dump", "exposed_service",
            "adverse_media", "breach_record",
        ]
        for ptype in priority_types:
            for f in findings:
                if f.get("type") == ptype:
                    label = type_labels.get(ptype, ptype.upper())
                    title = f.get("title", f.get("victim", f.get("domain", "")))[:120]
                    indiv = f" (owner: {f['searched_individual']})" if f.get("searched_individual") else ""
                    key_findings.append(f"[{label}] {title}{indiv}")
            if len(key_findings) >= 10:
                break

        # --- 1. Add dark_web_screening section (prominent, alongside other screenings) ---
        blob["dark_web_screening"] = {
            "risk_level": risk_level,
            "total_findings": total,
            "sources_searched": sources_searched,
            "sources_with_hits": sources_hit,
            "key_findings": key_findings[:10],
            "breakdown": {
                "interpol_red_notices": by_type.get("wanted_person", 0),
                "interpol_un_notices": by_type.get("un_sanctions_notice", 0),
                "worldbank_debarment": by_type.get("debarment_record", 0),
                "credential_compromise": by_type.get("infostealer_exposure", 0),
                "ransomware_victim": by_type.get("ransomware_victim", 0),
                "dark_web_mentions": by_type.get("dark_web_mention", 0),
                "sanctions_pep_hits": by_type.get("sanctions_pep", 0),
                "occrp_hits": by_type.get("organized_crime_data", 0),
                "offshore_entities": by_type.get("offshore_entity", 0),
                "leaked_documents": by_type.get("leaked_document", 0),
                "code_leaks": by_type.get("code_leak", 0),
                "paste_dumps": by_type.get("paste_dump", 0),
                "breach_records": by_type.get("breach_record", 0),
                "adverse_media": by_type.get("adverse_media", 0),
                "web_mentions": by_type.get("web_mention", 0),
                "ssl_certificates": by_type.get("certificate_transparency", 0),
                "corporate_records": by_type.get("corporate_record", 0),
                "website_scans": by_type.get("website_scan", 0),
                "social_mentions": by_type.get("social_mention", 0),
            },
            "sources_checked": [
                # Tor search engines (6)
                "Ahmia (.onion search)", "Torch (.onion search)", "Haystak (.onion search)",
                "DuckDuckGo via Tor", "DuckDuckGo adverse keywords", "Onion.live (Tor directory)",
                # Breach/credential (4)
                "Dehashed (breach DB)", "HIBP (Have I Been Pwned)", "LeakCheck", "BreachDirectory",
                # Leak/exposure (5)
                "Psbdmp (paste dumps)", "JustPaste.it", "LeakIX (exposed services)",
                "HudsonRock (infostealer DB)", "GitHub/Gist (code leaks)",
                # Ransomware (1)
                "Ransomlook (ransomware victims)",
                # Investigative & compliance (6)
                "OCCRP Aleph", "ICIJ Offshore Leaks", "OpenSanctions",
                "OpenCorporates", "Interpol Red Notices", "World Bank Debarment",
                # Document/social (5)
                "WikiLeaks", "Telegram channels", "Web Archive",
                "Court records (Tor-routed)", "Reddit",
                # Threat intel & infrastructure (10)
                "PulseDive", "FullHunt", "Greynoise", "Shodan",
                "VirusTotal", "AlienVault OTX", "AbuseIPDB",
                "crt.sh (Certificate Transparency)", "URLScan.io", "IntelligenceX",
            ],
        }

        # --- 2. Build dark web summary text ---
        if risk_level == "CLEAN":
            dw_text = f"DARK WEB SCREENING: CLEAN — {sources_searched} sources searched, no findings."
        else:
            top_hits = "; ".join(key_findings[:3])
            dw_text = (
                f"DARK WEB SCREENING: {risk_level} — "
                f"{total} findings across {sources_hit}/{sources_searched} sources. "
                f"Key: {top_hits}"
            )

        # Append to executive_summary (handles both dict and string formats)
        existing_summary = blob.get("executive_summary", "")
        if isinstance(existing_summary, dict):
            existing_summary["dark_web_screening"] = dw_text
            existing_summary["dark_web_risk"] = risk_level
            blob["executive_summary"] = existing_summary
        elif isinstance(existing_summary, str):
            blob["executive_summary"] = existing_summary + "\n\n" + dw_text
        else:
            blob["executive_summary"] = dw_text

        # --- 3. Build risk addendum ---
        if risk_level in ("CRITICAL", "HIGH"):
            risk_text = (
                f"DARK WEB RISK ({risk_level}): {total} findings detected. "
                f"Immediate review recommended. Key hits: {'; '.join(key_findings[:3])}"
            )
        elif risk_level == "MEDIUM":
            risk_text = (
                f"DARK WEB RISK (MEDIUM): {total} findings detected across "
                f"{sources_hit} sources. Review recommended."
            )
        else:
            risk_text = (
                f"DARK WEB RISK ({risk_level}): {total} findings. "
                f"No immediate action required."
            )

        # Append to risk_assessment (handles both dict and string)
        existing_risk = blob.get("risk_assessment", "")
        if isinstance(existing_risk, dict):
            existing_risk["dark_web"] = risk_text
            blob["risk_assessment"] = existing_risk
        elif isinstance(existing_risk, str):
            blob["risk_assessment"] = existing_risk + "\n\n" + risk_text
        else:
            blob["risk_assessment"] = risk_text

        # --- 4. Keep raw data for drill-down ---
        blob["dark_web_intelligence"] = {
            "status": darkweb_data.get("status", "unknown"),
            "sources_searched": sources_searched,
            "sources_with_results": sources_hit,
            "total_findings": total,
            "findings": findings,
            "source_status": darkweb_data.get("source_status", {}),
            "by_source": by_source,
            "by_type": by_type,
        }

        with open(local_file, "w") as f:
            json.dump(blob, f, indent=2, default=str)

        log.info("Injected dark-web screening into %s: %s risk, %d findings",
                 local_file.name, risk_level, total)
        return True
    except Exception as e:
        log.warning("Failed to inject dark-web data into %s: %s", local_file, e)
        return False


def _count_active_jobs() -> int:
    """Count jobs currently queued or running."""
    count = 0
    for f in JOBS_DIR.glob("*.json"):
        try:
            st = json.loads(f.read_text()).get("status", "")
            if st in ("queued", "running"):
                count += 1
        except Exception:
            pass
    return count


def _check_backpressure():
    """Raise 503 if too many jobs are already in-flight."""
    active = _count_active_jobs()
    if active >= _MAX_QUEUED_JOBS:
        raise HTTPException(
            status_code=503,
            detail=f"Server busy: {active} jobs in-flight (max {_MAX_QUEUED_JOBS}). Try again later.",
        )


async def dispatch_single(job_id: str, region: str, prompt: str, scenario: str):
    """Dispatch to one region. Retries once on failure. For CIR, also enriches with dark-web intel."""
    loop = asyncio.get_event_loop()
    job = await loop.run_in_executor(
        _ssh_pool, _run_ssh_research, job_id, region, prompt, scenario, 0
    )
    # Check per-region status for retry (not overall job status)
    region_st = (job.get("region_status") or {}).get(region, {})
    if region_st.get("status") == "failed" and MAX_RETRIES > 0:
        log.info("Job %s [%s/%s]: retrying...", job_id[:8], scenario, region)
        await asyncio.sleep(10)
        job = await loop.run_in_executor(
            _ssh_pool, _run_ssh_research, job_id, region, prompt, scenario, 1
        )

    # --- CIR dark-web enrichment ---
    # After CIR research completes, parse the blob for ALL discovered
    # entities/people/affiliates, then search them all on dark web.
    if scenario == "cir":
        job = load_job(job_id)
        if job.get("status") == "completed" or job.get("blob_path"):
            entity_name = job.get("entity_name", "")
            country = job.get("country", "")

            # Start with seed data
            owners = []
            seed = job.get("seed_data", {})
            for ind in seed.get("key_individuals", []):
                if isinstance(ind, dict) and ind.get("name"):
                    owners.append(ind["name"])
            domain = seed.get("entity_website", "")
            if domain:
                domain = domain.replace("https://", "").replace("http://", "").split("/")[0]

            # --- Parse the CIR blob for discovered entities/people ---
            def _to_snake(name: str) -> str:
                s = name.lower().replace(" ", "_")
                s = re.sub(r"[^a-z0-9_]", "", s)
                s = re.sub(r"_+", "_", s).strip("_")
                return s

            entity_snake = _to_snake(entity_name)
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            candidates = list(LOCAL_OUTPUT_DIR.glob(f"*{entity_snake}*{date_str}*.json"))
            if not candidates:
                candidates = list(LOCAL_OUTPUT_DIR.glob(f"*{entity_snake}*.json"))

            if candidates:
                blob_file = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                try:
                    with open(blob_file) as f:
                        cir_blob = json.load(f)

                    # Extract directors
                    cr = cir_blob.get("corporate_registry", {})
                    for d in cr.get("directors", []):
                        name = d.get("name", "")
                        if name and name not in owners:
                            owners.append(name)

                    # Extract shareholders (named individuals)
                    for sh in cr.get("shareholders", []):
                        name = sh.get("name", "")
                        sh_type = sh.get("type", "")
                        if name and name not in owners and sh_type in ("individual", "person", ""):
                            owners.append(name)

                    # Extract UBOs
                    bo = cir_blob.get("beneficial_ownership", {})
                    for u in bo.get("ubo_chain", bo.get("ubos", [])):
                        name = u.get("entity", u.get("name", ""))
                        if name and name not in owners:
                            owners.append(name)

                    # Extract trade names / affiliated entities for entity-level search
                    # (these get searched as additional entity names, not as owners)
                    affiliated_entities = []
                    trade_names = cr.get("trade_names", cir_blob.get("entity_trade_names", ""))
                    if isinstance(trade_names, str) and trade_names:
                        affiliated_entities.extend([t.strip() for t in trade_names.split(",") if t.strip()])
                    elif isinstance(trade_names, list):
                        affiliated_entities.extend(trade_names)

                    # Known affiliates from seed + discovered
                    for aff in seed.get("known_affiliates", []):
                        aname = aff.get("entity_name", "") if isinstance(aff, dict) else ""
                        if aname and aname not in affiliated_entities:
                            affiliated_entities.append(aname)

                    # Extract domain from website if not provided
                    if not domain:
                        website = cr.get("website", cir_blob.get("entity_website", ""))
                        if website:
                            domain = website.replace("https://", "").replace("http://", "").split("/")[0]

                    log.info(
                        "Job %s [cir]: parsed blob — %d owners, %d affiliates, domain=%s",
                        job_id[:8], len(owners), len(affiliated_entities), domain or "none"
                    )

                    # Add affiliated entities to owners list for dark web search
                    # (they'll be searched individually across investigative DBs)
                    for ae in affiliated_entities:
                        if ae not in owners:
                            owners.append(ae)

                except Exception as e:
                    log.warning("Job %s [cir]: failed to parse blob for enrichment: %s",
                                job_id[:8], e)

            log.info("Job %s [cir]: starting dark-web enrichment for %s + %d targets",
                     job_id[:8], entity_name, len(owners))
            log_job_event(job_id, "darkweb_start", scenario="cir",
                          details={"entity": entity_name, "targets": len(owners), "domain": domain})
            _dw_start = time.monotonic()

            try:
                dw_data = await loop.run_in_executor(
                    _ssh_pool, _run_darkweb_enrichment,
                    job_id, entity_name, country, owners, domain
                )

                # Inject dark-web findings into the local CIR blob file
                # (blob_file already found above during parsing)
                if candidates:
                    local_file = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                    _inject_darkweb_into_blob(local_file, dw_data)

                    # Re-upload the enriched blob
                    blob_path = job.get("blob_path", "")
                    if blob_path and _BLOB_SAS_TOKEN:
                        # blob_path is like "osint-staging/cir/americas/entity_20260429.json"
                        blob_name = blob_path.replace(f"{BLOB_CONTAINER}/", "")
                        subprocess.run(
                            [
                                "az", "storage", "blob", "upload",
                                "--account-name", BLOB_ACCOUNT,
                                "--container-name", BLOB_CONTAINER,
                                "--name", blob_name,
                                "--file", str(local_file),
                                "--sas-token", _BLOB_SAS_TOKEN,
                                "--overwrite",
                            ],
                            capture_output=True, text=True, timeout=60,
                        )
                        log.info("Job %s [cir]: re-uploaded enriched blob with dark-web data",
                                 job_id[:8])

                # Log dark-web completion
                _dw_dur = int((time.monotonic() - _dw_start) * 1000)
                dw_summary = dw_data.get("summary", {})
                log_job_event(job_id, "darkweb_complete", scenario="cir",
                              duration_ms=_dw_dur,
                              details={"findings": dw_summary.get("total_findings", 0),
                                       "sources": dw_summary.get("sources_searched", 0),
                                       "status": dw_data.get("status", "unknown")})

                # Update job with dark-web summary + prepend banner to report_summary
                dw_total = dw_summary.get("total_findings", 0)
                dw_sources = dw_summary.get("sources_searched", 0)
                dw_hits = dw_summary.get("sources_with_results", 0)
                dw_by_type = dw_summary.get("by_type", {})

                # Classify
                dw_findings_list = dw_data.get("findings", [])
                _has = lambda t: any(f.get("type") == t for f in dw_findings_list)
                if _has("wanted_person") or _has("un_sanctions_notice") or _has("debarment_record") or _has("infostealer_exposure") or _has("dark_web_mention") or _has("ransomware_victim") or _has("sanctions_pep") or _has("organized_crime_data"):
                    dw_risk = "CRITICAL"
                elif _has("offshore_entity") or _has("leaked_document") or _has("adverse_media") or _has("code_leak") or dw_total > 15:
                    dw_risk = "HIGH"
                elif dw_total > 5:
                    dw_risk = "MEDIUM"
                elif dw_total > 0:
                    dw_risk = "LOW"
                else:
                    dw_risk = "CLEAN"

                # Build key findings for banner
                _type_labels = {
                    "wanted_person": "INTERPOL RED NOTICE",
                    "un_sanctions_notice": "INTERPOL UN NOTICE",
                    "debarment_record": "WORLD BANK DEBARMENT",
                    "infostealer_exposure": "CREDENTIAL COMPROMISE",
                    "ransomware_victim": "RANSOMWARE VICTIM",
                    "dark_web_mention": "DARK WEB MENTION",
                    "sanctions_pep": "SANCTIONS/PEP HIT",
                    "organized_crime_data": "OCCRP HIT",
                    "offshore_entity": "OFFSHORE ENTITY (ICIJ)",
                    "leaked_document": "LEAKED DOCUMENT",
                    "code_leak": "CODE/CREDENTIAL LEAK (GITHUB)",
                    "paste_dump": "PASTE/LEAK DUMP",
                    "exposed_service": "EXPOSED SERVICE",
                    "adverse_media": "ADVERSE MEDIA",
                    "breach_record": "BREACH RECORD",
                    "certificate_transparency": "SSL CERTIFICATE",
                    "corporate_record": "CORPORATE RECORD",
                }
                _priority = [
                    "wanted_person", "un_sanctions_notice", "debarment_record",
                    "infostealer_exposure", "ransomware_victim", "dark_web_mention",
                    "sanctions_pep", "organized_crime_data", "offshore_entity",
                    "leaked_document", "code_leak", "paste_dump", "exposed_service",
                    "adverse_media", "breach_record",
                ]
                key_hits = []
                for pt in _priority:
                    for f in dw_findings_list:
                        if f.get("type") == pt:
                            lbl = _type_labels.get(pt, pt.upper())
                            ttl = f.get("title", f.get("victim", f.get("domain", "")))[:80]
                            ind = f" ({f['searched_individual']})" if f.get("searched_individual") else ""
                            key_hits.append(f"{lbl}: {ttl}{ind}")
                    if len(key_hits) >= 5:
                        break

                # Build banner
                banner_lines = [
                    "",
                    "---",
                    "",
                    f"## DARK WEB SCREENING — {dw_risk}",
                    f"**{dw_total} findings** from {dw_hits}/{dw_sources} sources via Tor (Netherlands exit node)",
                    "",
                ]
                if dw_risk == "CLEAN":
                    banner_lines.append(f"No dark web exposure detected. Entity and owners clean across all {dw_sources} sources.")
                else:
                    banner_lines.append("| Category | Count |")
                    banner_lines.append("|----------|-------|")
                    for cat, label in [
                        ("wanted_person", "Interpol Red Notices"),
                        ("un_sanctions_notice", "Interpol UN Notices"),
                        ("debarment_record", "World Bank Debarment"),
                        ("infostealer_exposure", "Credential compromise"),
                        ("ransomware_victim", "Ransomware victim"),
                        ("dark_web_mention", "Dark web mentions"),
                        ("sanctions_pep", "Sanctions/PEP hits"),
                        ("organized_crime_data", "OCCRP hits"),
                        ("offshore_entity", "Offshore entities (ICIJ)"),
                        ("leaked_document", "Leaked documents"),
                        ("code_leak", "Code/credential leaks (GitHub)"),
                        ("paste_dump", "Paste/leak dumps"),
                        ("breach_record", "Breach records"),
                        ("adverse_media", "Adverse media"),
                        ("certificate_transparency", "SSL certificates (crt.sh)"),
                        ("corporate_record", "Corporate records"),
                        ("web_mention", "Web mentions (Tor-routed)"),
                        ("social_mention", "Social media mentions"),
                        ("website_scan", "Website scans (URLScan)"),
                    ]:
                        cnt = dw_by_type.get(cat, 0)
                        if cnt > 0:
                            banner_lines.append(f"| {label} | {cnt} |")
                    banner_lines.append("")
                    if key_hits:
                        banner_lines.append("**Key findings:**")
                        for kh in key_hits:
                            banner_lines.append(f"- {kh}")
                    banner_lines.append("")

                banner_lines.append("*Full dark web data in blob: `dark_web_screening` + `dark_web_intelligence`*")
                banner_lines.append("")
                banner_lines.append("---")

                dw_banner = "\n".join(banner_lines)

                # Prepend banner to existing report_summary
                job_now = load_job(job_id)
                existing_summary = job_now.get("report_summary", "") or ""
                new_summary = existing_summary + dw_banner

                update_job_fields(job_id, {
                    "dark_web_findings": dw_total,
                    "dark_web_sources": dw_sources,
                    "report_summary": new_summary,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

            except Exception as e:
                log.error("Job %s [cir]: dark-web enrichment failed: %s", job_id[:8], e)
                update_job_fields(job_id, {
                    "dark_web_error": str(e)[:300],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })


async def dispatch_fanout(job_id: str, regions: list[str], prompts: dict[str, str], scenario: str):
    """
    Fan-out dispatch to multiple regions simultaneously.
    prompts is {region: prompt_text} — each region gets its own prompt.
    """
    tasks = []
    for region in regions:
        prompt = prompts[region]
        tasks.append(dispatch_single(job_id, region, prompt, scenario))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any unhandled exceptions from asyncio.gather
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            region = regions[i]
            log.error("Job %s [%s/%s]: unhandled exception in dispatch: %s",
                      job_id[:8], scenario, region, r)
            update_job_fields(job_id, {
                "_set_region_status": (region, "failed", str(r)[:400]),
                "_append_error": f"{region}: unhandled: {str(r)[:300]}",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    # Final status: completed / partial_success / failed
    job = load_job(job_id)
    blob_count = len(job.get("blob_paths") or [])
    region_count = len(regions)

    rs = job.get("region_status") or {}
    succeeded = [r for r, s in rs.items() if s.get("status") == "completed"]
    failed = [r for r, s in rs.items() if s.get("status") == "failed"]
    missing = [r for r in regions if r not in rs]

    if blob_count == region_count:
        job["status"] = "completed"
    elif blob_count > 0:
        job["status"] = "partial_success"
        job["error"] = f"{len(failed)} of {region_count} regions failed: {', '.join(failed + missing)}"
    else:
        job["status"] = "failed"
        if not job.get("error"):
            job["error"] = "All regions failed"

    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_job(job)

    log_job_event(job_id, "completed", scenario=scenario, status=job["status"],
                  details={"blobs": blob_count, "regions": region_count,
                           "succeeded": succeeded, "failed": failed, "missing": missing})

    log.info(
        "Job %s [%s] fan-out complete: %d/%d blobs. "
        "succeeded=%s failed=%s missing=%s",
        job_id[:8], scenario, blob_count, region_count,
        succeeded or "none", failed or "none", missing or "none"
    )


# ---------------------------------------------------------------------------
# Scenario Registry
# ---------------------------------------------------------------------------

SCENARIOS = {
    "cir": {
        "name": "Counterparty Intelligence Report",
        "description": "Due diligence research on a counterparty entity",
        "routing": "single",  # single region based on entity_country
        "skill": "counterparty_research",
        "blob_prefix": "cir",
    },
    "product-intel": {
        "name": "Product Market Intelligence",
        "description": "Product pricing, sourcing, competitor, and regulatory intelligence",
        "routing": "fanout",  # fan-out to all regions covering target_markets
        "skill": "product_intel",
        "blob_prefix": "product-intel",
    },
    "dark-web": {
        "name": "Dark Web Intelligence",
        "description": "Tor-routed OSINT: dark web mentions, leak/breach databases, paste dumps, "
                       "offshore leaks, sanctions, ransomware victims, adverse media (16 sources)",
        "routing": "darkweb",  # direct HTTP to crawl-darkweb VM (no OpenClaw)
        "skill": None,
        "blob_prefix": "dark-web",
    },
}


# ---------------------------------------------------------------------------
# Endpoints — v1 (backward compatible CIR)
# ---------------------------------------------------------------------------

@app.post("/api/v1/research", response_model=JobResponse)
async def submit_research_v1(req: CIRRequest, _: str = Depends(verify_api_key)):
    """
    BACKWARD COMPATIBLE — Submit counterparty for DD research.
    Internally routes through the scenario framework as scenario=cir.
    """
    payload = req.model_dump(exclude_none=True)
    return await _handle_cir(payload)


@app.get("/api/v1/research/{job_id}", response_model=JobResponse)
async def get_research_status(job_id: str, _: str = Depends(verify_api_key)):
    job = load_job(job_id)
    return _job_to_response(job)


@app.post("/api/v1/research/{job_id}/review", response_model=JobResponse)
async def submit_review(job_id: str, review: ReviewRequest, _: str = Depends(verify_api_key)):
    job = load_job(job_id)
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail=f"Job is {job['status']}, not completed")
    job["review"] = {
        "reviewer": review.reviewer,
        "score": review.score,
        "notes": review.notes,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_job(job)
    log.info("Job %s reviewed by %s: score=%d", job_id[:8], review.reviewer, review.score)
    return _job_to_response(job)


@app.get("/api/v1/research")
async def list_research_jobs(limit: int = 50, _: str = Depends(verify_api_key)):
    jobs = list_jobs(limit)
    return [_job_to_response(j) for j in jobs]


@app.get("/api/v1/reviews")
async def list_reviews(_: str = Depends(verify_api_key)):
    jobs = list_jobs(limit=500)
    reviewed = [j for j in jobs if j.get("review")]
    summary = {
        "total_reviewed": len(reviewed),
        "avg_score": round(sum(j["review"]["score"] for j in reviewed) / len(reviewed), 1) if reviewed else 0,
        "by_region": {},
        "reviews": [],
    }
    for j in reviewed:
        region = j.get("region", "unknown")
        if region not in summary["by_region"]:
            summary["by_region"][region] = {"count": 0, "total_score": 0, "avg": 0}
        summary["by_region"][region]["count"] += 1
        summary["by_region"][region]["total_score"] += j["review"]["score"]
        summary["by_region"][region]["avg"] = round(
            summary["by_region"][region]["total_score"] / summary["by_region"][region]["count"], 1
        )
        summary["reviews"].append({
            "job_id": j["job_id"],
            "entity": j.get("entity_name", j.get("product_name", "")),
            "region": region,
            "scenario": j.get("scenario", "cir"),
            "score": j["review"]["score"],
            "reviewer": j["review"]["reviewer"],
            "reviewed_at": j["review"]["reviewed_at"],
            "notes": j["review"].get("notes"),
        })
    return summary


# ---------------------------------------------------------------------------
# Endpoints — v1 Generic (new scenario gateway)
# ---------------------------------------------------------------------------

@app.post("/api/v1/jobs", response_model=JobResponse)
async def submit_job(req: JobRequest, _: str = Depends(verify_api_key)):
    """
    Generic job submission endpoint. Routes to scenario-specific handler.

    Scenarios:
      - cir: Counterparty Intelligence Report
      - product-intel: Product market intelligence

    All payloads are sanitized before dispatch. Internal fields
    (copap_relationship, source_report, etc.) are stripped automatically.
    No identifying information about the requesting organization reaches
    the research agents.
    """
    scenario = req.scenario.value
    payload = req.payload

    if scenario == "cir":
        return await _handle_cir(payload)
    elif scenario == "product-intel":
        return await _handle_product_intel(payload)
    elif scenario == "dark-web":
        return await _handle_dark_web(payload)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown scenario: {scenario}")


@app.post("/v1/market-signal", response_model=JobResponse)
async def submit_market_signal(req: dict, _: str = Depends(verify_api_key)):
    """
    Product intel convenience endpoint — matches the productintel team's contract.
    Accepts product-intel payload directly (no scenario wrapper needed).

    Equivalent to POST /api/v1/jobs with scenario="product-intel".
    Auth: X-API-Key header OR Authorization: Bearer <token>.
    """
    return await _handle_product_intel(req)


@app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
async def get_job_status(job_id: str, _: str = Depends(verify_api_key)):
    """Check status of any job (any scenario)."""
    job = load_job(job_id)
    return _job_to_response(job)


@app.get("/api/v1/jobs")
async def list_all_jobs(
    limit: int = 50,
    scenario: Optional[str] = None,
    _: str = Depends(verify_api_key),
):
    """List recent jobs, optionally filtered by scenario."""
    jobs = list_jobs(limit, scenario)
    return [_job_to_response(j) for j in jobs]


# ---------------------------------------------------------------------------
# Endpoints — System
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
async def health():
    return {
        "status": "ok",
        "service": "crawl-research-gateway",
        "version": API_VERSION,
        "scenarios": list(SCENARIOS.keys()),
        "regions": list(VM_CONFIG.keys()),
        "active_threads": len(_ssh_pool._threads) if hasattr(_ssh_pool, '_threads') else 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/scenarios")
async def list_scenarios():
    """List available research scenarios and their configurations."""
    return {
        name: {
            "name": cfg["name"],
            "description": cfg["description"],
            "routing": cfg["routing"],
        }
        for name, cfg in SCENARIOS.items()
    }


@app.get("/api/v1/regions")
async def list_regions(_: str = Depends(verify_api_key)):
    regions = {}
    for code, region in JURISDICTION_MAP.items():
        regions.setdefault(region, []).append(code)
    return {
        "regions": {r: sorted(codes) for r, codes in sorted(regions.items())},
    }


# ---------------------------------------------------------------------------
# Real-time Entity Verification  (no OpenClaw — direct registry lookups)
# ---------------------------------------------------------------------------

_SECP_URL = "https://eservices.secp.gov.pk/eServices/ControllerServlet"
_SECP_REFERER = "https://eservices.secp.gov.pk/eServices/NameSearch.jsp"
_SECP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _secp_query_via_ssh(entity_name: str) -> dict:
    """
    Query SECP eServices from Gulf VM via SSH.
    Tries collapsed name first (AGROCHINA) then original (AGRO CHINA).
    Uses both NameSearch and CTC endpoints for richest data.
    """
    vm = VM_CONFIG["gulf"]
    ssh = paramiko.SSHClient()
    ssh.load_host_keys(SSH_KNOWN_HOSTS)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        ssh.connect(hostname=vm["ip"], username=vm["user"],
                    key_filename=SSH_KEY_PATH, timeout=15)

        # Build search variants — SECP stores names inconsistently:
        # "AGROCHINA PAKISTAN" not "AGRO CHINA PAKISTAN"
        # Try: full collapsed, partial collapses, and original with spaces
        original = entity_name.strip().upper()
        full_collapsed = re.sub(r'\s+', '', original)
        words = original.split()
        variants = []
        # Full collapse (AGROCHINAPAKISTAN)
        variants.append(full_collapsed)
        # Progressive collapses: first 2 words joined, first 3, etc.
        for n in range(2, len(words)):
            partial = "".join(words[:n]) + " " + " ".join(words[n:])
            partial = partial.strip()
            if partial not in variants:
                variants.append(partial)
        # Original with spaces
        if original not in variants:
            variants.append(original)
        # Also try without common suffixes (PVT, LTD, PRIVATE, LIMITED, etc.)
        stripped = re.sub(
            r'\b(PVT|PRIVATE|LTD|LIMITED|INC|CORP|LLC|PLC|COMPANY|CO)\b\.?\s*',
            '', original
        ).strip()
        if stripped and stripped not in variants:
            variants.append(stripped)

        for name in variants:
            safe = name.replace("'", "'\\''").replace('"', '')

            # --- NameSearch (gives reg date + form filing) ---
            ns_cmd = (
                f"curl -s --max-time 25 -X POST '{_SECP_URL}' "
                f"-d 'request_id=SEARCH_NAME&searchName={safe}"
                f"&searchOption=Beginning+With&requesterProcess=' "
                f"-H 'Referer: {_SECP_REFERER}' "
                f"-H 'User-Agent: {_SECP_UA}' "
                f"-H 'Content-Type: application/x-www-form-urlencoded'"
            )
            _, stdout, _ = ssh.exec_command(ns_cmd, timeout=35)
            ns_html = stdout.read().decode("utf-8", errors="replace")

            if "were found according to given criteria" not in ns_html:
                continue

            # Parse NameSearch table (8 cols: idx, name, status, CRO, reg_no, reg_date, form_ab, mandatory)
            ns_cells = re.findall(r'<TD class="tableText">([^<]*)', ns_html)
            if not ns_cells:
                continue

            # --- CTC search (gives company type + ACTIVE status + internal ref) ---
            ctc_cmd = (
                f"curl -s --max-time 25 -X POST '{_SECP_URL}' "
                f"-d 'request_id=CTC_SEARCH_COMPANY&searchName={safe}"
                f"&searchOption=Beginning+With&requesterProcess=null' "
                f"-H 'Referer: https://eservices.secp.gov.pk/eServices/CTC_CompanySearch.jsp' "
                f"-H 'User-Agent: {_SECP_UA}' "
                f"-H 'Content-Type: application/x-www-form-urlencoded'"
            )
            _, stdout2, _ = ssh.exec_command(ctc_cmd, timeout=35)
            ctc_html = stdout2.read().decode("utf-8", errors="replace")

            # Parse CTC onclick for company_type
            ctc_type = None
            ctc_match = re.search(
                r'onclick="opener\.setGridCellValue\(&quot;([^"]+)&quot;\)', ctc_html
            )
            if ctc_match:
                parts = ctc_match.group(1).split("~")
                # format: name~~reg_no~filing~company_type~status~internal~CRO
                for p in parts:
                    p = p.strip()
                    if p in ("Private Limited Company", "Public Unlisted Company",
                             "Public Listed Company", "Single Member Company",
                             "Limited Liability Partnership", "Not For Profit Association",
                             "Foreign Company", "Trade Organization"):
                        ctc_type = p
                        break

            # Build results from NameSearch rows
            results = []
            row_size = 8
            for i in range(0, len(ns_cells), row_size):
                row = ns_cells[i:i + row_size]
                if len(row) < 6:
                    continue
                results.append({
                    "legal_name": row[1].strip(),
                    "status": row[2].strip(),
                    "cro": row[3].strip(),
                    "registration_number": row[4].strip(),
                    "registration_date": row[5].strip(),
                    "form_ab_filed_upto": row[6].strip() if len(row) > 6 else None,
                    "mandatory_filing": row[7].strip() if len(row) > 7 else None,
                    "company_type": ctc_type,
                })

            if results:
                return {"found": True, "query": name, "results": results}

        return {"found": False, "query": entity_name, "results": []}

    except Exception as e:
        log.warning("SECP lookup failed: %s", e)
        return {"found": False, "query": entity_name, "results": [],
                "error": str(e)[:200]}
    finally:
        ssh.close()


def _fbr_ntn_via_ssh(ntn: str) -> dict:
    """Try FBR ATL check from Gulf VM. Degrades gracefully when FBR is down."""
    vm = VM_CONFIG["gulf"]
    safe_ntn = re.sub(r'[^0-9\-]', '', ntn)
    ssh = paramiko.SSHClient()
    ssh.load_host_keys(SSH_KNOWN_HOSTS)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        ssh.connect(hostname=vm["ip"], username=vm["user"],
                    key_filename=SSH_KEY_PATH, timeout=15)
        cmd = (
            f"curl -s --max-time 15 -o /dev/null "
            f"-w '%{{http_code}}' 'https://e.fbr.gov.pk/' 2>&1"
        )
        _, stdout, _ = ssh.exec_command(cmd, timeout=25)
        code = stdout.read().decode().strip()
        if code == "000":
            return {"ntn": ntn, "status": "UNAVAILABLE",
                    "note": "FBR portal is down — manual verification required"}
        return {"ntn": ntn, "status": "UNVERIFIED",
                "note": f"FBR returned HTTP {code} — CAPTCHA required for ATL lookup"}
    except Exception as e:
        return {"ntn": ntn, "status": "ERROR", "error": str(e)[:200]}
    finally:
        ssh.close()


# ---------------------------------------------------------------------------
# Bright Data residential proxy — gov site access
# ---------------------------------------------------------------------------

_BD_PROXY = "brd.superproxy.io:33335"
_BD_USER = "brd-customer-hl_7bf69e76-zone-pk_residental"
_BD_PASS = get_secret("brightdata-proxy-pass")
_BD_CERT = Path(os.path.expanduser("~/crawl/config/brd-ca.crt"))
_BD_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Bright Data Web Unlocker — for non-gov sites (Tofler etc.)
_BD_UNLOCKER_API = "https://api.brightdata.com/request"
_BD_UNLOCKER_KEY = get_secret("brightdata-api-key")
_BD_UNLOCKER_ZONE = "pakistan"

# Session counter for unique proxy sessions
_bd_session_counter = 0
_bd_session_lock = threading.Lock()


def _bd_next_session(prefix: str) -> str:
    """Generate unique session ID for Bright Data proxy."""
    global _bd_session_counter
    with _bd_session_lock:
        _bd_session_counter += 1
        return f"{prefix}{_bd_session_counter}"


def _bd_residential_fetch(url: str, country: str) -> str:
    """Fetch URL via Bright Data residential proxy with in-country IP."""
    session = _bd_next_session(f"v{country}")
    proxy_user = f"{_BD_USER}-country-{country}-session-{session}:{_BD_PASS}"
    cert_args = ["--cacert", str(_BD_CERT)] if _BD_CERT.exists() else ["-k"]
    result = subprocess.run(
        ["curl", "-s", "-L", "--max-time", "30",
         "--proxy", _BD_PROXY, "--proxy-user", proxy_user,
         *cert_args,
         "-H", f"User-Agent: {_BD_UA}",
         url],
        capture_output=True, text=True, timeout=40,
    )
    return result.stdout


def _bd_unlocker_fetch(url: str, country: str = "in") -> str:
    """Fetch URL via Bright Data Web Unlocker (for non-gov sites only)."""
    payload = json.dumps({
        "zone": _BD_UNLOCKER_ZONE, "country": country,
        "url": url, "format": "raw", "data_format": "markdown",
    })
    result = subprocess.run(
        ["curl", "-s", "--max-time", "45", "-X", "POST", _BD_UNLOCKER_API,
         "-H", "Content-Type: application/json",
         "-H", f"Authorization: Bearer {_BD_UNLOCKER_KEY}",
         "-d", payload],
        capture_output=True, text=True, timeout=55,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# Country-specific registry lookups via Bright Data residential proxy
# ---------------------------------------------------------------------------

def _india_tofler_lookup(entity_name: str, cin: str = "") -> dict:
    """Look up Indian company on Tofler via Bright Data Unlocker (non-gov, no policy block)."""
    tofler_url = None
    if cin:
        slug = re.sub(r'[^a-z0-9\-]', '', entity_name.strip().lower().replace(" ", "-").replace(".", ""))
        tofler_url = f"https://www.tofler.in/{slug}/company/{cin}"
    else:
        body = _bd_unlocker_fetch(f"https://www.google.com/search?q={entity_name.replace(' ', '+')}+site:tofler.in+CIN", "in")
        match = re.search(r'https://www\.tofler\.in/[^\s\)\"\']+/company/[A-Z0-9]+', body)
        if match:
            tofler_url = match.group(0)
        else:
            match2 = re.search(r'(https://www\.tofler\.in/[^\s\)\"\'<>]+)', body)
            if match2:
                tofler_url = match2.group(1)

    if not tofler_url:
        return {"found": False, "error": "Could not find company on Tofler.in"}

    log.info("India verify: fetching %s", tofler_url)
    body = _bd_unlocker_fetch(tofler_url, "in")
    if not body or len(body) < 200:
        return {"found": False, "error": "Empty response from Tofler"}

    result = {"found": False, "source": "Tofler.in (MCA21 data)", "source_url": tofler_url}

    cin_match = re.search(r'([UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6})', body)
    if cin_match:
        result["cin"] = cin_match.group(1)
        result["found"] = True

    name_match = re.search(r'# ([A-Z][A-Z\s\.\&\(\)]+(?:PRIVATE|PUBLIC|LLP|LIMITED|COMPANY)[A-Z\s\.\(\)]*)', body)
    if name_match:
        result["legal_name"] = name_match.group(1).strip()

    if re.search(r'\bActive\b', body):
        result["status"] = "Active"
    elif re.search(r'\bStruck Off\b', body, re.IGNORECASE):
        result["status"] = "Struck Off"

    inc_match = re.search(r'incorporated on (\d{1,2}\s+\w+,?\s+\d{4})', body, re.IGNORECASE)
    if inc_match:
        result["incorporation_date"] = inc_match.group(1)

    for label, val in [("private limited", "Private Limited Company"),
                       ("public limited", "Public Limited Company"),
                       ("limited liability", "Limited Liability Partnership")]:
        if label in body.lower():
            result["company_type"] = val
            break

    addr_match = re.search(r'registered address is (?:at )?(.*?\d{6})', body, re.IGNORECASE | re.DOTALL)
    if addr_match:
        result["registered_address"] = re.sub(r'\s+', ' ', addr_match.group(1).strip()).rstrip(",. ")

    dir_match = re.search(
        r'(?:has\s+\w+\s+directors?\s*[-–—]\s*|directors?\s*[-–—:]\s*|directors?\s+(?:are|includes?|consists?\s+of)\s*[-–—:]?\s*)(.*?)(?:\.\s|$)',
        body, re.IGNORECASE)
    if dir_match:
        names = re.split(r',\s*(?:and\s+)?|\s+and\s+', dir_match.group(1))
        result["directors"] = [n.strip() for n in names if n.strip() and len(n.strip()) > 2]

    auth_m = re.search(r'authorized share capital is INR ([\d,\.]+\s*(?:lac|cr|lakh|crore)?)', body, re.IGNORECASE)
    paid_m = re.search(r'paid-up capital is INR ([\d,\.]+\s*(?:lac|cr|lakh|crore)?)', body, re.IGNORECASE)
    if auth_m:
        result["authorized_capital"] = auth_m.group(1).strip()
    if paid_m:
        result["paidup_capital"] = paid_m.group(1).strip()

    return result


    # TR, AE, CN registry lookups require browser rendering (JS apps).
    # Residential proxy can reach the portals but can't execute searches.
    # These countries return guidance to use CIR for full research.
    # DO NOT add automated requests to blocked gov sites (GSXT, DED, MCA, TOBB).


# ---------------------------------------------------------------------------
# Verify Endpoint
# ---------------------------------------------------------------------------

# Which countries are supported and what registries we check
_VERIFY_SOURCES = {
    "PK": "SECP eServices (direct) + FBR IRIS ATL (Multilogin anti-detect browser)",
    "IN": "Tofler.in (MCA21 data via Bright Data)",
    "TR": "MERSIS / GIB (via Bright Data TR residential proxy)",
    "AE": "DIFC / JAFZA / MOEC (via Bright Data AE residential proxy)",
    "CN": "SAMR / Tianyancha (via Bright Data CN residential proxy)",
}


@app.post("/api/v1/verify")
async def verify_entity(
    request: Request,
    _key: str = Depends(verify_api_key),
):
    """
    Real-time entity verification against government registries.
    No OpenClaw, no agent — direct registry queries via regional proxies.
    Response in ~5-15 seconds.

    Body: {
        "entity_name": "Agro China Pakistan",
        "country_code": "PK",              // PK, IN, TR, AE, CN
        "ntn": "4334750-9",                 // optional, Pakistan NTN
        "cin": "U24110MH2008PTC186710"      // optional, India CIN
    }

    Supported registries:
        PK — SECP (direct), FBR (residential proxy)
        IN — Tofler/MCA21 (Bright Data Unlocker)
        TR — MERSIS, GIB (residential proxy) — portal reachability check
        AE — DIFC, JAFZA, MOEC (residential proxy) — portal reachability check
        CN — SAMR (residential proxy) — portal reachability check
    """
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    country_code = body.get("country_code", "").strip().upper()
    ntn = body.get("ntn", "").strip()
    cin = body.get("cin", "").strip().upper()

    if not entity_name or not country_code:
        raise HTTPException(status_code=422, detail="entity_name and country_code required")
    if country_code not in _VERIFY_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"Verify not yet supported for {country_code}. Supported: {', '.join(sorted(_VERIFY_SOURCES))}.",
        )

    log.info("Verify: %s (%s) cin=%s ntn=%s", entity_name, country_code,
             cin or "none", ntn or "none")

    loop = asyncio.get_event_loop()

    # --------------- PAKISTAN ---------------
    if country_code == "PK":
        secp_fut = loop.run_in_executor(_ssh_pool, _secp_query_via_ssh, entity_name)
        fbr_fut = loop.run_in_executor(_ssh_pool, multilogin_fbr.fbr_atl_verify, ntn) if ntn else None

        secp = await secp_fut
        fbr = (await fbr_fut) if fbr_fut else None

        regs = secp.get("results", [])
        verified = secp.get("found", False)
        r = regs[0] if regs else {}

        now = datetime.now(timezone.utc).isoformat()
        resp = {
            "entity_name": entity_name, "country_code": "PK", "verified": verified,
            "legal_name": r.get("legal_name"),
            "registration_number": r.get("registration_number"),
            "status": r.get("status"), "company_type": r.get("company_type"),
            "cro": r.get("cro"), "registration_date": r.get("registration_date"),
            "form_ab_filed_upto": r.get("form_ab_filed_upto"),
            "fbr": fbr,
            "all_matches": regs if len(regs) > 1 else None,
            "validation_source": {
                "registry": "Securities and Exchange Commission of Pakistan (SECP)",
                "url": "https://eservices.secp.gov.pk/eServices/NameSearch.jsp",
                "method": "Direct POST to SECP ControllerServlet via Gulf VM (SSH)",
                "verified_at": now,
            },
            "timestamp": now,
        }
        if verified:
            resp["summary"] = (
                f"{r['legal_name']} — SECP #{r['registration_number']} — "
                f"{r.get('company_type') or r['status']} — {r['cro']} — Reg: {r['registration_date']}")
        else:
            resp["summary"] = f"No SECP registration found for '{entity_name}'"
            if secp.get("error"):
                resp["error"] = secp["error"]
        return resp

    # --------------- INDIA ---------------
    if country_code == "IN":
        result = await loop.run_in_executor(_ssh_pool, _india_tofler_lookup, entity_name, cin)
        verified = result.get("found", False)
        now = datetime.now(timezone.utc).isoformat()
        resp = {
            "entity_name": entity_name, "country_code": "IN", "verified": verified,
            "legal_name": result.get("legal_name"), "cin": result.get("cin"),
            "status": result.get("status"), "company_type": result.get("company_type"),
            "incorporation_date": result.get("incorporation_date"),
            "registered_address": result.get("registered_address"),
            "directors": result.get("directors"),
            "authorized_capital": result.get("authorized_capital"),
            "paidup_capital": result.get("paidup_capital"),
            "validation_source": {
                "registry": "Ministry of Corporate Affairs (MCA21) — via Tofler.in",
                "url": result.get("source_url"),
                "method": "Bright Data residential proxy (IN) → Tofler.in (aggregates MCA21 data)",
                "verified_at": now,
            },
            "timestamp": now,
        }
        if verified:
            resp["summary"] = (
                f"{result.get('legal_name', entity_name)} — CIN {result.get('cin', 'N/A')} — "
                f"{result.get('status', 'Unknown')} — {result.get('company_type', '')} — "
                f"Inc: {result.get('incorporation_date', 'N/A')}")
        else:
            resp["summary"] = f"No registration found for '{entity_name}'"
            if result.get("error"):
                resp["error"] = result["error"]
        return resp

    # --------------- TURKEY / UAE / CHINA ---------------
    # These registries require browser rendering for search.
    # No automated requests — return guidance to use CIR.
    now = datetime.now(timezone.utc).isoformat()
    registry_info = {
        "TR": {
            "available_registries": ["MERSIS (mersis.gtb.gov.tr)", "GIB (gib.gov.tr)", "E-Devlet (turkiye.gov.tr)"],
            "note": "Turkish registries require browser rendering. Use /api/v1/jobs with scenario=cir for full research.",
        },
        "AE": {
            "available_registries": ["DIFC (difc.ae)", "JAFZA (jafza.ae)", "MOEC (moec.gov.ae)", "ADGM (adgm.com)"],
            "note": "UAE free zone registries require browser rendering. Use /api/v1/jobs with scenario=cir for full research.",
        },
        "CN": {
            "available_registries": ["SAMR (samr.gov.cn)", "Tianyancha (tianyancha.com)", "Qichacha (qcc.com)"],
            "note": "Chinese registries require USCC and browser rendering. Use /api/v1/jobs with scenario=cir for full research.",
        },
    }
    info = registry_info[country_code]
    return {
        "entity_name": entity_name, "country_code": country_code,
        "verified": False,
        "available_registries": info["available_registries"],
        "note": info["note"],
        "timestamp": now,
        "summary": f"Real-time verify not available for {country_code}. Submit CIR job for full research.",
    }


# ---------------------------------------------------------------------------
# Verify Job — Async comprehensive verification (LinkedIn + Registry + Dark Web)
# ---------------------------------------------------------------------------

_BD_DATASETS_BASE = "https://api.brightdata.com/datasets/v3"
_BD_DATASETS_KEY = get_secret("brightdata-api-key")
_BD_LINKEDIN_PEOPLE_ID = "gd_l1viktl72bvl7bjuj0"
_BD_LINKEDIN_COMPANY_ID = "gd_l1vikfnt1wgvvqz95w"
_TAVILY_API_KEY = get_secret("tavily-api-key")

# Verify-job accepts ALL countries — LinkedIn + dark web are global.
# Registry check only works for PK/IN; others get "not available" for that check.
_VERIFY_REGISTRY_SUPPORTED = {"PK", "IN"}


def _brightdata_trigger(dataset_id: str, inputs: list[dict]) -> str | None:
    """Trigger Bright Data dataset scrape. Returns snapshot_id or None."""
    import requests as _req
    try:
        resp = _req.post(
            f"{_BD_DATASETS_BASE}/trigger",
            params={"dataset_id": dataset_id, "format": "json"},
            headers={"Authorization": f"Bearer {_BD_DATASETS_KEY}",
                     "Content-Type": "application/json"},
            json=inputs,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("snapshot_id")
        log.warning("Bright Data trigger failed (%d): %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        log.error("Bright Data trigger error: %s", e)
        return None


def _brightdata_poll(snapshot_id: str, timeout_s: int = 180) -> str:
    """Poll until snapshot ready. Returns 'ready' or 'failed'."""
    import requests as _req
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            resp = _req.get(
                f"{_BD_DATASETS_BASE}/progress/{snapshot_id}",
                headers={"Authorization": f"Bearer {_BD_DATASETS_KEY}"},
                timeout=15,
            )
            status = resp.json().get("status", "unknown")
            if status in ("ready", "failed"):
                return status
        except Exception:
            pass
        time.sleep(5)
    return "timeout"


def _brightdata_download(snapshot_id: str) -> list[dict]:
    """Download completed snapshot results."""
    import requests as _req
    try:
        resp = _req.get(
            f"{_BD_DATASETS_BASE}/snapshot/{snapshot_id}",
            params={"format": "json"},
            headers={"Authorization": f"Bearer {_BD_DATASETS_KEY}"},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.error("Bright Data download error: %s", e)
    return []


def _tavily_search(query: str, max_results: int = 3) -> list[dict]:
    """Run Tavily search. Returns list of result dicts with url, title, content."""
    import requests as _req
    if not _TAVILY_API_KEY:
        return []
    try:
        resp = _req.post(
            "https://api.tavily.com/search",
            json={"api_key": _TAVILY_API_KEY, "query": query,
                  "max_results": max_results, "search_depth": "basic"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("results", [])
    except Exception as e:
        log.warning("Tavily search error: %s", e)
    return []


def _find_linkedin_profile_url(person_name: str, company_name: str) -> str | None:
    """Use Tavily to find a person's LinkedIn /in/ URL."""
    results = _tavily_search(f'"{person_name}" "{company_name}" site:linkedin.com/in/')
    for r in results:
        url = r.get("url", "")
        if "linkedin.com/in/" in url:
            return url.split("?")[0]  # Strip query params
    # Fallback: broader search
    results = _tavily_search(f"{person_name} {company_name} linkedin")
    for r in results:
        url = r.get("url", "")
        if "linkedin.com/in/" in url:
            return url.split("?")[0]
    return None


def _find_linkedin_company_url(company_name: str, domain: str = "") -> str | None:
    """Use Tavily to find a company's LinkedIn /company/ URL."""
    query = f"{company_name} {domain} site:linkedin.com/company/" if domain else \
            f"{company_name} site:linkedin.com/company/"
    results = _tavily_search(query)
    for r in results:
        url = r.get("url", "")
        if "linkedin.com/company/" in url or "linkedin.com/school/" in url:
            return url.split("?")[0]
    return None


def _update_verify_check(job_id: str, check_name: str, data: dict):
    """Thread-safe update of a single check within a verify job."""
    lock = _get_job_lock(job_id)
    with lock:
        job = load_job(job_id)
        if "checks" not in job:
            job["checks"] = {}
        job["checks"][check_name] = data
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_job(job)


def _verify_check_registry(job_id: str, entity_name: str, country_code: str,
                            ntn: str = "", cin: str = "") -> dict:
    """Run government registry verification (blocking)."""
    _update_verify_check(job_id, "registry", {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    try:
        if country_code == "PK":
            secp = _secp_query_via_ssh(entity_name)
            fbr = multilogin_fbr.fbr_atl_verify(ntn) if ntn else None
            regs = secp.get("results", [])
            verified = secp.get("found", False)
            r = regs[0] if regs else {}
            result = {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "verified": verified,
                "legal_name": r.get("legal_name"),
                "registration_number": r.get("registration_number"),
                "entity_status": r.get("status"),
                "company_type": r.get("company_type"),
                "cro": r.get("cro"),
                "registration_date": r.get("registration_date"),
                "fbr": fbr,
                "all_matches": regs if len(regs) > 1 else None,
                "source": "SECP eServices (direct gov query)",
            }
        elif country_code == "IN":
            tofler = _india_tofler_lookup(entity_name, cin)
            result = {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "verified": tofler.get("found", False),
                "legal_name": tofler.get("legal_name"),
                "cin": tofler.get("cin"),
                "entity_status": tofler.get("status"),
                "company_type": tofler.get("company_type"),
                "incorporation_date": tofler.get("incorporation_date"),
                "directors": tofler.get("directors"),
                "registered_address": tofler.get("registered_address"),
                "source": "MCA21 via Tofler.in (Bright Data proxy)",
            }
        else:
            result = {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "verified": False,
                "note": f"Real-time registry check not available for {country_code}. Included in CIR job.",
                "source": None,
            }
    except Exception as e:
        result = {"status": "failed", "error": str(e)[:300]}

    _update_verify_check(job_id, "registry", result)
    return result


def _verify_check_linkedin_company(job_id: str, entity_name: str,
                                    linkedin_url: str = "", domain: str = "") -> dict:
    """Scrape company LinkedIn page via Bright Data."""
    _update_verify_check(job_id, "linkedin_company", {
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    try:
        # Find LinkedIn company URL if not provided
        url = linkedin_url
        if not url:
            url = _find_linkedin_company_url(entity_name, domain)
        if not url:
            result = {"status": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
                      "found": False, "note": "LinkedIn company page not found via search"}
            _update_verify_check(job_id, "linkedin_company", result)
            return result

        # Trigger Bright Data scrape
        snapshot_id = _brightdata_trigger(_BD_LINKEDIN_COMPANY_ID, [{"url": url}])
        if not snapshot_id:
            result = {"status": "failed", "error": "Bright Data trigger failed"}
            _update_verify_check(job_id, "linkedin_company", result)
            return result

        # Poll
        poll_status = _brightdata_poll(snapshot_id, timeout_s=60)
        if poll_status != "ready":
            result = {"status": "failed", "error": f"Bright Data poll: {poll_status}"}
            _update_verify_check(job_id, "linkedin_company", result)
            return result

        # Download
        data = _brightdata_download(snapshot_id)
        if not data:
            result = {"status": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
                      "found": False, "note": "No data returned from LinkedIn scrape"}
            _update_verify_check(job_id, "linkedin_company", result)
            return result

        company = data[0]
        result = {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "found": True,
            "name": company.get("name"),
            "linkedin_url": url,
            "industry": company.get("industries"),
            "company_size": company.get("company_size"),
            "locations": (company.get("locations") or [])[:5],
            "about": (company.get("about") or "")[:500],
            "followers": company.get("followers"),
            "employees_on_linkedin": company.get("employees_in_linkedin"),
            "organization_type": company.get("organization_type"),
            "website": company.get("website"),
            "specialties": company.get("specialties"),
            "notable_employees": [
                {"name": e.get("title", "").split(" is ")[0] if " is " in e.get("title", "") else e.get("title", ""),
                 "link": e.get("link")}
                for e in (company.get("employees") or [])[:10]
            ],
            "source": "LinkedIn via Bright Data Web Scraper API",
        }
    except Exception as e:
        result = {"status": "failed", "error": str(e)[:300]}

    _update_verify_check(job_id, "linkedin_company", result)
    return result


def _person_matches_entity(profile: dict, entity_name: str) -> bool:
    """Check if a LinkedIn profile actually belongs to someone at the entity."""
    entity_lower = entity_name.lower()
    # Normalize: remove common suffixes for matching
    entity_words = set(re.sub(r'\b(pvt|ltd|private|limited|llc|inc|corp)\b', '',
                              entity_lower).split())
    entity_words.discard("")

    # Check current company
    current_co = profile.get("current_company") or {}
    co_name = (current_co.get("name") or "").lower()
    if co_name and entity_words and len(entity_words.intersection(co_name.split())) >= 2:
        return True
    if co_name and entity_lower in co_name:
        return True

    # Check position field
    position = (profile.get("position") or "").lower()
    if entity_words and len(entity_words.intersection(position.split())) >= 2:
        return True

    # Check experience history
    for exp in (profile.get("experience") or []):
        exp_co = (exp.get("company") or "").lower()
        if exp_co and entity_words and len(entity_words.intersection(exp_co.split())) >= 2:
            return True

    return False


def _verify_check_linkedin_persons(job_id: str, persons: list[str],
                                    entity_name: str) -> dict:
    """Find and scrape LinkedIn profiles for each declared person.

    Only reports profiles that actually match the entity. If a scraped profile
    works at a different company, it's the wrong person — discard it and report
    as 'not confirmed' rather than showing misleading data.
    """
    _update_verify_check(job_id, "linkedin_persons", {
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
        "progress": f"0/{len(persons)} searching..."})
    try:
        # Step 1: Find LinkedIn URLs via Tavily
        found_urls = []
        person_url_map = {}
        for i, person in enumerate(persons):
            url = _find_linkedin_profile_url(person, entity_name)
            if url:
                found_urls.append({"url": url})
                person_url_map[url] = person
            _update_verify_check(job_id, "linkedin_persons", {
                "status": "running",
                "progress": f"Searching {i+1}/{len(persons)}... ({len(found_urls)} candidates)"})

        if not found_urls:
            result = {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "confirmed_count": 0,
                "not_confirmed": persons,
                "total_searched": len(persons),
                "profiles": [],
                "note": "No LinkedIn profiles found matching these persons at this entity",
            }
            _update_verify_check(job_id, "linkedin_persons", result)
            return result

        # Step 2: Batch trigger Bright Data for all found URLs
        _update_verify_check(job_id, "linkedin_persons", {
            "status": "running",
            "progress": f"Verifying {len(found_urls)} candidate profiles..."})

        snapshot_id = _brightdata_trigger(_BD_LINKEDIN_PEOPLE_ID, found_urls)
        if not snapshot_id:
            result = {"status": "failed", "error": "Bright Data trigger failed for person profiles"}
            _update_verify_check(job_id, "linkedin_persons", result)
            return result

        # Step 3: Poll (longer timeout — people profiles take 30-120s)
        poll_status = _brightdata_poll(snapshot_id, timeout_s=300)
        if poll_status != "ready":
            result = {"status": "failed", "error": f"Bright Data poll: {poll_status}"}
            _update_verify_check(job_id, "linkedin_persons", result)
            return result

        # Step 4: Download and filter — ONLY keep profiles that match the entity
        data = _brightdata_download(snapshot_id)
        confirmed = []
        not_confirmed = []
        checked_persons = set()

        for profile in data:
            input_url = (profile.get("input") or {}).get("url", "")
            declared_name = person_url_map.get(input_url, "Unknown")
            checked_persons.add(declared_name)

            if _person_matches_entity(profile, entity_name):
                current_co = profile.get("current_company") or {}
                confirmed.append({
                    "declared_name": declared_name,
                    "linkedin_name": profile.get("name"),
                    "linkedin_url": f"https://www.linkedin.com/in/{profile.get('id', '')}",
                    "position": profile.get("position"),
                    "current_company": current_co.get("name"),
                    "current_title": current_co.get("title"),
                    "city": profile.get("city"),
                    "country_code": profile.get("country_code"),
                    "confirmed_at_entity": True,
                })
            else:
                not_confirmed.append(declared_name)

        # Add persons we never found URLs for
        for person in persons:
            if person not in checked_persons:
                not_confirmed.append(person)

        # Build simple yes/no per person
        verification = []
        confirmed_names = {p["declared_name"] for p in confirmed}
        for person in persons:
            if person in confirmed_names:
                match = next(p for p in confirmed if p["declared_name"] == person)
                verification.append({
                    "name": person,
                    "works_at_entity": True,
                    "title": match.get("current_title") or match.get("position"),
                    "linkedin_url": match.get("linkedin_url"),
                    "city": match.get("city"),
                })
            else:
                verification.append({
                    "name": person,
                    "works_at_entity": False,
                    "note": f"Not found at {entity_name} on LinkedIn",
                })

        result = {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_count": len(confirmed),
            "total_searched": len(persons),
            "persons_verification": verification,
            "profiles": confirmed if confirmed else None,
            "note": (f"{len(confirmed)}/{len(persons)} persons confirmed at entity on LinkedIn"
                     if confirmed else
                     "No persons could be confirmed at this entity on LinkedIn"),
            "source": "LinkedIn via Bright Data Web Scraper API",
        }
    except Exception as e:
        result = {"status": "failed", "error": str(e)[:300]}

    _update_verify_check(job_id, "linkedin_persons", result)
    return result


def _verify_check_darkweb(job_id: str, entity_name: str, country: str,
                           persons: list[str], domain: str) -> dict:
    """Run dark web scan on entity + persons + domain."""
    _update_verify_check(job_id, "dark_web", {
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    try:
        dw_result = _run_darkweb_enrichment(job_id, entity_name, country, persons, domain)
        summary = dw_result.get("summary", {})
        total_findings = summary.get("total_findings", 0)
        sources_searched = summary.get("sources_searched", 0)

        # Classify risk
        if total_findings == 0:
            risk_level = "CLEAN"
        elif total_findings <= 5:
            risk_level = "LOW"
        elif total_findings <= 15:
            risk_level = "MEDIUM"
        else:
            risk_level = "HIGH"

        # Check for critical indicators
        findings = dw_result.get("findings", [])
        critical_types = {"infostealer_exposure", "ransomware_victim", "darknet_mention", "sanctions_hit"}
        if any(f.get("type") in critical_types for f in findings):
            risk_level = "CRITICAL"

        result = {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "risk_level": risk_level,
            "total_findings": total_findings,
            "sources_searched": sources_searched,
            "sources_with_results": summary.get("sources_with_results", 0),
            "by_type": summary.get("by_type", {}),
            "key_findings": [
                {"source": f.get("source"), "type": f.get("type"), "snippet": (f.get("data", "") or "")[:200]}
                for f in findings[:10]
            ],
            "source": "Crawl Dark Web Gateway (37 sources via Tor)",
        }
    except Exception as e:
        result = {"status": "failed", "error": str(e)[:300]}

    _update_verify_check(job_id, "dark_web", result)
    return result


async def _dispatch_verify_job(job_id: str, entity_name: str, country_code: str,
                                ntn: str, cin: str, persons: list[str],
                                domain: str, linkedin_url: str):
    """Coordinate all verification checks in parallel."""
    update_job_fields(job_id, {"status": "running"})
    loop = asyncio.get_event_loop()

    # Launch all checks concurrently
    tasks = [
        loop.run_in_executor(_ssh_pool, _verify_check_registry,
                             job_id, entity_name, country_code, ntn, cin),
        loop.run_in_executor(_ssh_pool, _verify_check_linkedin_company,
                             job_id, entity_name, linkedin_url, domain),
        loop.run_in_executor(_ssh_pool, _verify_check_darkweb,
                             job_id, entity_name, country_code, persons, domain),
    ]
    if persons:
        tasks.append(loop.run_in_executor(_ssh_pool, _verify_check_linkedin_persons,
                                          job_id, persons, entity_name))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any exceptions
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log.error("Verify job %s check %d failed: %s", job_id[:8], i, r)

    # Determine overall status
    job = load_job(job_id)
    checks = job.get("checks", {})
    statuses = [c.get("status") for c in checks.values()]
    if all(s == "completed" for s in statuses):
        overall = "completed"
    elif any(s == "failed" for s in statuses):
        overall = "partial_success" if any(s == "completed" for s in statuses) else "failed"
    else:
        overall = "completed"

    update_job_fields(job_id, {"status": overall, "updated_at": datetime.now(timezone.utc).isoformat()})
    log_job_event(job_id, "verify", "completed", overall)
    log.info("Verify job %s completed: %s", job_id[:8], overall)


@app.post("/api/v1/verify-job")
async def submit_verify_job(request: Request, _key: str = Depends(verify_api_key)):
    """
    Submit an async verification job. Runs registry + LinkedIn + dark web checks
    in parallel. Poll GET /api/v1/verify-job/{job_id} for progressive results.

    Body: {
        "entity_name": "Company Name",
        "country_code": "PK",
        "ntn": "",                     // optional — Pakistan NTN
        "cin": "",                     // optional — India CIN
        "persons": ["First Last"],     // optional — director/UBO names
        "domain": "company.pk",        // optional — for dark web + LinkedIn
        "linkedin_url": ""             // optional — skip Tavily search if known
    }
    """
    _check_backpressure()
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    country_code = body.get("country_code", "").strip().upper()
    persons = body.get("persons", [])
    domain = body.get("domain", "").strip()
    ntn = body.get("ntn", "").strip()
    cin = body.get("cin", "").strip().upper()
    linkedin_url = body.get("linkedin_url", "").strip()

    if not entity_name or not country_code:
        raise HTTPException(status_code=422, detail="entity_name and country_code required")
    if len(country_code) != 2:
        raise HTTPException(status_code=422, detail="country_code must be a 2-letter ISO code")

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    job = {
        "job_id": job_id,
        "scenario": "verify",
        "status": "queued",
        "entity_name": entity_name,
        "country_code": country_code,
        "created_at": now,
        "updated_at": now,
        "checks": {
            "registry": {"status": "pending"},
            "linkedin_company": {"status": "pending"},
            "linkedin_persons": {"status": "pending" if persons else "skipped"},
            "dark_web": {"status": "pending"},
        },
        "seed_data": {
            "entity_name": entity_name,
            "country_code": country_code,
            "persons": persons,
            "domain": domain,
            "ntn": ntn,
            "cin": cin,
        },
    }
    save_job(job)
    log_job_event(job_id, "verify", "submitted", "queued")
    log.info("Verify job %s: %s (%s) persons=%d domain=%s",
             job_id[:8], entity_name, country_code, len(persons), domain or "none")

    # Fire and forget — dispatch runs in background
    asyncio.create_task(_dispatch_verify_job(
        job_id, entity_name, country_code, ntn, cin, persons, domain, linkedin_url))

    return {
        "job_id": job_id,
        "status": "queued",
        "entity_name": entity_name,
        "country_code": country_code,
        "checks_planned": ["registry", "linkedin_company", "dark_web"] + (["linkedin_persons"] if persons else []),
        "poll_url": f"/api/v1/verify-job/{job_id}",
        "estimated_time": "1-5 minutes",
    }


@app.get("/api/v1/verify-job/{job_id}")
async def get_verify_job(job_id: str, _key: str = Depends(verify_api_key)):
    """Poll verification job status. Returns progressive results as checks complete."""
    job = load_job(job_id)
    if job.get("scenario") != "verify":
        raise HTTPException(status_code=404, detail="Not a verify job")
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "entity_name": job.get("entity_name"),
        "country_code": job.get("country_code"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "checks": job.get("checks", {}),
        "seed_data": job.get("seed_data"),
    }


@app.get("/api/v1/verify-jobs")
async def list_verify_jobs(limit: int = 20, _key: str = Depends(verify_api_key)):
    """List recent verification jobs."""
    jobs = list_jobs(limit=limit, scenario="verify")
    return [
        {
            "job_id": j["job_id"],
            "status": j["status"],
            "entity_name": j.get("entity_name"),
            "country_code": j.get("country_code"),
            "created_at": j.get("created_at"),
            "updated_at": j.get("updated_at"),
        }
        for j in jobs
    ]


# ---------------------------------------------------------------------------
# Scenario Handlers
# ---------------------------------------------------------------------------

async def _handle_cir(payload: dict) -> JobResponse:
    """Handle CIR scenario — single-region dispatch."""
    _check_backpressure()
    # Validate required fields
    if "entity_legal_name" not in payload or "entity_country" not in payload:
        raise HTTPException(
            status_code=422,
            detail="CIR scenario requires entity_legal_name and entity_country"
        )

    region = get_region(payload["entity_country"])
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    job = {
        "job_id": job_id,
        "scenario": "cir",
        "status": "queued",
        "region": region,
        "entity_name": payload["entity_legal_name"],
        "country": payload["entity_country"].upper(),
        "created_at": now,
        "updated_at": now,
        "blob_path": None,
        "report_summary": None,
        "error": None,
        "retry_count": 0,
        "review": None,
        "seed_data": payload,  # stored locally, NEVER sent to OpenClaw
    }
    save_job(job)
    log_job_event(job_id, "submitted", scenario="cir", region=region, status="queued",
                  details={"entity": payload["entity_legal_name"], "country": payload["entity_country"]})

    try:
        prompt = build_cir_prompt(payload)
    except ValueError as e:
        job["status"] = "failed"
        job["error"] = str(e)
        save_job(job)
        log_job_event(job_id, "sanitization_failed", scenario="cir", status="failed", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    asyncio.create_task(dispatch_single(job_id, region, prompt, "cir"))
    return _job_to_response(job)


async def _handle_product_intel(payload: dict) -> JobResponse:
    """Handle product-intel scenario — multi-region fan-out."""
    _check_backpressure()

    # Resolve product name from new or legacy format
    product = payload.get("product", {})
    product_name = None
    if isinstance(product, dict):
        product_name = product.get("generic_name")
    product_name = product_name or payload.get("product_name")
    if not product_name:
        raise HTTPException(
            status_code=422,
            detail="product-intel requires product.generic_name (or legacy product_name)"
        )

    # Resolve target markets from target_markets or region_hint
    target_markets = payload.get("target_markets")
    region_hint = payload.get("region_hint")
    if not target_markets and region_hint:
        target_markets = REGION_HINT_MAP.get(region_hint.lower())
        if not target_markets:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown region_hint '{region_hint}'. Valid: {list(REGION_HINT_MAP.keys())}"
            )
    if not target_markets:
        raise HTTPException(
            status_code=422,
            detail="product-intel requires target_markets[] or region_hint"
        )

    # Idempotency: if request_id provided and job exists, return existing
    request_id = payload.get("request_id")
    if request_id:
        existing = _find_job_by_request_id(request_id)
        if existing:
            log.info("Idempotent hit: request_id=%s -> job_id=%s", request_id, existing["job_id"])
            return _job_to_response(existing)

    regions = get_regions_for_markets(target_markets)
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    job = {
        "job_id": job_id,
        "scenario": "product-intel",
        "status": "queued",
        "regions": regions,
        "product_name": product_name,
        "target_markets": [cc.upper() for cc in target_markets],
        "request_id": request_id,
        "created_at": now,
        "updated_at": now,
        "blob_paths": [],
        "report_summary": None,
        "error": None,
        "retry_count": 0,
        "review": None,
        "seed_data": payload,
    }
    save_job(job)

    # Build per-region prompts (each region only sees its own markets)
    try:
        prompts = {}
        for region in regions:
            region_markets = [cc for cc in target_markets if get_region(cc) == region]
            prompts[region] = build_product_intel_prompt(payload, region, region_markets)
    except ValueError as e:
        job["status"] = "failed"
        job["error"] = str(e)
        save_job(job)
        raise HTTPException(status_code=400, detail=str(e))

    log_job_event(job_id, "submitted", scenario="product-intel", status="queued",
                  details={"product": product_name, "regions": regions, "markets": target_markets})
    asyncio.create_task(dispatch_fanout(job_id, regions, prompts, "product-intel"))
    return _job_to_response(job)


async def _handle_dark_web(payload: dict) -> JobResponse:
    """Handle dark-web scenario — direct HTTP dispatch to crawl-darkweb VM."""
    _check_backpressure()
    entity_name = payload.get("entity_name") or payload.get("entity_legal_name")
    if not entity_name:
        raise HTTPException(
            status_code=422,
            detail="dark-web scenario requires entity_name"
        )

    # Sanitize — hard fail on blocked terms
    try:
        sanitize_payload(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    country = (payload.get("country") or payload.get("entity_country", "")).upper()
    owners = payload.get("owners", [])
    domain = payload.get("domain")
    depth = payload.get("depth", "medium")

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    def _to_snake(name: str) -> str:
        s = name.lower().replace(" ", "_")
        s = re.sub(r"[^a-z0-9_]", "", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s

    job = {
        "job_id": job_id,
        "scenario": "dark-web",
        "status": "queued",
        "region": "darkweb",
        "entity_name": entity_name,
        "country": country,
        "created_at": now,
        "updated_at": now,
        "blob_path": None,
        "report_summary": None,
        "error": None,
        "retry_count": 0,
        "review": None,
        "seed_data": payload,
    }
    save_job(job)

    log_job_event(job_id, "submitted", scenario="dark-web", status="queued",
                  details={"entity": entity_name, "country": country, "depth": depth,
                           "owners": len(owners), "domain": domain})
    # Dispatch to dark-web VM via HTTP (not SSH/OpenClaw)
    asyncio.create_task(_dispatch_darkweb(job_id, entity_name, country, owners, domain, depth))
    return _job_to_response(job)


async def _dispatch_darkweb(job_id: str, entity_name: str, country: str,
                            owners: list[str], domain: str, depth: str):
    """HTTP dispatch to crawl-darkweb VM gateway, then SFTP + blob upload."""
    vm = DARKWEB_VM
    ssh = None

    def _to_snake(name: str) -> str:
        s = name.lower().replace(" ", "_")
        s = re.sub(r"[^a-z0-9_]", "", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s

    try:
        update_job_fields(job_id, {
            "status": "running",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        # Call the dark-web gateway API via SSH tunnel (VM only allows SSH from us)
        ssh = paramiko.SSHClient()
        ssh.load_host_keys(SSH_KNOWN_HOSTS)
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(
            hostname=vm["ip"],
            username="copapadmin",
            key_filename=SSH_KEY_PATH,
            timeout=15,
        )

        # Build the curl command to call the local gateway on the VM
        req_body = json.dumps({
            "entity_name": entity_name,
            "country": country,
            "owners": owners or [],
            "domain": domain or "",
            "depth": depth,
        })
        safe_body = req_body.replace("'", "'\\''")

        cmd = (
            f"curl -s -X POST http://127.0.0.1:{vm['port']}/api/v1/research "
            f"-H 'Content-Type: application/json' "
            f"-H 'X-API-Key: {vm['api_key']}' "
            f"-d '{safe_body}'"
        )

        _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
        result_raw = stdout.read().decode()
        error_raw = stderr.read().decode()

        if not result_raw.strip():
            err_msg = f"darkweb: empty response (stderr: {error_raw[:300]})"
            log.error("Job %s [dark-web]: %s", job_id[:8], err_msg)
            update_job_fields(job_id, {
                "status": "failed",
                "error": err_msg,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            return

        dw_result = json.loads(result_raw)
        dw_job_id = dw_result.get("job_id", "")
        dw_status = dw_result.get("status", "unknown")
        findings_count = dw_result.get("findings_count", 0)

        log.info("Job %s [dark-web]: remote job %s status=%s findings=%d",
                 job_id[:8], dw_job_id, dw_status, findings_count)

        # SFTP the full result file from the darkweb VM
        entity_snake = _to_snake(entity_name)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        remote_filename = f"{dw_job_id}.json"
        local_filename = f"darkweb_{entity_snake}_{date_str}.json"
        local_file = LOCAL_OUTPUT_DIR / local_filename
        blob_name = f"dark-web/{entity_snake}_{date_str}.json"

        sftp = ssh.open_sftp()
        try:
            sftp.get(f"/home/copapadmin/crawl/output/{remote_filename}", str(local_file))
        except FileNotFoundError:
            log.warning("Job %s [dark-web]: remote file %s not found, listing dir",
                        job_id[:8], remote_filename)
            remote_files = sftp.listdir("/home/copapadmin/crawl/output/")
            match = [f for f in remote_files if dw_job_id in f]
            if match:
                sftp.get(f"/home/copapadmin/crawl/output/{match[0]}", str(local_file))
            else:
                log.error("Job %s [dark-web]: no output file found. Available: %s",
                          job_id[:8], remote_files[:10])
        sftp.close()

        # Upload to blob
        blob_error = None
        if local_file.exists() and local_file.stat().st_size > 0 and _BLOB_SAS_TOKEN:
            upload_result = subprocess.run(
                [
                    "az", "storage", "blob", "upload",
                    "--account-name", BLOB_ACCOUNT,
                    "--container-name", BLOB_CONTAINER,
                    "--name", blob_name,
                    "--file", str(local_file),
                    "--sas-token", _BLOB_SAS_TOKEN,
                    "--overwrite",
                ],
                capture_output=True, text=True, timeout=60,
            )
            if upload_result.returncode != 0:
                blob_error = f"Blob upload failed: {upload_result.stderr[:200]}"
        elif not local_file.exists() or local_file.stat().st_size == 0:
            blob_error = "Report file empty or not found"

        this_blob = f"{BLOB_CONTAINER}/{blob_name}" if not blob_error else None

        updates = {
            "status": "completed" if dw_status == "completed" else "failed",
            "blob_path": this_blob,
            "report_summary": f"{findings_count} findings from dark web sources",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if blob_error:
            updates["error"] = blob_error

        update_job_fields(job_id, updates)
        log_job_event(job_id, "completed", scenario="dark-web", status=updates["status"],
                      details={"findings": findings_count, "blob_path": this_blob})
        log.info("Job %s [dark-web]: %s (blob: %s, findings: %d)",
                 job_id[:8], updates["status"], this_blob, findings_count)

    except Exception as e:
        log.error("Job %s [dark-web] failed: %s", job_id[:8], e)
        update_job_fields(job_id, {
            "status": "failed",
            "error": str(e)[:400],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    finally:
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass


def _find_job_by_request_id(request_id: str) -> dict | None:
    """Find existing job by request_id for idempotency."""
    for p in JOBS_DIR.glob("*.json"):
        try:
            with open(p) as f:
                job = json.load(f)
            if job.get("request_id") == request_id:
                return job
        except (json.JSONDecodeError, KeyError):
            continue
    return None


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------

def _job_to_response(job: dict) -> JobResponse:
    """Convert stored job dict to API response (strips seed_data)."""
    # Build dark_web summary block if enrichment ran
    dark_web = None
    if job.get("dark_web_findings") is not None or job.get("dark_web_sources") is not None:
        dw_findings = job.get("dark_web_findings", 0)
        dw_sources = job.get("dark_web_sources", 0)
        dw_error = job.get("dark_web_error")

        # Determine alert level
        if dw_findings == 0:
            alert = "CLEAN"
        elif dw_findings <= 5:
            alert = "LOW"
        elif dw_findings <= 15:
            alert = "MEDIUM"
        else:
            alert = "HIGH"

        dark_web = {
            "alert": alert,
            "findings_count": dw_findings,
            "sources_searched": dw_sources,
            "status": "error" if dw_error else "completed",
            "error": dw_error,
            "note": f"Dark web scan: {dw_findings} findings across {dw_sources} sources. Full details in blob under 'dark_web_intelligence'.",
        }

    return JobResponse(
        job_id=job["job_id"],
        scenario=job.get("scenario", "cir"),
        status=job["status"],
        request_id=job.get("request_id"),
        region=job.get("region"),
        regions=job.get("regions"),
        region_status=job.get("region_status"),
        entity_name=job.get("entity_name"),
        country=job.get("country"),
        created_at=job["created_at"],
        updated_at=job.get("updated_at"),
        blob_path=job.get("blob_path"),
        blob_paths=job.get("blob_paths"),
        report_summary=job.get("report_summary"),
        error=job.get("error"),
        errors=job.get("errors"),
        retry_count=job.get("retry_count", 0),
        review=job.get("review"),
        dark_web=dark_web,
    )
