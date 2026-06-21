#!/usr/bin/env bash
# ccswitch auto-deploy: poll origin/master and, when behind, fast-forward,
# rebuild the frontend, run the test gate, then restart the systemd service.
#
# Idempotent — a no-op when already up to date. Invoked on a schedule by
# ccswitch-deploy.timer. Any failing gate (frontend build, backend tests, or
# the post-restart health check) rolls the working tree back to the last-good
# commit, so a broken master can neither take down the running service NOR
# wedge future deploys: because HEAD is restored to the previous commit, the
# next timer tick again sees HEAD != origin/master and re-attempts the upgrade.
# The deploy therefore self-heals the moment master goes green again.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

git fetch --quiet origin master
LOCAL="$(git rev-parse @)"
REMOTE="$(git rev-parse origin/master)"
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0   # already current — nothing to do
fi

# Restore the working tree to the last-good commit and rebuild it. Called when
# any gate fails. Crucially this resets HEAD back to $LOCAL: without it, a gate
# that fails *after* the fast-forward would leave HEAD parked at the broken
# master, so every later tick would see HEAD == origin/master and exit as a
# no-op — a permanent wedge that never retries the (now red) build.
rollback_to_local() {
  echo "[deploy] gate failed — ROLLING BACK ${REMOTE:0:9} -> ${LOCAL:0:9}" >&2
  git reset --hard "$LOCAL"
  ( cd web && npm run build ) || echo "[deploy] WARNING: rollback build failed; web/dist may be stale" >&2
}

echo "[deploy] ${LOCAL:0:9} -> ${REMOTE:0:9}"
git pull --ff-only origin master

# Frontend gate — regenerates web/dist (served by the backend via StaticFiles).
# `if !` keeps the failure out of `set -e` so the rollback below actually runs.
# The service has NOT been restarted yet, so the old backend stays live as-is.
if ! ( cd web && npm run build ); then
  rollback_to_local
  exit 1
fi

# Backend gate — keep the old version live if the suite is red.
# CCSWITCH_DEV=1 + cleared allowlist makes the Tailscale-auth tests hermetic.
if ! env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 \
     "$REPO/.venv/bin/python" -m pytest server/tests -q; then
  rollback_to_local
  exit 1
fi

# All gates green — swap in the new code.
sudo systemctl restart ccswitch.service

# Post-restart health gate: the new process must answer /healthz (schema OK)
# within ~30s, else roll back to the previous commit. /healthz is unauthenticated
# and runs a schema-touching query, so a schema-drift boot fails this check.
HEALTH_URL="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
healthy=0
for _ in $(seq 1 15); do
  if curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  echo "[deploy] /healthz never came up after restart" >&2
  rollback_to_local
  # The new (broken) backend was already swapped in above — restart the
  # rolled-back code so the last-good version is the one actually serving.
  sudo systemctl restart ccswitch.service
  echo "[deploy] rolled back to ${LOCAL:0:9}" >&2
  exit 1
fi

echo "[deploy] live + healthy at ${REMOTE:0:9}"
