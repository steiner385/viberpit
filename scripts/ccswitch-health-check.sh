#!/usr/bin/env bash
# ccswitch-health-check.sh — watchdog + early-warning for the ccswitch session daemon.
#
# Run every ~60s by ccswitch-health.timer (User=tony). Idempotent, no deps beyond
# curl / df / python3 / systemctl. Two jobs:
#   1. WATCHDOG: probe the daemon's control-plane socket; if the process is alive but
#      the socket is unresponsive (the "wedge" — invisible to systemd's Restart=on-failure
#      because the process never exits), restart ccswitch-daemon.service. Honors a cooldown
#      so a genuine crash-loop is left to Restart=on-failure instead of being thrashed here.
#   2. EARLY WARNING: alert on low disk, account needs-reauth, or daemon memory near the
#      MemoryMax ceiling — the three conditions that silently broke prior deploys.
#
# Alerts always go to the journal; if CCSWITCH_ALERT_WEBHOOK is set (e.g. an ntfy topic
# URL) they are also POSTed there, de-duplicated so a standing condition pings ~every 30m
# rather than every 60s.
set -uo pipefail

SOCK="${CCSWITCH_SESSIOND_SOCKET:-/run/ccswitch/sessiond.sock}"
LEDGER="${CCSWITCH_LEDGER:-/home/tony/.claude-accounts/ledger.json}"
DISK_PATH="${CCSWITCH_DISK_PATH:-/}"
DISK_WARN_PCT="${CCSWITCH_DISK_WARN_PCT:-85}"
MEM_WARN_BYTES="${CCSWITCH_MEM_WARN_BYTES:-11811160064}"   # 11 GiB (below the 12G MemoryMax)
PROBE_RETRIES="${CCSWITCH_PROBE_RETRIES:-3}"
PROBE_TIMEOUT="${CCSWITCH_PROBE_TIMEOUT:-5}"
RESTART_COOLDOWN="${CCSWITCH_RESTART_COOLDOWN:-300}"        # >=1 auto-restart per 5 min
STARTUP_GRACE="${CCSWITCH_STARTUP_GRACE:-60}"              # don't restart a still-booting daemon
ALERT_WEBHOOK="${CCSWITCH_ALERT_WEBHOOK:-}"
STATE_DIR="${CCSWITCH_HEALTH_STATE_DIR:-/var/lib/ccswitch/health}"

mkdir -p "$STATE_DIR" 2>/dev/null || true
log() { echo "ccswitch-health: $*"; }   # -> journal via systemd

now() { date +%s; }

# alert <key> <message>: journal always; webhook on first sighting then every 30m.
alert() {
  local key="$1"; shift; local msg="$*"
  log "ALERT[$key] $msg"
  [ -z "$ALERT_WEBHOOK" ] && return 0
  local statef="$STATE_DIR/alert-$key" last=0 t; t=$(now)
  [ -f "$statef" ] && last=$(cat "$statef" 2>/dev/null || echo 0)
  if [ $(( t - last )) -ge 1800 ]; then
    curl -fsS -m 10 -H "Title: ccswitch alert" -d "$msg" "$ALERT_WEBHOOK" >/dev/null 2>&1 || true
    echo "$t" > "$statef"
  fi
}
clear_alert() { rm -f "$STATE_DIR/alert-$1" 2>/dev/null || true; }

# --- 1. Control-plane watchdog --------------------------------------------------
probe_ok() {
  curl -fsS -m "$PROBE_TIMEOUT" --unix-socket "$SOCK" http://daemon/healthz 2>/dev/null \
    | grep -q '"status"'
}
healthy=0
for _ in $(seq 1 "$PROBE_RETRIES"); do
  if probe_ok; then healthy=1; break; fi
  sleep 2
done

if [ "$healthy" -eq 1 ]; then
  clear_alert daemon-wedged
elif systemctl is-active --quiet ccswitch-daemon.service; then
  # Process up but socket dead == wedged. Restart within cooldown.
  rf="$STATE_DIR/last-restart"; last=0; t=$(now)
  [ -f "$rf" ] && last=$(cat "$rf" 2>/dev/null || echo 0)
  # Don't fight a still-booting daemon: rehydrate/relaunch can briefly outlast the
  # probe window, and a deploy restart must be allowed to come up on its own.
  started=$(date -d "$(systemctl show ccswitch-daemon.service -p ActiveEnterTimestamp --value 2>/dev/null)" +%s 2>/dev/null || echo 0)
  age=$(( t - started ))
  if [ "$started" -gt 0 ] && [ "$age" -lt "$STARTUP_GRACE" ]; then
    log "daemon active only ${age}s (< ${STARTUP_GRACE}s grace) — still booting, deferring wedge check"
  elif [ $(( t - last )) -ge "$RESTART_COOLDOWN" ]; then
    log "daemon socket unresponsive after ${PROBE_RETRIES} probes — restarting ccswitch-daemon.service"
    if sudo -n systemctl restart ccswitch-daemon.service 2>/dev/null; then
      echo "$t" > "$rf"
      alert daemon-wedged "control plane was wedged (socket unresponsive); auto-restarted $(date -Is)"
    else
      alert daemon-restart-failed "daemon wedged AND auto-restart failed — manual action needed"
    fi
  else
    log "daemon unresponsive but within ${RESTART_COOLDOWN}s restart cooldown — leaving to Restart=on-failure"
  fi
else
  log "daemon service not active — not our job (systemd Restart=on-failure owns this)"
fi

# --- 2. Disk --------------------------------------------------------------------
use=$(df --output=pcent "$DISK_PATH" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$use" ] && [ "$use" -ge "$DISK_WARN_PCT" ]; then
  alert disk "disk ${DISK_PATH} at ${use}% (>= ${DISK_WARN_PCT}%) — archive/rehydrate writes at risk"
else
  clear_alert disk
fi

# --- 3. Account needs-reauth ----------------------------------------------------
reauth=$(python3 -c "
import json,sys
try: d=json.load(open('$LEDGER'))
except Exception: sys.exit(0)
print(','.join(n for n,i in (d.get('accounts') or {}).items()
                if isinstance(i,dict) and i.get('status')=='needs-reauth'))
" 2>/dev/null)
if [ -n "$reauth" ]; then
  alert reauth "account(s) need reauth: ${reauth} — their sessions fail to authenticate until reauth"
else
  clear_alert reauth
fi

# --- 4. Daemon memory near the ceiling ------------------------------------------
mem=$(systemctl show ccswitch-daemon.service -p MemoryCurrent --value 2>/dev/null)
if [[ "$mem" =~ ^[0-9]+$ ]] && [ "$mem" -ge "$MEM_WARN_BYTES" ]; then
  alert memory "daemon memory $(( mem / 1024 / 1024 ))MiB near the 12G MemoryMax — OOM-stop risk"
else
  clear_alert memory
fi

exit 0
