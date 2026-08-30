#!/bin/bash
# SSH liveness test for the watchdog daemon (/etc/watchdog.d).
#
# Verifies that sshd is not merely listening but actually speaking the SSH
# protocol: we open a TCP connection to the local sshd port, read the protocol
# version-exchange greeting, and require it to start with "SSH-".
#
# No authentication is attempted, so this generates no auth failures.
#
# SAFETY: a non-zero exit here eventually reboots the physical host, so this
# script is deliberately fail-safe. It exits 0 whenever the result is
# inconclusive (missing tooling, unexpected output, transient error) and only
# exits 1 on a clear, repeated, positively-verified determination that sshd is
# not answering. There is intentionally no `set -e`.

# Every knob is overridable by environment purely so the behavioural spec
# (server/tests/test_watchdog_ssh.py) can drive this against stub listeners on
# ephemeral ports. Production uses the defaults.
HOST="${WDS_HOST:-127.0.0.1}"
PORT="${WDS_PORT:-22}"
ATTEMPTS="${WDS_ATTEMPTS:-3}"                  # probes that must ALL fail before we fail
CONNECT_TIMEOUT="${WDS_CONNECT_TIMEOUT:-4}"    # hard wall-clock cap per probe (seconds)
READ_TIMEOUT="${WDS_READ_TIMEOUT:-3}"          # cap on waiting for the greeting (seconds)
RETRY_SLEEP="${WDS_RETRY_SLEEP:-1}"
PROBE_PORT="${WDS_PROBE_PORT:-1}"              # a port expected CLOSED, for the self-test
# Worst case runtime ~= 3*4 + 2*1 + 4 = 18s, well under watchdog test-timeout=30.

# Read the first line sshd sends. Prints the banner on stdout.
# Exit codes: 0 got a line, 97 could not connect, 98 connected but no greeting.
probe() {
    timeout -s KILL "$CONNECT_TIMEOUT" bash -c '
        exec 3<>/dev/tcp/'"$HOST"'/'"$PORT"' || exit 97
        IFS= read -r -t '"$READ_TIMEOUT"' line <&3 || exit 98
        exec 3<&- 3>&-
        printf "%s" "$line"
    ' 2>/dev/null
}

# Confirm this bash actually supports /dev/tcp before we trust a connect
# failure. A bash built with --disable-net-redirections reports
# "No such file or directory"; a working one reports "Connection refused"
# against a closed port. Anything else is treated as inconclusive.
dev_tcp_supported() {
    local err
    err=$(timeout -s KILL "$CONNECT_TIMEOUT" bash -c \
              'exec 3<>/dev/tcp/'"$HOST"'/'"$PROBE_PORT" 2>&1)
    case "$err" in
        *"Connection refused"*) return 0 ;;
        *)                      return 1 ;;
    esac
}

banner=""
for ((i = 1; i <= ATTEMPTS; i++)); do
    banner=$(probe)
    case "$banner" in
        SSH-*) exit 0 ;;   # sshd spoke SSH: healthy, log nothing.
    esac
    [ "$i" -lt "$ATTEMPTS" ] && sleep "$RETRY_SLEEP"
done

# Every attempt failed. Only fail the watchdog if our own probe mechanism is
# demonstrably working; otherwise this is a broken test, not a broken sshd.
if ! dev_tcp_supported; then
    logger -t watchdog-ssh "SSH test inconclusive (bash /dev/tcp unavailable) - passing"
    exit 0
fi

logger -t watchdog-ssh "SSH connectivity test FAILED: no SSH- greeting from ${HOST}:${PORT} after ${ATTEMPTS} attempts"
exit 1
