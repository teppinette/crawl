"""
Raw Response Store — 90-day retention of upstream API responses.

Every outbound HTTP call to a registry, sanctions list, or data provider
gets its raw response saved here. GC compliance team needs these for audit:
they must be able to reproduce any data point from the original upstream response.

Storage: local JSON files under ~/crawl/raw_responses/<YYYY-MM-DD>/<hash>.json
Cleanup: files older than 90 days deleted by daily cron.
Retrieval: GET /api/v2/raw/{response_id} returns the stored response.

Each stored response contains:
  - response_id (UUID)
  - timestamp (ISO 8601)
  - source (provider name, e.g. "CSL", "UK_FCDO", "firecrawl")
  - entity_name, country_code (what was queried)
  - request (method, url, params, headers — with secrets redacted)
  - response (status_code, headers, body — raw text, truncated at 500KB)
  - duration_ms
"""

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("raw_store")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_BASE_DIR = Path(os.path.expanduser("~/crawl/raw_responses"))
_MAX_BODY_BYTES = 500_000  # 500KB max per response body
_RETENTION_DAYS = 90

# Secrets to redact from stored request headers
_REDACT_HEADERS = {
    "authorization", "x-api-key", "subscription-key", "dehashed-api-key",
    "cookie", "set-cookie", "proxy-authorization",
}


def _redact_headers(headers: dict) -> dict:
    """Redact sensitive headers before storage."""
    if not headers:
        return {}
    out = {}
    for k, v in headers.items():
        if k.lower() in _REDACT_HEADERS:
            out[k] = f"***REDACTED*** ({len(str(v))} chars)"
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Store a raw response
# ---------------------------------------------------------------------------

def store(
    source: str,
    entity_name: str = "",
    country_code: str = "",
    request_method: str = "GET",
    request_url: str = "",
    request_params: Optional[dict] = None,
    request_headers: Optional[dict] = None,
    response_status: int = 0,
    response_headers: Optional[dict] = None,
    response_body: str = "",
    duration_ms: int = 0,
    extra: Optional[dict] = None,
) -> str:
    """
    Store a raw upstream response. Returns the response_id.

    Call this after every outbound HTTP call to an upstream data source.
    Runs synchronously — fast enough for inline use (~1ms for file write).
    """
    response_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    date_dir = now.strftime("%Y-%m-%d")

    # Truncate body
    body = response_body
    truncated = False
    if len(body) > _MAX_BODY_BYTES:
        body = body[:_MAX_BODY_BYTES]
        truncated = True

    record = {
        "response_id": response_id,
        "timestamp": now.isoformat(),
        "source": source,
        "entity_name": entity_name,
        "country_code": country_code,
        "request": {
            "method": request_method,
            "url": request_url,
            "params": request_params or {},
            "headers": _redact_headers(request_headers or {}),
        },
        "response": {
            "status_code": response_status,
            "headers": _redact_headers(response_headers or {}),
            "body": body,
            "body_truncated": truncated,
            "body_bytes": len(response_body),
        },
        "duration_ms": duration_ms,
    }
    if extra:
        record["extra"] = extra

    # Write to disk
    try:
        out_dir = _BASE_DIR / date_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{response_id}.json"
        with open(out_path, "w") as f:
            json.dump(record, f, ensure_ascii=False)
        return response_id
    except Exception as e:
        log.warning("Failed to store raw response: %s", e)
        return response_id


# ---------------------------------------------------------------------------
# Retrieve a stored response
# ---------------------------------------------------------------------------

def retrieve(response_id: str) -> Optional[dict]:
    """Retrieve a stored raw response by ID. Scans date directories."""
    if not response_id or len(response_id) != 36:
        return None
    # Search recent date dirs (most recent first)
    try:
        if not _BASE_DIR.exists():
            return None
        for date_dir in sorted(_BASE_DIR.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            candidate = date_dir / f"{response_id}.json"
            if candidate.exists():
                with open(candidate) as f:
                    return json.load(f)
    except Exception as e:
        log.warning("Failed to retrieve raw response %s: %s", response_id, e)
    return None


# ---------------------------------------------------------------------------
# List stored responses (for a date range or entity)
# ---------------------------------------------------------------------------

def list_responses(
    date: str = "",
    source: str = "",
    entity_name: str = "",
    limit: int = 50,
) -> list:
    """
    List stored response metadata (without body).
    Filters by date (YYYY-MM-DD), source, or entity_name.
    """
    results = []
    try:
        if not _BASE_DIR.exists():
            return []
        dirs = sorted(_BASE_DIR.iterdir(), reverse=True)
        for date_dir in dirs:
            if not date_dir.is_dir():
                continue
            if date and date_dir.name != date:
                continue
            for f in sorted(date_dir.iterdir(), reverse=True):
                if not f.name.endswith(".json"):
                    continue
                try:
                    with open(f) as fh:
                        record = json.load(fh)
                    if source and record.get("source", "").lower() != source.lower():
                        continue
                    if entity_name and entity_name.lower() not in record.get("entity_name", "").lower():
                        continue
                    # Return metadata only (no body)
                    results.append({
                        "response_id": record["response_id"],
                        "timestamp": record["timestamp"],
                        "source": record["source"],
                        "entity_name": record.get("entity_name", ""),
                        "country_code": record.get("country_code", ""),
                        "status_code": record.get("response", {}).get("status_code"),
                        "body_bytes": record.get("response", {}).get("body_bytes", 0),
                        "duration_ms": record.get("duration_ms", 0),
                    })
                    if len(results) >= limit:
                        return results
                except Exception:
                    continue
    except Exception as e:
        log.warning("Failed to list raw responses: %s", e)
    return results


# ---------------------------------------------------------------------------
# Cleanup old responses
# ---------------------------------------------------------------------------

def cleanup(retention_days: int = _RETENTION_DAYS) -> dict:
    """Delete raw response files older than retention_days. Returns stats."""
    import shutil
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    deleted_dirs = 0
    deleted_files = 0

    try:
        if not _BASE_DIR.exists():
            return {"deleted_dirs": 0, "deleted_files": 0}
        for date_dir in sorted(_BASE_DIR.iterdir()):
            if not date_dir.is_dir():
                continue
            if date_dir.name < cutoff_str:
                count = len(list(date_dir.iterdir()))
                shutil.rmtree(date_dir)
                deleted_dirs += 1
                deleted_files += count
                log.info("Cleaned up raw responses: %s (%d files)", date_dir.name, count)
    except Exception as e:
        log.warning("Raw response cleanup failed: %s", e)

    return {"deleted_dirs": deleted_dirs, "deleted_files": deleted_files, "cutoff": cutoff_str}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict:
    """Return storage stats."""
    total_files = 0
    total_bytes = 0
    date_range = {"earliest": None, "latest": None}

    try:
        if not _BASE_DIR.exists():
            return {"total_files": 0, "total_bytes": 0, "retention_days": _RETENTION_DAYS}
        for date_dir in sorted(_BASE_DIR.iterdir()):
            if not date_dir.is_dir():
                continue
            if date_range["earliest"] is None:
                date_range["earliest"] = date_dir.name
            date_range["latest"] = date_dir.name
            for f in date_dir.iterdir():
                if f.name.endswith(".json"):
                    total_files += 1
                    total_bytes += f.stat().st_size
    except Exception as e:
        log.warning("Raw response stats failed: %s", e)

    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1_048_576, 1),
        "retention_days": _RETENTION_DAYS,
        **date_range,
    }
