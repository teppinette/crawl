#!/usr/bin/env bash
# Provision all crawl infrastructure: resource group, storage, 5 VMs.
# Run from a machine with az CLI authenticated to the COPAPCrawl subscription.
#
# Usage: ./provision_all.sh [ssh_key_path]

set -euo pipefail

SSH_KEY="${1:-~/.ssh/crawl_rsa.pub}"
RG="rg-crawl-osint"
STORAGE_ACCOUNT="stcrawlosint"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo "  Crawl OSINT Infrastructure Provisioning"
echo "============================================"
echo ""

# Verify subscription
CURRENT_SUB=$(az account show --query name -o tsv)
echo "Active subscription: $CURRENT_SUB"
echo ""
read -p "Confirm this is the COPAPCrawl subscription (y/n): " CONFIRM
if [ "$CONFIRM" != "y" ]; then
  echo "Aborted. Switch subscription with: az account set --subscription <COPAPCrawl_ID>"
  exit 1
fi

# Step 1: Resource group
echo ""
echo "=== Step 1: Resource Group ==="
az group create \
  --name "$RG" \
  --location eastus2 \
  --output none
echo "Created: $RG"

# Step 2: Storage account + container
echo ""
echo "=== Step 2: Storage Account ==="
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RG" \
  --location eastus2 \
  --sku Standard_LRS \
  --output none
echo "Created: $STORAGE_ACCOUNT"

az storage container create \
  --name osint-staging \
  --account-name "$STORAGE_ACCOUNT" \
  --output none
echo "Created container: osint-staging"

# Step 3: Provision VMs
echo ""
echo "=== Step 3: Provisioning 5 Regional VMs ==="

declare -A VMS
VMS[crawl-americas]=eastus2
VMS[crawl-europe]=westeurope
VMS[crawl-gulf]=uaenorth
VMS[crawl-china]=eastasia
VMS[crawl-india]=centralindia

for VM_NAME in crawl-americas crawl-europe crawl-gulf crawl-china crawl-india; do
  LOCATION="${VMS[$VM_NAME]}"
  echo ""
  echo "--- Provisioning $VM_NAME ($LOCATION) ---"
  bash "$SCRIPT_DIR/provision_vm.sh" "$VM_NAME" "$LOCATION" "$SSH_KEY"
done

# Step 4: Budget alert
echo ""
echo "=== Step 4: Budget Alert ==="
bash "$SCRIPT_DIR/budget_alert.sh"

# Summary
echo ""
echo "============================================"
echo "  Provisioning Complete"
echo "============================================"
echo ""
echo "VM IPs:"
for VM_NAME in crawl-americas crawl-europe crawl-gulf crawl-china crawl-india; do
  IP=$(az vm show -d --resource-group "$RG" --name "$VM_NAME" --query publicIps -o tsv 2>/dev/null || echo "N/A")
  echo "  $VM_NAME: $IP"
done
echo ""
echo "Next steps:"
echo "  1. SSH into each VM and run: bash base_install.sh"
echo "  2. Copy region-specific skills to each VM"
echo "  3. Run onboarding on each VM"
echo "  4. Configure SwarmClaw on crawl-americas (control plane)"
