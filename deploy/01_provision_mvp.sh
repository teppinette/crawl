#!/bin/bash
#
# crawldevvm → Container App MVP provisioning.
# Run from Azure Cloud Shell (bash, COPAP AI Subscription active).
#
# What this does:
#   1. Create ACR (Azure Container Registry) — copapcrawlacr
#   2. Clone the crawl repo into Cloud Shell
#   3. Build the gateway image via `az acr build` (no local docker needed)
#   4. Create Container Apps Environment — copap-crawl-cae (Consumption-only)
#   5. Deploy crawl-gateway Container App with the image
#   6. Print the Container App URL — visit /api/v1/health to validate
#
# What this DOESN'T do (follow-up scripts):
#   - User-assigned Managed Identity + Key Vault grants
#   - Azure Files mount for /app/api/jobs/ (bulk verify cross-replica state)
#   - NSG/firewall allowlists for crawl-verify, PG, BD, Multilogin
#   - Foundry tool URL rewrite + agent redeploy
#   - Cutover from old crawldevvm to new Container App
#
# Idempotent — safe to re-run if a step fails partway through.
#
set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration — change names here if you want different defaults.
# -----------------------------------------------------------------------------
SUBSCRIPTION="COPAP AI Subscription"
RG="COPAPAI_Resource_Group"
LOC="eastus2"

ACR_NAME="copapcrawlacr"             # globally unique, lowercase, 5-50 chars
ENV_NAME="copap-crawl-cae"           # Container Apps Environment
APP_NAME="crawl-gateway"             # Container App
IMAGE_TAG="v0.1"

REPO_URL="https://github.com/teppinette/crawl.git"
REPO_DIR="$HOME/crawl-build"

# -----------------------------------------------------------------------------
# Pre-flight: set subscription, register required providers.
# -----------------------------------------------------------------------------
echo "=== 1. Set subscription + register providers ==="
az account set --subscription "$SUBSCRIPTION"
az provider register -n Microsoft.App --wait
az provider register -n Microsoft.ContainerRegistry --wait
az provider register -n Microsoft.OperationalInsights --wait

# -----------------------------------------------------------------------------
# 2. Azure Container Registry
# -----------------------------------------------------------------------------
echo "=== 2. ACR: $ACR_NAME ==="
if ! az acr show -n "$ACR_NAME" -g "$RG" >/dev/null 2>&1; then
  az acr create -g "$RG" -n "$ACR_NAME" --sku Basic --admin-enabled true
  echo "ACR created."
else
  echo "ACR already exists, skipping create."
fi

# -----------------------------------------------------------------------------
# 3. Clone repo to Cloud Shell home (idempotent: pulls latest if exists)
# -----------------------------------------------------------------------------
echo "=== 3. Clone repo to $REPO_DIR ==="
if [ -d "$REPO_DIR/.git" ]; then
  cd "$REPO_DIR" && git pull --ff-only
else
  git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  cd "$REPO_DIR"
fi

# -----------------------------------------------------------------------------
# 4. Build image in ACR — no local docker needed.
# -----------------------------------------------------------------------------
echo "=== 4. ACR build: crawl-gateway:$IMAGE_TAG ==="
az acr build \
  -r "$ACR_NAME" \
  -t "crawl-gateway:$IMAGE_TAG" \
  -f deploy/Dockerfile \
  .

# -----------------------------------------------------------------------------
# 5. Container Apps Environment (Consumption-only — simplest MVP)
# -----------------------------------------------------------------------------
echo "=== 5. Container Apps Environment: $ENV_NAME ==="
if ! az containerapp env show -g "$RG" -n "$ENV_NAME" >/dev/null 2>&1; then
  az containerapp env create \
    -g "$RG" \
    -n "$ENV_NAME" \
    -l "$LOC"
  echo "Container Apps Environment created."
else
  echo "Environment already exists, skipping create."
fi

# -----------------------------------------------------------------------------
# 6. Deploy Container App
# -----------------------------------------------------------------------------
echo "=== 6. Container App: $APP_NAME ==="
ACR_USER=$(az acr credential show -n "$ACR_NAME" --query username -o tsv)
ACR_PASS=$(az acr credential show -n "$ACR_NAME" --query "passwords[0].value" -o tsv)
IMAGE="${ACR_NAME}.azurecr.io/crawl-gateway:${IMAGE_TAG}"

if az containerapp show -g "$RG" -n "$APP_NAME" >/dev/null 2>&1; then
  # Update existing — bumps to the new image tag
  az containerapp update \
    -g "$RG" -n "$APP_NAME" \
    --image "$IMAGE"
  echo "Container App updated to image $IMAGE."
else
  az containerapp create \
    -g "$RG" -n "$APP_NAME" \
    --environment "$ENV_NAME" \
    --image "$IMAGE" \
    --target-port 8400 \
    --ingress external \
    --min-replicas 1 \
    --max-replicas 3 \
    --cpu 0.5 --memory 1.0Gi \
    --registry-server "${ACR_NAME}.azurecr.io" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS"
  echo "Container App created."
fi

# -----------------------------------------------------------------------------
# 7. Output URL
# -----------------------------------------------------------------------------
URL=$(az containerapp show -g "$RG" -n "$APP_NAME" \
  --query 'properties.configuration.ingress.fqdn' -o tsv)
echo ""
echo "=================================================================="
echo "Container App is up."
echo "URL: https://${URL}"
echo "Health: https://${URL}/api/v1/health"
echo "=================================================================="
echo ""
echo "Expected behaviour in this MVP:"
echo "  - /api/v1/health returns 200 (no auth required)."
echo "  - Most other endpoints will return errors — no Key Vault access yet."
echo "  - Next script will wire MI + KV + Azure Files + ingress hardening."
