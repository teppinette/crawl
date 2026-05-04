#!/usr/bin/env bash
# Upload completed OSINT reports to Azure Blob staging container.
# Run on each OpenClaw VM after research completes.
#
# Usage:
#   ./blob_upload.sh <region>                   # upload all from ~/crawl/output/
#   ./blob_upload.sh <region> <specific_file>   # upload one file

set -euo pipefail

REGION="${1:?Usage: $0 <region> [file]}"
FILE="${2:-}"

STORAGE_ACCOUNT="stcrawlosint"
CONTAINER="osint-staging"
OUTPUT_DIR="$HOME/crawl/output"

if [ -n "$FILE" ]; then
    # Upload single file
    FILENAME=$(basename "$FILE")
    echo "Uploading $FILENAME to $CONTAINER/$REGION/..."
    az storage blob upload \
        --account-name "$STORAGE_ACCOUNT" \
        --container-name "$CONTAINER" \
        --name "${REGION}/${FILENAME}" \
        --file "$FILE" \
        --auth-mode login \
        --overwrite
    echo "Done: ${REGION}/${FILENAME}"
else
    # Upload all JSON files from output directory
    COUNT=0
    for JSON_FILE in "$OUTPUT_DIR"/*.json; do
        [ -f "$JSON_FILE" ] || continue
        FILENAME=$(basename "$JSON_FILE")
        echo "Uploading $FILENAME..."
        az storage blob upload \
            --account-name "$STORAGE_ACCOUNT" \
            --container-name "$CONTAINER" \
            --name "${REGION}/${FILENAME}" \
            --file "$JSON_FILE" \
            --auth-mode login \
            --overwrite
        COUNT=$((COUNT + 1))
    done
    echo "Done: uploaded $COUNT files to $CONTAINER/$REGION/"
fi
