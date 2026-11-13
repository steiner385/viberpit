#!/bin/sh
# ViberPit D-23 (#3367): roll back deploy/sandbox/install.sh (plan §1.4 "Rollback").
# With the managed file gone the CLI falls back to /usr/bin/bwrap, the preflight's
# seccomp_helper check fails, and the sandbox toggle is disabled on this host.
set -eu

sudo rm /etc/claude-code/managed-settings.json
sudo apparmor_parser -R /etc/apparmor.d/viberpit-sbx-bwrap
sudo rm /etc/apparmor.d/viberpit-sbx-bwrap /usr/local/libexec/viberpit/bwrap /etc/apt/apt.conf.d/80viberpit-sbx-bwrap
