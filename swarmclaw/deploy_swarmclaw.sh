#!/usr/bin/env bash
# Deploy SwarmClaw control plane on crawl-americas VM.
# Run this ON the crawl-americas VM after base_install.sh with IS_CONTROL_PLANE=true.
#
# Usage: bash deploy_swarmclaw.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SWARMCLAW_DIR="$HOME/swarmclaw"

echo "=== SwarmClaw Control Plane Setup ==="

# Verify SwarmClaw is installed
if ! command -v swarmclaw &>/dev/null; then
  echo "ERROR: SwarmClaw not installed. Run base_install.sh with IS_CONTROL_PLANE=true first."
  exit 1
fi

# Create SwarmClaw directory
mkdir -p "$SWARMCLAW_DIR"

# Copy config (user must replace ${CRAWL_*_IP} placeholders with actual IPs)
if [ -f "$SCRIPT_DIR/config.json" ]; then
  cp "$SCRIPT_DIR/config.json" "$SWARMCLAW_DIR/config.json"
  echo "Copied config.json to $SWARMCLAW_DIR/"
else
  echo "WARNING: config.json not found in $SCRIPT_DIR"
  echo "Copy it manually from the repo."
fi

echo ""
echo "IMPORTANT: Edit $SWARMCLAW_DIR/config.json"
echo "Replace these placeholders with actual VM public IPs:"
echo '  ${CRAWL_EUROPE_IP}'
echo '  ${CRAWL_GULF_IP}'
echo '  ${CRAWL_CHINA_IP}'
echo '  ${CRAWL_INDIA_IP}'
echo ""
echo "(americas uses localhost since SwarmClaw runs on that VM)"
echo ""

# Create systemd service for SwarmClaw
echo "Creating systemd service..."
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/swarmclaw.service << 'UNIT'
[Unit]
Description=SwarmClaw Control Plane
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/swarmclaw
ExecStart=/usr/bin/env swarmclaw --config %h/swarmclaw/config.json
Restart=on-failure
RestartSec=10
Environment=NODE_ENV=production

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable swarmclaw.service

echo ""
echo "=== SwarmClaw setup complete ==="
echo ""
echo "Commands:"
echo "  Start:   systemctl --user start swarmclaw"
echo "  Stop:    systemctl --user stop swarmclaw"
echo "  Status:  systemctl --user status swarmclaw"
echo "  Logs:    journalctl --user -u swarmclaw -f"
echo ""
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):3456"
echo ""
echo "Before starting, make sure:"
echo "  1. config.json has real VM IPs (not placeholders)"
echo "  2. All 5 gateways are running (openclaw gateway status)"
echo "  3. NSG allows port 18789 between VMs (internal only)"
