#!/usr/bin/env bash
# Establish the sshfs mounts rocky needs from dobby. Idempotent: an already-live
# mount is left alone. Run as tony (NOT root) so ssh uses tony's key and known_hosts.
#
# Install: sudo install -m755 deploy/bin-viberpit-mounts.sh /home/tony/bin-viberpit-mounts.sh
#
# rocky is a session host; dobby is canonical for all of these (spec decision 4).
# Without them the daemon starts but every session fails: no credentials, wrong skill
# set, and archiving errors with Permission denied on /mnt/hdd-storage.
#
# avd-golden holds the signed-in emulator goldens (see probe/avdgolden.sh). These
# carry live Google auth tokens, so the store stays 0700 and only ever rides this
# tailnet mount -- never shared/cloud storage. GOLDEN_DIR (/mnt/ssk-ssd/tony/
# avd-golden) is a symlink to this path on both hosts, so avdgolden.sh's default
# resolves here with no config.
#
# ---------------------------------------------------------------------------------
# WHY THE RETRY LOOP EXISTS (2026-08-26 outage; sibling of #765):
# At boot this unit races tailscaled. On 2026-08-26 the first sshfs attempt for
# /home/tony/.claude-accounts died with `read: Connection reset by peer` ~2 min into
# boot. A single-attempt script exits 1 -> the unit fails -> systemd CANCELS
# viberpit-daemon.service's start job (Requires=), and a cancelled start job never
# engages Restart=always. The daemon then sits dead until viberpit-daemon-ensure.timer
# notices, up to 60s later, with the whole session fleet down in the meantime.
#
# Retrying HERE means the transient case never reaches the unit boundary at all. The
# unit's own Restart=on-failure stays as the outer net for the non-transient case.
# Retries are bounded (attempts x delay) so a genuinely-down dobby still fails loudly
# and promptly rather than hanging past the unit's TimeoutStartSec.
set -uo pipefail

DOBBY="${VIBERPIT_DOBBY_HOST:-${CCSWITCH_DOBBY_HOST:-tony@100.102.11.121}}"
SSH_OPTS="reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,idmap=user"

# ---------------------------------------------------------------------------------
# TWO CACHE PROFILES (#1005) — a mount is either read-mostly or coherent, never both.
#
# CACHED_OPTS is the 10-minute attribute/entry cache #826/#851 added to keep the
# daemon's hot read paths off the tailnet. It is correct ONLY for a mount whose
# contents dobby does not rewrite underneath us.
#
# COHERENT_OPTS is for the account store. Claude Code rewrites `.claude.json`
# atomically (temp file + rename), so the inode and the size both change on every
# write, and dobby's live sessions rewrite it continuously. With a 600s attr/entry
# cache rocky keeps resolving the name to the OLD inode / OLD size, so a reader gets
# a truncated body -- which Claude Code diagnoses as corruption, backs up, and
# "repairs" by writing a DEFAULT config back through the mount onto dobby's disk.
# That cross-host write war reset the shared account config ~1,100x/day from
# 2026-09-01 (#1005). A file another host rewrites cannot be attribute-cached; the
# per-stat round trip is the price of coherence, and this mount's reads are cold
# per-spawn anyway (a few config files), not the hot per-message paths #826 tuned.
CACHED_OPTS="${SSH_OPTS},dir_cache=yes,dcache_timeout=600,attr_timeout=600,entry_timeout=600"
COHERENT_OPTS="${SSH_OPTS},dir_cache=no,dcache_timeout=0,attr_timeout=0,entry_timeout=0"

# Overridable so the tests can drive this against stubbed sshfs on temp paths.
DEFAULT_PATHS="/home/tony/.claude-accounts /home/tony/.claude /mnt/hdd-storage /home/tony/avd-golden"
PATHS="${VIBERPIT_MOUNT_PATHS:-${CCSWITCH_MOUNT_PATHS:-$DEFAULT_PATHS}}"
# Paths that get COHERENT_OPTS. Only the account store: /home/tony/.claude is the
# read-mostly share source (plugins/commands/settings), /mnt/hdd-storage and
# avd-golden are archives.
DEFAULT_COHERENT_PATHS="/home/tony/.claude-accounts"
COHERENT_PATHS="${VIBERPIT_MOUNT_COHERENT_PATHS-${CCSWITCH_MOUNT_COHERENT_PATHS-$DEFAULT_COHERENT_PATHS}}"
ATTEMPTS="${VIBERPIT_MOUNT_ATTEMPTS:-${CCSWITCH_MOUNT_ATTEMPTS:-5}}"
RETRY_DELAY="${VIBERPIT_MOUNT_RETRY_DELAY:-${CCSWITCH_MOUNT_RETRY_DELAY:-3}}"
# Opt-in: tear down and re-establish an ALREADY-mounted path so a cache-profile
# change lands without a reboot. Off by default -- unmounting a live mount breaks
# every session reading through it, so an operator has to ask for it explicitly
# (deploy/README.md § "Cache profiles: the account store must be mounted coherently").
FORCE_REMOUNT="${VIBERPIT_MOUNT_FORCE_REMOUNT:-${CCSWITCH_MOUNT_FORCE_REMOUNT:-0}}"

opts_for() {  # opts_for <path> — the cache profile this mount must use
  local p="$1" c
  for c in $COHERENT_PATHS; do
    [ "$p" = "$c" ] && { printf '%s' "$COHERENT_OPTS"; return; }
  done
  printf '%s' "$CACHED_OPTS"
}

rc=0
for p in $PATHS; do
  opts=$(opts_for "$p")
  # A live, readable mount is left strictly alone: sessions are reading through it.
  if mountpoint -q "$p" && timeout 5 ls "$p" >/dev/null 2>&1; then
    if [ "$FORCE_REMOUNT" != "1" ]; then
      echo "viberpit-mounts: ok (already mounted) $p"
      continue
    fi
    echo "viberpit-mounts: forced remount of $p (CCSWITCH_MOUNT_FORCE_REMOUNT=1)"
  fi
  mountpoint -q "$p" && fusermount -u "$p" 2>/dev/null   # clear a stale mount
  mkdir -p "$p" 2>/dev/null

  mounted=0
  for attempt in $(seq 1 "$ATTEMPTS"); do
    if sshfs -o "$opts" "${DOBBY}:${p}" "$p"; then
      echo "viberpit-mounts: mounted $p (attempt $attempt/$ATTEMPTS)"
      mounted=1
      break
    fi
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
      echo "viberpit-mounts: attempt $attempt/$ATTEMPTS failed for $p — retrying in ${RETRY_DELAY}s" >&2
      sleep "$RETRY_DELAY"
    fi
  done

  if [ "$mounted" -ne 1 ]; then
    echo "viberpit-mounts: FAILED $p after $ATTEMPTS attempt(s)" >&2
    rc=1
  fi
done
exit $rc
