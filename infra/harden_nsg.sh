#!/usr/bin/env bash
# =============================================================================
# NSG Hardening Script — Lock down ALL crawl VMs
# =============================================================================
#
# This script enforces the rule: ONLY crawldevvm can reach regional VMs.
# No other machine — not production, not VPN, not the internet — can see
# these VMs exist. The ONLY entry point is crawldevvm (20.94.45.219),
# which runs the Crawl Research Gateway API.
#
# What this does:
#   1. Removes any existing permissive SSH rules (source: *)
#   2. Creates DENY ALL inbound rule (lowest priority)
#   3. Allows SSH (22) ONLY from crawldevvm IP
#   4. Allows OpenClaw gateway (18789) ONLY from localhost/VNet
#   5. Optionally allows SwarmClaw (3456) on crawl-americas only
#   6. Verifies rules after application
#
# Usage:
#   ./harden_nsg.sh              # Dry-run (show what would change)
#   ./harden_nsg.sh --apply      # Apply changes
#   ./harden_nsg.sh --verify     # Just verify current state
#
# =============================================================================

set -euo pipefail

RG="crawldevvm_group"
CRAWLDEVVM_IP="20.94.45.219"

# All regional VMs and their NSGs (actual Azure resource names)
declare -A VM_NSGS=(
    ["crawl-americas"]="crawlamericasvm-nsg"
    ["crawl-europe"]="crawleuropevm-nsg"
    ["crawl-gulf"]="crawelUAEVM-nsg"
    ["crawl-china"]="crawlchinavm-nsg"
    ["crawl-india"]="crawlindiavm-nsg"
)

MODE="${1:---dry-run}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# -----------------------------------------------------------------------------
# Verify mode — just check current NSG state
# -----------------------------------------------------------------------------
verify_nsg() {
    local nsg_name="$1"
    echo ""
    log_info "=== $nsg_name ==="

    # List all inbound rules
    az network nsg rule list \
        --resource-group "$RG" \
        --nsg-name "$nsg_name" \
        --query "[?direction=='Inbound'].{Name:name, Priority:priority, Access:access, Source:sourceAddressPrefix, Ports:destinationPortRange}" \
        --output table 2>/dev/null || log_warn "NSG $nsg_name not found in RG $RG"

    # Check for dangerous rules (source: *)
    OPEN_RULES=$(az network nsg rule list \
        --resource-group "$RG" \
        --nsg-name "$nsg_name" \
        --query "[?direction=='Inbound' && sourceAddressPrefix=='*' && access=='Allow'].name" \
        --output tsv 2>/dev/null)

    if [ -n "$OPEN_RULES" ]; then
        log_error "OPEN INBOUND RULES DETECTED: $OPEN_RULES"
    else
        log_info "No open inbound rules. Good."
    fi
}

# -----------------------------------------------------------------------------
# Harden a single NSG
# -----------------------------------------------------------------------------
harden_nsg() {
    local vm_name="$1"
    local nsg_name="$2"

    echo ""
    log_info "=== Hardening $nsg_name ($vm_name) ==="

    # Step 1: Delete any existing permissive SSH rules
    EXISTING_RULES=$(az network nsg rule list \
        --resource-group "$RG" \
        --nsg-name "$nsg_name" \
        --query "[?direction=='Inbound' && sourceAddressPrefix=='*'].name" \
        --output tsv 2>/dev/null)

    for rule in $EXISTING_RULES; do
        if [ "$MODE" = "--apply" ]; then
            log_warn "Deleting permissive rule: $rule"
            az network nsg rule delete \
                --resource-group "$RG" \
                --nsg-name "$nsg_name" \
                --name "$rule" \
                --output none
        else
            log_warn "[DRY-RUN] Would delete permissive rule: $rule"
        fi
    done

    # Step 2: DENY ALL inbound (priority 4096 — lowest custom priority)
    if [ "$MODE" = "--apply" ]; then
        log_info "Creating DenyAllInbound rule..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name DenyAllInbound \
            --priority 4096 \
            --direction Inbound \
            --access Deny \
            --protocol '*' \
            --source-address-prefix '*' \
            --destination-port-range '*' \
            --description "DENY ALL — only explicitly allowed sources can reach this VM" \
            --output none 2>/dev/null || \
        az network nsg rule update \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name DenyAllInbound \
            --access Deny \
            --source-address-prefix '*' \
            --output none
    else
        log_info "[DRY-RUN] Would create DenyAllInbound (priority 4096)"
    fi

    # Step 3: Allow SSH ONLY from crawldevvm
    if [ "$MODE" = "--apply" ]; then
        log_info "Creating AllowSSH-from-crawldevvm rule..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowSSH-crawldevvm \
            --priority 100 \
            --direction Inbound \
            --access Allow \
            --protocol Tcp \
            --source-address-prefix "$CRAWLDEVVM_IP" \
            --destination-port-range 22 \
            --description "SSH from crawldevvm ONLY — the single control plane" \
            --output none 2>/dev/null || \
        az network nsg rule update \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowSSH-crawldevvm \
            --source-address-prefix "$CRAWLDEVVM_IP" \
            --destination-port-range 22 \
            --access Allow \
            --output none
    else
        log_info "[DRY-RUN] Would create AllowSSH-crawldevvm (priority 100, source: $CRAWLDEVVM_IP)"
    fi

    # Step 4: Allow OpenClaw gateway (18789) from VNet only (VM-to-VM internal)
    if [ "$MODE" = "--apply" ]; then
        log_info "Creating AllowOpenClaw-VNet rule..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowOpenClaw-VNet \
            --priority 200 \
            --direction Inbound \
            --access Allow \
            --protocol Tcp \
            --source-address-prefix VirtualNetwork \
            --destination-port-range 18789 \
            --description "OpenClaw gateway — VNet internal only" \
            --output none 2>/dev/null || \
        az network nsg rule update \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowOpenClaw-VNet \
            --source-address-prefix VirtualNetwork \
            --destination-port-range 18789 \
            --access Allow \
            --output none
    else
        log_info "[DRY-RUN] Would create AllowOpenClaw-VNet (priority 200, source: VirtualNetwork)"
    fi

    # Step 5: SwarmClaw (3456) only on crawl-americas, only from crawldevvm
    if [ "$vm_name" = "crawl-americas" ]; then
        if [ "$MODE" = "--apply" ]; then
            log_info "Creating AllowSwarmClaw rule (americas only)..."
            az network nsg rule create \
                --resource-group "$RG" \
                --nsg-name "$nsg_name" \
                --name AllowSwarmClaw-crawldevvm \
                --priority 300 \
                --direction Inbound \
                --access Allow \
                --protocol Tcp \
                --source-address-prefix "$CRAWLDEVVM_IP" \
                --destination-port-range 3456 \
                --description "SwarmClaw dashboard — crawldevvm only" \
                --output none 2>/dev/null || \
            az network nsg rule update \
                --resource-group "$RG" \
                --nsg-name "$nsg_name" \
                --name AllowSwarmClaw-crawldevvm \
                --source-address-prefix "$CRAWLDEVVM_IP" \
                --destination-port-range 3456 \
                --access Allow \
                --output none
        else
            log_info "[DRY-RUN] Would create AllowSwarmClaw-crawldevvm (priority 300)"
        fi
    fi

    log_info "Done: $nsg_name"
}

# -----------------------------------------------------------------------------
# Also harden crawldevvm itself — only API port + SSH from VPN
# -----------------------------------------------------------------------------
harden_crawldevvm_nsg() {
    local nsg_name="crawldevvm-nsg"

    echo ""
    log_info "=== Hardening crawldevvm NSG ==="

    # The CIR API (port 8400) should ONLY be reachable from:
    #   1. The GC App (172.20.0.11) — production caller
    #   2. VPN IP — for admin/testing
    # SSH should ONLY be from VPN

    local GC_APP_IP="172.20.0.11"
    local PRODUCTINTEL_IP="104.209.146.16"
    local VPN_IP="${COPAP_VPN_PUBLIC_IP:-}"

    if [ -z "$VPN_IP" ]; then
        log_warn "COPAP_VPN_PUBLIC_IP not set. Skipping crawldevvm hardening."
        log_warn "Set this env var and re-run to lock down crawldevvm."
        return
    fi

    # Delete permissive rules
    EXISTING_RULES=$(az network nsg rule list \
        --resource-group "$RG" \
        --nsg-name "$nsg_name" \
        --query "[?direction=='Inbound' && sourceAddressPrefix=='*'].name" \
        --output tsv 2>/dev/null)

    for rule in $EXISTING_RULES; do
        if [ "$MODE" = "--apply" ]; then
            log_warn "Deleting permissive rule: $rule"
            az network nsg rule delete \
                --resource-group "$RG" \
                --nsg-name "$nsg_name" \
                --name "$rule" \
                --output none
        else
            log_warn "[DRY-RUN] Would delete permissive rule: $rule"
        fi
    done

    if [ "$MODE" = "--apply" ]; then
        # Deny all
        log_info "Creating DenyAllInbound..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name DenyAllInbound \
            --priority 4096 \
            --direction Inbound \
            --access Deny \
            --protocol '*' \
            --source-address-prefix '*' \
            --destination-port-range '*' \
            --description "DENY ALL" \
            --output none 2>/dev/null || true

        # SSH from VPN only
        log_info "Creating AllowSSH-VPN..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowSSH-VPN \
            --priority 100 \
            --direction Inbound \
            --access Allow \
            --protocol Tcp \
            --source-address-prefix "$VPN_IP" \
            --destination-port-range 22 \
            --description "SSH from VPN only" \
            --output none 2>/dev/null || true

        # API (8400) from GC App
        log_info "Creating AllowAPI-GCApp..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowAPI-GCApp \
            --priority 200 \
            --direction Inbound \
            --access Allow \
            --protocol Tcp \
            --source-address-prefix "$GC_APP_IP" \
            --destination-port-range 8400 \
            --description "CIR API from GC App only" \
            --output none 2>/dev/null || true

        # API (8400) from productintel app
        log_info "Creating AllowAPI-ProductIntel..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowAPI-ProductIntel \
            --priority 205 \
            --direction Inbound \
            --access Allow \
            --protocol Tcp \
            --source-address-prefix "$PRODUCTINTEL_IP" \
            --destination-port-range 8400 \
            --description "CIR API from productintel app" \
            --output none 2>/dev/null || true

        # API (8400) from VPN (admin/testing)
        log_info "Creating AllowAPI-VPN..."
        az network nsg rule create \
            --resource-group "$RG" \
            --nsg-name "$nsg_name" \
            --name AllowAPI-VPN \
            --priority 210 \
            --direction Inbound \
            --access Allow \
            --protocol Tcp \
            --source-address-prefix "$VPN_IP" \
            --destination-port-range 8400 \
            --description "CIR API from VPN for testing" \
            --output none 2>/dev/null || true
    else
        log_info "[DRY-RUN] Would lock crawldevvm: SSH=$VPN_IP, API=$GC_APP_IP+$PRODUCTINTEL_IP+$VPN_IP"
    fi

    log_info "Done: crawldevvm"
}

# =============================================================================
# Main
# =============================================================================

echo "=============================================="
echo "  Crawl VM NSG Hardening"
echo "  Mode: $MODE"
echo "  Control plane: $CRAWLDEVVM_IP"
echo "  Resource group: $RG"
echo "=============================================="

if [ "$MODE" = "--verify" ]; then
    for vm_name in "${!VM_NSGS[@]}"; do
        verify_nsg "${VM_NSGS[$vm_name]}"
    done
    verify_nsg "crawldevvm-nsg"
    echo ""
    log_info "Verification complete."
    exit 0
fi

if [ "$MODE" = "--apply" ]; then
    echo ""
    log_warn "THIS WILL MODIFY NSG RULES ON ALL CRAWL VMs."
    log_warn "After this, ONLY crawldevvm ($CRAWLDEVVM_IP) can SSH to regional VMs."
    log_warn "Press Ctrl+C to abort, or wait 5 seconds..."
    sleep 5
fi

# Harden each regional VM
for vm_name in "${!VM_NSGS[@]}"; do
    harden_nsg "$vm_name" "${VM_NSGS[$vm_name]}"
done

# Harden crawldevvm itself
harden_crawldevvm_nsg

echo ""
echo "=============================================="
if [ "$MODE" = "--apply" ]; then
    log_info "NSG hardening APPLIED."
    log_info "Verifying..."
    for vm_name in "${!VM_NSGS[@]}"; do
        verify_nsg "${VM_NSGS[$vm_name]}"
    done
else
    log_info "DRY RUN complete. Run with --apply to execute."
fi
echo "=============================================="
