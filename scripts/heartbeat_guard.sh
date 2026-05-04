#!/bin/bash
# Heartbeat Guard — runs every 15 min via cron on crawldevvm
# Ensures HEARTBEAT.md stays empty on all regional VMs.
# If OpenClaw regenerates it (e.g. after update/restart), this empties it.

SSH_KEY="$HOME/.ssh/crawldevvm_key.pem"
SSH_OPTS="-i $SSH_KEY -o UserKnownHostsFile=$HOME/.ssh/crawl_known_hosts -o ConnectTimeout=10 -o BatchMode=yes"

VMS=(
    "copapadmin@172.206.2.41"
    "copapadmin@172.189.56.218"
    "copadmin@20.233.46.58"
    "copapadmin@10.0.0.4"
    "copapadmin@20.193.150.43"
)

for vm in "${VMS[@]}"; do
    size=$(ssh $SSH_OPTS "$vm" "wc -c < ~/.openclaw/workspace/HEARTBEAT.md 2>/dev/null || echo 0" 2>/dev/null)
    size=$(echo "$size" | tr -d '[:space:]')
    if [ "$size" -gt 1 ] 2>/dev/null; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALERT: $vm HEARTBEAT.md has content ($size bytes) — clearing"
        ssh $SSH_OPTS "$vm" "echo '' > ~/.openclaw/workspace/HEARTBEAT.md"
    fi
done
