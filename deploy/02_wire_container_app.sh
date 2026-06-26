#!/bin/bash
#
# Phase 2: wire the crawl-gateway Container App for real production use.
# Adds Managed Identity + Key Vault access + Azure Files for cross-replica
# state + opens crawl-verify so the Container App can reach it.
#
# Run from Azure Cloud Shell (bash, after 01_provision_mvp.sh succeeded).
# Idempotent — safe to re-run.
#
set -euo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
SUBSCRIPTION="COPAP AI Subscription"
RG="COPAPAI_Resource_Group"
LOC="eastus2"

ENV_NAME="copap-crawl-cae"
APP_NAME="crawl-gateway"

MI_NAME="crawl-gateway-mi"

STORAGE_ACCT="copapcrawlfs"           # for Azure Files mount — globally unique
SHARE_NAME="crawl-jobs"               # File share name
SHARE_QUOTA_GB=10                     # share size

# KV in the OTHER subscription that we need MI to read from
KV_SUBSCRIPTION="COPAPCrawl"
KV_NAME="crawlkeyvault"

# crawl-verify still in COPAPCrawl — we need to open its NSG to AzureCloud.eastus2
# so the Container App (whose egress IP is dynamic) can reach it on 8460.
CRAWLVERIFY_NSG_RG="crawldevvm_group"
CRAWLVERIFY_NSG_NAME=""               # auto-discovered below

# -----------------------------------------------------------------------------
# 1. Set subscription
# -----------------------------------------------------------------------------
echo "=== 1. Set subscription: $SUBSCRIPTION ==="
az account set --subscription "$SUBSCRIPTION"

# -----------------------------------------------------------------------------
# 2. User-assigned Managed Identity
# -----------------------------------------------------------------------------
echo "=== 2. User-assigned Managed Identity: $MI_NAME ==="
if ! az identity show -g "$RG" -n "$MI_NAME" >/dev/null 2>&1; then
  az identity create -g "$RG" -n "$MI_NAME" -l "$LOC"
fi
MI_ID=$(az identity show -g "$RG" -n "$MI_NAME" --query id -o tsv)
MI_CLIENT_ID=$(az identity show -g "$RG" -n "$MI_NAME" --query clientId -o tsv)
MI_PRINCIPAL_ID=$(az identity show -g "$RG" -n "$MI_NAME" --query principalId -o tsv)
echo "MI principalId: $MI_PRINCIPAL_ID"
echo "MI clientId:    $MI_CLIENT_ID"

# -----------------------------------------------------------------------------
# 3. Grant MI: Key Vault Secrets User on crawlkeyvault (in OTHER subscription)
# -----------------------------------------------------------------------------
echo "=== 3. Grant Key Vault Secrets User on crawlkeyvault ==="
az account set --subscription "$KV_SUBSCRIPTION"
KV_RESOURCE_ID=$(az keyvault show -n "$KV_NAME" --query id -o tsv)
echo "KV resource ID: $KV_RESOURCE_ID"

# Use the AAD principal ID of the MI (not the clientId)
if ! az role assignment list --assignee "$MI_PRINCIPAL_ID" --scope "$KV_RESOURCE_ID" \
        --query "[?roleDefinitionName=='Key Vault Secrets User'].id" -o tsv | grep -q .; then
  az role assignment create \
    --assignee-object-id "$MI_PRINCIPAL_ID" \
    --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" \
    --scope "$KV_RESOURCE_ID"
  echo "Role assignment created."
else
  echo "Role already assigned, skipping."
fi
az account set --subscription "$SUBSCRIPTION"

# -----------------------------------------------------------------------------
# 4. Storage account + Azure Files share for /app/api/jobs/ mount
# -----------------------------------------------------------------------------
echo "=== 4. Storage account + Azure Files share ==="
if ! az storage account show -g "$RG" -n "$STORAGE_ACCT" >/dev/null 2>&1; then
  az storage account create \
    -g "$RG" -n "$STORAGE_ACCT" -l "$LOC" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --allow-blob-public-access false
fi
STORAGE_KEY=$(az storage account keys list -g "$RG" -n "$STORAGE_ACCT" --query '[0].value' -o tsv)

if ! az storage share-rm show -g "$RG" --storage-account "$STORAGE_ACCT" -n "$SHARE_NAME" >/dev/null 2>&1; then
  az storage share-rm create \
    -g "$RG" \
    --storage-account "$STORAGE_ACCT" \
    -n "$SHARE_NAME" \
    --quota "$SHARE_QUOTA_GB"
fi

# -----------------------------------------------------------------------------
# 5. Attach Azure Files share to Container Apps Environment
# -----------------------------------------------------------------------------
echo "=== 5. Attach Azure Files to Container Apps Environment ==="
ENV_STORAGE_NAME="crawl-jobs-mount"
if ! az containerapp env storage show \
       -g "$RG" -n "$ENV_NAME" --storage-name "$ENV_STORAGE_NAME" >/dev/null 2>&1; then
  az containerapp env storage set \
    -g "$RG" -n "$ENV_NAME" \
    --storage-name "$ENV_STORAGE_NAME" \
    --azure-file-account-name "$STORAGE_ACCT" \
    --azure-file-account-key "$STORAGE_KEY" \
    --azure-file-share-name "$SHARE_NAME" \
    --access-mode ReadWrite
fi

# -----------------------------------------------------------------------------
# 6. Update Container App: attach MI + mount the share at /app/api/jobs
# -----------------------------------------------------------------------------
echo "=== 6. Update Container App with MI + Files mount ==="
az containerapp identity assign \
  -g "$RG" -n "$APP_NAME" \
  --user-assigned "$MI_ID"

# Add the volume + volume mount via YAML patch (simplest path).
# Also sets AZURE_CLIENT_ID so DefaultAzureCredential picks the right MI
# (otherwise it tries system-assigned which we don't have).
TMP_YAML=$(mktemp --suffix=.yaml)
cat > "$TMP_YAML" <<YAML
properties:
  template:
    volumes:
    - name: crawl-jobs-vol
      storageType: AzureFile
      storageName: $ENV_STORAGE_NAME
    containers:
    - name: $APP_NAME
      env:
      - name: AZURE_CLIENT_ID
        value: "$MI_CLIENT_ID"
      - name: CRAWL_JOBS_DIR
        value: "/app/api/jobs"
      - name: VERIFY_VM_URL
        value: "http://20.110.193.6:8460"
      volumeMounts:
      - volumeName: crawl-jobs-vol
        mountPath: /app/api/jobs
YAML
az containerapp update -g "$RG" -n "$APP_NAME" --yaml "$TMP_YAML"
rm -f "$TMP_YAML"

# -----------------------------------------------------------------------------
# 7. Open crawl-verify NSG to AzureCloud.eastus2 on port 8460
#    (one-shot: Container Apps egress is dynamic; service tag is the
#    realistic allowlist. crawl-verify also has API key auth at the
#    application layer.)
# -----------------------------------------------------------------------------
echo "=== 7. Open crawl-verify NSG to AzureCloud.eastus2:8460 ==="
az account set --subscription "$KV_SUBSCRIPTION"

# Discover crawl-verify's NSG
CRAWLVERIFY_NIC=$(az vm show -g "$CRAWLVERIFY_NSG_RG" -n crawl-verify \
  --query "networkProfile.networkInterfaces[0].id" -o tsv)
NIC_NSG=$(az network nic show --ids "$CRAWLVERIFY_NIC" --query "networkSecurityGroup.id" -o tsv)
if [ -z "$NIC_NSG" ]; then
  # NSG may be on the subnet instead of the NIC
  SUBNET_ID=$(az network nic show --ids "$CRAWLVERIFY_NIC" --query "ipConfigurations[0].subnet.id" -o tsv)
  CRAWLVERIFY_NSG_NAME=$(az network vnet subnet show --ids "$SUBNET_ID" --query "networkSecurityGroup.id" -o tsv | awk -F/ '{print $NF}')
  CRAWLVERIFY_NSG_RG_EFFECTIVE=$(az network vnet subnet show --ids "$SUBNET_ID" --query "networkSecurityGroup.id" -o tsv | awk -F/ '{print $5}')
else
  CRAWLVERIFY_NSG_NAME=$(echo "$NIC_NSG" | awk -F/ '{print $NF}')
  CRAWLVERIFY_NSG_RG_EFFECTIVE=$(echo "$NIC_NSG" | awk -F/ '{print $5}')
fi
echo "crawl-verify NSG: $CRAWLVERIFY_NSG_NAME (in RG $CRAWLVERIFY_NSG_RG_EFFECTIVE)"

if ! az network nsg rule show -g "$CRAWLVERIFY_NSG_RG_EFFECTIVE" \
     --nsg-name "$CRAWLVERIFY_NSG_NAME" -n allow-azure-eastus2-8460 >/dev/null 2>&1; then
  az network nsg rule create \
    -g "$CRAWLVERIFY_NSG_RG_EFFECTIVE" \
    --nsg-name "$CRAWLVERIFY_NSG_NAME" \
    -n allow-azure-eastus2-8460 \
    --priority 200 \
    --source-address-prefixes "AzureCloud.eastus2" \
    --destination-port-ranges 8460 \
    --access Allow \
    --protocol Tcp \
    --direction Inbound
  echo "NSG rule created."
else
  echo "NSG rule already exists, skipping."
fi

az account set --subscription "$SUBSCRIPTION"

# -----------------------------------------------------------------------------
# 8. Restart Container App + smoke test
# -----------------------------------------------------------------------------
echo "=== 8. Restart Container App to pick up MI + volume + env ==="
REV=$(az containerapp revision list -g "$RG" -n "$APP_NAME" \
       --query "[?properties.active==\`true\`].name" -o tsv | head -1)
if [ -n "$REV" ]; then
  az containerapp revision restart -g "$RG" -n "$APP_NAME" --revision "$REV"
fi

URL=$(az containerapp show -g "$RG" -n "$APP_NAME" \
       --query 'properties.configuration.ingress.fqdn' -o tsv)
echo ""
echo "Waiting 45s for container to stabilize..."
sleep 45
echo ""
echo "=== Smoke test: /api/v1/health ==="
curl -fsS --max-time 30 "https://$URL/api/v1/health" || echo "(health check failed — check logs)"
echo ""
echo ""
echo "=================================================================="
echo "Phase 2 done."
echo "URL:        https://$URL"
echo "Health:     https://$URL/api/v1/health"
echo "Logs:       az containerapp logs show -g $RG -n $APP_NAME --tail 50"
echo ""
echo "Next: Phase 3 — Foundry tool URL rewrite + agent redeploy"
echo "      Phase 4 — Cron jobs to Logic Apps"
echo "=================================================================="
