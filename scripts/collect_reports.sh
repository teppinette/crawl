#!/usr/bin/env bash
# Collect completed OSINT reports from Azure Blob staging container.
# Run on the dev machine or GC server to pull reports for review.
#
# Usage:
#   ./collect_reports.sh                  # download all regions
#   ./collect_reports.sh china            # download china region only
#   ./collect_reports.sh --list           # list without downloading

set -euo pipefail

STORAGE_ACCOUNT="stcrawlosint"
CONTAINER="osint-staging"
LOCAL_DIR="./reports"
REGION="${1:-}"

# List mode
if [ "$REGION" = "--list" ]; then
    echo "=== Reports in osint-staging ==="
    az storage blob list \
        --account-name "$STORAGE_ACCOUNT" \
        --container-name "$CONTAINER" \
        --auth-mode login \
        --output table \
        --query "[].{Name:name, Size:properties.contentLength, Modified:properties.lastModified}"
    exit 0
fi

# Download mode
mkdir -p "$LOCAL_DIR"

if [ -n "$REGION" ]; then
    # Single region
    echo "Downloading reports from $CONTAINER/$REGION/..."
    mkdir -p "$LOCAL_DIR/$REGION"
    az storage blob download-batch \
        --source "$CONTAINER" \
        --destination "$LOCAL_DIR" \
        --account-name "$STORAGE_ACCOUNT" \
        --auth-mode login \
        --pattern "${REGION}/*.json"
else
    # All regions
    for R in americas europe gulf china india; do
        echo "Downloading $R..."
        mkdir -p "$LOCAL_DIR/$R"
        az storage blob download-batch \
            --source "$CONTAINER" \
            --destination "$LOCAL_DIR" \
            --account-name "$STORAGE_ACCOUNT" \
            --auth-mode login \
            --pattern "${R}/*.json" 2>/dev/null || echo "  (no reports in $R)"
    done
fi

# Summary
echo ""
echo "=== Download Summary ==="
for R in americas europe gulf china india; do
    COUNT=$(find "$LOCAL_DIR/$R" -name "*.json" 2>/dev/null | wc -l)
    [ "$COUNT" -gt 0 ] && echo "  $R: $COUNT reports"
done
TOTAL=$(find "$LOCAL_DIR" -name "*.json" 2>/dev/null | wc -l)
echo "  Total: $TOTAL reports in $LOCAL_DIR/"
