#!/usr/bin/env bash
# ccswitch-health-check.sh — watchdog + early-warning for the ccswitch session daemon.
#
# Run every ~60s by ccswitch-health.timer (User=tony). Idempotent, no deps beyond
# curl / df / python3 / systemctl / ssh. Two jobs:
#   1. WATCHDOG: probe the daemon's control plane over whatever the daemon unit's OWN
#      environment says it binds — the classic Unix socket, or https+mTLS for a local
#      fleet-runner daemon (#807/#839). If the process is alive but the probe is
#      unresponsive (the "wedge" — invisible to systemd's Restart= policy
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
#       Skipped when the endpoint points at THIS host (#839): a local daemon has no SSH
#       hop and no sshfs to go stale — its stores are plain local directories, owned by
#       the disk check in section 2.
#   5d. Remote DB reachability — a TCP-only connect check, run over SSH FROM the daemon
#       host TO the database host/port, so a DB outage doesn't just look like a wedged
#       daemon. With a local daemon the same probe runs from this host directly, no SSH.
#   5e. Split-brain guard — an https endpoint at ANOTHER host AND this host's local
#       ccswitch-daemon.service active is the 2026-06-23 "Ready while working" incident
#       shape (two orchestrators, one database) and is alerted loudly regardless of any
#       other check's result. Fleet-aware since #839: a local daemon is NOT split-brain
#       when the endpoint points at THIS host (it IS the configured daemon), or when this
#       host is a registered+enabled fleet host in the hosts table (#807) — both are the
#       deliberate fleet-runner layout, not the two-orchestrators accident.
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

# --- Fleet-aware topology helpers (#839) -----------------------------------------
# The wedge watchdog must probe whatever the LOCAL daemon actually binds, which is
# decided by the daemon unit's OWN environment (server/ipc/paths.py sessiond_endpoint()),
# not this script's: a fleet-runner daemon (#807) binds https and never creates the UDS,
# and probing the missing socket is how a healthy, freshly onboarded daemon got
# wedge-restarted every cooldown during the 2026-08-30 dobby cutover (#839).
daemon_unit_env() {  # daemon_unit_env VAR — VAR's value in ccswitch-daemon.service's env
  local var="$1" val="" f v
  # systemd applies Environment= first, then EnvironmentFile= files in order — later wins.
  val=$(systemctl show ccswitch-daemon.service -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | sed -n "s/^${var}=//p" | tail -1)
  while IFS= read -r f; do
    f="${f%% *}"                      # strip systemd's " (ignore_errors=...)" suffix
    [ -n "$f" ] && [ -r "$f" ] || continue
    v=$(sed -n "s/^${var}=//p" "$f" | tail -1)
    v="${v%\"}"; v="${v#\"}"          # env files may quote values
    [ -n "$v" ] && val="$v"
  done < <(systemctl show ccswitch-daemon.service -p EnvironmentFiles --value 2>/dev/null)
  printf '%s' "$val"
}

LOCAL_BIND=$(daemon_unit_env CCSWITCH_SESSIOND_ENDPOINT)
if [ -z "$LOCAL_BIND" ]; then
  LOCAL_BIND=$(daemon_unit_env CCSWITCH_SESSIOND_SOCKET)
  if [ -n "$LOCAL_BIND" ] && [[ "$LOCAL_BIND" != *://* ]]; then LOCAL_BIND="unix://$LOCAL_BIND"; fi
fi
[ -z "$LOCAL_BIND" ] && LOCAL_BIND="unix://$SOCK"   # pre-fleet default: the well-known UDS
LOCAL_BIND_SCHEME=unix LOCAL_BIND_SOCK="${LOCAL_BIND#unix://}"
[[ "$LOCAL_BIND" == https://* ]] && LOCAL_BIND_SCHEME=https

endpoint_host() {  # endpoint_host <https://host:port> — the bare hostname
  local h="${1#https://}"; h="${h%%/*}"; printf '%s' "${h%%:*}"
}

# host_is_local <name-or-ip> — does this endpoint host refer to THIS machine? Matches
# the kernel hostname (short / fqdn / first label, which covers tailnet MagicDNS names
# like dobby.<tailnet>.ts.net) and falls back to comparing resolved addresses against
# this host's own interface addresses.
host_is_local() {
  local h="$1" short fqdn ip addrs
  [ -z "$h" ] && return 1
  case "$h" in localhost|127.*|::1) return 0 ;; esac
  short=$(hostname 2>/dev/null); fqdn=$(hostname -f 2>/dev/null)
  [ -n "$short" ] && [ "${h%%.*}" = "${short%%.*}" ] && return 0
  [ -n "$fqdn" ] && [ "$h" = "$fqdn" ] && return 0
  addrs=$(ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
  for ip in $(timeout 2 getent ahosts "$h" 2>/dev/null | awk '{print $1}' | sort -u); do
    grep -qxF "$ip" <<<"$addrs" && return 0
  done
  return 1
}

DAEMON_IS_LOCAL=0
if [[ "$DAEMON_ENDPOINT" == https://* ]] && host_is_local "$(endpoint_host "$DAEMON_ENDPOINT")"; then
  DAEMON_IS_LOCAL=1
fi

# A deliberately onboarded local fleet runner is registered + enabled in the hosts
# table (#807); ask the shared DB via the checkout's venv. Any failure to read the
# registry (no CCSWITCH_DB_URL, DB down, missing deps) returns non-zero so the
# split-brain guard fails CLOSED — it keeps its teeth when we cannot tell.
local_daemon_is_registered_fleet_host() {
  [ -n "${CCSWITCH_DB_URL:-}" ] || return 1
  local eps ep
  eps=$(cd "$HEALTH_REPO_ROOT" 2>/dev/null && timeout 15 "$HEALTH_PYTHON" - <<'PY' 2>/dev/null
import asyncio, os

async def main() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(os.environ["CCSWITCH_DB_URL"])
    try:
        async with engine.connect() as conn:
            for (ep,) in await conn.execute(text("SELECT endpoint FROM hosts WHERE enabled")):
                print(ep)
    finally:
        await engine.dispose()

asyncio.run(main())
PY
)
  [ -n "$eps" ] || return 1
  while IFS= read -r ep; do
    case "$ep" in
      unix://*|/*) return 0 ;;   # a UDS registry entry is this host by definition
      https://*) host_is_local "$(endpoint_host "$ep")" && return 0 ;;
    esac
  done <<<"$eps"
  return 1
}

mount_alert_key() {  # mount_alert_key <path> — stable alert-state key for a mount path
  printf 'mount-%s' "$(printf '%s' "$1" | tr -c 'a-zA-Z0-9' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')"
}

# --- 1. Control-plane watchdog --------------------------------------------------
# Probes the local daemon over whatever IT binds (LOCAL_BIND above): the UDS in the
# classic layout, https+mTLS for a fleet-runner daemon.
curl_daemon() {  # curl_daemon <path> [curl args...] — hit the local daemon's control plane
  local path="$1"; shift
  if [ "$LOCAL_BIND_SCHEME" = https ]; then
    curl -fsS -m "$PROBE_TIMEOUT" --cacert "$CLIENT_CA" --cert "$CLIENT_CERT" --key "$CLIENT_KEY" \
      "$@" "${LOCAL_BIND}${path}" 2>/dev/null
  else
    curl -fsS -m "$PROBE_TIMEOUT" --unix-socket "$LOCAL_BIND_SOCK" "$@" "http://daemon${path}" 2>/dev/null
  fi
}
probe_ok() { curl_daemon /healthz | grep -q '"status"'; }

# Best-effort graceful drain before a hard restart. begin_drain snapshots each
# live session's in-flight prompt + background tasks (pending_rearm) so the
# post-restart rehydrate can resume them, and refuses new turns; we then wait up
# to DRAIN_WAIT for in-flight turns to finish (drain_status == "ready"). If the
# socket is truly wedged these RPCs just fail fast and we restart anyway — so this
# is strictly an improvement: a merely slow/overloaded daemon (the common near-
# wedge) keeps its turns instead of orphaning them to needs_attention. The daemon
# self-clears the drain after ~600s, so a restart that never lands can't wedge it.
rpc() {  # rpc <method> — POST an empty-param RPC to the sessiond control plane
  curl_daemon "/rpc/$1" -X POST -H 'content-type: application/json' -d '{}'
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
# An https-bound daemon can only be probed with the edge's client cert material. With
# it missing, a probe "failure" says nothing about the daemon — restarting on a probe
# we cannot run is #839's failure mode with a different root cause, so don't.
PROBE_POSSIBLE=1
if [ "$LOCAL_BIND_SCHEME" = https ] && { [ -z "$CLIENT_CERT" ] || [ -z "$CLIENT_KEY" ] || [ -z "$CLIENT_CA" ]; }; then
  PROBE_POSSIBLE=0
fi

healthy=0
if [ "$PROBE_POSSIBLE" -eq 1 ]; then
  for _ in $(seq 1 "$PROBE_RETRIES"); do
    if probe_ok; then healthy=1; break; fi
    sleep 2
  done
fi

LOCAL_DAEMON_ACTIVE=0
systemctl is-active --quiet ccswitch-daemon.service && LOCAL_DAEMON_ACTIVE=1

if [ "$PROBE_POSSIBLE" -eq 0 ]; then
  if [ "$LOCAL_DAEMON_ACTIVE" -eq 1 ]; then
    alert local-daemon-unprobeable "local daemon binds $LOCAL_BIND but CCSWITCH_SESSIOND_CLIENT_CERT/KEY/CA are incomplete in the health-check env — wedge watchdog is blind (and deliberately NOT restarting on a probe it cannot run)"
  else
    clear_alert local-daemon-unprobeable
  fi
elif [ "$healthy" -eq 1 ]; then
  clear_alert daemon-wedged
  clear_alert local-daemon-unprobeable
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
    log "daemon control plane ($LOCAL_BIND) unresponsive after ${PROBE_RETRIES} probes — restarting ccswitch-daemon.service"
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

  # 5e. Split-brain guard — checked first regardless of what follows. Two orchestrators
  # over one database (2026-06-23 "Ready while working"). Fleet-aware since #839: an
  # active local daemon is legitimate when the endpoint points at THIS host (it IS the
  # configured daemon — the dobby fleet-runner layout) or when this host is a
  # registered+enabled fleet host (#807, deliberate fleet runner). Only the
  # unregistered second orchestrator — the shape the guard was written for — alerts.
  if [ "$LOCAL_DAEMON_ACTIVE" -eq 1 ] && [ "$DAEMON_IS_LOCAL" -ne 1 ] \
      && ! local_daemon_is_registered_fleet_host; then
    alert split-brain "SPLIT-BRAIN RISK: remote daemon endpoint ($DAEMON_ENDPOINT) is configured but THIS host's ccswitch-daemon.service is ALSO active and is NOT a registered+enabled fleet host — two orchestrators over one database. Stop the local unit immediately (systemctl stop ccswitch-daemon.service) unless a deliberate rollback to local is in progress."
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
  #
  # Both assume the daemon host is REMOTE, reached over SSH, with sshfs mounts back to
  # this host. With the daemon local (#839) there is no SSH hop (the configured
  # CCSWITCH_DAEMON_SSH_HOST may even point at a decommissioned host) and nothing sshfs
  # to go stale — the stores are plain local directories owned by the disk check in
  # section 2. The DB probe keeps its value, run from this host directly.
  if [ "$DAEMON_IS_LOCAL" -eq 1 ]; then
    if timeout 5 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$REMOTE_PG_HOST" "$REMOTE_PG_PORT" 2>/dev/null; then
      clear_alert remote-db-unreachable
    else
      alert remote-db-unreachable "this host (local fleet-runner daemon) cannot reach Postgres at ${REMOTE_PG_HOST}:${REMOTE_PG_PORT} (TCP connect failed) — daemon may be up but every DB-backed operation is failing"
    fi
    # Alert state left over from a previous remote-daemon topology must not linger.
    clear_alert remote-ssh-unreachable
    for m in $REMOTE_MOUNTS; do clear_alert "$(mount_alert_key "$m")"; done
  elif [ -n "$DAEMON_SSH_HOST" ]; then
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
        mount_key="$(mount_alert_key "$m")"
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
