#!/usr/bin/env bats
# Idle gate in scripts/auto-deploy.sh: a busy server defers the restart (no
# fast-forward) until idle or the max-defer cap. Uses the CCSWITCH_DEPLOY_BUSY
# seam (no real server) and CCSWITCH_DEPLOY_CI_OK=1 to pass the CI gate.

SCRIPT="${BATS_TEST_DIRNAME}/../auto-deploy.sh"

setup() {
  TMP="$(mktemp -d)"
  SEED="$TMP/seed"; DEPLOY="$TMP/deploy"
  git init -q "$SEED"
  git -C "$SEED" checkout -q -b master
  git -C "$SEED" config user.email t@example.com
  git -C "$SEED" config user.name tester
  mkdir -p "$SEED/scripts" "$SEED/web"
  cp "$SCRIPT" "$SEED/scripts/auto-deploy.sh"
  printf 'v1\n' > "$SEED/tracked.txt"
  printf '/.venv/\n' > "$SEED/.gitignore"
  git -C "$SEED" add -A
  git -C "$SEED" commit -qm c1
  git clone -q "$SEED" "$DEPLOY"
  git -C "$DEPLOY" config user.email t@example.com
  git -C "$DEPLOY" config user.name tester
  printf 'v2\n' > "$SEED/tracked.txt"
  git -C "$SEED" commit -qam c2
  MARKER="$TMP/defer-state"
}
teardown() { rm -rf "$TMP"; }

# busy => no fast-forward, marker written, exit 0
@test "busy server defers: no fast-forward, marker written" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1800 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"busy"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" != "$(git -C "$DEPLOY" rev-parse origin/master)" ]
  [ -f "$MARKER" ]
}

# busy but past the cap => force (reaches the stop-after-sync hook = fast-forwarded)
@test "busy past max-defer cap forces the deploy" {
  REMOTE="$(git -C "$DEPLOY" ls-remote "$SEED" refs/heads/master | cut -f1)"
  printf '%s 1\n' "$REMOTE" > "$MARKER"   # deferred since epoch=1 (ancient)
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1 \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"forcing"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

# A new commit landing mid-streak must NOT reset the force-cap clock. The cap is
# anchored to the start of a continuous busy streak, not the current commit —
# otherwise a busy server during a rapid-merge burst defers forever (each new
# commit restarts the 30-min clock = deploy starvation).
@test "new commit during a busy streak does not reset the force-cap" {
  # Marker recorded for a STALE sha, deferred since epoch=1 (ancient streak).
  # origin/master has since advanced to c2 (a different sha).
  printf 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef 1\n' > "$MARKER"
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1 \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"forcing"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

# idle => proceeds (reaches the stop-after-sync hook = fast-forwarded)
@test "idle server proceeds to deploy" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=0 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
