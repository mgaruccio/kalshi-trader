#!/bin/bash
# Check if collector has logged a heartbeat in the last 5 minutes
RECENT=$(journalctl -u collector --since "5 minutes ago" --no-pager -o cat | grep -c "HEARTBEAT")
if [ "$RECENT" -eq 0 ]; then
    curl -s -H "Title: Collector Down" \
         -H "Priority: high" \
         -d "No collector heartbeat in 5 minutes on $(hostname)" \
         https://ntfy.sh/mike-scheduler
fi
