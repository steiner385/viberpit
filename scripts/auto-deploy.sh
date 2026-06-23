#!/usr/bin/env bash
# ccswitch auto-deploy: poll origin/master and, when behind, deploy it.
#
# Testing now lives in GitHub Actions, NOT on this host: instead of re-running the
# suite locally, this script GATES on the commit's GitHub CI conclusion and only
# deploys a commit whose CI is green. It then syncs deps (if lockfiles changed),
# rebuilds the frontend bundle (web/dist, served by the backend), restarts the
# service, and verifies the new process answers /healthz with the EXPECTED SHA.
#
# Idempotent — a no-op when already up to date. Invoked on a schedule by
# ccswitch-deploy.timer. A non-green / unverifiable CI is a skip-and-retry (the
# old version stays live); any gate failure AFTER the fast-forward rolls the tree
# back to the last-good commit, so a broken master can neither take down the
# running service NOR wedge future deploys — the next tick re-attempts once master
# goes green. The pre-pull working-tree guard discards stray tracked edits so a
# dirty checkout can't wedge the fast-forward itself.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Transient busy-defer state file. Resolve it inside git's real dir via
# --absolute-git-dir so it lands in a writable directory even when $REPO is a git
# WORKTREE — there ".git" is a FILE (a gitdir pointer), so the old
# "$REPO/.git/ccswitch-deploy-defer" path treats a regular file as a directory and
# the write fails with "Not a directory", which wedged the deploy timer (issue #95).
DEFER_FILE="${CCSWITCH_DEPLOY_DEFER_FILE:-$(git rev-parse --absolute-git-dir)/ccswitch-deploy-defer}"

git fetch --quiet origin master
LOCAL="$(git rev-parse @)"
REMOTE="$(git rev-parse origin/master)"
if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0   # already current — nothing to do
fi

# CI verdict for a commit: echoes green | pending | red | unavailable.
# Testing is GitHub's job now; we only deploy a commit GitHub already proved green.
# Test seam: CCSWITCH_DEPLOY_CI_OK forces the verdict (1/green, or pending/red) so
# the gate can be exercised without calling gh.
ci_verdict() {
  local sha="$1"
  if [ -n "${CCSWITCH_DEPLOY_CI_OK:-}" ]; then
    case "$CCSWITCH_DEPLOY_CI_OK" in
      1|green) echo green ;;
      *) echo "$CCSWITCH_DEPLOY_CI_OK" ;;
    esac
    return 0
  fi
  gh api "repos/{owner}/{repo}/commits/$sha/check-runs" --jq '
    if (.check_runs | length) == 0 then "pending"
    elif ([.check_runs[] | select(.status != "completed")] | length) > 0 then "pending"
    elif ([.check_runs[] | select((.conclusion // "") as $c
            | ($c == "success" or $c == "neutral" or $c == "skipped") | not)] | length) > 0 then "red"
    else "green" end' 2>/dev/null || echo unavailable
}

# Gate on GitHub CI. Fail CLOSED: anything but a definitive green keeps the old
# version live and retries next tick (CI catches up, or a human fixes red master).
verdict="$(ci_verdict "$REMOTE")"
case "$verdict" in
  green) ;;
  red)        echo "[deploy] CI is RED for ${REMOTE:0:9} — not deploying; waiting for a green commit" >&2; exit 0 ;;
  pending)    echo "[deploy] CI still running for ${REMOTE:0:9} — will retry next tick"; exit 0 ;;
  *)          echo "[deploy] WARNING: cannot determine CI status for ${REMOTE:0:9} (gh/network?) — skipping this tick" >&2; exit 0 ;;
esac

# Classify the pending change: does it touch code the DAEMON runs (orchestrator/IPC/session
# logic)? If only edge code changed AND the edge is running in remote mode, restarting the edge
# does NOT disrupt sessions (the daemon holds them), so we can skip the idle gate entirely.
# Fail SAFE: if the diff can't be computed, assume daemon_changed (gate + restart both).
if daemon_files="$(git diff --name-only "$LOCAL" "$REMOTE" 2>/dev/null)"; then
  if printf '%s\n' "$daemon_files" | grep -qE '^(server/ipc/|server/sessions/|src/ccswitch/)'; then
    daemon_changed=1
  else
    daemon_changed=0
  fi
else
  daemon_changed=1   # fail-safe: cannot classify → treat as a daemon change
fi

# Is the running edge in remote mode? Its /healthz reports "daemon":"ok" only then.
remote_active=0
if [ -z "${CCSWITCH_DEPLOY_REMOTE_ACTIVE:-}" ]; then
  _hz="$(curl -fsS -m 3 "http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz" 2>/dev/null || true)"
  grep -q '"daemon":"ok"' <<<"$_hz" && remote_active=1
else
  remote_active="$CCSWITCH_DEPLOY_REMOTE_ACTIVE"   # test seam
fi

# Idle gate. A restart kills every live session, so defer while the running server
# is mid-turn (status in_progress/working/waiting_input) or a VNC client is attached.
# Skip-and-retry until idle/detached, capped at CCSWITCH_DEPLOY_MAX_DEFER_SEC so a
# perpetually-busy/attached session can't block deploys forever. Placed BEFORE the
# pull so the "HEAD == origin/master ⇒ deployed" invariant holds (we never pull then
# defer).
# Test seam: CCSWITCH_DEPLOY_BUSY (1/0) forces the verdict without curling /healthz.
busy_verdict() {
  if [ -n "${CCSWITCH_DEPLOY_BUSY:-}" ]; then
    [ "$CCSWITCH_DEPLOY_BUSY" = "1" ] && echo busy || echo idle
    return 0
  fi
  local url="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
  local body; body="$(curl -fsS -m 3 "$url" 2>/dev/null || true)"
  # Unreachable / no busy field => treat as idle (recover a down server; the CI gate
  # already enforced correctness, and the post-restart health gate guards the rest).
  if grep -q '"busy":true' <<<"$body"; then echo busy; else echo idle; fi
}

# VNC-client gate. Defers the deploy while a VNC client is attached to the running
# Xvfb framebuffer, so a human watching a session is not disrupted mid-view.
# Test seam: CCSWITCH_DEPLOY_VNC_ATTACHED (1/0) forces the verdict without curling.
# Reads /healthz (auth-exempt) rather than /api/x11/metrics (identity-gated) so the
# deploy curl — which has no Tailscale identity — can reach it without a 401.
# Degrades gracefully: unreachable / missing field => none (same as busy_verdict).
vnc_verdict() {
  if [ -n "${CCSWITCH_DEPLOY_VNC_ATTACHED:-}" ]; then
    [ "$CCSWITCH_DEPLOY_VNC_ATTACHED" = "1" ] && echo attached || echo none
    return 0
  fi
  local url="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
  local body; body="$(curl -fsS -m 3 "$url" 2>/dev/null || true)"
  # Treat attached when vncAttached is true; anything else (unreachable, missing
  # field, false) => none.
  if grep -q '"vncAttached":true' <<<"$body"; then
    echo attached
  else
    echo none
  fi
}

# Only idle-gate when a session-disrupting restart will happen: a daemon change, or an
# in-process edge (where any restart kills sessions). A remote edge + edge-only change is
# non-disruptive → never defer.
if [ "$daemon_changed" = "1" ] || [ "$remote_active" != "1" ]; then
  # Compute defer_reason: "busy", "vnc-attached", or empty (proceed).
  defer_reason=""
  if [ "$(busy_verdict)" = "busy" ]; then
    defer_reason="busy"
  elif [ "$(vnc_verdict)" = "attached" ]; then
    defer_reason="vnc-attached"
  fi

  if [ -n "$defer_reason" ]; then
    MAX_DEFER="${CCSWITCH_DEPLOY_MAX_DEFER_SEC:-1800}"
    now="$(date +%s)"
    msha=""; mts=0
    if [ -f "$DEFER_FILE" ]; then read -r msha mts < "$DEFER_FILE" || true; fi
    if [ "$msha" != "$REMOTE" ]; then
      printf '%s %s\n' "$REMOTE" "$now" > "$DEFER_FILE"
      echo "[deploy] ${defer_reason} — deferring ${REMOTE:0:9} (will retry)"; exit 0
    elif [ "$(( now - mts ))" -gt "$MAX_DEFER" ]; then
      echo "[deploy] ${defer_reason} for $(( now - mts ))s (> ${MAX_DEFER}s) — forcing deploy of ${REMOTE:0:9}" >&2
    else
      echo "[deploy] ${defer_reason} — deferring ${REMOTE:0:9} ($(( now - mts ))s so far)"; exit 0
    fi
  fi
else
  echo "[deploy] edge-only change, remote mode — non-disruptive restart, skipping idle gate"
fi

# Restore the working tree to the last-good commit and rebuild it. Called when any
# gate fails AFTER the fast-forward. Resetting HEAD back to $LOCAL is what makes the
# next tick see HEAD != origin/master and re-attempt, instead of treating the broken
# master as "already deployed" and wedging.
rollback_to_local() {
  echo "[deploy] gate failed — ROLLING BACK ${REMOTE:0:9} -> ${LOCAL:0:9}" >&2
  git reset --hard "$LOCAL"
  ( cd web && npm run build ) || echo "[deploy] WARNING: rollback build failed; web/dist may be stale" >&2
}

echo "[deploy] ${LOCAL:0:9} -> ${REMOTE:0:9} (CI green)"

# Working-tree guard. This deploy checkout is a pristine mirror of master, so any
# tracked-file modification is unintended drift. Left in place it makes
# `git pull --ff-only` abort on EVERY tick — a silent, permanent wedge the
# gate-failure rollback does NOT cover (that only runs after a successful
# fast-forward). Discard tracked changes; untracked files (e.g. .venv) are kept.
if ! git diff --quiet HEAD --; then
  echo "[deploy] WARNING: discarding unexpected local changes before pull:" >&2
  git status --short --untracked-files=no >&2
  git reset --hard HEAD
fi

# Fast-forward. `if !` keeps a pull failure out of `set -e` so it self-heals via
# rollback instead of crashing mid-state.
if ! git pull --ff-only origin master; then
  echo "[deploy] fast-forward pull failed unexpectedly" >&2
  rollback_to_local
  exit 1
fi

# Test hook: stop after the working tree is synced + fast-forwarded, so the guard
# and pull can be exercised in isolation without dep-sync/build/restart side
# effects. Unset in production.
if [ -n "${CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC:-}" ]; then
  echo "[deploy] TEST: synced + fast-forwarded to $(git rev-parse --short HEAD), stopping"
  exit 0
fi

# Dependency sync — only when a lockfile actually changed between old and new, so
# normal deploys stay fast. A new dependency would otherwise crash the new process
# on boot (ImportError) → health gate → endless rollback loop. Mirrors CI's install
# (pip install -e ".[server]" / npm ci) against this checkout's venv + node_modules.
if [ -n "$(git diff --name-only "$LOCAL" "$REMOTE" -- pyproject.toml uv.lock)" ]; then
  echo "[deploy] python deps changed — reinstalling into .venv"
  if ! "$REPO/.venv/bin/pip" install -e ".[server]" --quiet; then rollback_to_local; exit 1; fi
fi
if [ -n "$(git diff --name-only "$LOCAL" "$REMOTE" -- web/package-lock.json)" ]; then
  echo "[deploy] web deps changed — npm ci"
  if ! ( cd web && npm ci ); then rollback_to_local; exit 1; fi
fi

# Frontend build — regenerates web/dist (served via StaticFiles). CI already proved
# it compiles; this just produces the bundle to serve. Rollback on the off chance
# the host build differs.
if ! ( cd web && npm run build ); then
  rollback_to_local
  exit 1
fi

# All gates green — swap in the new code.
sudo systemctl restart ccswitch.service

# Post-restart health gate. The new process must answer /healthz with status ok
# AND the SHA we just deployed, within ~30s, else roll back.
#
# Asserting the SHA (not just status:ok) closes two gaps a bare check misses: the
# SPA static mount returns 200 for ANY path (so a process on OLD code lacking the
# route, or a stale server squatting the port, would still 200), and it proves
# THIS commit — not merely "some healthy ccswitch" — is the one now serving.
EXPECTED_SHA="$(git rev-parse --short "$REMOTE")"
HEALTH_URL="http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz"
healthy=0
for _ in $(seq 1 15); do
  body="$(curl -fsS -m 3 "$HEALTH_URL" 2>/dev/null || true)"
  if grep -q '"status":"ok"' <<<"$body" && grep -q "\"sha\":\"${EXPECTED_SHA}\"" <<<"$body"; then
    healthy=1; break
  fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  echo "[deploy] /healthz did not report status:ok + sha=${EXPECTED_SHA} after restart" >&2
  rollback_to_local
  # The new (broken) backend was already swapped in above — restart the rolled-back
  # code so the last-good version is the one actually serving.
  sudo systemctl restart ccswitch.service
  echo "[deploy] rolled back to ${LOCAL:0:9}" >&2
  exit 1
fi

echo "[deploy] live + healthy at ${REMOTE:0:9}"

# Restart the session daemon only when daemon code changed AND the unit is enabled on
# this host. Guarded so a host (or CI) without the daemon unit is a no-op. Edge-only
# changes leave the daemon running — it holds all live sessions in remote mode.
if [ "$daemon_changed" = "1" ] && systemctl is-enabled ccswitch-daemon.service >/dev/null 2>&1; then
  echo "[deploy] daemon code changed — restarting ccswitch-daemon.service"
  sudo systemctl restart ccswitch-daemon.service || \
    echo "[deploy] WARNING: daemon restart failed (edge already live)" >&2
fi

rm -f "$DEFER_FILE"
