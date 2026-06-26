# crawldevvm gateway → Azure Container App
#
# Build from repo root:
#   docker build -f deploy/Dockerfile -t crawl-gateway:latest .
#
# What's deliberately NOT in here:
#   - Playwright browser binaries (~500MB) — gateway never calls
#     sync_playwright at runtime, only the import graph requires the
#     Python package.
#   - SSH keys / known_hosts — legacy paramiko paths exist in main.py
#     but are dead code now that the 5 regional VMs are deallocated.
#     The container has no reason to SSH anywhere.
#   - Multilogin xcli / Tor / privoxy — those live on crawl-verify
#     and crawl-darkweb respectively.
#
# Secrets come from Key Vault via the Container App's Managed Identity
# (api/keyvault.py uses azure.identity.DefaultAzureCredential).

FROM python:3.12-slim AS base

# Stop pip cache from inflating the image; print logs unbuffered.
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# System packages needed by Python wheels at runtime:
#   - libssl3 / libffi8     → paramiko, curl-cffi, azure SDK
#   - libpq5                → psycopg2-binary uses bundled libpq but
#                              having libpq5 around helps if a wheel
#                              isn't available for the platform
#   - ca-certificates       → outbound HTTPS to Foundry, BD, Multilogin,
#                              crawl-verify (public IP), PostgreSQL
#   - curl                  → some adapter paths shell out to curl
#                              (legacy; safe to keep, ~3 MB)
#   - tini                  → clean signal handling so SIGTERM from
#                              Container Apps properly stops uvicorn
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 libffi8 libpq5 ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (best cache hit)
COPY deploy/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the gateway code. Excluded via .dockerignore:
#   api/jobs/, api/jobs_archive/   — runtime state (use Azure Files mount)
#   api/output/                     — local SFTP landing (not needed in container)
#   raw_responses/                  — local archive (use blob)
#   data/, output/, logs/           — runtime state
#   *.pyc, __pycache__/             — build noise
COPY api/      /app/api/
COPY agents/   /app/agents/
COPY scripts/  /app/scripts/

# Mount points for Azure Files (created at deploy time):
#   /app/api/jobs           — bulk verify file-backed cross-worker storage
#                              (CRITICAL — without this, /api/v1/verify/bulk
#                               loses results between replicas)
#   /app/output             — CIR JSON drops (legacy; can probably drop)
RUN mkdir -p /app/api/jobs /app/api/jobs/bulk /app/output

# Non-root runtime user — Container Apps strongly prefers this.
RUN useradd -u 10001 -m -d /home/crawl -s /bin/bash crawl \
    && chown -R crawl:crawl /app
USER crawl

# Container App routes inbound port to whatever EXPOSE / app target
# you set in the ingress config. Gateway listens on 8400.
EXPOSE 8400

# Health check uses the existing /api/v1/health endpoint (already in
# main.py, no auth required for the no-key form).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8400/api/v1/health || exit 1

# tini gives us proper signal forwarding so Container Apps' scale-in
# (SIGTERM → wait → SIGKILL) lets uvicorn drain in-flight requests
# cleanly instead of getting SIGKILL'd in the middle of a verify.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "main:app", \
     "--app-dir", "/app/api", \
     "--host", "0.0.0.0", \
     "--port", "8400", \
     "--workers", "4", \
     "--log-level", "info", \
     "--access-log"]
