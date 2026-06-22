#!/usr/bin/env bats
# VNC-attached gate in scripts/auto-deploy.sh: a VNC client attached defers the
# restart (no fast-forward) until detached or the max-defer cap. Uses the
# CCSWITCH_DEPLOY_VNC_ATTACHED seam (no real server) and CCSWITCH_DEPLOY_CI_OK=1
# to pass the CI gate.

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

# vnc attached => no fast-forward, marker written, exit 0
@test "vnc attached defers: no fast-forward, marker written" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=0 \
      CCSWITCH_DEPLOY_VNC_ATTACHED=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1800 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"defer"* ]] || [[ "$output" == *"vnc"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" != "$(git -C "$DEPLOY" rev-parse origin/master)" ]
  [ -f "$MARKER" ]
}

# vnc attached but past the cap => force (reaches the stop-after-sync hook = fast-forwarded)
@test "vnc attached past max-defer cap forces the deploy" {
  REMOTE="$(git -C "$DEPLOY" ls-remote "$SEED" refs/heads/master | cut -f1)"
  printf '%s 1\n' "$REMOTE" > "$MARKER"   # deferred since epoch=1 (ancient)
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=0 \
      CCSWITCH_DEPLOY_VNC_ATTACHED=1 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" CCSWITCH_DEPLOY_MAX_DEFER_SEC=1 \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"forcing"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

# neither busy nor vnc attached => proceeds to deploy
@test "neither busy nor vnc attached proceeds" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=0 \
      CCSWITCH_DEPLOY_VNC_ATTACHED=0 \
      CCSWITCH_DEPLOY_DEFER_FILE="$MARKER" \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
