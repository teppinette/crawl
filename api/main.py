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
from urllib.parse import quote_plus

import paramiko
from fastapi import FastAPI, HTTPException, Security, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, model_validator

from keyvault import get_secret, load_vm_tokens
from event_log import log_job_event, log_api_access
from report_db import save_cir_report, save_darkweb_report, save_verification
import adverse_media
import enrichment
import screening
import aggregator
import raw_store
import sandbox_india
# Multilogin modules now run on crawl-verify VM (180.20.0.4:8460)
# import multilogin_fbr
# import multilogin_dgft
# import multilogin_bizfile
VERIFY_VM_URL = "http://180.20.0.4:8460"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crawl-gateway")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = get_secret("cir-api-key")
INTERNAL_API_TOKEN = get_secret("internal-api-token")
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
_ssh_pool = ThreadPoolExecutor(max_workers=30, thread_name_prefix="ssh")
_MAX_QUEUED_JOBS = 100  # Reject new jobs if this many are already running/queued
_JOB_STALE_MINUTES = 90  # Auto-fail "running"/"dispatched" jobs older than this
_REAPER_INTERVAL_SECONDS = 300  # Reaper sweep cadence

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
    2. Redact any _BLOCKED_TERMS in string values (case-insensitive → [REDACTED])
    3. Log a warning when a redaction happens — we still want to know

    TEMP 2026-05-22: switched from hard-fail to silent redact while we
    triage which GC field is firing false positives. Revert when fixed.
    """
    cleaned = {}
    redactions = []

    for key, value in data.items():
        if key in _INTERNAL_FIELDS:
            continue

        if isinstance(value, str):
            lower = value.lower()
            hit = next((t for t in _BLOCKED_TERMS if t in lower), None)
            if hit:
                redactions.append(f"{key}={hit}")
                cleaned[key] = sanitize_text(value)
            else:
                cleaned[key] = value
        elif isinstance(value, list):
            cleaned[key] = _sanitize_list(value, key, redactions)
        elif isinstance(value, dict):
            cleaned[key] = sanitize_payload(value)
        else:
            cleaned[key] = value

    if redactions:
        log.warning("sanitize_payload redacted: %s", "; ".join(redactions))

    return cleaned


def _sanitize_list(items: list, parent_key: str, redactions: list) -> list:
    """Sanitize list items recursively, redacting blocked terms in-place."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(sanitize_payload(item))
        elif isinstance(item, str):
            lower = item.lower()
            hit = next((t for t in _BLOCKED_TERMS if t in lower), None)
            if hit:
                redactions.append(f"{parent_key}[]={hit}")
                result.append(sanitize_text(item))
            else:
                result.append(item)
        else:
            result.append(item)
    return result


def verify_prompt_clean(prompt: str) -> str:
    """
    Final gate before any prompt is sent to OpenClaw.
    Scans the assembled prompt for blocked terms and redacts.

    TEMP 2026-05-22: redact instead of hard-fail while triaging GC payloads.
    """
    lower = prompt.lower()
    hit = next((t for t in _BLOCKED_TERMS if t in lower), None)
    if hit:
        log.warning("verify_prompt_clean redacted blocked term '%s' before dispatch", hit)
        return sanitize_text(prompt)
    return prompt


# ---------------------------------------------------------------------------
# Models — Shared
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    enriching = "enriching"
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

# Multilogin modules run on crawl-verify VM (180.20.0.4:8460)
# No local initialization needed — gateway proxies to verify VM
log.info("Verify VM configured at %s", VERIFY_VM_URL)


@app.on_event("startup")
async def _start_reaper():
    """Launch the stale-job reaper. Without this, jobs that crash/hang on
    the regional VM side stay in "running" forever and eventually exhaust
    _MAX_QUEUED_JOBS, causing all new submissions to 503."""
    asyncio.create_task(_reaper_loop())


# ---------------------------------------------------------------------------
# Rate limiting — per-IP sliding window
# ---------------------------------------------------------------------------

@app.middleware("http")
async def schema_version_middleware(request: Request, call_next):
    """Inject X-API-Version and X-Schema-Version headers on v2 responses."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/v2/"):
        response.headers["X-API-Version"] = V2_VERSION
        # Match longest prefix
        schema_v = None
        for prefix, ver in _V2_SCHEMA_VERSIONS.items():
            if path.startswith(prefix):
                if schema_v is None or len(prefix) > len(schema_v[0]):
                    schema_v = (prefix, ver)
        if schema_v:
            response.headers["X-Schema-Version"] = schema_v[1]
    return response


_RATE_LIMIT_DEFAULT = 30   # max requests per window (most endpoints)
_RATE_WINDOW = 60          # window in seconds
# Per-endpoint overrides (prefix match, longest wins)
_RATE_LIMIT_OVERRIDES = {
    "/api/v2/screening": 600,   # 10 req/s — free, cache-driven
    "/tools/adverse_media": 60, # 1 req/s — paid BD calls
}
_rate_hits: dict[str, collections.deque] = {}
_rate_lock = threading.Lock()


def _rate_limit_for_path(path: str) -> int:
    """Return the rate limit for a given request path."""
    best_prefix, best_limit = "", _RATE_LIMIT_DEFAULT
    for prefix, limit in _RATE_LIMIT_OVERRIDES.items():
        if path.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix, best_limit = prefix, limit
    return best_limit


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Reject requests if a single IP exceeds the per-endpoint rate limit."""
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path
    limit = _rate_limit_for_path(path)
    bucket_key = f"{client_ip}:{path}"
    now = time.monotonic()
    with _rate_lock:
        if bucket_key not in _rate_hits:
            _rate_hits[bucket_key] = collections.deque()
        dq = _rate_hits[bucket_key]
        # Evict old entries
        while dq and dq[0] < now - _RATE_WINDOW:
            dq.popleft()
        if len(dq) >= limit:
            retry_after = int(_RATE_WINDOW - (now - dq[0])) + 1
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded ({limit} req/{_RATE_WINDOW}s)"},
                headers={"Retry-After": str(retry_after)},
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

def _sanitize_openclaw_json(local_file: Path) -> bool:
    """Fix malformed JSON from OpenClaw agents before parsing.

    OpenClaw sometimes emits '___' as field separators instead of proper
    JSON structure.  This strips those artifacts so json.load() succeeds.
    Returns True if the file was modified.
    """
    if not local_file.exists():
        return False
    try:
        raw = local_file.read_text()
        if "___" not in raw:
            return False
        # Remove ___ separators: they appear between JSON key-value pairs
        # e.g.  "date": "2026-05-04",___"other_field": "value",___
        cleaned = raw.replace(',___', ',').replace('___,', ',').replace('___', '')
        # Verify the cleaned version is valid JSON
        json.loads(cleaned)
        local_file.write_text(cleaned)
        log.info("Sanitized ___ separators from %s", local_file.name)
        return True
    except (json.JSONDecodeError, OSError) as e:
        log.warning("JSON sanitize failed for %s: %s", local_file.name, e)
        return False


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
            f"source ~/crawl/config/proxy.env 2>/dev/null; "
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

            # Sanitize OpenClaw output (strips ___ separators) then inject request_id
            if local_file.exists():
                _sanitize_openclaw_json(local_file)
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
                # For CIR: set "enriching" not "completed" — dark web still needs to run
                if scenario == "cir" and region_status == "completed":
                    updates["status"] = "enriching"
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
    Direct HTTP call to dark-web VM gateway (20.86.161.6:8450).
    Submits research request, polls for completion, returns findings.
    Called from thread pool — never blocks event loop.
    """
    import requests as _req
    vm = DARKWEB_VM
    dw_url = f"http://{vm['ip']}:{vm['port']}/api/v1/research"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": vm["api_key"],
    }
    payload = {
        "entity_name": entity_name,
        "country": country,
        "owners": owners or [],
        "domain": domain or "",
        "depth": "heavy",
    }

    try:
        # Submit the research request — this blocks until complete (up to 5 min)
        resp = _req.post(dw_url, json=payload, headers=headers, timeout=300)
        if resp.status_code != 200:
            log.warning("Job %s: dark-web returned HTTP %d", job_id[:8], resp.status_code)
            return {"status": "failed", "findings": [], "error": f"HTTP {resp.status_code}"}

        dw_result = resp.json()
        dw_status = dw_result.get("status", "unknown")
        findings_count = dw_result.get("findings_count", 0)
        blob_path = dw_result.get("blob_path", "")

        log.info("Job %s: dark-web enrichment %s — %d findings, blob: %s",
                 job_id[:8], dw_status, findings_count, blob_path)

        if not blob_path:
            return {"status": dw_status, "findings": [], "summary": dw_result.get("summary", {})}

        # Fetch the full result from blob storage
        # Strip container prefix if gateway included it (e.g. "osint-staging/dark-web/...")
        clean_blob_path = blob_path
        container_prefix = f"{BLOB_CONTAINER}/"
        if clean_blob_path.startswith(container_prefix):
            clean_blob_path = clean_blob_path[len(container_prefix):]
        sas = _BLOB_SAS_TOKEN
        blob_url = f"https://{BLOB_ACCOUNT}.blob.core.windows.net/{BLOB_CONTAINER}/{clean_blob_path}?{sas}"
        blob_resp = _req.get(blob_url, timeout=30)

        if blob_resp.status_code == 200 and blob_resp.text.strip():
            full_result = blob_resp.json()
            log.info("Job %s: dark-web full result — %d findings from %d sources",
                     job_id[:8],
                     full_result.get("summary", {}).get("total_findings", 0),
                     full_result.get("summary", {}).get("sources_searched", 0))
            return full_result

        # Blob not available — return the summary from the initial response
        return {"status": dw_status, "findings": [], "summary": dw_result.get("summary", {})}

    except _req.Timeout:
        log.error("Job %s: dark-web enrichment timed out (300s)", job_id[:8])
        return {"status": "failed", "findings": [], "error": "dark-web search timed out (300s)"}
    except Exception as e:
        log.error("Job %s: dark-web enrichment failed: %s", job_id[:8], e)
        return {"status": "failed", "findings": [], "error": str(e)[:300]}


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
        raw_findings = darkweb_data.get("findings", [])
        sources_searched = summary.get("sources_searched", 0)
        sources_hit = summary.get("sources_with_results", 0)

        # --- Deduplicate findings by (source, type, key identifier) ---
        seen = set()
        findings = []
        for f in raw_findings:
            if f.get("type") == "error":
                continue
            key = (
                f.get("source", ""),
                f.get("type", ""),
                (f.get("title") or f.get("email") or f.get("domain") or
                 f.get("database_name") or f.get("victim") or "")[:100],
            )
            if key not in seen:
                seen.add(key)
                findings.append(f)

        total = len(findings)
        deduplicated = len(raw_findings) - total
        if deduplicated > 0:
            log.info("Dark web dedup: %d raw -> %d unique (%d duplicates removed)",
                     len(raw_findings), total, deduplicated)

        # Recompute by_source / by_type from deduplicated findings
        by_source = {}
        by_type = {}
        for f in findings:
            src = f.get("source", "unknown")
            typ = f.get("type", "unknown")
            by_source[src] = by_source.get(src, 0) + 1
            by_type[typ] = by_type.get(typ, 0) + 1

        # --- Noise types: informational only, don't count toward risk ---
        _NOISE_TYPES = {
            "certificate_transparency", "social_mention", "web_mention",
            "corporate_record", "website_scan", "domain_reputation",
        }
        actionable_findings = [f for f in findings if f.get("type") not in _NOISE_TYPES]
        actionable_count = len(actionable_findings)

        # --- Classify risk level (based on actionable findings only) ---
        def _count_type(t):
            return sum(1 for f in actionable_findings if f.get("type") == t)

        n_infostealer = _count_type("infostealer_exposure")
        n_ransomware = _count_type("ransomware_victim")
        n_darknet = _count_type("dark_web_mention")
        n_sanctions = _count_type("sanctions_pep")
        n_occrp = _count_type("organized_crime_data")
        n_interpol = _count_type("wanted_person")
        n_un_notice = _count_type("un_sanctions_notice")
        n_debarment = _count_type("debarment_record")
        n_offshore = _count_type("offshore_entity")
        n_wikileaks = _count_type("leaked_document")
        n_code_leak = _count_type("code_leak")
        n_adverse = _count_type("adverse_media")
        n_breach = _count_type("breach_record")

        # CRITICAL requires genuinely alarming findings (not just breach DB noise)
        if n_interpol or n_un_notice or n_debarment or n_ransomware:
            risk_level = "CRITICAL"
        elif n_darknet >= 2 or n_sanctions >= 2 or n_occrp >= 2 or n_infostealer >= 2:
            risk_level = "CRITICAL"
        elif (n_darknet or n_infostealer or n_sanctions or n_occrp
              or n_offshore or n_wikileaks or n_code_leak):
            risk_level = "HIGH"
        elif n_adverse or n_breach >= 3 or actionable_count > 10:
            risk_level = "MEDIUM"
        elif actionable_count > 0:
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
            "actionable_findings": actionable_count,
            "duplicates_removed": deduplicated,
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
            noise_note = (f" ({total - actionable_count} informational/noise excluded)"
                          if total > actionable_count else "")
            dw_text = (
                f"DARK WEB SCREENING: {risk_level} — "
                f"{actionable_count} actionable findings across {sources_hit}/{sources_searched} sources{noise_note}. "
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
                f"DARK WEB RISK ({risk_level}): {actionable_count} actionable findings detected. "
                f"Immediate review recommended. Key hits: {'; '.join(key_findings[:3])}"
            )
        elif risk_level == "MEDIUM":
            risk_text = (
                f"DARK WEB RISK (MEDIUM): {actionable_count} actionable findings across "
                f"{sources_hit} sources. Review recommended."
            )
        else:
            risk_text = (
                f"DARK WEB RISK ({risk_level}): {actionable_count} actionable findings. "
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
            "actionable_findings": actionable_count,
            "duplicates_removed": deduplicated,
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


def _load_local_report_json(entity_snake: str, date_str: str) -> dict | None:
    """Read the most-recent enriched local CIR JSON for DB persistence."""
    try:
        candidates = list(LOCAL_OUTPUT_DIR.glob(f"*{entity_snake}*{date_str}*.json"))
        if not candidates:
            return None
        local_file = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        with open(local_file) as f:
            return json.load(f)
    except Exception as e:
        log.warning("Failed to load local report JSON for %s/%s: %s", entity_snake, date_str, e)
        return None


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


def _reap_stale_jobs() -> int:
    """Mark "running"/"dispatched"/"queued"/"enriching" jobs older than
    _JOB_STALE_MINUTES as failed, so they stop counting against the queue cap.

    Returns the number reaped. Safe to call from a background loop — uses
    the same per-job locks as the dispatcher so we never clobber a real write.
    """
    from datetime import datetime, timezone
    reaped = 0
    cutoff_min = _JOB_STALE_MINUTES
    active_states = ("running", "dispatched", "queued", "enriching")
    for f in JOBS_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                d = json.load(fp)
        except Exception:
            continue
        if d.get("status") not in active_states:
            continue
        ts = d.get("updated_at") or d.get("created_at")
        if not ts:
            continue
        try:
            age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 60
        except Exception:
            continue
        if age_min <= cutoff_min:
            continue
        # Take the per-job lock so we don't race a real dispatcher write
        lock = _get_job_lock(d.get("job_id") or f.stem)
        with lock:
            try:
                with open(f) as fp:
                    d = json.load(fp)
            except Exception:
                continue
            if d.get("status") not in active_states:
                continue
            d["status"] = "failed"
            d["error"] = f"orphan_timeout (stale {age_min:.0f}min, reaper cutoff {cutoff_min}min)"
            d["updated_at"] = datetime.now(timezone.utc).isoformat()
            try:
                with open(f, "w") as out:
                    json.dump(d, out, indent=2)
                reaped += 1
                log.warning("Reaper: failed stale job %s (age=%dmin, region=%s)",
                            (d.get("job_id") or "?")[:8], age_min, d.get("region", "?"))
            except Exception as e:
                log.error("Reaper: failed to write %s: %s", f, e)
    return reaped


async def _reaper_loop():
    """Background task: periodically reap stale jobs so the queue cap reflects
    reality. Runs forever; survives individual reap failures."""
    log.info("Reaper started: cutoff=%dmin interval=%ds", _JOB_STALE_MINUTES, _REAPER_INTERVAL_SECONDS)
    while True:
        try:
            n = _reap_stale_jobs()
            if n > 0:
                log.warning("Reaper swept %d stale jobs", n)
        except Exception as e:
            log.error("Reaper sweep failed: %s", e)
        await asyncio.sleep(_REAPER_INTERVAL_SECONDS)


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
        if job.get("status") in ("completed", "enriching") or job.get("blob_path"):
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

                    # NOTE: affiliated entities (trade names, DBAs) are NOT added to
                    # the owners list. They're company names, not people — searching
                    # them as individuals across OCCRP/ICIJ/OpenSanctions produces
                    # massive false positives. The primary entity_name already covers
                    # the company-level search.

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
                dw_sources = dw_summary.get("sources_searched", 0)
                dw_hits = dw_summary.get("sources_with_results", 0)

                # Deduplicate + filter noise (same logic as _inject_darkweb_into_blob)
                _NOISE_TYPES_DW = {
                    "certificate_transparency", "social_mention", "web_mention",
                    "corporate_record", "website_scan", "domain_reputation",
                }
                _seen_dw = set()
                dw_findings_list = []
                for _f in dw_data.get("findings", []):
                    if _f.get("type") == "error":
                        continue
                    _key = (
                        _f.get("source", ""),
                        _f.get("type", ""),
                        (_f.get("title") or _f.get("email") or _f.get("domain") or
                         _f.get("database_name") or _f.get("victim") or "")[:100],
                    )
                    if _key not in _seen_dw:
                        _seen_dw.add(_key)
                        dw_findings_list.append(_f)
                dw_total = len(dw_findings_list)
                dw_actionable = [f for f in dw_findings_list if f.get("type") not in _NOISE_TYPES_DW]

                # Recompute by_type from deduped findings
                dw_by_type = {}
                dw_by_source = dw_summary.get("by_source", {})
                for _f in dw_findings_list:
                    _t = _f.get("type", "unknown")
                    dw_by_type[_t] = dw_by_type.get(_t, 0) + 1

                # Classify (matching thresholds from _inject_darkweb_into_blob)
                def _cnt(t):
                    return sum(1 for f in dw_actionable if f.get("type") == t)
                _n_interpol = _cnt("wanted_person")
                _n_un = _cnt("un_sanctions_notice")
                _n_debarment = _cnt("debarment_record")
                _n_ransomware = _cnt("ransomware_victim")
                _n_darknet = _cnt("dark_web_mention")
                _n_sanctions = _cnt("sanctions_pep")
                _n_occrp = _cnt("organized_crime_data")
                _n_infostealer = _cnt("infostealer_exposure")
                _n_offshore = _cnt("offshore_entity")
                _n_wikileaks = _cnt("leaked_document")
                _n_code_leak = _cnt("code_leak")
                _n_adverse = _cnt("adverse_media")
                _n_breach = _cnt("breach_record")

                if _n_interpol or _n_un or _n_debarment or _n_ransomware:
                    dw_risk = "CRITICAL"
                elif _n_darknet >= 2 or _n_sanctions >= 2 or _n_occrp >= 2 or _n_infostealer >= 2:
                    dw_risk = "CRITICAL"
                elif (_n_darknet or _n_infostealer or _n_sanctions or _n_occrp
                      or _n_offshore or _n_wikileaks or _n_code_leak):
                    dw_risk = "HIGH"
                elif _n_adverse or _n_breach >= 3 or len(dw_actionable) > 10:
                    dw_risk = "MEDIUM"
                elif len(dw_actionable) > 0:
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
                _noise_excluded = dw_total - len(dw_actionable)
                _noise_note = f" ({_noise_excluded} informational excluded)" if _noise_excluded else ""
                banner_lines = [
                    "",
                    "---",
                    "",
                    f"## DARK WEB SCREENING — {dw_risk}",
                    f"**{len(dw_actionable)} actionable findings** from {dw_hits}/{dw_sources} sources via Tor (Netherlands exit node){_noise_note}",
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

                # Source-by-source breakdown with actual findings
                if dw_by_source:
                    banner_lines.append("### Sources with findings")
                    banner_lines.append("")
                    banner_lines.append("| Source | Findings | Details |")
                    banner_lines.append("|--------|----------|---------|")
                    for src_name, src_count in sorted(dw_by_source.items(), key=lambda x: -x[1]):
                        if src_count > 0:
                            # Get sample findings from this source
                            src_findings = [f for f in dw_findings_list if f.get("source", "") == src_name][:3]
                            details = "; ".join(
                                (f.get("title") or f.get("victim") or f.get("domain") or f.get("type", ""))[:60]
                                for f in src_findings
                            )
                            if len(src_findings) < src_count:
                                details += f" (+{src_count - len(src_findings)} more)"
                            banner_lines.append(f"| {src_name} | {src_count} | {details} |")
                    banner_lines.append("")

                # Enumerate all findings (grouped by type)
                if dw_findings_list and dw_total <= 100:
                    banner_lines.append("### All findings detail")
                    banner_lines.append("")
                    by_type_grouped = {}
                    for f in dw_findings_list:
                        ft = f.get("type", "unknown")
                        by_type_grouped.setdefault(ft, []).append(f)
                    for ft in _priority + [t for t in by_type_grouped if t not in _priority]:
                        items = by_type_grouped.get(ft, [])
                        if not items:
                            continue
                        label = _type_labels.get(ft, ft.replace("_", " ").upper())
                        banner_lines.append(f"**{label}** ({len(items)})")
                        for f in items[:20]:
                            title = f.get("title") or f.get("victim") or f.get("domain") or ""
                            src = f.get("source", "")
                            url = f.get("url", "")
                            indiv = f.get("searched_individual", "")
                            line = f"- {title[:100]}"
                            if src:
                                line += f" — *{src}*"
                            if indiv:
                                line += f" (searched: {indiv})"
                            if url:
                                line += f" [{url[:80]}]"
                            banner_lines.append(line)
                        if len(items) > 20:
                            banner_lines.append(f"- ... and {len(items) - 20} more")
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
                    "status": "completed",
                    "dark_web_findings": len(dw_actionable),
                    "dark_web_sources": dw_sources,
                    "report_summary": new_summary,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

                # Persist to crawl_reports DB
                _dw_alert = "CLEAN"
                if len(dw_actionable) >= 16:
                    _dw_alert = "CRITICAL"
                elif len(dw_actionable) >= 6:
                    _dw_alert = "HIGH"
                elif len(dw_actionable) >= 1:
                    _dw_alert = "MEDIUM"
                _job_final = load_job(job_id)
                _report_json = _load_local_report_json(entity_snake, date_str)
                save_cir_report(
                    job_id=job_id,
                    entity_name=entity_name,
                    country=country,
                    region=_job_final.get("region", ""),
                    status="completed",
                    blob_path=_job_final.get("blob_path"),
                    report_summary=new_summary,
                    dark_web_findings=len(dw_actionable),
                    dark_web_sources=dw_sources,
                    dark_web_alert=_dw_alert,
                    seed_data=_job_final.get("seed_data"),
                    created_at=_job_final.get("created_at"),
                    report_json=_report_json,
                )

            except Exception as e:
                log.error("Job %s [cir]: dark-web enrichment failed: %s", job_id[:8], e)
                update_job_fields(job_id, {
                    "status": "completed",
                    "dark_web_error": str(e)[:300],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

                # Still persist to crawl_reports DB (dark web failed but CIR completed)
                _job_err = load_job(job_id)
                _report_json = _load_local_report_json(entity_snake, date_str)
                save_cir_report(
                    job_id=job_id,
                    entity_name=entity_name,
                    country=country,
                    region=_job_err.get("region", ""),
                    status="completed",
                    blob_path=_job_err.get("blob_path"),
                    report_summary=_job_err.get("report_summary"),
                    dark_web_findings=0,
                    dark_web_sources=0,
                    dark_web_alert="ERROR",
                    seed_data=_job_err.get("seed_data"),
                    created_at=_job_err.get("created_at"),
                    report_json=_report_json,
                )


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
                f"source ~/crawl/config/proxy.env 2>/dev/null; "
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
                f"source ~/crawl/config/proxy.env 2>/dev/null; "
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
            f"source ~/crawl/config/proxy.env 2>/dev/null; "
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

def _verify_vm_call(payload: dict) -> dict:
    """Proxy verification request to crawl-verify VM (180.20.0.4:8460)."""
    import requests as _req
    try:
        resp = _req.post(
            f"{VERIFY_VM_URL}/verify",
            json=payload,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            timeout=120,
        )
        return resp.json()
    except Exception as e:
        log.warning("Verify VM call failed: %s", e)
        return {"found": False, "error": str(e)[:200], "note": "Verify VM unreachable"}


# Which countries are supported and what registries we check
_VERIFY_SOURCES = {
    "PK": "SECP eServices (direct) + FBR IRIS ATL (via crawl-verify VM)",
    "IN": "Tofler.in (MCA21 data) + DGFT IEC (via crawl-verify VM)",
    "SG": "ACRA Bizfile (via crawl-verify VM) — UEN, status, address",
    "TR": "GIB VKN tax ID verification (via crawl-verify VM) — company name, tax office",
    "AE": "FTA TRN verification (via crawl-verify VM) — entity name, status",
    "CN": "SAMR/GSXT (via crawl-china VM) — company name, USCC, legal rep",
    "GB": "Companies House (gov.uk) — company name, number, status, address, SIC codes",
    "BR": "Receita Federal CNPJ (BrasilAPI) — company name, status, address, partners, CNAE",
    "US": "SEC EDGAR — CIK, entity type, SIC, EIN, tickers, exchanges, addresses, filings",
    "KR": "DART (FSS) — corp name (KR+EN), stock code, CEO, market, BRN, address, industry",
    "SA": "Wathq (MCI) — commercial registration, owners, managers, capital (free API)",
    "CL": "SII RUT — taxpayer status, economic activities, address (free, no CAPTCHA)",
    "CO": "RUES — commercial registration, NIT, legal form, chamber of commerce (free)",
    "PE": "SUNAT RUC — company name, status, condition, address, economic activity (free API)",
    "MX": "DENUE (INEGI) — establishment name, legal name, activity, address, employee size (free API)",
    "IL": "ICA (data.gov.il) — company name (HE+EN), number, type, status, address (free CKAN API)",
    "CA": "BC OrgBook — entity name, BN, status, type, registration date, jurisdiction (free API)",
    "FR": "Registre National des Entreprises (INSEE/INPI) — SIREN, directors, legal form, activity, address (free API)",
    "TW": "GCIS Open Data (MOEA) — UBN, company name, status, capital, address, responsible person (free JSON API)",
    "BE": "VIES (EU VAT) + KBO/BCE — CBE number, legal name, status, legal form, address (free REST API)",
    "ZA": "GLEIF LEI API (primary) + CIPC eServices enterprise-number (secondary) — legal name, LEI, status, address (directors require paid CIPC account)",
    "PL": "KRS (Krajowy Rejestr Sądowy, Ministry of Justice) — KRS number, NIP, REGON, legal form, address, representatives, PKD codes (free API); VIES fallback for NIP-only lookups",
    "EC": "SRI (Servicio de Rentas Internas) — RUC, legal name, status, economic activity, address (free API)",
    "HK": "ICRIS (Companies Registry) — CR number, company name, status, type (free public search)",
    "CH": "Zefix (FOSC) — UID, legal name, status, legal form, purpose, canton, address (free REST API)",
    "AU": "ABR (Australian Business Register) — ABN, ACN, legal name, entity type, status, GST (free JSONP API)",
    "JP": "Houjin Bangou (NTA) — corporate number, legal name (JP+EN), kind, status, address (free API, needs app ID)",
    "NL": "KvK (Kamer van Koophandel) — KVK number, legal name, status, legal form, address (free public search)",
    "IT": "VIES (EU VAT) — P.IVA, legal name, status, address (free EU tax validation)",
    "AR": "AFIP (CUIT) — CUIT, legal name, tax status, address, economic activities (free API)",
    "EG": "GLEIF LEI Registry — LEI, legal name (AR+EN), status, commercial reg, address (free API, ~322 entities)",
    "ES": "VIES (EU VAT) — CIF, legal name, status, address (free EU tax validation)",
    "DE": "VIES (EU VAT) — USt-IdNr, legal name, status, address (free EU tax validation)",
    "PT": "VIES (EU VAT) — NIPC/NIF, legal name, status, address (free EU tax validation)",
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
        "country_code": "PK",              // PK, IN, TR, AE, CN, GB, BR, US, KR
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
    iec = body.get("iec", "").strip().upper()  # IEC = PAN for Indian companies

    _verify_start = time.monotonic()

    def _persist_verify(resp):
        """Fire-and-forget: persist to crawl_verification DB."""
        resp["duration_ms"] = int((time.monotonic() - _verify_start) * 1000)
        save_verification(resp)
        return resp

    # US allows lookup by CIK or ticker without entity_name
    has_alt_id = country_code == "US" and (body.get("cik", "").strip() or body.get("ticker", "").strip())
    if not country_code or (not entity_name and not has_alt_id):
        raise HTTPException(status_code=422, detail="entity_name and country_code required (US allows cik or ticker instead of entity_name)")
    if country_code not in _VERIFY_SOURCES:
        # Fall through to aggregator scrape for 50+ other countries
        if country_code in aggregator.COUNTRIES:
            log.info("Verify (aggregator): %s (%s)", entity_name, country_code)
            result = await aggregator.lookup(country_code, entity_name)
            if result is not None:
                # If aggregator found no directors, try Deep Lookup as fallback
                # (free preview only — no cost, enriches with revenue/CEO/HQ)
                if not result.get("officers") and not result.get("verified"):
                    log.info("Verify (aggregator empty, trying Deep Lookup fallback): %s (%s)",
                             entity_name, country_code)
                    try:
                        # Call Deep Lookup directly (not full enrich — skip Crunchbase to save time)
                        dl = await asyncio.wait_for(
                            enrichment._query_deep_lookup(entity_name, country_code),
                            timeout=75,
                        )
                        if dl.get("status") == "ok" and dl.get("profile"):
                            p = dl["profile"]
                            result["deep_lookup"] = {
                                "name": p.get("name"),
                                "industry": p.get("industry"),
                                "employee_count": p.get("employee_count"),
                                "headquarters": p.get("headquarters"),
                                "website": p.get("website"),
                                "ceo": p.get("ceo"),
                                "revenue": p.get("revenue"),
                                "founded": p.get("founded"),
                                "source": "deep_lookup",
                                "citations": dl.get("citations", [])[:10],
                            }
                            if p.get("ceo"):
                                result["officers"] = [{
                                    "name": p["ceo"],
                                    "role": "CEO (Deep Lookup)",
                                    "source_url": p.get("website"),
                                    "source_label": "deep_lookup",
                                }]
                    except Exception as e:
                        log.warning("Deep Lookup fallback failed for %s: %s", entity_name, e)

                result["timestamp"] = datetime.now(timezone.utc).isoformat()
                return _persist_verify(result)
        # Country not in gov sources or aggregator — return clean JSON, not HTTP error
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": country_code,
            "verified": False,
            "status": "NOT_COVERED",
            "note": f"Government registry verification not yet available for {country_code}. "
                    f"Direct gov sources: {', '.join(sorted(_VERIFY_SOURCES.keys()))}. "
                    f"Aggregator coverage: {len(aggregator.COUNTRIES)} countries.",
            "timestamp": now,
            "summary": f"{entity_name} ({country_code}) — verification not yet available for this country",
            "duration_ms": 0,
        })

    log.info("Verify: %s (%s) cin=%s ntn=%s", entity_name, country_code,
             cin or "none", ntn or "none")

    loop = asyncio.get_event_loop()

    # --------------- PAKISTAN ---------------
    if country_code == "PK":
        secp_fut = loop.run_in_executor(_ssh_pool, _secp_query_via_ssh, entity_name)
        fbr_fut = loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "PK", "ntn": ntn}
        ) if ntn else None

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
        return _persist_verify(resp)

    # --------------- INDIA ---------------
    if country_code == "IN":
        pan = body.get("pan", "").strip().upper()
        gstin = body.get("gstin", "").strip().upper()

        tofler_fut = loop.run_in_executor(_ssh_pool, _india_tofler_lookup, entity_name, cin)
        dgft_fut = loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "IN", "iec": iec}
        ) if iec else None
        pan_fut = sandbox_india.verify_pan(pan, name=entity_name) if pan else None
        gstin_fut = sandbox_india.verify_gstin(gstin) if gstin else None

        result = await tofler_fut
        dgft = (await dgft_fut) if dgft_fut else None
        pan_result = (await pan_fut) if pan_fut else None
        gstin_result = (await gstin_fut) if gstin_fut else None

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
            "dgft": dgft,
            "pan": pan_result,
            "gstin": gstin_result,
            "validation_source": {
                "registry": "Ministry of Corporate Affairs (MCA21), Government of India",
                "url": result.get("source_url"),
                "record_id": result.get("cin"),
                "how_to_reproduce": (
                    f"Visit https://www.mca.gov.in/content/mca/global/en/mca/fo-llp-services/company-llp-name-search.html → "
                    f"Search for '{entity_name}' or CIN {result.get('cin', 'N/A')}"
                ),
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
        return _persist_verify(resp)

    # --------------- SINGAPORE ---------------
    if country_code == "SG":
        uen = body.get("uen", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "SG", "uen": uen}
        )

        now = datetime.now(timezone.utc).isoformat()
        resp = {
            "entity_name": entity_name, "country_code": "SG",
            "verified": result.get("found", False),
            "uen": result.get("uen"),
            "legal_name": result.get("legal_name"),
            "status": result.get("status"),
            "former_name": result.get("former_name"),
            "industry": result.get("industry"),
            "address": result.get("address"),
            "directors_available": result.get("directors_available", False),
            "directors_note": result.get("directors_note"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
        }
        if result.get("found"):
            resp["summary"] = (
                f"{result.get('legal_name', entity_name)} — UEN {result.get('uen', 'N/A')} — "
                f"{result.get('status', 'Unknown')} — {result.get('industry', '')}")
        else:
            resp["summary"] = f"No ACRA registration found for '{entity_name}'"
            if result.get("error"):
                resp["error"] = result["error"]
        return _persist_verify(resp)

    # --------------- TURKEY ---------------
    if country_code == "TR":
        vkn = body.get("vkn", "").strip()
        if not vkn:
            raise HTTPException(status_code=422, detail="vkn (10-digit tax ID) required for TR verification")
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "TR", "vkn": vkn}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "TR",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name"),
            "vkn": result.get("vkn"),
            "tax_office": result.get("tax_office"),
            "status": result.get("status"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": result.get("legal_name", f"VKN {vkn}") if result.get("found") else f"VKN {vkn} not verified",
        })

    # --------------- UAE ---------------
    if country_code == "AE":
        trn = body.get("trn", "").strip()
        if not trn:
            raise HTTPException(status_code=422, detail="trn (15-digit TRN) required for AE verification")
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "AE", "trn": trn}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "AE",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name"),
            "trn": result.get("trn"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": result.get("legal_name", f"TRN {trn}") if result.get("found") else f"TRN {trn} not verified",
        })

    # --------------- CHINA ---------------
    if country_code == "CN":
        uscc = body.get("uscc", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "CN", "uscc": uscc}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "CN",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name"),
            "uscc": result.get("uscc"),
            "legal_representative": result.get("legal_representative"),
            "status": result.get("status"),
            "registered_capital": result.get("registered_capital"),
            "address": result.get("address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": result.get("legal_name", entity_name) if result.get("found") else f"'{entity_name}' not verified",
        })

    # --------------- UK ---------------
    if country_code == "GB":
        company_number = body.get("company_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "GB", "company_number": company_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "GB",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "company_number": result.get("company_number"),
            "status": result.get("status"),
            "company_type": result.get("company_type"),
            "incorporated_on": result.get("incorporated_on"),
            "registered_address": result.get("registered_address"),
            "sic_codes": result.get("sic_codes"),
            "previous_names": result.get("previous_names"),
            "alternatives": result.get("alternatives"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — #{result.get('company_number', 'N/A')} — "
                f"{result.get('status', 'Unknown')} — Inc: {result.get('incorporated_on', 'N/A')}"
            ) if result.get("found") else f"'{entity_name}' not found in Companies House",
        })

    # --------------- BRAZIL ---------------
    if country_code == "BR":
        cnpj = body.get("cnpj", "").strip()
        if not cnpj:
            raise HTTPException(status_code=422, detail="cnpj (14-digit CNPJ) required for BR verification")
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "BR", "cnpj": cnpj}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "BR",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "trade_name": result.get("trade_name"),
            "cnpj": result.get("cnpj"),
            "status": result.get("status"),
            "date_opened": result.get("date_opened"),
            "legal_nature": result.get("legal_nature"),
            "registered_address": result.get("registered_address"),
            "cnae_code": result.get("cnae_code"),
            "cnae_description": result.get("cnae_description"),
            "capital_social": result.get("capital_social"),
            "partners": result.get("partners"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — CNPJ {result.get('cnpj', 'N/A')} — "
                f"{result.get('status', 'Unknown')} — {result.get('cnae_description', '')}"
            ) if result.get("found") else f"CNPJ not found in Receita Federal",
        })

    # --------------- US (SEC EDGAR) ---------------
    if country_code == "US":
        cik = body.get("cik", "").strip()
        ticker = body.get("ticker", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "US", "cik": cik, "ticker": ticker}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "US",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "cik": result.get("cik"),
            "status": result.get("status"),
            "entity_type": result.get("entity_type"),
            "sic_code": result.get("sic_code"),
            "sic_description": result.get("sic_description"),
            "ein": result.get("ein"),
            "state_of_incorporation": result.get("state_of_incorporation"),
            "tickers": result.get("tickers"),
            "exchanges": result.get("exchanges"),
            "category": result.get("category"),
            "fiscal_year_end": result.get("fiscal_year_end"),
            "phone": result.get("phone"),
            "mailing_address": result.get("mailing_address"),
            "business_address": result.get("business_address"),
            "former_names": result.get("former_names"),
            "total_filings": result.get("total_filings"),
            "alternatives": result.get("alternatives"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — CIK {result.get('cik', 'N/A')} — "
                f"{result.get('status', 'Unknown')} — {result.get('sic_description', '')}"
            ) if result.get("found") else f"'{entity_name}' not found in SEC EDGAR",
        })

    # --------------- SOUTH KOREA (DART/FSS) ---------------
    if country_code == "KR":
        corp_code = body.get("corp_code", "").strip()
        brn = body.get("brn", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "KR", "corp_code": corp_code, "brn": brn}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "KR",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "entity_name_eng": result.get("entity_name_eng"),
            "corp_code": result.get("corp_code"),
            "status": result.get("status"),
            "stock_code": result.get("stock_code"),
            "market": result.get("market"),
            "ceo": result.get("ceo"),
            "business_registration_number": result.get("business_registration_number"),
            "address": result.get("address"),
            "industry_code": result.get("industry_code"),
            "established_date": result.get("established_date"),
            "fiscal_year_end_month": result.get("fiscal_year_end_month"),
            "homepage": result.get("homepage"),
            "phone": result.get("phone"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"{result.get('market', '')} — {result.get('entity_name_eng', '')}"
            ) if result.get("found") else f"'{entity_name}' not found in DART (FSS)",
        })

    # --------------- SAUDI ARABIA (Wathq/MCI) ---------------
    if country_code == "SA":
        cr_number = body.get("cr_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "SA", "cr_number": cr_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "SA",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "cr_number": result.get("cr_number"),
            "trade_name": result.get("trade_name"),
            "status": result.get("status"),
            "business_type": result.get("business_type"),
            "capital": result.get("capital"),
            "issue_date": result.get("issue_date"),
            "expiry_date": result.get("expiry_date"),
            "location": result.get("location"),
            "activities": result.get("activities"),
            "owners": result.get("owners"),
            "managers": result.get("managers"),
            "alternatives": result.get("alternatives"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — CR {result.get('cr_number', 'N/A')} — "
                f"{result.get('status', 'Unknown')}"
            ) if result.get("found") else f"'{entity_name}' not found in MCI (Wathq)",
        })

    # --------------- CHILE (SII RUT) ---------------
    if country_code == "CL":
        rut = body.get("rut", "").strip()
        if not rut:
            raise HTTPException(status_code=422, detail="rut (Chilean RUT e.g. 76123456-7) required for CL verification")
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "CL", "rut": rut}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "CL",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "rut": result.get("rut"),
            "status": result.get("status"),
            "activity_start_date": result.get("activity_start_date"),
            "economic_activities": result.get("economic_activities"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — RUT {result.get('rut', 'N/A')} — "
                f"{result.get('status', 'Unknown')}"
            ) if result.get("found") else f"RUT {rut} not verified via SII",
        })

    # --------------- COLOMBIA (RUES) ---------------
    if country_code == "CO":
        nit = body.get("nit", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "CO", "nit": nit}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "CO",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "nit": result.get("nit"),
            "status": result.get("status"),
            "registration_number": result.get("registration_number"),
            "chamber_of_commerce": result.get("chamber_of_commerce"),
            "legal_form": result.get("legal_form"),
            "category": result.get("category"),
            "economic_activity": result.get("economic_activity"),
            "registration_date": result.get("registration_date"),
            "last_renewal": result.get("last_renewal"),
            "alternatives": result.get("alternatives"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — NIT {result.get('nit', 'N/A')} — "
                f"{result.get('status', 'Unknown')}"
            ) if result.get("found") else f"NIT {nit or entity_name} not found in RUES",
        })

    # --------------- PERU (SUNAT RUC) ---------------
    if country_code == "PE":
        ruc = body.get("ruc", "").strip()
        if not ruc:
            raise HTTPException(status_code=422, detail="ruc (11-digit RUC starting with 10 or 20) required for PE verification")
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "PE", "ruc": ruc}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "PE",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "ruc": result.get("ruc"),
            "status": result.get("status"),
            "condition": result.get("condition"),
            "trade_name": result.get("trade_name"),
            "taxpayer_type": result.get("taxpayer_type"),
            "registered_address": result.get("registered_address"),
            "department": result.get("department"),
            "province": result.get("province"),
            "district": result.get("district"),
            "economic_activity": result.get("economic_activity"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — RUC {result.get('ruc', 'N/A')} — "
                f"{result.get('status', 'Unknown')}"
            ) if result.get("found") else f"RUC {ruc} not found in SUNAT",
        })

    # --------------- MEXICO (DENUE / INEGI) ---------------
    if country_code == "MX":
        rfc = body.get("rfc", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "MX", "rfc": rfc}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "MX",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name") or result.get("entity_name"),
            "rfc": result.get("rfc"),
            "status": result.get("status"),
            "establishment_name": result.get("establishment_name"),
            "economic_activity": result.get("economic_activity"),
            "employee_size": result.get("employee_size"),
            "registered_address": result.get("registered_address"),
            "postal_code": result.get("postal_code"),
            "phone": result.get("phone"),
            "email": result.get("email"),
            "website": result.get("website"),
            "coordinates": result.get("coordinates"),
            "denue_id": result.get("denue_id"),
            "total_matches": result.get("total_matches"),
            "other_matches": result.get("other_matches"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"{result.get('economic_activity', 'N/A')} — "
                f"{result.get('registered_address', 'N/A')}"
            ) if result.get("found") else f"{entity_name} not found in DENUE",
        })

    # --------------- ISRAEL (ICA / data.gov.il) ---------------
    if country_code == "IL":
        company_number = body.get("company_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "IL", "company_number": company_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "IL",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "legal_name_hebrew": result.get("legal_name_hebrew"),
            "legal_name_english": result.get("legal_name_english"),
            "company_number": result.get("company_number"),
            "company_type": result.get("company_type"),
            "status": result.get("status"),
            "incorporation_date": result.get("incorporation_date"),
            "registered_address": result.get("registered_address"),
            "city": result.get("city"),
            "is_government_company": result.get("is_government_company"),
            "limitations": result.get("limitations"),
            "violator": result.get("violator"),
            "last_annual_report_year": result.get("last_annual_report_year"),
            "total_matches": result.get("total_matches"),
            "other_matches": result.get("other_matches"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('legal_name_english') or result.get('entity_name', entity_name)} — "
                f"#{result.get('company_number', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in ICA registry",
        })

    # ── CA (Canada) — BC OrgBook ───────────────────────────────
    if country_code == "CA":
        bn = body.get("business_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "CA", "business_number": bn}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "CA",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "business_number": result.get("business_number"),
            "source_id": result.get("source_id"),
            "entity_type": result.get("entity_type"),
            "entity_type_code": result.get("entity_type_code"),
            "status": result.get("status"),
            "registration_date": result.get("registration_date"),
            "home_jurisdiction": result.get("home_jurisdiction"),
            "registered_jurisdiction": result.get("registered_jurisdiction"),
            "total_matches": result.get("total_matches"),
            "other_matches": result.get("other_matches"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"BN {result.get('business_number', 'N/A')} — {result.get('status', 'Unknown')} — "
                f"{result.get('home_jurisdiction', '')}"
            ) if result.get("found") else f"{entity_name} not found in BC OrgBook",
        })

    # ── FR (France) — Registre National des Entreprises ────────
    if country_code == "FR":
        siren = body.get("siren", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "FR", "siren": siren}
        )
        now = datetime.now(timezone.utc).isoformat()
        directors = result.get("directors")
        return _persist_verify({
            "entity_name": entity_name, "country_code": "FR",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "siren": result.get("siren"),
            "raison_sociale": result.get("raison_sociale"),
            "sigle": result.get("sigle"),
            "legal_form": result.get("legal_form"),
            "status": result.get("status"),
            "creation_date": result.get("creation_date"),
            "category": result.get("category"),
            "employee_range": result.get("employee_range"),
            "economic_activity": result.get("economic_activity"),
            "registered_address": result.get("registered_address"),
            "commune": result.get("commune"),
            "directors": directors,
            "establishments_total": result.get("establishments_total"),
            "establishments_active": result.get("establishments_active"),
            "total_matches": result.get("total_matches"),
            "other_matches": result.get("other_matches"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"SIREN {result.get('siren', 'N/A')} — {result.get('status', 'Unknown')} — "
                f"{result.get('commune', '')}"
            ) if result.get("found") else f"{entity_name} not found in French registry",
        })

    # ── ZA (South Africa) — BizPortal / CIPC ──────────────────
    if country_code == "ZA":
        crn = body.get("crn", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call,
            {"entity_name": entity_name, "country_code": "ZA", "crn": crn},
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "ZA",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name"),
            "crn": result.get("crn"),
            "status": result.get("status"),
            "entity_type": result.get("entity_type"),
            "registered_address": result.get("registered_address"),
            "registration_date": result.get("registration_date"),
            "total_matches": result.get("total_matches"),
            "other_matches": result.get("other_matches"),
            "directors_note": result.get("directors_note"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('legal_name', entity_name)} — "
                f"CRN {result.get('crn', 'N/A')} — {result.get('status', 'Unknown')} — "
                f"{result.get('entity_type', '')}"
            ) if result.get("found") else f"{entity_name} not found in CIPC/BizPortal",
        })

    # ── TW (Taiwan) — MOEA GCIS Open Data ──────────────────────
    if country_code == "TW":
        ubn = body.get("ubn", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "TW", "ubn": ubn}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "TW",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name"),
            "ubn": result.get("ubn"),
            "status": result.get("status"),
            "capital": result.get("capital"),
            "registered_address": result.get("registered_address"),
            "responsible_person": result.get("responsible_person"),
            "establishment_date": result.get("establishment_date"),
            "organisation_type": result.get("organisation_type"),
            "business_scope": result.get("business_scope"),
            "total_matches": result.get("total_matches"),
            "other_matches": result.get("other_matches"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('legal_name', entity_name)} — "
                f"UBN {result.get('ubn', 'N/A')} — {result.get('status', 'Unknown')} — "
                f"{result.get('registered_address', '')}"
            ) if result.get("found") else f"{entity_name} not found in MOEA GCIS registry",
        })

    if country_code == "BE":
        cbe_number = body.get("cbe_number", body.get("cbe", body.get("vat_id", ""))).strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "BE", "cbe_number": cbe_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "BE",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name") or result.get("legal_name"),
            "cbe_number": result.get("cbe_number"),
            "vat_number": result.get("vat_number"),
            "legal_form": result.get("legal_form"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "start_date": result.get("start_date"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('legal_name', entity_name)} — "
                f"CBE {result.get('cbe_number', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in Belgian KBO/BCE registry",
        })

    # --------------- ECUADOR (SRI / Supercias) ---------------
    if country_code == "EC":
        ruc = body.get("ruc", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "EC", "ruc": ruc}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "EC",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name") or result.get("legal_name"),
            "ruc": result.get("ruc"),
            "status": result.get("status"),
            "economic_activity": result.get("economic_activity"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"RUC {result.get('ruc', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in SRI/Supercias",
        })

    # --------------- HONG KONG (ICRIS) ---------------
    if country_code == "HK":
        cr_number = body.get("cr_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "HK", "cr_number": cr_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "HK",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "cr_number": result.get("cr_number"),
            "company_type": result.get("company_type"),
            "status": result.get("status"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"CR# {result.get('cr_number', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in ICRIS",
        })

    # --------------- SWITZERLAND (Zefix) ---------------
    if country_code == "CH":
        uid = body.get("uid", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "CH", "uid": uid}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "CH",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "uid": result.get("uid"),
            "status": result.get("status"),
            "legal_form": result.get("legal_form"),
            "canton": result.get("canton"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"UID {result.get('uid', 'N/A')} — {result.get('status', 'Unknown')} — "
                f"{result.get('canton', '')}"
            ) if result.get("found") else f"{entity_name} not found in Zefix",
        })

    # --------------- AUSTRALIA (ABR) ---------------
    if country_code == "AU":
        abn = body.get("abn", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "AU", "abn": abn}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "AU",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "abn": result.get("abn"),
            "acn": result.get("acn"),
            "entity_type": result.get("entity_type"),
            "status": result.get("status"),
            "state": result.get("state"),
            "registered_address": result.get("registered_address"),
            "gst_registered": result.get("gst_registered"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"ABN {result.get('abn', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in ABR",
        })

    # --------------- JAPAN (Houjin Bangou) ---------------
    if country_code == "JP":
        corp_number = body.get("corp_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "JP", "corp_number": corp_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "JP",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "legal_name_ja": result.get("legal_name_ja"),
            "legal_name_en": result.get("legal_name_en"),
            "corp_number": result.get("corp_number"),
            "kind": result.get("kind"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"Corp# {result.get('corp_number', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in Houjin Bangou",
        })

    # --------------- NETHERLANDS (KvK) ---------------
    if country_code == "NL":
        kvk_number = body.get("kvk_number", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "NL", "kvk_number": kvk_number}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "NL",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "kvk_number": result.get("kvk_number"),
            "status": result.get("status"),
            "legal_form": result.get("legal_form"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"KVK {result.get('kvk_number', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in KvK",
        })

    # --------------- ITALY (VIES) ---------------
    if country_code == "IT":
        partita_iva = body.get("partita_iva", body.get("vat_id", "")).strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "IT", "partita_iva": partita_iva}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "IT",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "partita_iva": result.get("partita_iva"),
            "vat_number": result.get("vat_number"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"P.IVA {result.get('partita_iva', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} — P.IVA required for Italy verification",
        })

    # --------------- ARGENTINA (AFIP) ---------------
    if country_code == "AR":
        cuit = body.get("cuit", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "AR", "cuit": cuit}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "AR",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "cuit": result.get("cuit"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"CUIT {result.get('cuit', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} — CUIT required for Argentina verification",
        })

    # --------------- EGYPT (GLEIF) ---------------
    if country_code == "EG":
        commercial_reg = body.get("commercial_reg", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "EG", "commercial_reg": commercial_reg}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "EG",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name") or result.get("legal_name"),
            "commercial_reg": result.get("commercial_reg"),
            "lei": result.get("lei"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"LEI {result.get('lei', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in GLEIF (EG)",
        })

    # --------------- SPAIN (VIES) ---------------
    if country_code == "ES":
        cif = body.get("cif", body.get("vat_id", "")).strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "ES", "cif": cif}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "ES",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "cif": result.get("cif"),
            "vat_number": result.get("vat_number"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"CIF {result.get('cif', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} — CIF required for Spain verification",
        })

    # --------------- GERMANY (VIES) ---------------
    if country_code == "DE":
        vat_id = body.get("vat_id", body.get("ust_id", "")).strip()
        hrb = body.get("hrb", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "DE", "vat_id": vat_id, "hrb": hrb}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "DE",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "vat_id": result.get("vat_id"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"VAT {result.get('vat_id', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} — USt-IdNr required for Germany verification",
        })

    # --------------- PORTUGAL (VIES) ---------------
    if country_code == "PT":
        nipc = body.get("nipc", body.get("nif", body.get("vat_id", ""))).strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "PT", "nipc": nipc}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "PT",
            "verified": result.get("found", False),
            "legal_name": result.get("entity_name"),
            "nipc": result.get("nipc"),
            "vat_number": result.get("vat_number"),
            "status": result.get("status"),
            "registered_address": result.get("registered_address"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('entity_name', entity_name)} — "
                f"NIPC {result.get('nipc', 'N/A')} — {result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} — NIPC required for Portugal verification",
        })

    # ── PL (Poland) — KRS (Krajowy Rejestr Sądowy) + VIES ──────
    if country_code == "PL":
        krs_number = body.get("krs", "").strip()
        nip = body.get("nip", "").strip()
        result = await loop.run_in_executor(
            _ssh_pool, _verify_vm_call, {"entity_name": entity_name, "country_code": "PL", "krs": krs_number, "nip": nip}
        )
        now = datetime.now(timezone.utc).isoformat()
        return _persist_verify({
            "entity_name": entity_name, "country_code": "PL",
            "verified": result.get("found", False),
            "legal_name": result.get("legal_name"),
            "krs": result.get("krs"),
            "nip": result.get("nip"),
            "regon": result.get("regon"),
            "legal_form": result.get("legal_form"),
            "status": result.get("status"),
            "registration_date": result.get("registration_date"),
            "registered_address": result.get("registered_address"),
            "court": result.get("court"),
            "representatives": result.get("representatives"),
            "pkd_codes": result.get("pkd_codes"),
            "validation_source": result.get("validation_source"),
            "timestamp": now,
            "summary": (
                f"{result.get('legal_name', entity_name)} — "
                f"KRS {result.get('krs', 'N/A')} — NIP {result.get('nip', 'N/A')} — "
                f"{result.get('status', 'Unknown')}"
            ) if result.get("found") else f"{entity_name} not found in KRS registry",
        })


@app.post("/api/v1/verify/lei")
async def verify_lei(
    request: Request,
    _key: str = Depends(verify_api_key),
):
    """
    GLEIF LEI lookup — corporate hierarchy mapping.
    Returns: LEI, entity details, direct parent, ultimate parent.

    Body: {
        "entity_name": "HSBC Holdings",    // Search by name
        "lei": "MLU0ZO3ML4LN2LL2TL39",    // Or direct LEI lookup
        "country_code": "GB",              // Optional country filter
    }
    """
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    lei = body.get("lei", "").strip()
    country_code = body.get("country_code", "").strip()

    if not entity_name and not lei:
        raise HTTPException(status_code=422, detail="entity_name or lei required")

    import requests as _req
    try:
        resp = _req.post(
            f"{VERIFY_VM_URL}/verify/lei",
            json={"entity_name": entity_name, "lei": lei, "country_code": country_code},
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            timeout=30,
        )
        return resp.json()
    except Exception as e:
        log.warning("LEI lookup failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LEI lookup failed: {str(e)[:200]}")


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
_VERIFY_REGISTRY_SUPPORTED = {"PK", "IN", "SG", "TR", "AE", "CN"}


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


_BD_SERP_URL = "https://api.brightdata.com/request"
_BD_SERP_ZONE = os.environ.get("BD_SERP_ZONE", "serp_api1")


def _bd_serp_linkedin_search(query: str, path: str) -> str | None:
    """Find a LinkedIn URL via Bright Data SERP (Google). `path` is 'company' or 'in'.
    Returns first matching linkedin.com/<path>/... URL or None.

    Replaces Tavily (removed — banned per open-source-only rule and key deleted)."""
    if not _BD_DATASETS_KEY:
        return None
    import requests as _req
    google_url = f"https://www.google.com/search?q={quote_plus(query)}&num=10"
    try:
        resp = _req.post(
            _BD_SERP_URL,
            headers={"Authorization": f"Bearer {_BD_DATASETS_KEY}",
                     "Content-Type": "application/json"},
            json={"zone": _BD_SERP_ZONE, "url": google_url, "format": "raw"},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("BD SERP %s for LinkedIn search: %s", resp.status_code, resp.text[:200])
            return None
        body = resp.text
        # Google wraps result URLs in /url?q=<encoded>&... — extract the LinkedIn one
        pattern = rf"https?://(?:[a-z]{{2,3}}\.)?linkedin\.com/{path}/[A-Za-z0-9\-._%/]+"
        for m in re.finditer(pattern, body):
            url = m.group(0)
            # Strip Google tracking + URL-encoded artifacts
            url = url.split("&")[0].split("%26")[0].rstrip("/")
            return url
    except Exception as e:
        log.warning("BD SERP LinkedIn search error: %s", e)
    return None


def _linkedin_lookup_company(entity_name: str, linkedin_url: str, domain: str) -> dict:
    """Stateless company lookup — extracted from _verify_check_linkedin_company,
    minus the job-state plumbing. Uses BD SERP for URL discovery (not Tavily).
    Returns a flat dict."""
    try:
        url = linkedin_url
        if not url:
            query = f'"{entity_name}" {domain} site:linkedin.com/company/' if domain \
                    else f'"{entity_name}" site:linkedin.com/company/'
            url = _bd_serp_linkedin_search(query, "company")
        if not url:
            return {"found": False, "note": "LinkedIn company page not found via search"}

        snapshot_id = _brightdata_trigger(_BD_LINKEDIN_COMPANY_ID, [{"url": url}])
        if not snapshot_id:
            return {"found": False, "error": "Bright Data trigger failed"}

        poll_status = _brightdata_poll(snapshot_id, timeout_s=60)
        if poll_status != "ready":
            return {"found": False, "error": f"Bright Data poll: {poll_status}"}

        data = _brightdata_download(snapshot_id)
        if not data:
            return {"found": False, "note": "No data returned from LinkedIn scrape"}

        company = data[0]
        return {
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
        }
    except Exception as e:
        return {"found": False, "error": str(e)[:300]}


def _linkedin_lookup_persons(entity_name: str, persons: list) -> dict:
    """Stateless persons lookup — extracted from _verify_check_linkedin_persons,
    minus the job-state plumbing. Returns a flat dict with per-person verification."""
    try:
        found_urls = []
        person_url_map = {}
        for person in persons:
            query = f'"{person}" "{entity_name}" site:linkedin.com/in/'
            url = _bd_serp_linkedin_search(query, "in")
            if url:
                found_urls.append({"url": url})
                person_url_map[url] = person

        if not found_urls:
            return {
                "confirmed_count": 0,
                "total_searched": len(persons),
                "persons_verification": [
                    {"name": p, "works_at_entity": False,
                     "note": f"No LinkedIn profile found via search"}
                    for p in persons
                ],
                "note": "No LinkedIn profiles found matching these persons at this entity",
            }

        snapshot_id = _brightdata_trigger(_BD_LINKEDIN_PEOPLE_ID, found_urls)
        if not snapshot_id:
            return {"error": "Bright Data trigger failed for person profiles"}

        poll_status = _brightdata_poll(snapshot_id, timeout_s=300)
        if poll_status != "ready":
            return {"error": f"Bright Data poll: {poll_status}"}

        data = _brightdata_download(snapshot_id)
        confirmed = []
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

        confirmed_names = {p["declared_name"] for p in confirmed}
        verification = []
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

        return {
            "confirmed_count": len(confirmed),
            "total_searched": len(persons),
            "persons_verification": verification,
            "profiles": confirmed or None,
            "note": (f"{len(confirmed)}/{len(persons)} persons confirmed at entity on LinkedIn"
                     if confirmed else
                     "No persons could be confirmed at this entity on LinkedIn"),
        }
    except Exception as e:
        return {"error": str(e)[:300]}


@app.post("/api/v1/linkedin/lookup")
async def linkedin_lookup(request: Request, _key: str = Depends(verify_api_key)):
    """LinkedIn-only lookup via Bright Data. Synchronous, no job state.

    NinjaPear replacement for .11. Returns company profile and (optionally)
    per-person verification.

    Body: {
        "entity_name": "Tesco PLC",        // required
        "domain": "tesco.com",              // optional — helps URL discovery
        "linkedin_url": "",                 // optional — skips Tavily if known
        "persons": ["Ken Murphy"]           // optional — verifies each at entity
    }
    """
    body = await request.json()
    entity_name = (body.get("entity_name") or "").strip()
    if not entity_name:
        raise HTTPException(status_code=422, detail="entity_name required")
    domain = (body.get("domain") or "").strip()
    linkedin_url = (body.get("linkedin_url") or "").strip()
    persons = body.get("persons") or []
    if persons and not isinstance(persons, list):
        raise HTTPException(status_code=422, detail="persons must be a list of strings")

    log.info("LinkedIn lookup: %s (domain=%s, persons=%d)", entity_name, domain or "none", len(persons))

    t0 = time.time()
    company = await asyncio.to_thread(
        _linkedin_lookup_company, entity_name, linkedin_url, domain)
    persons_result = None
    if persons:
        persons_result = await asyncio.to_thread(
            _linkedin_lookup_persons, entity_name, persons)

    return {
        "entity_name": entity_name,
        "domain": domain or None,
        "company": company,
        "persons": persons_result,
        "duration_ms": int((time.time() - t0) * 1000),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "LinkedIn via Bright Data Web Scraper API",
    }


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

        # --- Dual-write: persist full JSON + summary to crawl_reports.darkweb_reports ---
        try:
            _dw_full = None
            if local_file.exists() and local_file.stat().st_size > 0:
                with open(local_file) as _f:
                    _dw_full = json.load(_f)
            _summary = (_dw_full or {}).get("summary", {}) if isinstance(_dw_full, dict) else {}
            _alert = "CLEAN"
            if findings_count >= 16:
                _alert = "CRITICAL"
            elif findings_count >= 6:
                _alert = "HIGH"
            elif findings_count >= 1:
                _alert = "MEDIUM"
            _job_dw = load_job(job_id)
            save_darkweb_report(
                job_id=job_id,
                entity_name=entity_name,
                country=country,
                owners=owners or [],
                domain=domain or "",
                depth=depth,
                status=updates["status"],
                blob_path=this_blob,
                findings_count=findings_count,
                sources_searched=_summary.get("sources_searched", 0),
                sources_with_results=_summary.get("sources_with_results", 0),
                alert_level=_alert,
                report_summary=updates.get("report_summary"),
                error=updates.get("error"),
                report_json=_dw_full,
                created_at=(_job_dw or {}).get("created_at"),
            )
        except Exception as _e:
            log.warning("Job %s [dark-web]: DB dual-write failed: %s", job_id[:8], _e)

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


# ---------------------------------------------------------------------------
# Adverse Media Tool — /tools/adverse_media
# ---------------------------------------------------------------------------

_internal_token_header = APIKeyHeader(name="X-Internal-Token", auto_error=False)


async def verify_internal_token(
    request: Request,
    token: str = Security(_internal_token_header),
):
    """Auth for internal tool endpoints. Accepts X-Internal-Token or falls back to X-API-Key."""
    # Accept internal token
    if token and INTERNAL_API_TOKEN and token == INTERNAL_API_TOKEN:
        return token
    # Fall back to standard API key (so OpenClaw agents can also call this)
    key = request.headers.get("X-API-Key", "")
    if not key:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
    if key and key == API_KEY:
        return key
    client_ip = request.client.host if request.client else "unknown"
    log.warning("INTERNAL AUTH FAIL from %s", client_ip)
    raise HTTPException(status_code=403, detail="Invalid token")


class AdverseMediaRequest(BaseModel):
    entity_id: int = Field(0, description="Entity ID (echoed back, not used for lookup)")
    company_name: str = Field(..., description="Full legal name")
    country: str = Field(..., description="ISO 2-letter country code", min_length=2, max_length=2)
    languages: Optional[list[str]] = Field(None, description="ISO 639-1 codes; defaults by country")
    tier: str = Field("STANDARD", description="BASE | STANDARD | ENHANCED")
    days_back: int = Field(7, ge=1, le=90, description="Lookback window in days")
    max_results: int = Field(20, ge=1, le=250, description="Max articles returned")
    website: Optional[str] = Field(None, description="Entity website for shell signal checks")


@app.post("/tools/adverse_media")
async def adverse_media_scan(req: AdverseMediaRequest, _: str = Depends(verify_internal_token)):
    """Multi-provider adverse media scan. Returns structured articles + shell signals."""
    result = await adverse_media.scan(
        company_name=req.company_name,
        country=req.country.upper(),
        entity_id=req.entity_id,
        languages=req.languages,
        tier=req.tier.upper(),
        days_back=req.days_back,
        max_results=req.max_results,
        website=req.website,
    )
    return result


@app.get("/tools/adverse_media/health")
async def adverse_media_health():
    """Provider health check — no auth required (used by RegistryAdapterHealth probe)."""
    return await adverse_media.health()


# ===========================================================================
# API v2 — Crawl Data Gateway
# ===========================================================================
# Clean, versioned endpoints for GC, Onboarding, SalesTracker, iPhone app.
# No AI, no dark web. Structured data with validation_source on every response.
#
# Roles (no duplication):
#   /api/v2/verify     → gov registry verification (crawl-verify VM)
#   /api/v2/verify/lei → GLEIF corporate hierarchy (crawl-verify VM)
#   /api/v2/media      → adverse media (GDELT, BD SERP, BD Discover on crawldevvm)
#   /api/v2/enrich     → company enrichment (Crunchbase + Deep Lookup via Bright Data)
#   /api/v2/lookup     → one-shot fan-out (verify + LEI + media + enrich)
#   /api/v2/health     → per-source health
#
# NOT in v2 (covered elsewhere):
#   Sanctions/PEP/watchlist → LexisNexis Bridger (on GC)
#   Offshore leaks → ICIJ Neo4j mirror (on .11)
#   Market data (SEC/GLEIF/Yahoo/OpenFIGI) → GC direct (sub-second)
#   Trade data (Volza/Panjiva) → GC deepdive.py (custom parsing)
# ===========================================================================

V2_VERSION = "2.2.0"

# Schema versions per endpoint — bump when response shape changes.
# GC can pin to a schema version via Accept header or just read the response header.
_V2_SCHEMA_VERSIONS = {
    "/api/v2/verify": "1.0",
    "/api/v2/verify/pan": "1.0",
    "/api/v2/verify/gstin": "1.0",
    "/api/v2/verify/lei": "1.0",
    "/api/v2/media": "1.0",
    "/api/v2/enrich": "1.0",
    "/api/v2/screening": "1.0",
    "/api/v2/lookup": "1.0",
    "/api/v2/health": "1.0",
    "/api/v2/metrics": "1.0",
    "/api/v2/raw": "1.0",
}


@app.post("/api/v2/verify")
async def v2_verify(request: Request, _key: str = Depends(verify_api_key)):
    """
    Registry verification — v2 passthrough to v1 verify.
    Same logic, clean v2 path. See /api/v1/verify for full docs.
    """
    return await verify_entity(request, _key)


@app.post("/api/v2/verify/pan")
async def v2_verify_pan(request: Request, _key: str = Depends(verify_api_key)):
    """
    India PAN verification via Sandbox.co.in.

    Body: {
        "pan": "AAACR5055K",                    // REQUIRED — 10-char PAN
        "name": "RELIANCE INDUSTRIES LIMITED",   // REQUIRED — name as per PAN
        "dob": "08/05/1973"                      // REQUIRED — DD/MM/YYYY (incorporation date for companies)
    }

    Response: {
        "verified": true,
        "pan": "AAACR5055K",
        "status": "valid",
        "category": "company",
        "name_match": true,
        "dob_match": true,
        "aadhaar_linked": "na",
        "validation_source": {...},
        "latency_ms": 1234
    }
    """
    body = await request.json()
    pan = body.get("pan", "").strip().upper()
    if not pan:
        raise HTTPException(status_code=422, detail="pan required (10-char, e.g. AAACR5055K)")
    return await sandbox_india.verify_pan(
        pan,
        name=body.get("name", "").strip(),
        dob=body.get("dob", "").strip(),
    )


@app.post("/api/v2/verify/gstin")
async def v2_verify_gstin(request: Request, _key: str = Depends(verify_api_key)):
    """
    India GSTIN verification via Sandbox.co.in.

    Body: {
        "gstin": "27AAACR5055K1Z7"    // REQUIRED — 15-char GSTIN
    }

    Response: {
        "verified": true,
        "gstin": "27AAACR5055K1Z7",
        "legal_name": "RELIANCE INDUSTRIES LIMITED",
        "trade_name": "RELIANCE",
        "status": "Active",
        "taxpayer_type": "Regular",
        "constitution": "Public Limited Company",
        "registration_date": "01/07/2017",
        "business_activities": ["Factory / Manufacturing", ...],
        "address": {"building": "...", "street": "...", "district": "...", "state": "...", "pincode": "..."},
        "state_jurisdiction": "...",
        "center_jurisdiction": "...",
        "validation_source": {...},
        "latency_ms": 1234
    }
    """
    body = await request.json()
    gstin = body.get("gstin", "").strip().upper()
    if not gstin:
        raise HTTPException(status_code=422, detail="gstin required (15-char, e.g. 27AAACR5055K1Z7)")
    return await sandbox_india.verify_gstin(gstin)


@app.post("/api/v2/verify/lei")
async def v2_verify_lei(request: Request, _key: str = Depends(verify_api_key)):
    """
    GLEIF LEI lookup — v2 passthrough to v1 verify/lei.
    """
    return await verify_lei(request, _key)


@app.post("/api/v2/media")
async def v2_media(request: Request, _key: str = Depends(verify_api_key)):
    """
    Adverse media scan — v2 wrapper around /tools/adverse_media.
    Accepts v2 field names (entity_name → company_name).
    """
    body = await request.json()

    # Map v2 field names to internal adverse_media field names
    am_request = AdverseMediaRequest(
        company_name=body.get("entity_name") or body.get("company_name", ""),
        country=body.get("country_code") or body.get("country", "XX"),
        entity_id=body.get("entity_id", 0),
        languages=body.get("languages"),
        tier=body.get("tier", "STANDARD"),
        days_back=body.get("days_back", 7),
        max_results=body.get("max_results", 20),
        website=body.get("domain") or body.get("website"),
    )

    result = await adverse_media.scan(
        company_name=am_request.company_name,
        country=am_request.country.upper(),
        entity_id=am_request.entity_id,
        languages=am_request.languages,
        tier=am_request.tier.upper(),
        days_back=am_request.days_back,
        max_results=am_request.max_results,
        website=am_request.website,
    )
    return result


@app.post("/api/v2/screening")
async def v2_screening(request: Request, _key: str = Depends(verify_api_key)):
    """
    Sanctions & watchlist screening — 7 sources in parallel.
    CSL (11 US gov lists), UK FCDO, EU, UN SC, FBI, INTERPOL, OpenSanctions.

    Body: {
        "entity_name": "NIS A.D. NOVI SAD",
        "country": "RS",            // optional, ISO-2
        "entity_type": "company"     // optional: company|person|both (default: both)
    }
    """
    body = await request.json()
    entity_name = (body.get("entity_name") or "").strip()
    if not entity_name:
        raise HTTPException(status_code=422, detail="entity_name required")

    result = await screening.screen(
        entity_name=entity_name,
        country=(body.get("country") or "").strip().upper(),
        entity_type=(body.get("entity_type") or "both").strip().lower(),
    )
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result


@app.post("/api/v2/enrich")
async def v2_enrich(request: Request, _key: str = Depends(verify_api_key)):
    """
    Company enrichment — Crunchbase + Deep Lookup via Bright Data.
    Returns structured company profile with citations from 1000+ sources.

    Body: {
        "entity_name": "Tesla Inc",
        "country_code": "US",       // optional
        "domain": "tesla.com"       // optional, improves Crunchbase match
    }
    """
    body = await request.json()
    entity_name = (body.get("entity_name") or "").strip()
    if not entity_name:
        raise HTTPException(status_code=422, detail="entity_name required")

    result = await enrichment.enrich(
        entity_name=entity_name,
        country_code=(body.get("country_code") or "").strip().upper(),
        domain=(body.get("domain") or "").strip(),
    )
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result


@app.post("/api/v2/lookup")
async def v2_lookup(request: Request, _key: str = Depends(verify_api_key)):
    """
    One-shot lookup — fan-out to verify + LEI + media in parallel.
    Returns combined result. Designed for iPhone app / quick lookups.

    Body: {
        "entity_name": "Samsung Electronics",
        "country_code": "KR",
        "ticker": "",
        "domain": "samsung.com"
    }
    """
    body = await request.json()
    entity_name = body.get("entity_name", "").strip()
    country_code = body.get("country_code", "").strip().upper()

    if not entity_name:
        raise HTTPException(status_code=422, detail="entity_name required")

    t0 = time.time()
    loop = asyncio.get_event_loop()

    # --- Fan-out: verify + LEI + media in parallel ---

    # 1. Registry verify (only if country is supported)
    async def _do_verify():
        if country_code and country_code in _VERIFY_SOURCES:
            try:
                result = await loop.run_in_executor(
                    _ssh_pool, _verify_vm_call,
                    {**body, "entity_name": entity_name, "country_code": country_code},
                )
                return {
                    "verified": result.get("found", False),
                    "legal_name": result.get("entity_name"),
                    "status": result.get("status"),
                    "summary": result.get("summary", ""),
                    "validation_source": result.get("validation_source"),
                }
            except Exception as e:
                return {"verified": False, "error": str(e)[:200]}
        return {"verified": False, "note": f"Country {country_code} not supported for verify"}

    # 2. LEI lookup (sync call in executor)
    def _do_lei_sync():
        import requests as _req
        try:
            resp = _req.post(
                f"{VERIFY_VM_URL}/verify/lei",
                json={"entity_name": entity_name, "country_code": country_code},
                headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
                timeout=20,
            )
            data = resp.json()
            if data.get("found"):
                return {
                    "found": True,
                    "lei": data.get("lei"),
                    "entity_name": data.get("entity_name"),
                    "parent": data.get("parent"),
                    "ultimate_parent": data.get("ultimate_parent"),
                    "jurisdiction": data.get("jurisdiction"),
                }
            return {"found": False}
        except Exception as e:
            return {"found": False, "error": str(e)[:200]}

    # 3. Adverse media
    async def _do_media():
        try:
            result = await asyncio.wait_for(
                adverse_media.scan(
                    company_name=entity_name,
                    country=country_code or "XX",
                    days_back=30,
                    max_results=10,
                    website=body.get("domain"),
                ),
                timeout=35,
            )
            articles = result.get("articles", [])
            # Determine risk level from article count
            n = len(articles)
            if n == 0:
                risk = "NONE"
            elif n <= 3:
                risk = "LOW"
            elif n <= 10:
                risk = "MEDIUM"
            else:
                risk = "HIGH"
            top = articles[0] if articles else None
            return {
                "total_articles": n,
                "risk_level": risk,
                "top_article": f"{top['title'][:100]} — {top['source']}" if top else None,
                "providers": {k: v["status"] for k, v in result.get("providers", {}).items()},
            }
        except asyncio.TimeoutError:
            return {"total_articles": 0, "risk_level": "UNKNOWN", "error": "media scan timed out"}
        except Exception as e:
            return {"total_articles": 0, "risk_level": "UNKNOWN", "error": str(e)[:200]}

    # 4. Enrichment (Crunchbase + Deep Lookup)
    async def _do_enrich():
        try:
            result = await asyncio.wait_for(
                enrichment.enrich(
                    entity_name=entity_name,
                    country_code=country_code,
                    domain=body.get("domain", ""),
                ),
                timeout=70,
            )
            if result.get("profile"):
                return {
                    "status": result["status"],
                    "name": result["profile"].get("name"),
                    "industry": result["profile"].get("industries") or result["profile"].get("industry"),
                    "employee_count": result["profile"].get("employee_count"),
                    "headquarters": result["profile"].get("headquarters") or result["profile"].get("region"),
                    "website": result["profile"].get("website"),
                    "revenue": result["profile"].get("revenue"),
                }
            return {"status": result.get("status", "not_found")}
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}

    # 5. Screening (sanctions + watchlists)
    async def _do_screening():
        try:
            result = await asyncio.wait_for(
                screening.screen(
                    entity_name=entity_name,
                    country=country_code,
                    entity_type="both",
                ),
                timeout=45,
            )
            return {
                "status": result.get("status", "clear"),
                "risk_level": result.get("risk_level", "CLEAR"),
                "total_hits": result.get("total_hits", 0),
                "sources": {k: v.get("status", "error") for k, v in result.get("sources", {}).items()},
            }
        except asyncio.TimeoutError:
            return {"status": "timeout", "risk_level": "UNKNOWN"}
        except Exception as e:
            return {"status": "error", "risk_level": "UNKNOWN", "error": str(e)[:200]}

    # Run all five in parallel
    verify_r, lei_r, media_r, enrich_r, screening_r = await asyncio.gather(
        _do_verify(),
        loop.run_in_executor(None, _do_lei_sync),
        _do_media(),
        _do_enrich(),
        _do_screening(),
    )

    duration_ms = int((time.time() - t0) * 1000)

    return {
        "entity_name": entity_name,
        "country_code": country_code,
        "lookup_time_ms": duration_ms,
        "registry": verify_r,
        "lei": lei_r,
        "media": media_r,
        "enrichment": enrich_r,
        "screening": screening_r,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v2/health")
async def v2_health():
    """
    Per-source health — shows each upstream source individually.
    No auth required (monitoring probes).
    """
    import requests as _req

    sources = {}

    # 1. Gateway itself
    sources["gateway"] = {
        "status": "up",
        "version": API_VERSION,
        "v2_version": V2_VERSION,
        "active_threads": len(_ssh_pool._threads) if hasattr(_ssh_pool, '_threads') else 0,
    }

    # 2. Verify VM
    try:
        resp = _req.get(f"{VERIFY_VM_URL}/health", timeout=5)
        if resp.status_code == 200:
            vdata = resp.json()
            sources["verify_vm"] = {
                "status": "up",
                "version": vdata.get("version", "unknown"),
                "countries": vdata.get("countries", []),
            }
        else:
            sources["verify_vm"] = {"status": f"down (HTTP {resp.status_code})"}
    except Exception as e:
        sources["verify_vm"] = {"status": f"down ({type(e).__name__})"}

    # 3. Adverse media providers
    try:
        am_health = await adverse_media.health()
        sources["adverse_media"] = am_health.get("providers", {})
    except Exception as e:
        sources["adverse_media"] = {"status": f"down ({e})"}

    # 4. Enrichment providers
    try:
        en_health = await enrichment.health()
        sources["enrichment"] = en_health.get("providers", {})
    except Exception as e:
        sources["enrichment"] = {"status": f"down ({e})"}

    # 5. Screening providers
    try:
        sc_health = await screening.health()
        sources["screening"] = sc_health.get("providers", {})
    except Exception as e:
        sources["screening"] = {"status": f"down ({e})"}

    # 6. Aggregator registry (50+ countries via Firecrawl)
    try:
        agg_health = await aggregator.health()
        sources["aggregator"] = agg_health
    except Exception as e:
        sources["aggregator"] = {"status": f"down ({e})"}

    # 7. Supported countries for verify
    sources["verify_countries"] = sorted(set(
        list(_VERIFY_SOURCES.keys()) + aggregator.supported_countries()
    ))

    # 8. Raw response store stats
    try:
        sources["raw_store"] = raw_store.stats()
    except Exception as e:
        sources["raw_store"] = {"status": f"error ({e})"}

    return {
        "status": "ok",
        "service": "crawl-data-gateway",
        "api_version": V2_VERSION,
        "sources": sources,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Latency Metrics — p50/p95/p99 per endpoint from api_access_log
# ---------------------------------------------------------------------------

# Per-call cost in USD for each v2 endpoint (upstream provider costs)
_ENDPOINT_COSTS = {
    "/api/v2/verify": {
        "gov_registry": {"cost_per_call": 0.00, "note": "Free gov registries via crawl-verify VM"},
        "aggregator": {"cost_per_call": 0.02, "note": "~5 Firecrawl searches × $0.004/search"},
        "deep_lookup_fallback": {"cost_per_call": 0.00, "note": "Free preview only, no trigger"},
        "total_typical": 0.02,
    },
    "/api/v2/verify/lei": {
        "gleif": {"cost_per_call": 0.00, "note": "Free GLEIF API"},
        "total_typical": 0.00,
    },
    "/api/v2/screening": {
        "csl": {"cost_per_call": 0.00, "note": "Free US gov API (subscription key)"},
        "uk_fcdo": {"cost_per_call": 0.00, "note": "Free XML, cached 12h"},
        "un_sc": {"cost_per_call": 0.00, "note": "Free XML, cached 12h"},
        "fbi": {"cost_per_call": 0.00, "note": "Free JSON API, cached 12h"},
        "interpol": {"cost_per_call": 0.00, "note": "Free REST API"},
        "total_typical": 0.00,
    },
    "/api/v2/media": {
        "gdelt": {"cost_per_call": 0.00, "note": "Free public API"},
        "bd_serp": {"cost_per_call": 0.005, "note": "Bright Data SERP, ~$5/1K"},
        "bd_discover": {"cost_per_call": 0.01, "note": "Bright Data Discover, ~$10/1K"},
        "crt_sh": {"cost_per_call": 0.00, "note": "Free CT log"},
        "wayback": {"cost_per_call": 0.00, "note": "Free CDX API"},
        "translate": {"cost_per_call": 0.001, "note": "Claude Haiku per language"},
        "total_typical": 0.02,
    },
    "/api/v2/enrich": {
        "crunchbase": {"cost_per_call": 0.01, "note": "Bright Data Web Scraper"},
        "deep_lookup": {"cost_per_call": 0.00, "note": "Free preview only (10 samples)"},
        "total_typical": 0.01,
    },
    "/api/v2/lookup": {
        "note": "Fan-out: verify + LEI + media + enrich + screening",
        "total_typical": 0.05,
    },
}


@app.get("/api/v2/metrics")
async def v2_metrics(_key: str = Depends(verify_api_key)):
    """
    Latency SLOs — p50/p95/p99 per endpoint from api_access_log.
    Includes per-call cost breakdown.
    """
    from event_log import _get_conn

    metrics = {}
    try:
        conn = _get_conn()
        cur = conn.cursor()

        # Aggregate latency by endpoint path prefix
        cur.execute("""
            SELECT
                CASE
                    WHEN path = '/api/v2/verify/lei' THEN '/api/v2/verify/lei'
                    WHEN path LIKE '/api/v2/raw/%' THEN '/api/v2/raw'
                    WHEN path LIKE '/api/v2/%' THEN split_part(path, '/', 4)
                    WHEN path LIKE '/tools/adverse_media%' THEN '/tools/adverse_media'
                    ELSE path
                END as endpoint,
                count(*) as request_count,
                round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)::numeric, 0) as p50_ms,
                round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 0) as p95_ms,
                round(percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms)::numeric, 0) as p99_ms,
                round(avg(duration_ms)::numeric, 0) as avg_ms,
                min(duration_ms) as min_ms,
                max(duration_ms) as max_ms,
                count(*) FILTER (WHERE status_code >= 500) as error_5xx,
                count(*) FILTER (WHERE status_code = 200) as success_count
            FROM api_access_log
            WHERE duration_ms > 0
              AND path LIKE '/api/v2/%'
              AND status_code = 200
            GROUP BY endpoint
            ORDER BY request_count DESC
        """)

        for row in cur.fetchall():
            endpoint = row[0]
            path_key = f"/api/v2/{endpoint}" if not endpoint.startswith("/") else endpoint
            metrics[path_key] = {
                "request_count": row[1],
                "latency_ms": {
                    "p50": int(row[2]),
                    "p95": int(row[3]),
                    "p99": int(row[4]),
                    "avg": int(row[5]),
                    "min": row[6],
                    "max": row[7],
                },
                "errors_5xx": row[8],
                "cost": _ENDPOINT_COSTS.get(path_key, {}),
            }

        # Also get /tools/adverse_media
        cur.execute("""
            SELECT count(*),
                round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)::numeric, 0),
                round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric, 0),
                round(percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms)::numeric, 0),
                round(avg(duration_ms)::numeric, 0),
                min(duration_ms), max(duration_ms)
            FROM api_access_log
            WHERE path = '/tools/adverse_media' AND status_code = 200 AND duration_ms > 0
        """)
        row = cur.fetchone()
        if row and row[0] > 0:
            metrics["/tools/adverse_media"] = {
                "request_count": row[0],
                "latency_ms": {
                    "p50": int(row[1]), "p95": int(row[2]), "p99": int(row[3]),
                    "avg": int(row[4]), "min": row[5], "max": row[6],
                },
                "cost": _ENDPOINT_COSTS.get("/api/v2/media", {}),
            }

        conn.close()
    except Exception as e:
        return {"status": "error", "error": str(e), "metrics": {}}

    # Cost summary — variable (per-call) + fixed (infrastructure)
    fixed_monthly = 550  # midpoint of $455-645 range
    cir_flow_cost = {
        "variable_per_entity": {
            "verify": 0.02,
            "screening": 0.00,
            "media": 0.02,
            "enrich": 0.01,
            "total": 0.05,
        },
        "fixed_monthly": {
            "multilogin": 80,
            "dehashed": 15,
            "azure_vms": "190-260",
            "azure_backup": "80-120",
            "azure_storage": "8-10",
            "claude_api": "50-100",
            "deepseek_api": "15-30",
            "networking": "10-20",
            "total_range": "455-645",
            "note": "Bright Data proxy costs included in variable per-call costs above",
        },
        "loaded_per_entity": {
            "10_entities_day": {"variable_mo": 15, "fixed_mo": fixed_monthly, "total_mo": 15 + fixed_monthly, "per_entity": round((15 + fixed_monthly) / 300, 2)},
            "50_entities_day": {"variable_mo": 75, "fixed_mo": fixed_monthly, "total_mo": 75 + fixed_monthly, "per_entity": round((75 + fixed_monthly) / 1500, 2)},
            "100_entities_day": {"variable_mo": 150, "fixed_mo": fixed_monthly, "total_mo": 150 + fixed_monthly, "per_entity": round((150 + fixed_monthly) / 3000, 2)},
        },
        "note": "Variable = Bright Data (SERP, Discover, Web Scraper), Firecrawl. Fixed = Multilogin, Dehashed, Azure VMs/backup/storage, Claude/DeepSeek APIs.",
    }

    return {
        "status": "ok",
        "endpoints": metrics,
        "cost_summary": cir_flow_cost,
        "slo_targets": {
            "/api/v2/verify": {"p95_target_ms": 15000, "note": "Gov registry 2-15s, aggregator 15-30s"},
            "/api/v2/verify/lei": {"p95_target_ms": 5000},
            "/api/v2/screening": {"p95_target_ms": 10000, "note": "7 sources in parallel"},
            "/api/v2/media": {"p95_target_ms": 30000, "note": "GDELT rate-limited, 6s stagger"},
            "/api/v2/enrich": {"p95_target_ms": 75000, "note": "Deep Lookup polls up to 60s"},
            "/api/v2/lookup": {"p95_target_ms": 75000, "note": "Fan-out, bounded by slowest source"},
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Raw Response Store — 90-day retention endpoints
# ---------------------------------------------------------------------------

@app.get("/api/v2/raw/stats")
async def v2_raw_stats(_key: str = Depends(verify_api_key)):
    """Get raw response store statistics."""
    return raw_store.stats()


@app.get("/api/v2/raw")
async def v2_raw_list(
    request: Request,
    _key: str = Depends(verify_api_key),
    date: str = "",
    source: str = "",
    entity_name: str = "",
    limit: int = 50,
):
    """
    List stored raw responses (metadata only, no body).
    Query params: date (YYYY-MM-DD), source, entity_name, limit.
    """
    results = raw_store.list_responses(
        date=date, source=source, entity_name=entity_name,
        limit=min(limit, 200),
    )
    return {"count": len(results), "responses": results}


@app.post("/api/v2/raw/cleanup")
async def v2_raw_cleanup(_key: str = Depends(verify_api_key)):
    """Manually trigger cleanup of raw responses older than 90 days."""
    stats = raw_store.cleanup()
    return {"status": "ok", **stats}


@app.get("/api/v2/raw/{response_id}")
async def v2_raw_get(response_id: str, _key: str = Depends(verify_api_key)):
    """Retrieve a stored raw upstream response by ID."""
    record = raw_store.retrieve(response_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Raw response not found")
    return record
