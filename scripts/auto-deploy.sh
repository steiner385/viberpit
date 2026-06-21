#!/usr/bin/env bash
# ccswitch auto-deploy: poll origin/master and, when behind, fast-forward,
# rebuild the frontend, run the test gate, then restart the systemd service.
#
# Idempotent — a no-op when already up to date. Invoked on a schedule by
# ccswitch-deploy.timer. A failing build or test suite aborts BEFORE the
# restart, so a broken master can never take down the running service.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

git fetch --quiet origin master
LOCAL="$(git rev-parse @)"
REMOTE="$(git rev-parse origin/master)"
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0   # already current — nothing to do
fi

echo "[deploy] ${LOCAL:0:9} -> ${REMOTE:0:9}"
git pull --ff-only origin master

# Frontend gate — regenerates web/dist (served by the backend via StaticFiles).
# A failed build exits here, leaving the running service untouched.
( cd web && npm run build )

# Backend gate — keep the old version live if the suite is red.
# CCSWITCH_DEV=1 + cleared allowlist makes the Tailscale-auth tests hermetic.
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 \
  "$REPO/.venv/bin/python" -m pytest server/tests -q

# All gates green — swap in the new code.
sudo systemctl restart ccswitch.service
echo "[deploy] live at ${REMOTE:0:9}"
