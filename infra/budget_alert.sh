#!/usr/bin/env bash
# Create budget alert for crawl infrastructure.
# Alerts at 80% of $500/month to admin@crawl.internal

set -euo pipefail

RG="rg-crawl-osint"
ALERT_EMAIL="${CRAWL_ALERT_EMAIL:-admin@crawl.internal}"

echo "Creating $500/month budget alert (80% threshold)..."
echo "Alert email: $ALERT_EMAIL"

az consumption budget create \
  --budget-name crawl-monthly \
  --amount 500 \
  --time-grain Monthly \
  --category Cost \
  --resource-group "$RG" \
  --notifications "{\"actual_gt_80\":{\"enabled\":true,\"operator\":\"GreaterThan\",\"threshold\":80,\"contactEmails\":[\"$ALERT_EMAIL\"]}}"

echo "Budget alert created: $500/month, alert at 80% ($400)"
