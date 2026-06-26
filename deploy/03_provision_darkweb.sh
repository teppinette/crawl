#!/bin/bash
#
# Phase 3: provision new crawl-darkweb VM in COPAP AI West Europe.
#
# Mirror of the existing crawl-darkweb (20.86.161.6 in COPAPCrawl):
#   - Standalone VM, Tor + privoxy + Python 3.12 + darkweb_gateway_v4.py
#   - Listens on port 8450, API-key gated
#   - 33 sources via Tor (Ahmia, Torch, Haystak, DDG-Tor, Dehashed,
#     LeakIX, HudsonRock, OCCRP, ICIJ, etc.)
#
# This script does the provisioning + cloud-init bootstrap.
# Manual follow-up (post-script):
#   - SCP/clone darkweb_gateway_v4.py from repo
#   - Configure systemd service with API key
#   - Smoke test from a curl
#   - Update agents/tools/darkweb_scan.openapi.yaml server URL
#   - Foundry update_agent on darkweb_collector to pick up new tool URL
#   - Side-by-side observation
#   - Deallocate old crawl-darkweb after 7d clean
#
# Run from Azure Cloud Shell (bash, COPAP AI Subscription active).
# Idempotent — safe to re-run.
#
set -euo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
SUBSCRIPTION="COPAP AI Subscription"
RG="COPAPAI_Resource_Group"
LOC="westeurope"                      # match the old VM's region
VM_NAME="copap-darkweb"
VM_SIZE="Standard_D2s_v3"             # same as old (per CLAUDE.md)
VM_USERNAME="copapadmin"
SSH_KEY_PATH="$HOME/.ssh/id_rsa.pub"  # Cloud Shell creates this on first SSH

# Tor takes ~30s to bootstrap. Open port 8450 only to AzureCloud (Container
# App's egress is dynamic, service tag is the realistic allowlist).
INGRESS_SOURCE="AzureCloud"
INGRESS_PORT=8450

# -----------------------------------------------------------------------------
# 1. Set subscription, ensure SSH key exists
# -----------------------------------------------------------------------------
echo "=== 1. Set subscription: $SUBSCRIPTION ==="
az account set --subscription "$SUBSCRIPTION"

if [ ! -f "$SSH_KEY_PATH" ]; then
  echo "Creating Cloud Shell SSH key (no passphrase)..."
  ssh-keygen -t rsa -b 4096 -N "" -f "$HOME/.ssh/id_rsa" -C "$VM_USERNAME@cloudshell"
fi

# -----------------------------------------------------------------------------
# 2. cloud-init bootstrap script (runs on first VM boot)
#    Installs Tor, privoxy, Python 3.12, system deps. Clones the crawl repo
#    so darkweb_gateway_v4.py is available at /opt/darkweb/.
# -----------------------------------------------------------------------------
CLOUD_INIT=$(mktemp --suffix=.yaml)
cat > "$CLOUD_INIT" <<'CLOUDINIT'
#cloud-config
package_update: true
package_upgrade: false

packages:
  - tor
  - privoxy
  - python3
  - python3-pip
  - python3-venv
  - git
  - curl
  - ca-certificates

write_files:
  - path: /etc/systemd/system/darkweb-gateway.service
    content: |
      [Unit]
      Description=Dark Web Research Gateway (Tor-routed OSINT)
      After=network-online.target tor.service privoxy.service
      Requires=tor.service privoxy.service
      [Service]
      Type=simple
      User=copapadmin
      WorkingDirectory=/opt/darkweb
      EnvironmentFile=-/etc/darkweb-gateway.env
      ExecStart=/opt/darkweb/venv/bin/uvicorn darkweb_gateway_v4:app \
        --host 0.0.0.0 --port 8450 --workers 2
      Restart=always
      RestartSec=5
      [Install]
      WantedBy=multi-user.target
  - path: /etc/darkweb-gateway.env.example
    content: |
      # Copy to /etc/darkweb-gateway.env and fill in:
      DARKWEB_API_KEY=replace-me
      DEHASHED_API_KEY=replace-me
      # (HudsonRock, LeakIX, etc. have free tiers; add their keys if you have them)
  - path: /etc/privoxy/config
    content: |
      listen-address 127.0.0.1:8118
      forward-socks5t / 127.0.0.1:9050 .
      hostname privoxy

runcmd:
  # Tor + privoxy
  - systemctl enable --now tor
  - systemctl restart privoxy
  - systemctl enable privoxy

  # /opt/darkweb workspace
  - install -d -o copapadmin -g copapadmin /opt/darkweb
  - cd /opt/darkweb && sudo -u copapadmin git clone https://github.com/teppinette/crawl.git /opt/darkweb/_repo
  - sudo -u copapadmin cp /opt/darkweb/_repo/darkweb_gateway_v4.py /opt/darkweb/
  - sudo -u copapadmin python3 -m venv /opt/darkweb/venv
  - sudo -u copapadmin /opt/darkweb/venv/bin/pip install --no-cache-dir \
      fastapi uvicorn[standard] httpx requests beautifulsoup4 pysocks \
      'requests[socks]'

  # darkweb-gateway service — DISABLED until /etc/darkweb-gateway.env is filled
  - systemctl daemon-reload
  - echo "darkweb-gateway service installed but NOT started." > /etc/motd
  - echo "Fill /etc/darkweb-gateway.env then run: sudo systemctl enable --now darkweb-gateway" >> /etc/motd
CLOUDINIT

# -----------------------------------------------------------------------------
# 3. Create VM
# -----------------------------------------------------------------------------
echo "=== 3. Create VM: $VM_NAME ($VM_SIZE, $LOC) ==="
if ! az vm show -g "$RG" -n "$VM_NAME" >/dev/null 2>&1; then
  az vm create \
    -g "$RG" -n "$VM_NAME" -l "$LOC" \
    --image Ubuntu2404 \
    --size "$VM_SIZE" \
    --admin-username "$VM_USERNAME" \
    --ssh-key-values "$SSH_KEY_PATH" \
    --custom-data "$CLOUD_INIT" \
    --public-ip-sku Standard \
    --nsg-rule SSH
else
  echo "VM already exists, skipping create."
fi
rm -f "$CLOUD_INIT"

# -----------------------------------------------------------------------------
# 4. NSG inbound for port 8450 (from AzureCloud — covers Container App egress)
# -----------------------------------------------------------------------------
echo "=== 4. Open NSG inbound on port $INGRESS_PORT from $INGRESS_SOURCE ==="
VM_NIC=$(az vm show -g "$RG" -n "$VM_NAME" \
  --query "networkProfile.networkInterfaces[0].id" -o tsv)
NIC_NSG=$(az network nic show --ids "$VM_NIC" --query "networkSecurityGroup.id" -o tsv)
NSG_NAME=$(echo "$NIC_NSG" | awk -F/ '{print $NF}')
NSG_RG=$(echo "$NIC_NSG" | awk -F/ '{print $5}')

if ! az network nsg rule show -g "$NSG_RG" --nsg-name "$NSG_NAME" -n allow-darkweb-8450 >/dev/null 2>&1; then
  az network nsg rule create \
    -g "$NSG_RG" --nsg-name "$NSG_NAME" -n allow-darkweb-8450 \
    --priority 300 \
    --source-address-prefixes "$INGRESS_SOURCE" \
    --destination-port-ranges "$INGRESS_PORT" \
    --access Allow --protocol Tcp --direction Inbound
fi

# -----------------------------------------------------------------------------
# 5. Output VM IPs + next steps
# -----------------------------------------------------------------------------
PUB_IP=$(az vm show -g "$RG" -n "$VM_NAME" -d --query "publicIps" -o tsv)
PRIV_IP=$(az vm show -g "$RG" -n "$VM_NAME" -d --query "privateIps" -o tsv)
echo ""
echo "=================================================================="
echo "New crawl-darkweb VM provisioned in COPAP AI / West Europe."
echo ""
echo "  Name:        $VM_NAME"
echo "  Public IP:   $PUB_IP"
echo "  Private IP:  $PRIV_IP"
echo "  SSH:         ssh ${VM_USERNAME}@${PUB_IP}"
echo ""
echo "Cloud-init is installing Tor + privoxy + Python (~3-5 min)."
echo "Wait, then SSH in and:"
echo ""
echo "  1. Fill in API keys:"
echo "     sudo cp /etc/darkweb-gateway.env.example /etc/darkweb-gateway.env"
echo "     sudo vim /etc/darkweb-gateway.env"
echo "     (DARKWEB_API_KEY = dwk_crawl_2026Q2_f8a3b7e1d9c4 — same as old VM)"
echo "     (DEHASHED_API_KEY = read from KV: crawlkeyvault/dehashed-api-key)"
echo ""
echo "  2. Start the service:"
echo "     sudo systemctl enable --now darkweb-gateway"
echo "     sudo systemctl status darkweb-gateway"
echo ""
echo "  3. Test from this Cloud Shell:"
echo "     curl -s -X POST http://${PUB_IP}:${INGRESS_PORT}/scan \\"
echo "       -H 'X-API-Key: dwk_crawl_2026Q2_f8a3b7e1d9c4' \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"entity\":\"test corp\", \"country\":\"US\"}' | jq ."
echo ""
echo "  4. Once working, update Foundry tool YAML:"
echo "     agents/tools/darkweb_scan.openapi.yaml"
echo "       server URL: http://${PUB_IP}:${INGRESS_PORT}"
echo "     then run scripts/redeploy_darkweb_agent.py"
echo ""
echo "  5. After 7d clean, deallocate old:"
echo "     az vm deallocate -g crawldevvm_group -n crawldarkwebvm \\"
echo "                       --subscription COPAPCrawl"
echo "=================================================================="
