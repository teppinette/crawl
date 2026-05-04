#!/usr/bin/env bash
# Provision a single crawl VM in the COPAPCrawl subscription.
# Usage: ./provision_vm.sh <vm_name> <location> [ssh_key_path]
#
# Example:
#   ./provision_vm.sh crawl-india centralindia ~/.ssh/crawl_rsa.pub

set -euo pipefail

VM_NAME="${1:?Usage: $0 <vm_name> <location> [ssh_key_path]}"
LOCATION="${2:?Usage: $0 <vm_name> <location> [ssh_key_path]}"
SSH_KEY="${3:-~/.ssh/crawl_rsa.pub}"

RG="rg-crawl-osint"
VM_SIZE="Standard_B2ms"
IMAGE="Canonical:ubuntu-24_04-lts:server:latest"
ADMIN_USER="crawladmin"
DISK_SIZE=64

# Load VPN IP from env if available
COPAP_VPN_IP="${COPAP_VPN_PUBLIC_IP:-}"

echo "=== Provisioning $VM_NAME in $LOCATION ==="

# Create NSG
echo "[1/4] Creating NSG..."
az network nsg create \
  --resource-group "$RG" \
  --name "nsg-${VM_NAME}" \
  --location "$LOCATION" \
  --output none

# SSH rule -- lock to VPN IP if provided, otherwise warn
if [ -n "$COPAP_VPN_IP" ]; then
  echo "[2/4] Adding SSH rule (locked to $COPAP_VPN_IP)..."
  az network nsg rule create \
    --resource-group "$RG" \
    --nsg-name "nsg-${VM_NAME}" \
    --name AllowSSH \
    --priority 100 \
    --direction Inbound \
    --access Allow \
    --protocol Tcp \
    --destination-port-range 22 \
    --source-address-prefix "$COPAP_VPN_IP" \
    --description "SSH from COPAP VPN only" \
    --output none
else
  echo "[2/4] WARNING: COPAP_VPN_PUBLIC_IP not set. SSH will be open."
  echo "       Set the env var and re-run, or manually add NSG rule."
  az network nsg rule create \
    --resource-group "$RG" \
    --nsg-name "nsg-${VM_NAME}" \
    --name AllowSSH \
    --priority 100 \
    --direction Inbound \
    --access Allow \
    --protocol Tcp \
    --destination-port-range 22 \
    --source-address-prefix "*" \
    --description "TEMPORARY -- lock down to VPN IP" \
    --output none
fi

# Create VM
echo "[3/4] Creating VM..."
az vm create \
  --resource-group "$RG" \
  --name "$VM_NAME" \
  --location "$LOCATION" \
  --image "$IMAGE" \
  --size "$VM_SIZE" \
  --admin-username "$ADMIN_USER" \
  --ssh-key-values "$SSH_KEY" \
  --nsg "nsg-${VM_NAME}" \
  --os-disk-size-gb "$DISK_SIZE" \
  --storage-sku StandardSSD_LRS \
  --public-ip-sku Standard \
  --tags project=crawl-osint region="$LOCATION" \
  --output table

# Auto-shutdown at 23:00 UTC
echo "[4/4] Setting auto-shutdown..."
az vm auto-shutdown \
  --resource-group "$RG" \
  --name "$VM_NAME" \
  --time 2300 \
  --output none

PUBLIC_IP=$(az vm show -d --resource-group "$RG" --name "$VM_NAME" --query publicIps -o tsv)
echo ""
echo "=== $VM_NAME provisioned ==="
echo "  IP: $PUBLIC_IP"
echo "  SSH: ssh -i ${SSH_KEY%.pub} ${ADMIN_USER}@${PUBLIC_IP}"
echo "  Next: run base_install.sh on the VM"
