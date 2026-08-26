#!/usr/bin/env bash
# Establish the sshfs mounts rocky needs from dobby. Idempotent: an already-live
# mount is left alone. Run as tony (NOT root) so ssh uses tony's key and known_hosts.
#
# Install: sudo install -m755 deploy/bin-ccswitch-mounts.sh /home/tony/bin-ccswitch-mounts.sh
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
# ccswitch-daemon.service's start job (Requires=), and a cancelled start job never
# engages Restart=always. The daemon then sits dead until ccswitch-daemon-ensure.timer
# notices, up to 60s later, with the whole session fleet down in the meantime.
#
# Retrying HERE means the transient case never reaches the unit boundary at all. The
# unit's own Restart=on-failure stays as the outer net for the non-transient case.
# Retries are bounded (attempts x delay) so a genuinely-down dobby still fails loudly
# and promptly rather than hanging past the unit's TimeoutStartSec.
set -uo pipefail

DOBBY="${CCSWITCH_DOBBY_HOST:-tony@100.102.11.121}"
OPTS="reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,idmap=user,dir_cache=yes,dcache_timeout=600,attr_timeout=600,entry_timeout=600"

# Overridable so the tests can drive this against stubbed sshfs on temp paths.
DEFAULT_PATHS="/home/tony/.claude-accounts /home/tony/.claude /mnt/hdd-storage /home/tony/avd-golden"
PATHS="${CCSWITCH_MOUNT_PATHS:-$DEFAULT_PATHS}"
ATTEMPTS="${CCSWITCH_MOUNT_ATTEMPTS:-5}"
RETRY_DELAY="${CCSWITCH_MOUNT_RETRY_DELAY:-3}"

rc=0
for p in $PATHS; do
  # A live, readable mount is left strictly alone: sessions are reading through it.
  if mountpoint -q "$p" && timeout 5 ls "$p" >/dev/null 2>&1; then
    echo "ccswitch-mounts: ok (already mounted) $p"
    continue
  fi
  mountpoint -q "$p" && fusermount -u "$p" 2>/dev/null   # clear a stale mount
  mkdir -p "$p" 2>/dev/null

  mounted=0
  for attempt in $(seq 1 "$ATTEMPTS"); do
    if sshfs -o "$OPTS" "${DOBBY}:${p}" "$p"; then
      echo "ccswitch-mounts: mounted $p (attempt $attempt/$ATTEMPTS)"
      mounted=1
      break
    fi
    if [ "$attempt" -lt "$ATTEMPTS" ]; then
      echo "ccswitch-mounts: attempt $attempt/$ATTEMPTS failed for $p — retrying in ${RETRY_DELAY}s" >&2
      sleep "$RETRY_DELAY"
    fi
  done

  if [ "$mounted" -ne 1 ]; then
    echo "ccswitch-mounts: FAILED $p after $ATTEMPTS attempt(s)" >&2
    rc=1
  fi
done
exit $rc
