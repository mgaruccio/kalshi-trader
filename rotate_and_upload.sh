#!/bin/bash
# Daily rotation: upload old sessions to R2, then restart collector.
# Run via cron at a quiet market hour (e.g., 5am ET / 10:00 UTC).
#
# CRITICAL: Upload BEFORE restart. If disk is full and collector cant
# start, we still want the upload to succeed and free space.

set -euo pipefail

LOG="/root/kalshi-trader/upload.log"
echo "[$(date -Is)] === Starting daily rotation ===" | tee -a "$LOG"

# 1. Upload old sessions FIRST (frees disk before restart)
/root/kalshi-trader/upload_to_r2.sh

# 2. Restart collector via systemd (creates fresh session)
if \! systemctl restart collector.service; then
    echo "[$(date -Is)] WARNING: collector restart failed" | tee -a "$LOG"
    curl -s -H "Title: Collector Restart Failed" \
         -H "Priority: high" \
         -d "Rotation uploaded sessions but collector restart failed on $(hostname). Check disk space." \
         https://ntfy.sh/mike-scheduler
fi

# 3. Wait and verify collector is running
sleep 15
if systemctl is-active --quiet collector.service; then
    echo "[$(date -Is)] Collector running after restart" | tee -a "$LOG"
else
    echo "[$(date -Is)] Collector NOT running after restart" | tee -a "$LOG"
    curl -s -H "Title: Collector Not Running" \
         -H "Priority: urgent" \
         -d "Collector failed to start after rotation on $(hostname)" \
         https://ntfy.sh/mike-scheduler
fi

echo "[$(date -Is)] === Rotation complete ===" | tee -a "$LOG"
