#!/usr/bin/env bash
# Quick CIR research submission script
# Usage:
#   research.sh "Entity Legal Name" "CC" "123 Main St, City"
#   research.sh "Acme Trading LLC" "AE" "JAFZA South, Dubai"
#   research.sh "Sinochem Holdings" "CN"              # address is optional
#
# Polls until complete (or failed), then prints the result.

set -euo pipefail

API_URL="http://localhost:8400"
API_KEY="${COPAP_CIR_API_KEY:-$(az keyvault secret show --vault-name crawlkeyvault --name cir-api-key --query value -o tsv 2>/dev/null)}"

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 \"Entity Name\" \"CC\" [\"Address\"]"
  echo "  CC = ISO 2-letter country code (US, AE, CN, IN, TR, etc.)"
  echo ""
  echo "Examples:"
  echo "  $0 \"Acme Trading LLC\" \"AE\" \"JAFZA South, Dubai\""
  echo "  $0 \"Sinochem Holdings\" \"CN\""
  exit 1
fi

ENTITY="$1"
COUNTRY="$2"
ADDRESS="${3:-}"

# Build payload
if [[ -n "$ADDRESS" ]]; then
  PAYLOAD=$(jq -n \
    --arg name "$ENTITY" \
    --arg cc "$COUNTRY" \
    --arg addr "$ADDRESS" \
    '{entity_legal_name: $name, entity_country: $cc, entity_address: $addr}')
else
  PAYLOAD=$(jq -n \
    --arg name "$ENTITY" \
    --arg cc "$COUNTRY" \
    '{entity_legal_name: $name, entity_country: $cc}')
fi

echo "Submitting CIR research for: $ENTITY ($COUNTRY)"
[[ -n "$ADDRESS" ]] && echo "Address: $ADDRESS"
echo ""

# Submit
RESPONSE=$(curl -s -X POST "$API_URL/api/v1/research" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

JOB_ID=$(echo "$RESPONSE" | jq -r '.job_id // empty')
if [[ -z "$JOB_ID" ]]; then
  echo "ERROR: Failed to submit job"
  echo "$RESPONSE" | jq .
  exit 1
fi

echo "Job submitted: $JOB_ID"
echo "Region: $(echo "$RESPONSE" | jq -r '.region // "unknown"')"
echo ""

# Poll
echo "Polling for results..."
while true; do
  STATUS_RESP=$(curl -s "$API_URL/api/v1/research/$JOB_ID" \
    -H "X-API-Key: $API_KEY")
  STATUS=$(echo "$STATUS_RESP" | jq -r '.status')

  case "$STATUS" in
    completed)
      echo ""
      echo "=== COMPLETED ==="
      BLOB=$(echo "$STATUS_RESP" | jq -r '.blob_path // empty')
      if [[ -n "$BLOB" ]]; then
        echo "Blob: $BLOB"
      fi
      echo "$STATUS_RESP" | jq .
      exit 0
      ;;
    failed)
      echo ""
      echo "=== FAILED ==="
      echo "$STATUS_RESP" | jq .
      exit 1
      ;;
    *)
      printf "."
      sleep 15
      ;;
  esac
done
