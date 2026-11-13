#!/bin/sh
# ViberPit D-23 (#3367): roll back deploy/sandbox/install.sh (plan §1.4 "Rollback").
# With the managed file gone the CLI falls back to /usr/bin/bwrap, the preflight's
# seccomp_helper check fails, and the sandbox toggle is disabled on this host.
#
# Order-safe: every step runs even if an earlier one fails (an already-unloaded
# profile, a half-finished install). The managed file goes first so the CLI stops
# naming the copy; the copy and the apt hook go before the profile is unloaded.
status=0
step() {
    "$@" || { echo "uninstall.sh: failed (continuing): $*" >&2; status=1; }
}

step sudo rm -f /etc/claude-code/managed-settings.json
step sudo rm -f /usr/libexec/viberpit/bwrap /etc/apt/apt.conf.d/80viberpit-sbx-bwrap
step sudo rmdir --ignore-fail-on-non-empty /usr/libexec/viberpit
sudo apparmor_parser -R /etc/apparmor.d/viberpit-sbx-bwrap || true   # not loaded is fine
step sudo rm -f /etc/apparmor.d/viberpit-sbx-bwrap
exit $status
