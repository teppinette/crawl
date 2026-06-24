#!/usr/bin/env bash
# =============================================================================
# Migrate Crawl infrastructure from COPAPCrawl subscription to COPAP AI.
#
# Path A: az resource move (in-place, preserves hostnames, public IPs, managed
# identity object IDs — all the RBAC grants on copapfoundry-resource stay
# valid because the principal_id doesn't change on resource move).
#
# Usage:
#   ./scripts/migrate_to_copap.sh                       # show plan only
#   ./scripts/migrate_to_copap.sh preflight             # discovery + readiness
#   ./scripts/migrate_to_copap.sh decommission_regional # delete the 5 retired VMs
#   ./scripts/migrate_to_copap.sh disable_backups       # remove Azure Backup protection
#   ./scripts/migrate_to_copap.sh move_data             # postgres + storage + keyvault
#   ./scripts/migrate_to_copap.sh move_darkweb          # crawl-darkweb VM (West Europe)
#   ./scripts/migrate_to_copap.sh move_verify           # crawl-verify VM
#   ./scripts/migrate_to_copap.sh move_gateway          # crawldevvm — LAST, everything depends on it
#   ./scripts/migrate_to_copap.sh post_verify           # sanity checks
#   ./scripts/migrate_to_copap.sh cleanup               # delete empty RG, backup vaults, sub
#
# All phases default to dry-run. Pass --apply to execute. Re-runnable.
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration — review and adjust before running
# -----------------------------------------------------------------------------
SRC_SUB="6184b4c7-7866-4d10-ab6d-427b698b3345"            # COPAPCrawl
DST_SUB="98b0b551-324a-46f8-81c1-2455359d7e34"            # COPAP AI Subscription

SRC_RG="crawldevvm_group"                                  # current crawl RG
DST_RG="copap-crawl-rg"                                    # new RG in COPAP AI (created on first move)
DST_LOCATION="eastus2"                                     # for the new RG

# Source VMs / resources (these get moved or deleted)
REGIONAL_VMS=(crawlamericasvm crawleuropevm crawl-gulf crawlchinavm crawlindiavm)
MOVED_VMS=(crawldarkwebvm crawl-verify crawldevvm)
POSTGRES_SERVER="crawl-monitor-db"
STORAGE_ACCOUNT="stcrawlosint"
KEY_VAULT="crawlkeyvault"

# Backup vaults (deleted as part of cleanup; backup protection disabled per VM beforehand)
BACKUP_VAULTS=(crawl-backup-vault crawl-backup-westeurope crawl-backup-eastasia
               crawl-backup-centralindia crawl-backup-francecentral crawl-backup-uaenorth)

LOG="${LOG:-/tmp/migrate_to_copap_$(date +%Y%m%d_%H%M%S).log}"
APPLY=0
for a in "$@"; do [[ "$a" == "--apply" ]] && APPLY=1; done

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
log()  { printf "\n\033[1;34m[%s]\033[0m %s\n" "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
warn() { printf "\033[1;33m  ! %s\033[0m\n" "$*" | tee -a "$LOG"; }
ok()   { printf "\033[1;32m  ✓ %s\033[0m\n" "$*" | tee -a "$LOG"; }
fail() { printf "\033[1;31m  ✗ %s\033[0m\n" "$*" | tee -a "$LOG"; }
run()  {
  echo "  + $*" | tee -a "$LOG"
  if (( APPLY )); then
    "$@" 2>&1 | tee -a "$LOG"
  else
    echo "    (dry-run — pass --apply to execute)" | tee -a "$LOG"
  fi
}
confirm() {
  if (( APPLY )); then
    read -p "  ${1:-Proceed?} [y/N] " r
    [[ "$r" == [yY] ]] || { warn "skipped by operator"; return 1; }
  fi
  return 0
}

set_src() { az account set --subscription "$SRC_SUB"; }
set_dst() { az account set --subscription "$DST_SUB"; }

resource_id() {
  set_src
  az resource show -g "$SRC_RG" -n "$1" --resource-type "$2" --query id -o tsv 2>/dev/null
}

vm_id() {
  set_src
  az vm show -g "$SRC_RG" -n "$1" --query id -o tsv 2>/dev/null
}

# -----------------------------------------------------------------------------
# Phase: preflight — discover resources and surface blockers
# -----------------------------------------------------------------------------
preflight() {
  log "PHASE: preflight — discovery + readiness checks"

  set_src
  log "Source RG ($SRC_RG) inventory:"
  az resource list -g "$SRC_RG" -o table --query "[].{name:name,type:type,location:location}" || true

  log "VMs to move (Standard SKU public IP required for move):"
  for vm in "${MOVED_VMS[@]}"; do
    local pip
    pip=$(az vm show -g "$SRC_RG" -n "$vm" --query "networkProfile.networkInterfaces[0].id" -o tsv 2>/dev/null) || { warn "$vm not found"; continue; }
    local nic_name="${pip##*/}"
    local pip_id
    pip_id=$(az network nic show -g "$SRC_RG" -n "$nic_name" --query "ipConfigurations[0].publicIPAddress.id" -o tsv 2>/dev/null) || true
    if [[ -n "${pip_id:-}" ]]; then
      local sku addr
      sku=$(az network public-ip show --ids "$pip_id" --query "sku.name" -o tsv 2>/dev/null) || sku="?"
      addr=$(az network public-ip show --ids "$pip_id" --query "ipAddress" -o tsv 2>/dev/null) || addr="?"
      if [[ "$sku" == "Standard" ]]; then
        ok "$vm: SKU=$sku addr=$addr — safe to move"
      else
        fail "$vm: SKU=$sku addr=$addr — Basic SKU public IPs CANNOT be moved across subs"
      fi
    else
      warn "$vm: no public IP attached"
    fi
  done

  log "VM extensions that commonly block moves (especially MDE.Linux):"
  for vm in "${MOVED_VMS[@]}"; do
    az vm extension list -g "$SRC_RG" --vm-name "$vm" --query "[].name" -o tsv 2>/dev/null \
      | sed "s|^|  $vm: |"
  done

  log "Backup protection status (must be disabled before VM move):"
  for vault in "${BACKUP_VAULTS[@]}"; do
    local items
    items=$(az backup item list -g "$SRC_RG" --vault-name "$vault" --query "[].name" -o tsv 2>/dev/null) || continue
    if [[ -n "$items" ]]; then
      while read -r it; do echo "  $vault: $it"; done <<< "$items"
    fi
  done

  log "Postgres flexible server move readiness:"
  az postgres flexible-server show -g "$SRC_RG" -n "$POSTGRES_SERVER" \
    --query "{state:state,version:version,location:location,sku:sku.name,storage:storage.storageSizeGb}" -o table 2>/dev/null || warn "$POSTGRES_SERVER lookup failed"

  log "Validate-move dry run (per resource):"
  set_dst
  az group show -n "$DST_RG" >/dev/null 2>&1 || {
    log "Target RG $DST_RG does not exist in COPAP AI yet — would create on first move"
    run az group create -n "$DST_RG" -l "$DST_LOCATION"
  }
  set_src
  for vm in "${MOVED_VMS[@]}"; do
    local rid; rid=$(vm_id "$vm") || continue
    [[ -z "$rid" ]] && continue
    log "  validating $vm move..."
    az resource invoke-action --action validateMoveResources \
      --ids "/subscriptions/$SRC_SUB/resourceGroups/$SRC_RG" \
      --request-body "{\"resources\":[\"$rid\"],\"targetResourceGroup\":\"/subscriptions/$DST_SUB/resourceGroups/$DST_RG\"}" \
      2>&1 | tail -5 || warn "validate failed for $vm"
  done

  ok "Preflight complete. Review the log: $LOG"
}

# -----------------------------------------------------------------------------
# Phase: decommission_regional — delete the 5 retired regional VMs
# (these are getting retired anyway per the Foundry migration directive)
# -----------------------------------------------------------------------------
decommission_regional() {
  log "PHASE: decommission_regional — delete the 5 retired regional VMs"
  set_src
  for vm in "${REGIONAL_VMS[@]}"; do
    log "  $vm:"
    if ! az vm show -g "$SRC_RG" -n "$vm" --query name -o tsv >/dev/null 2>&1; then
      ok "    not found — skipping"
      continue
    fi
    confirm "Delete $vm (VM + NIC + disks + public IP)?" || continue
    # Capture associated resources first
    local nic_id disk_ids pip_id
    nic_id=$(az vm show -g "$SRC_RG" -n "$vm" --query "networkProfile.networkInterfaces[0].id" -o tsv)
    pip_id=$(az network nic show --ids "$nic_id" --query "ipConfigurations[0].publicIPAddress.id" -o tsv 2>/dev/null || true)
    disk_ids=$(az vm show -g "$SRC_RG" -n "$vm" --query "[storageProfile.osDisk.managedDisk.id, storageProfile.dataDisks[].managedDisk.id]" -o tsv)
    run az vm delete -g "$SRC_RG" -n "$vm" --yes
    [[ -n "$nic_id" ]] && run az network nic delete --ids "$nic_id"
    [[ -n "$pip_id" ]] && run az network public-ip delete --ids "$pip_id"
    for d in $disk_ids; do
      [[ -n "$d" ]] && run az disk delete --ids "$d" --yes
    done
  done
  ok "Regional VM decommission complete."
}

# -----------------------------------------------------------------------------
# Phase: disable_backups — remove Azure Backup protection on all VMs
# Azure Backup association blocks resource moves; must disable per VM before move.
# -----------------------------------------------------------------------------
disable_backups() {
  log "PHASE: disable_backups — remove Azure Backup protection so moves are allowed"
  set_src
  for vault in "${BACKUP_VAULTS[@]}"; do
    log "  vault: $vault"
    local items
    items=$(az backup item list -g "$SRC_RG" --vault-name "$vault" \
      --query "[].{container:properties.containerName,item:name}" -o tsv 2>/dev/null) || continue
    [[ -z "$items" ]] && { ok "    no protected items"; continue; }
    while read -r container item; do
      [[ -z "$container" ]] && continue
      confirm "Disable backup for $item in $vault (retain backup data)?" || continue
      # --delete-backup-data false keeps the restore points; safer than wiping them
      run az backup protection disable \
        --resource-group "$SRC_RG" --vault-name "$vault" \
        --container-name "$container" --item-name "$item" \
        --backup-management-type AzureIaasVM \
        --delete-backup-data false --yes
    done <<< "$items"
  done
  ok "Backup protection disabled."
}

# -----------------------------------------------------------------------------
# Phase: move_data — Postgres + storage + key vault
# Done first because: (1) no downtime if done right, (2) zero IP/DNS changes,
# (3) validates the whole sub-move flow before touching VMs.
# -----------------------------------------------------------------------------
move_data() {
  log "PHASE: move_data — Postgres + storage account + Key Vault"
  set_src

  # Ensure target RG exists
  set_dst; az group show -n "$DST_RG" >/dev/null 2>&1 || {
    log "  creating target RG $DST_RG in COPAP AI"
    run az group create -n "$DST_RG" -l "$DST_LOCATION"
  }
  set_src

  for triple in \
    "PostgreSQL|Microsoft.DBforPostgreSQL/flexibleServers|$POSTGRES_SERVER" \
    "Storage|Microsoft.Storage/storageAccounts|$STORAGE_ACCOUNT" \
    "KeyVault|Microsoft.KeyVault/vaults|$KEY_VAULT"
  do
    IFS="|" read -r label rtype name <<< "$triple"
    log "  $label: $name"
    local rid; rid=$(resource_id "$name" "$rtype")
    if [[ -z "$rid" ]]; then warn "    not found in $SRC_RG"; continue; fi
    confirm "Move $name to $DST_SUB/$DST_RG?" || continue
    run az resource move \
      --destination-subscription-id "$DST_SUB" \
      --destination-group "$DST_RG" \
      --ids "$rid"
    ok "    $name move dispatched"
  done

  log "Post-move validation:"
  set_dst
  for n in "$POSTGRES_SERVER" "$STORAGE_ACCOUNT" "$KEY_VAULT"; do
    if az resource show -g "$DST_RG" -n "$n" --resource-type "$(case $n in
        $POSTGRES_SERVER) echo Microsoft.DBforPostgreSQL/flexibleServers ;;
        $STORAGE_ACCOUNT) echo Microsoft.Storage/storageAccounts ;;
        $KEY_VAULT) echo Microsoft.KeyVault/vaults ;; esac)" \
        --query name -o tsv >/dev/null 2>&1; then
      ok "    $n now in COPAP AI / $DST_RG"
    else
      fail "    $n NOT found in target — manual investigation needed"
    fi
  done

  warn "Test from crawldevvm BEFORE moving any VMs:"
  warn "  psql 'host=crawl-monitor-db.postgres.database.azure.com user=crawladmin sslmode=require'"
  warn "  python3 -c 'from api.keyvault import get_secret; print(bool(get_secret(\"cir-api-key\")))'"
  warn "  Both should still work — hostname + MI grants are unchanged by the move."
}

# -----------------------------------------------------------------------------
# Phase: move_<vm> — same pattern, one VM at a time with dependencies
# -----------------------------------------------------------------------------
_move_vm() {
  local vm="$1"
  log "  preparing $vm move"
  set_src
  local vm_id nic_id pip_id nsg_id disk_ids ids
  vm_id=$(az vm show -g "$SRC_RG" -n "$vm" --query id -o tsv) || { fail "$vm not found"; return; }
  nic_id=$(az vm show -g "$SRC_RG" -n "$vm" --query "networkProfile.networkInterfaces[0].id" -o tsv)
  pip_id=$(az network nic show --ids "$nic_id" --query "ipConfigurations[0].publicIPAddress.id" -o tsv 2>/dev/null || true)
  nsg_id=$(az network nic show --ids "$nic_id" --query "networkSecurityGroup.id" -o tsv 2>/dev/null || true)
  disk_ids=$(az vm show -g "$SRC_RG" -n "$vm" \
    --query "[storageProfile.osDisk.managedDisk.id, storageProfile.dataDisks[].managedDisk.id]" -o tsv | tr '\n' ' ')
  ids="$vm_id $nic_id ${pip_id:-} ${nsg_id:-} $disk_ids"
  log "    moving as a group: $ids"
  confirm "Move $vm + NIC + public IP + NSG + disks together?" || return
  run az resource move \
    --destination-subscription-id "$DST_SUB" \
    --destination-group "$DST_RG" \
    --ids $ids
  ok "    $vm move dispatched (public IP preserved — Foundry tool specs unaffected)"
}

move_darkweb()  { log "PHASE: move_darkweb — Tor gateway (West Europe)";  _move_vm "crawldarkwebvm"; }
move_verify()   { log "PHASE: move_verify — verify-gateway VM";            _move_vm "crawl-verify"; }
move_gateway()  {
  log "PHASE: move_gateway — crawldevvm (LAST — everything depends on it)"
  warn "  This VM holds the running gateway service (copap-cir-api). The move briefly stops the VM."
  warn "  GC App / Onboarding will see /api/v1/* return connection errors for ~10–15 min."
  _move_vm "crawldevvm"
}

# -----------------------------------------------------------------------------
# Phase: post_verify — sanity checks after VMs land in COPAP AI
# -----------------------------------------------------------------------------
post_verify() {
  log "PHASE: post_verify — confirm everything still serves"

  # crawldevvm gateway
  log "Gateway /health:"
  curl -sS -m 10 http://20.94.45.219:8400/api/v1/health 2>&1 | head -c 200 && echo

  # crawl-verify
  log "Verify-gateway /health (internal IP 180.20.0.4 may need updating if VNet changed):"
  ssh -o ConnectTimeout=10 crawldevvm \
    "curl -sS -m 10 http://180.20.0.4:8460/health 2>&1 | head -c 200" || warn "  verify unreachable — check internal IP"

  # crawl-darkweb
  log "Darkweb gateway:"
  ssh -o ConnectTimeout=10 crawl-darkweb \
    "curl -sS -m 10 http://localhost:8450/health 2>&1 | head -c 200" || warn "  darkweb unreachable"

  # Foundry agent end-to-end
  log "End-to-end agent run on TESCO PLC (verifies tool reachability post-move):"
  warn "  (run scripts/smoke_test_foundry_agent.py if it exists, or use the one-off Python in earlier commits)"

  ok "Post-verify done."
}

# -----------------------------------------------------------------------------
# Phase: cleanup — delete empty RG, backup vaults, decommission subscription
# -----------------------------------------------------------------------------
cleanup() {
  log "PHASE: cleanup — remove empty resources in COPAPCrawl"
  warn "  This phase is DESTRUCTIVE. Re-verify the COPAPCrawl sub holds nothing useful."
  set_src

  log "  remaining resources in COPAPCrawl ($SRC_SUB):"
  az resource list -o table --query "[].{name:name,type:type,rg:resourceGroup}"

  confirm "Delete the now-empty resource group $SRC_RG?" || return
  run az group delete -n "$SRC_RG" --yes --no-wait

  log "  backup vaults (one per region):"
  for v in "${BACKUP_VAULTS[@]}"; do
    if az backup vault show -g "$SRC_RG" -n "$v" >/dev/null 2>&1; then
      confirm "Delete backup vault $v? (after backup protection was disabled in earlier phase)" || continue
      run az backup vault delete -g "$SRC_RG" -n "$v" --yes
    fi
  done

  warn "  Cancel the COPAPCrawl subscription itself only via portal (CLI cannot cancel an enrollment sub)."
  warn "  Portal → Subscriptions → COPAPCrawl → Cancel subscription. Wait 90 days for permanent delete."
}

# -----------------------------------------------------------------------------
# Dispatch
# -----------------------------------------------------------------------------
phase="${1:-help}"
case "$phase" in
  preflight)             preflight ;;
  decommission_regional) decommission_regional ;;
  disable_backups)       disable_backups ;;
  move_data)             move_data ;;
  move_darkweb)          move_darkweb ;;
  move_verify)           move_verify ;;
  move_gateway)          move_gateway ;;
  post_verify)           post_verify ;;
  cleanup)               cleanup ;;
  --apply|help|"")
    cat <<USAGE
$0 — migrate Crawl infrastructure from COPAPCrawl to COPAP AI subscription

Run phases in order. Each defaults to dry-run; pass --apply to execute:

  1) preflight              — discovery + readiness (no changes; ALWAYS run first)
  2) decommission_regional  — delete the 5 retired regional VMs (americas/europe/gulf/china/india)
  3) disable_backups        — remove Azure Backup protection on remaining VMs
  4) move_data              — Postgres + storage + Key Vault (safest, no downtime)
                              VALIDATE BEFORE PROCEEDING — gateway must still read DB + KV
  5) move_darkweb           — crawl-darkweb (West Europe, isolated)
  6) move_verify            — crawl-verify (verify-gateway, 41 country adapters)
  7) move_gateway           — crawldevvm  (LAST — gateway service depends on everything above)
                              Expect ~10–15 min gateway unavailability
  8) post_verify            — health checks + Foundry agent end-to-end
  9) cleanup                — delete empty RG + backup vaults; cancel COPAPCrawl in portal

Config (edit top of file): SRC_SUB / DST_SUB / SRC_RG / DST_RG / DST_LOCATION
Log: $LOG
USAGE
    ;;
  *) fail "Unknown phase: $phase"; exit 2 ;;
esac
