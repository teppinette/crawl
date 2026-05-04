#!/usr/bin/env bash
# Base installation script for each crawl VM.
# SSH into the VM and run: bash base_install.sh
#
# Installs: Node 24, crawl agent tools, Azure CLI

set -euo pipefail

IS_CONTROL_PLANE="${1:-false}"  # pass "true" for crawl-americas

echo "=== Crawl Agent Base Install ==="
echo "Control plane mode: $IS_CONTROL_PLANE"

# System updates
echo "[1/7] System updates..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git jq chromium-browser unzip

# Node.js 24
echo "[2/7] Installing Node.js 24..."
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt install -y nodejs
echo "Node: $(node --version)"
echo "npm: $(npm --version)"

# Agent runtime (installed globally, aliased locally as crawl tooling)
echo "[3/7] Installing agent runtime..."
npm install -g openclaw@latest
echo "Agent runtime installed"

# Orchestration layer (control plane only)
if [ "$IS_CONTROL_PLANE" = "true" ]; then
  echo "[4/7] Installing orchestration layer (control plane)..."
  npm install -g @swarmclawai/swarmclaw
  echo "Orchestration layer installed"
else
  echo "[4/7] Skipping orchestration (not control plane)"
fi

# Azure CLI (for blob uploads)
echo "[5/7] Installing Azure CLI..."
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

# Create working directories
echo "[6/7] Creating directories..."
mkdir -p ~/crawl/skills/counterparty-research
mkdir -p ~/crawl/output
mkdir -p ~/crawl/logs

# Systemd service for auto-start (created by onboarding, but prepare dir)
echo "[7/7] Preparing systemd user directory..."
mkdir -p ~/.config/systemd/user

echo ""
echo "=== Base install complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy .env to ~/crawl/.env (with API keys)"
echo "  2. Copy SKILL.md files to ~/crawl/skills/"
echo "  3. Run: openclaw onboard --install-daemon"
echo "  4. Verify: openclaw gateway status"
echo "  5. Diagnose: openclaw doctor"
if [ "$IS_CONTROL_PLANE" = "true" ]; then
  echo "  6. Start orchestration: swarmclaw (runs on port 3456)"
fi
