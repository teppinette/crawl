#!/bin/bash
# Job file cleanup — archive jobs older than 30 days, delete archives older than 90 days
# Runs daily via cron

JOBS_DIR="$HOME/crawl/api/jobs"
ARCHIVE_DIR="$HOME/crawl/api/jobs_archive"
mkdir -p "$ARCHIVE_DIR"

# Archive jobs older than 30 days
find "$JOBS_DIR" -name "*.json" -mtime +30 -exec mv {} "$ARCHIVE_DIR/" \;
ARCHIVED=$(find "$ARCHIVE_DIR" -name "*.json" -mtime -1 | wc -l)

# Delete archived jobs older than 90 days
DELETED=$(find "$ARCHIVE_DIR" -name "*.json" -mtime +90 -delete -print | wc -l)

if [ "$ARCHIVED" -gt 0 ] || [ "$DELETED" -gt 0 ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Archived: $ARCHIVED, Deleted: $DELETED"
fi
