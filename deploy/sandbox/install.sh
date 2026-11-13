#!/bin/sh
# ViberPit D-23 (#3367): the host half of enabling the Claude session sandbox.
# Plan: docs/superpowers/plans/2026-11-13-sandbox-enablement-3367.md §1.4.
#
# Installs a root-owned bwrap copy with its own AppArmor profile, points the CLI at it
# (managed settings sandbox.bwrapPath) and keeps it in step with /usr/bin/bwrap (apt
# hook). Stock /usr/bin/bwrap and its profile are untouched. Nothing here touches a
# live session or turns the sandbox on (VIBERPIT_SANDBOX_ENABLED stays as it is).
#
# Usage: deploy/sandbox/install.sh [<viberpit-user>]
#   <viberpit-user> is the unprivileged user the ViberPit daemon runs as; default
#   $SUDO_USER. Only that user's primary group may execute the copy (mode 0750).
# Run on dobby first, then rocky. Everything is validated before anything changes.
# The profile is loaded BEFORE the copy exists, so the copy never runs unconfined.
# Verify and roll back: deploy/README.md, "Claude session sandbox".
set -eu
cd "$(dirname "$0")"

DEST_DIR=/usr/libexec/viberpit
DEST=$DEST_DIR/bwrap

refuse() {
    echo "install.sh: $*" >&2
    exit 1
}

user=${1:-${SUDO_USER:-}}
[ -n "$user" ] || refuse "name the ViberPit user: install.sh <viberpit-user> (or run it under sudo)"
uid=$(id -u "$user" 2>/dev/null) || refuse "no such user: $user"
[ "$uid" != 0 ] || refuse "$user is root; name the unprivileged user the ViberPit daemon runs as"
group=$(id -gn "$user") || refuse "cannot read the primary group of $user"

# The copy runs bwrap without Ubuntu's userns restriction, so whoever can write any
# directory on its path could swap it (rename the tree away, plant their own). Every
# existing directory from $DEST_DIR up to / must be root-owned, not a symlink, and not
# writable by group or others.
d=$DEST_DIR
while :; do
    if [ -L "$d" ]; then
        refuse "$d is a symlink; the bwrap copy needs a root-owned directory chain"
    elif [ -e "$d" ]; then
        [ -d "$d" ] || refuse "$d is not a directory"
        owner_mode=$(stat -c '%u %a' "$d")
        owner=${owner_mode% *}
        mode=${owner_mode#* }
        [ "$owner" = 0 ] || refuse "$d is not owned by root (uid $owner); the bwrap copy needs a root-owned directory chain"
        [ $((0$mode & 022)) -eq 0 ] || refuse "$d is writable by group or others (mode $mode); the bwrap copy needs a directory chain only root can write"
    fi
    [ "$d" = / ] && break
    d=$(dirname "$d")
done

sudo install -d -o root -g root -m 0755 "$DEST_DIR"
sudo install -o root -g root -m 0644 viberpit-sbx-bwrap /etc/apparmor.d/viberpit-sbx-bwrap
sudo apparmor_parser -Q -K -T /etc/apparmor.d/viberpit-sbx-bwrap     # parse again in place
sudo apparmor_parser -r -W /etc/apparmor.d/viberpit-sbx-bwrap        # load and cache; persists via apparmor.service
sudo install -o root -g "$group" -m 0750 /usr/bin/bwrap "$DEST"
sudo install -o root -g root -m 0644 80viberpit-sbx-bwrap /etc/apt/apt.conf.d/80viberpit-sbx-bwrap
sudo install -d -o root -g root -m 0755 /etc/claude-code
sudo install -o root -g root -m 0644 managed-settings.json /etc/claude-code/managed-settings.json
