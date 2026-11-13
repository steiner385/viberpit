#!/bin/sh
# ViberPit D-23 (#3367): the host half of enabling the Claude session sandbox.
# Plan: docs/superpowers/plans/2026-11-13-sandbox-enablement-3367.md §1.4.
#
# Installs a root-owned bwrap copy with its own AppArmor profile, points the CLI at it
# (managed settings sandbox.bwrapPath) and keeps it in step with /usr/bin/bwrap (apt
# hook). Stock /usr/bin/bwrap and its profile are untouched. Nothing here touches a
# live session or turns the sandbox on (VIBERPIT_SANDBOX_ENABLED stays as it is).
#
# Run from anywhere, as the ViberPit user with sudo: dobby first, then rocky. The
# profile is loaded BEFORE the copy exists, so the copy never runs unconfined.
# Verify and roll back: deploy/README.md, "Claude session sandbox".
set -eu
cd "$(dirname "$0")"

sudo install -d -o root -g root -m 0755 /usr/local/libexec/viberpit
sudo install -o root -g root -m 0644 viberpit-sbx-bwrap /etc/apparmor.d/viberpit-sbx-bwrap
sudo apparmor_parser -Q -K -T /etc/apparmor.d/viberpit-sbx-bwrap     # parse again in place
sudo apparmor_parser -r -W /etc/apparmor.d/viberpit-sbx-bwrap        # load and cache; persists via apparmor.service
sudo install -o root -g tony -m 0750 /usr/bin/bwrap /usr/local/libexec/viberpit/bwrap
sudo install -o root -g root -m 0644 80viberpit-sbx-bwrap /etc/apt/apt.conf.d/80viberpit-sbx-bwrap
sudo install -d -o root -g root -m 0755 /etc/claude-code
sudo install -o root -g root -m 0644 managed-settings.json /etc/claude-code/managed-settings.json
