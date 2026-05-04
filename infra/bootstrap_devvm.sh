#!/usr/bin/env bash
# Bootstrap script for crawldevvm -- the management VM in COPAPCrawl subscription.
# SCP this to the VM and run it:
#   scp -i <key> infra/bootstrap_devvm.sh copapadmin@<IP>:~/
#   ssh -i <key> copapadmin@<IP> "bash ~/bootstrap_devvm.sh"

set -euo pipefail

echo "============================================"
echo "  crawldevvm Bootstrap"
echo "============================================"

# System updates
echo "[1/8] System updates..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git jq unzip build-essential

# Node.js 24
echo "[2/8] Installing Node.js 24..."
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt install -y nodejs
echo "  Node: $(node --version)"
echo "  npm: $(npm --version)"

# Azure CLI
echo "[3/8] Installing Azure CLI..."
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
echo "  az: $(az version --query '\"azure-cli\"' -o tsv)"

# Generate SSH keys for crawl VMs
echo "[4/8] Generating SSH key pair for crawl VMs..."
if [ ! -f ~/.ssh/crawl_rsa ]; then
  ssh-keygen -t rsa -b 4096 -f ~/.ssh/crawl_rsa -N "" -q
  echo "  Created ~/.ssh/crawl_rsa"
else
  echo "  Key already exists, skipping"
fi

# Agent runtime tools
echo "[5/8] Installing agent runtime..."
npm install -g openclaw@latest
echo "  Agent runtime installed"

# Orchestration layer (SwarmClaw)
echo "[6/8] Installing orchestration layer..."
npm install -g @swarmclawai/swarmclaw
echo "  Orchestration installed"

# Create working directories
echo "[7/8] Creating directory structure..."
mkdir -p ~/crawl/{skills,output,logs,reports}
mkdir -p ~/repos

# Claude Code
echo "[8/8] Installing Claude Code..."
npm install -g @anthropic-ai/claude-code
echo "  Claude Code installed"

echo ""
echo "============================================"
echo "  Bootstrap Complete"
echo "============================================"
echo ""
echo "Next steps (run manually):"
echo ""
echo "  1. Authenticate az CLI to COPAPCrawl subscription:"
echo "     az login"
echo "     az account set --subscription COPAPCrawl"
echo "     az account show"
echo ""
echo "  2. Enable managed identity (if assigned in Portal):"
echo "     az login --identity"
echo ""
echo "  3. Clone the repo:"
echo "     cd ~/repos"
echo "     git clone https://github.com/COPAP-INC/openclaw.git"
echo ""
echo "  4. Set your VPN IP and provision crawl VMs:"
echo "     export COPAP_VPN_PUBLIC_IP=<your_vpn_ip>"
echo "     cd ~/repos/openclaw/infra"
echo "     bash provision_all.sh ~/.ssh/crawl_rsa.pub"
echo ""
echo "  5. Request vCPU quota if not yet done:"
echo "     Portal > COPAPCrawl > Usage + quotas > Compute"
echo "     Need 12+ vCPUs across: eastus2, westeurope, uaenorth, eastasia, centralindia"
echo ""
echo "  SSH key for crawl VMs: ~/.ssh/crawl_rsa"
echo "  SSH user on crawl VMs: crawladmin"
