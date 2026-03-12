#!/bin/bash
# Check collector heartbeat and disk space.
# Runs via systemd timer (collector-health.timer).
#
# Collector logs to /root/kalshi-trader/collector.log (NOT journald).
# Heartbeat check uses log file age + HEARTBEAT grep.

COLLECTOR_LOG="/root/kalshi-trader/collector.log"
NTFY_TOPIC="mike-scheduler"

# --- Heartbeat check ---
if [ \! -f "$COLLECTOR_LOG" ]; then
    curl -s -H "Title: Collector Down" \
         -H "Priority: high" \
         -d "No collector log found on $(hostname)" \
         https://ntfy.sh/$NTFY_TOPIC
else
    LOG_AGE=$(( $(date +%s) - $(stat -c %Y "$COLLECTOR_LOG") ))
    RECENT_HB=$(tail -1000 "$COLLECTOR_LOG" 2>/dev/null | grep -c "HEARTBEAT")

    if [ "$LOG_AGE" -gt 300 ]; then
        curl -s -H "Title: Collector Down" \
             -H "Priority: high" \
             -d "Collector log stale (${LOG_AGE}s since last write) on $(hostname)" \
             https://ntfy.sh/$NTFY_TOPIC
    elif [ "$RECENT_HB" -eq 0 ]; then
        curl -s -H "Title: Collector Stuck" \
             -H "Priority: high" \
             -d "Collector writing but no heartbeat in last 1000 lines on $(hostname)" \
             https://ntfy.sh/$NTFY_TOPIC
    fi
fi

# --- Disk space check (alert at 80%, suppress duplicates for 12h) ---
USAGE=$(df / --output=pcent | tail -1 | tr -d " %")
DISK_FLAG="/tmp/disk_alert_sent"

if [ "$USAGE" -ge 80 ]; then
    SHOULD_ALERT=0
    if [ \! -f "$DISK_FLAG" ]; then
        SHOULD_ALERT=1
    elif [ $(( $(date +%s) - $(stat -c %Y "$DISK_FLAG") )) -gt 43200 ]; then
        SHOULD_ALERT=1
    fi

    if [ "$SHOULD_ALERT" -eq 1 ]; then
        AVAIL=$(df -h / --output=avail | tail -1 | tr -d " ")
        curl -s -H "Title: Disk Space Low" \
             -H "Priority: high" \
             -d "Collector disk at ${USAGE}% (${AVAIL} free) on $(hostname). Run upload_to_r2.sh or check rotation." \
             https://ntfy.sh/$NTFY_TOPIC
        touch "$DISK_FLAG"
    fi
elif [ -f "$DISK_FLAG" ]; then
    rm -f "$DISK_FLAG"
fi
