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

# Post-restart health gate: the new process must answer /healthz (schema OK)
# within ~30s, else roll back to the previous commit. /healthz is unauthenticated
# and runs a schema-touching query, so a schema-drift boot fails this check.
#
# We assert the JSON body ("status":"ok"), NOT just a 200 — the SPA static mount
# serves index.html (HTTP 200) for ANY unmatched path, so a process running OLD
# code without the /healthz route (or a stale server squatting the port) would
# pass a bare status-code check. Matching the body proves THIS code is answering.
HEALTH_URL="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
healthy=0
for _ in $(seq 1 15); do
  if curl -fsS -m 3 "$HEALTH_URL" 2>/dev/null | grep -q '"status":"ok"'; then healthy=1; break; fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  echo "[deploy] /healthz never came up — ROLLING BACK ${REMOTE:0:9} -> ${LOCAL:0:9}" >&2
  git reset --hard "$LOCAL"
  ( cd web && npm run build ) || echo "[deploy] WARNING: rollback build failed; restarting old backend anyway" >&2
  sudo systemctl restart ccswitch.service
  echo "[deploy] rolled back to ${LOCAL:0:9}" >&2
  exit 1
fi

echo "[deploy] live + healthy at ${REMOTE:0:9}"
