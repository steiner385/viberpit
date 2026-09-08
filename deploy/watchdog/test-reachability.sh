#!/usr/bin/env bash
# test-reachability.sh — outward-reachability test for the hardware watchdog daemon.
#
# Fixes issue #767. The stock rocky watchdog only ran a LOOPBACK ssh/tcp test
# (`test-ssh.sh`), which proves sshd is alive *on the box* but says nothing about
# whether anything can reach the box. On 2026-08-25 rocky was off the network for
# ~46 min, internally healthy the whole time, and the watchdog never fired. This
# script fills that gap: it pings a set of INDEPENDENT off-box targets and, only
# when the host is provably cut off from all of them, asks the watchdog to reboot.
#
# ------------------------------------------------------------------------------
# THE HAZARD (read before touching the thresholds): a naive ping-then-reboot
# reboot-LOOPS the whole fleet. If the *network* is down rather than this host —
# a router reboot, an upstream blip, a peer rebooting — every host running this
# test reboots, repeatedly, into the same dead network. Four guards prevent that,
# and none of them are optional:
#
#   1. MULTIPLE INDEPENDENT TARGETS, all must fail. One dead peer (e.g. dobby
#      rebooting) can never trip it — some other target still answers.
#   2. SUSTAINED WINDOW. The outage must persist continuously for WDR_FAIL_WINDOW
#      (default 5 min), far above a normal blip, before a reboot is even eligible.
#   3. REBOOT COOLDOWN. A hard rate limit: at most one reboot per
#      WDR_REBOOT_COOLDOWN (default 30 min) regardless of anything else, so a
#      genuinely-down network degrades to "one reboot every 30 min", not a loop.
#   4. BOOT GRACE. For WDR_BOOT_GRACE (default 10 min) after boot the test is a
#      no-op, so a host that just came up can't immediately re-reboot before its
#      network stack (Tailscale, routes, DHCP) has settled.
#
# And the safest default of all: with NO targets configured the script is a
# no-op. A misconfigured host does nothing rather than reboot-loop.
#
# ------------------------------------------------------------------------------
# How the watchdog daemon drives this (watchdog(8), `test-directory = /etc/watchdog.d`):
#   * every interval it runs this file with argument `test`; non-zero == "problem"
#   * on a non-zero test it re-runs the SAME file with `repair <errno>`; if repair
#     returns 0 the problem is considered fixed (no reboot), non-zero == reboot.
# We cannot "repair" an unreachable network, so the `repair` path is where the
# cooldown gate lives and where the actual reboot decision is committed.
#
# Config lives in WDR_CONF (default /etc/viberpit/watchdog-reachability.conf; legacy fallback); see
# reachability.conf.example. Every knob is also overridable by environment for
# testing (server/tests/test_watchdog_reachability.py drives it with injected
# clock, uptime, ping stub and state dir — no real network is touched).
set -uo pipefail

WDR_DEFAULT=/etc/viberpit/watchdog-reachability.conf
if [ ! -e "$WDR_DEFAULT" ] && [ -e /etc/ccswitch/watchdog-reachability.conf ]; then
    WDR_DEFAULT=/etc/ccswitch/watchdog-reachability.conf
fi
WDR_CONF="${WDR_CONF:-$WDR_DEFAULT}"
# shellcheck disable=SC1090  # operator-provided path, sourced only if present
[ -f "$WDR_CONF" ] && . "$WDR_CONF"

TARGETS="${WDR_TARGETS:-}"
STATE_DIR="${WDR_STATE_DIR:-/var/lib/watchdog-reachability}"
BOOT_GRACE="${WDR_BOOT_GRACE:-600}"          # 10 min: let the network stack settle after boot
FAIL_WINDOW="${WDR_FAIL_WINDOW:-300}"        # 5 min of continuous total isolation before eligible
REBOOT_COOLDOWN="${WDR_REBOOT_COOLDOWN:-1800}"  # >=30 min between reboots, always
PING="${WDR_PING:-ping}"
PING_COUNT="${WDR_PING_COUNT:-2}"
PING_TIMEOUT="${WDR_PING_TIMEOUT:-2}"        # per-target deadline (ping -w), seconds

FIRST_FAILURE="$STATE_DIR/first-failure"     # epoch of the start of the current outage
MARKER="$STATE_DIR/last-reboot"              # epoch of the last committed reboot (cooldown anchor)

mkdir -p "$STATE_DIR" 2>/dev/null || true

log() {  # -> journal (watchdog runs us under systemd); also stderr for tests
  if command -v logger >/dev/null 2>&1; then
    logger -t watchdog-reachability -- "$*" 2>/dev/null || true
  fi
  echo "watchdog-reachability: $*" >&2
}

now() { echo "${WDR_NOW:-$(date +%s)}"; }

uptime_s() {
  if [ -n "${WDR_UPTIME_S:-}" ]; then
    echo "$WDR_UPTIME_S"
  else
    awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0
  fi
}

# reachable: 0 (success) as soon as ANY target answers; 1 only if ALL fail.
# Short-circuits on the first live target so the healthy path is a single ping.
reachable() {
  local t
  for t in $TARGETS; do
    if "$PING" -c "$PING_COUNT" -w "$PING_TIMEOUT" "$t" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

# reboot cooldown gate — true (0) while a prior reboot is still within cooldown.
in_cooldown() {
  local t last=0
  [ -f "$MARKER" ] || return 1
  last=$(cat "$MARKER" 2>/dev/null || echo 0)
  t=$(now)
  [ $(( t - last )) -lt "$REBOOT_COOLDOWN" ]
}

ARG="${1:-test}"

# Guard 0 (safest default): nothing configured -> do nothing.
if [ -z "${TARGETS// /}" ]; then
  log "no targets configured (set WDR_TARGETS in $WDR_CONF) — no-op"
  exit 0
fi

if reachable; then
  # Any answer at all clears the outage window; nothing to do.
  rm -f "$FIRST_FAILURE" 2>/dev/null || true
  exit 0
fi

# --- unreachable from here down -------------------------------------------------

if [ "$ARG" = "repair" ]; then
  # We only get here after a failed `test`, and the network is still down. This is
  # the commit point for a reboot. Cooldown is the hard rate limit.
  if in_cooldown; then
    log "unreachable, but within ${REBOOT_COOLDOWN}s reboot cooldown — NOT rebooting"
    exit 0
  fi
  now > "$MARKER" 2>/dev/null || true
  log "REBOOTING: all targets ($TARGETS) unreachable and past the sustained window — asking watchdog to reset"
  exit 1
fi

# --- ARG == test (or any unexpected value: treat as a safe test) ----------------

# Guard 4: boot grace. Right after boot the stack may not be up yet — never reboot,
# and reset the window so it must accrue afresh once we're past grace.
up=$(uptime_s)
if [ "$up" -lt "$BOOT_GRACE" ]; then
  log "host up only ${up}s (< ${BOOT_GRACE}s boot grace) — deferring reachability verdict"
  rm -f "$FIRST_FAILURE" 2>/dev/null || true
  exit 0
fi

t=$(now)
if [ ! -f "$FIRST_FAILURE" ]; then
  echo "$t" > "$FIRST_FAILURE" 2>/dev/null || true
  log "all targets ($TARGETS) unreachable — starting ${FAIL_WINDOW}s sustained-outage window"
  exit 0
fi

first=$(cat "$FIRST_FAILURE" 2>/dev/null || echo "$t")
elapsed=$(( t - first ))
if [ "$elapsed" -lt "$FAIL_WINDOW" ]; then
  log "all targets unreachable for ${elapsed}s (< ${FAIL_WINDOW}s window) — holding"
  exit 0
fi

# Guard 3: even once sustained, stay quiet during cooldown so the watchdog never
# escalates to the repair/reboot path while a recent reboot is still cooling down.
if in_cooldown; then
  log "unreachable for ${elapsed}s but within reboot cooldown — holding"
  exit 0
fi

log "all targets unreachable for ${elapsed}s (>= ${FAIL_WINDOW}s) — signalling watchdog for repair/reboot"
exit 1
