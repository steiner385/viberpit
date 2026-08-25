#!/usr/bin/env bash
# ccswitch-health-check.sh — watchdog + early-warning for the ccswitch session daemon.
#
# Run every ~60s by ccswitch-health.timer (User=tony). Idempotent, no deps beyond
# curl / df / python3 / systemctl / ssh. Two jobs:
#   1. WATCHDOG: probe the daemon's control-plane socket; if the process is alive but
#      the socket is unresponsive (the "wedge" — invisible to systemd's Restart= policy
#      because the process never exits), restart ccswitch-daemon.service. Honors a cooldown
#      so a genuine crash-loop is left to systemd's Restart= policy instead of being thrashed here.
#   2. EARLY WARNING: alert on low disk, account needs-reauth, or daemon memory near the
#      MemoryMax ceiling — the three conditions that silently broke prior deploys.
#
# Alerts always go to the journal; if CCSWITCH_ALERT_WEBHOOK is set (e.g. an ntfy topic
# URL) they are also POSTed there, de-duplicated so a standing condition pings ~every 30m
# rather than every 60s.
#
# --- Remote-daemon mode (cross-host deployments, e.g. dobby edge + rocky daemon) ------
# Sections 1-4 above only ever look at THIS host (unix socket + local systemd unit), so
# once the daemon moves to another host over mTLS (CCSWITCH_SESSIOND_ENDPOINT=https://...)
# they go permanently quiet — "not our job" — and the watchdog is blind to a real outage.
# When a remote https daemon endpoint is configured (CCSWITCH_DAEMON_ENDPOINT, or reusing
# the edge's own CCSWITCH_SESSIOND_ENDPOINT — see server/ipc/paths.py), section 5 adds:
#   5a. Remote daemon reachability — GET /info over mTLS with the edge's client cert/key/CA.
#       Alerted, never auto-restarted: a restart across SSH is a bigger decision than the
#       local wedge-restart in section 1. NOTE: this used to say "systemd Restart=always
#       already owns recovery on the daemon host itself" — that is FALSE for one case, and
#       it cost a 45-minute outage on 2026-08-25 (#765). Restart= only restarts a process
#       that actually ran and exited; a start job CANCELLED by a failed Requires= (e.g.
#       ccswitch-mounts.service not ready at boot) leaves the unit inactive forever and
#       Restart= never engages. That gap is now covered on the daemon host itself by
#       ccswitch-daemon-ensure.timer, not by this script.
#   5b. Protocol compatibility — /info's "protocol" must match this checkout's
#       server.ipc.protocol.PROTOCOL_VERSION, or the edge silently refuses the daemon
#       (looks like a hang, is actually a stale build on one side).
#   5c. Remote mount health — the daemon host's sshfs mounts (accounts/config store,
#       archive volume) checked over SSH with a short-timeout read, not just `mountpoint`,
#       because a stale sshfs mount can pass the mountpoint test while every read hangs.
#   5d. Remote DB reachability — a TCP-only connect check, run over SSH FROM the daemon
#       host TO the database host/port, so a DB outage doesn't just look like a wedged
#       daemon.
#   5e. Split-brain guard — remote mode configured AND this host's local
#       ccswitch-daemon.service active is the 2026-06-23 "Ready while working" incident
#       shape (two orchestrators, one database) and is alerted loudly regardless of any
#       other check's result.
# All of 5a-5e are strictly additive and opt-in: with no remote endpoint configured, this
# script's behavior (including section 1's own systemctl check) is unchanged.
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
DRAIN_WAIT="${CCSWITCH_DRAIN_WAIT:-20}"                    # best-effort graceful-drain budget before a wedge restart
ALERT_WEBHOOK="${CCSWITCH_ALERT_WEBHOOK:-}"
STATE_DIR="${CCSWITCH_HEALTH_STATE_DIR:-/var/lib/ccswitch/health}"

# --- Remote-daemon mode config (all opt-in; empty DAEMON_ENDPOINT == section 5 is a no-op) --
# CCSWITCH_DAEMON_ENDPOINT is health-check-specific; falls back to the edge's own
# CCSWITCH_SESSIOND_ENDPOINT (server/ipc/paths.py's sessiond_endpoint()) since a cross-host
# install already sets that for the edge process and EnvironmentFile= sources the same file
# here. Only an https:// value turns remote mode on — unix:// / unset stays local-only.
DAEMON_ENDPOINT="${CCSWITCH_DAEMON_ENDPOINT:-${CCSWITCH_SESSIOND_ENDPOINT:-}}"
DAEMON_SSH_HOST="${CCSWITCH_DAEMON_SSH_HOST:-}"
CLIENT_CERT="${CCSWITCH_SESSIOND_CLIENT_CERT:-}"
CLIENT_KEY="${CCSWITCH_SESSIOND_CLIENT_KEY:-}"
CLIENT_CA="${CCSWITCH_SESSIOND_CLIENT_CA:-}"
# Space-separated mountpoints the daemon host must have live+readable (default: the rocky
# sshfs cutover's three mounts — account store, shared ~/.claude, archive volume).
REMOTE_MOUNTS="${CCSWITCH_HEALTH_REMOTE_MOUNTS:-/home/tony/.claude-accounts /home/tony/.claude /mnt/hdd-storage}"
# Database host:port as reachable FROM the daemon host — deliberately separate from
# CCSWITCH_DB_URL, whose host is typically 127.0.0.1 (the edge's own loopback to a
# same-host Postgres), which is meaningless from a remote daemon host.
REMOTE_PG_HOST="${CCSWITCH_HEALTH_REMOTE_PG_HOST:-100.102.11.121}"
REMOTE_PG_PORT="${CCSWITCH_HEALTH_REMOTE_PG_PORT:-5432}"
SSH_CONNECT_TIMEOUT="${CCSWITCH_SSH_CONNECT_TIMEOUT:-5}"
SSH_TIMEOUT="${CCSWITCH_SSH_TIMEOUT:-15}"          # overall budget per ssh invocation
# Local checkout used to read this build's PROTOCOL_VERSION (defaults to this script's repo).
HEALTH_REPO_ROOT="${CCSWITCH_HEALTH_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
HEALTH_PYTHON="${CCSWITCH_HEALTH_PYTHON:-}"
if [ -z "$HEALTH_PYTHON" ]; then
  if [ -x "$HEALTH_REPO_ROOT/.venv/bin/python3" ]; then
    HEALTH_PYTHON="$HEALTH_REPO_ROOT/.venv/bin/python3"
  else
    HEALTH_PYTHON="python3"
  fi
fi

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

# Best-effort graceful drain before a hard restart. begin_drain snapshots each
# live session's in-flight prompt + background tasks (pending_rearm) so the
# post-restart rehydrate can resume them, and refuses new turns; we then wait up
# to DRAIN_WAIT for in-flight turns to finish (drain_status == "ready"). If the
# socket is truly wedged these RPCs just fail fast and we restart anyway — so this
# is strictly an improvement: a merely slow/overloaded daemon (the common near-
# wedge) keeps its turns instead of orphaning them to needs_attention. The daemon
# self-clears the drain after ~600s, so a restart that never lands can't wedge it.
rpc() {  # rpc <method> — POST an empty-param RPC to the sessiond socket
  curl -fsS -m "$PROBE_TIMEOUT" --unix-socket "$SOCK" \
    -X POST "http://daemon/rpc/$1" -H 'content-type: application/json' -d '{}' 2>/dev/null
}
graceful_drain() {
  rpc begin_drain >/dev/null || return 0   # wedged/unreachable → skip, restart as before
  local deadline=$(( $(now) + DRAIN_WAIT )) state
  while [ "$(now)" -lt "$deadline" ]; do
    state=$(rpc drain_status | python3 -c \
      'import sys,json; print(json.load(sys.stdin).get("result",{}).get("state",""))' 2>/dev/null)
    if [ "$state" = "ready" ]; then
      log "graceful drain reached 'ready' — restarting cleanly"
      return 0
    fi
    sleep 2
  done
  log "graceful drain did not reach 'ready' within ${DRAIN_WAIT}s — restarting anyway"
}
healthy=0
for _ in $(seq 1 "$PROBE_RETRIES"); do
  if probe_ok; then healthy=1; break; fi
  sleep 2
done

LOCAL_DAEMON_ACTIVE=0
systemctl is-active --quiet ccswitch-daemon.service && LOCAL_DAEMON_ACTIVE=1

if [ "$healthy" -eq 1 ]; then
  clear_alert daemon-wedged
elif [ "$LOCAL_DAEMON_ACTIVE" -eq 1 ]; then
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
    # Preserve in-flight turns when the daemon is slow-but-responsive; no-op when truly wedged.
    graceful_drain
    if sudo -n systemctl restart ccswitch-daemon.service 2>/dev/null; then
      echo "$t" > "$rf"
      alert daemon-wedged "control plane was wedged (socket unresponsive); auto-restarted $(date -Is)"
    else
      alert daemon-restart-failed "daemon wedged AND auto-restart failed — manual action needed"
    fi
  else
    log "daemon unresponsive but within ${RESTART_COOLDOWN}s restart cooldown — leaving to systemd Restart= policy"
  fi
else
  # Restart= covers a process that ran and died; ccswitch-daemon-ensure.timer (on the
  # daemon host) covers a start job cancelled by a failed dependency, which Restart=
  # does NOT cover (#765). Neither is this script's job.
  log "daemon service not active — not our job (systemd Restart= + ccswitch-daemon-ensure.timer own this)"
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

# --- 5. Remote-daemon mode (cross-host: dobby edge + rocky daemon) --------------
# No-op (identical to today) unless a remote https endpoint is configured. Every
# sub-check here alerts on its own failure instead of raising, so a slow/unreachable
# remote host degrades to "one more alert" rather than aborting sections 1-4 above —
# those already ran and are unaffected by anything below.
if [[ "$DAEMON_ENDPOINT" == https://* ]]; then

  # 5e. Split-brain guard — cheap, local-only, checked first regardless of what
  # follows. Two orchestrators over one database (2026-06-23 "Ready while working").
  if [ "$LOCAL_DAEMON_ACTIVE" -eq 1 ]; then
    alert split-brain "SPLIT-BRAIN RISK: remote daemon endpoint ($DAEMON_ENDPOINT) is configured but THIS host's ccswitch-daemon.service is ALSO active — two orchestrators over one database. Stop the local unit immediately (systemctl stop ccswitch-daemon.service) unless a deliberate rollback to local is in progress."
  else
    clear_alert split-brain
  fi

  # 5a/5b. Remote reachability + protocol compatibility, via one mTLS GET /info.
  remote_probe() {
    curl -fsS -m "$PROBE_TIMEOUT" --cacert "$CLIENT_CA" --cert "$CLIENT_CERT" --key "$CLIENT_KEY" \
      "$DAEMON_ENDPOINT/info" 2>/dev/null
  }
  remote_info="" remote_healthy=0
  if [ -n "$CLIENT_CERT" ] && [ -n "$CLIENT_KEY" ] && [ -n "$CLIENT_CA" ]; then
    for _ in $(seq 1 "$PROBE_RETRIES"); do
      remote_info=$(remote_probe) && [ -n "$remote_info" ] && { remote_healthy=1; break; }
      sleep 2
    done
  else
    log "remote daemon endpoint set but CCSWITCH_SESSIOND_CLIENT_CERT/KEY/CA incomplete — cannot probe"
  fi

  if [ "$remote_healthy" -eq 1 ]; then
    clear_alert remote-daemon-unreachable
  else
    alert remote-daemon-unreachable "remote daemon at $DAEMON_ENDPOINT unreachable after ${PROBE_RETRIES} mTLS probes — NOT auto-restarting (cross-host restart is a bigger decision; systemd Restart=always on the daemon host owns recovery there)"
  fi

  # Protocol check only runs off a successful probe; a failure to read either side's
  # version must not itself alert (per design — this is a best-effort cross-check).
  if [ "$remote_healthy" -eq 1 ]; then
    remote_protocol=$(printf '%s' "$remote_info" | python3 -c \
      'import sys,json
try:
    print(json.load(sys.stdin).get("protocol",""))
except Exception:
    pass' 2>/dev/null)
    local_protocol=$(cd "$HEALTH_REPO_ROOT" 2>/dev/null && "$HEALTH_PYTHON" -c \
      'from server.ipc import protocol; print(protocol.PROTOCOL_VERSION)' 2>/dev/null)
    if [[ "$remote_protocol" =~ ^[0-9]+$ ]] && [[ "$local_protocol" =~ ^[0-9]+$ ]]; then
      if [ "$remote_protocol" != "$local_protocol" ]; then
        alert protocol-mismatch "protocol mismatch: remote daemon at $DAEMON_ENDPOINT speaks protocol $remote_protocol, this edge checkout ($HEALTH_REPO_ROOT) speaks $local_protocol — edge will refuse the daemon (looks like a hang, is actually a stale build on one side)"
      else
        clear_alert protocol-mismatch
      fi
    else
      log "protocol compatibility check skipped — could not read local (${local_protocol:-<empty>}) and/or remote (${remote_protocol:-<empty>}) protocol version"
    fi
  fi

  # 5c/5d. Remote mount health + DB TCP reachability — one SSH round-trip so a slow
  # network doesn't multiply the every-60s cost. If SSH itself is unreachable, alert
  # that once and leave the per-mount/per-DB alert states as they were (no data this
  # cycle to justify flipping them either way).
  if [ -n "$DAEMON_SSH_HOST" ]; then
    # shellcheck disable=SC2086  # intentional: REMOTE_MOUNTS is a space-separated list of paths
    remote_out=$(timeout "$SSH_TIMEOUT" ssh -o BatchMode=yes -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" \
      "$DAEMON_SSH_HOST" bash -s -- "$REMOTE_PG_HOST" "$REMOTE_PG_PORT" $REMOTE_MOUNTS <<'REMOTE_SCRIPT' 2>/dev/null
set -u
pg_host="$1"; pg_port="$2"; shift 2
if timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$pg_host" "$pg_port" 2>/dev/null; then
  echo "PG_OK"
else
  echo "PG_FAIL"
fi
for m in "$@"; do
  if mountpoint -q "$m" 2>/dev/null && timeout 5 ls "$m" >/dev/null 2>&1; then
    echo "MOUNT_OK $m"
  else
    echo "MOUNT_FAIL $m"
  fi
done
REMOTE_SCRIPT
)
    ssh_rc=$?
    if [ "$ssh_rc" -ne 0 ] || [ -z "$remote_out" ]; then
      alert remote-ssh-unreachable "cannot reach daemon host ($DAEMON_SSH_HOST) via SSH to check mounts/DB — mount and DB alert states below are stale until this recovers"
    else
      clear_alert remote-ssh-unreachable

      if grep -q '^PG_OK$' <<<"$remote_out"; then
        clear_alert remote-db-unreachable
      else
        alert remote-db-unreachable "daemon host ($DAEMON_SSH_HOST) cannot reach Postgres at ${REMOTE_PG_HOST}:${REMOTE_PG_PORT} (TCP connect failed) — daemon may be up but every DB-backed operation is failing"
      fi

      for m in $REMOTE_MOUNTS; do
        mount_key="mount-$(printf '%s' "$m" | tr -c 'a-zA-Z0-9' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')"
        if grep -qF "MOUNT_OK $m" <<<"$remote_out"; then
          clear_alert "$mount_key"
        else
          alert "$mount_key" "remote mount unhealthy on $DAEMON_SSH_HOST: $m is not a live mount, or a read against it timed out (stale sshfs?) — sessions may run with stale/missing config or fail account/credential reads"
        fi
      done
    fi
  else
    log "remote daemon endpoint set but CCSWITCH_DAEMON_SSH_HOST unset — skipping mount + DB TCP checks"
  fi
fi

exit 0
