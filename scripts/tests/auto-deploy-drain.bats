#!/usr/bin/env bats
# Drain handshake in scripts/auto-deploy.sh: in daemon (remote) mode, a daemon-affecting
# change drains the daemon (CCSWITCH_DEPLOY_DRAIN_RESULT seam) before fast-forwarding.

SCRIPT="${BATS_TEST_DIRNAME}/../auto-deploy.sh"

setup() {
  TMP="$(mktemp -d)"
  SEED="$TMP/seed"; DEPLOY="$TMP/deploy"
  git init -q "$SEED"
  git -C "$SEED" checkout -q -b master
  git -C "$SEED" config user.email t@example.com
  git -C "$SEED" config user.name tester
  mkdir -p "$SEED/scripts" "$SEED/web" "$SEED/server/sessions"
  cp "$SCRIPT" "$SEED/scripts/auto-deploy.sh"
  printf 'x\n' > "$SEED/server/sessions/orchestrator.py"   # non-edge → daemon_changed=1
  printf '/.venv/\n' > "$SEED/.gitignore"
  git -C "$SEED" add -A
  git -C "$SEED" commit -qm c1
  git clone -q "$SEED" "$DEPLOY"
  git -C "$DEPLOY" config user.email t@example.com
  git -C "$DEPLOY" config user.name tester
  printf 'y\n' > "$SEED/server/sessions/orchestrator.py"
  git -C "$SEED" commit -qam c2
}

teardown() { rm -rf "$TMP"; }

@test "remote+daemon change: drain ready → fast-forwards" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_REMOTE_ACTIVE=1 \
      CCSWITCH_DEPLOY_DRAIN_RESULT=ready \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"drained"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

@test "remote+daemon change: drain cap → forces with a notice" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_REMOTE_ACTIVE=1 \
      CCSWITCH_DEPLOY_DRAIN_RESULT=cap \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"cap"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

@test "remote+daemon change: drain unavailable → proceeds fail-open" {
  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_REMOTE_ACTIVE=1 \
      CCSWITCH_DEPLOY_DRAIN_RESULT=unavailable \
      CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
  [ "$status" -eq 0 ]
  [[ "$output" == *"unavailable"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
