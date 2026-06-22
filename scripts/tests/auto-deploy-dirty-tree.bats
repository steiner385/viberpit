#!/usr/bin/env bats
# Tests the working-tree guard in scripts/auto-deploy.sh: a dirty deploy checkout
# must NOT wedge the fast-forward. Exercises the real script up to its
# CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC hook (no npm/pytest/systemctl side effects).

SCRIPT="${BATS_TEST_DIRNAME}/../auto-deploy.sh"

setup() {
  TMP="$(mktemp -d)"
  SEED="$TMP/seed"
  DEPLOY="$TMP/deploy"

  # Seed repo = the "origin". Force a `master` branch regardless of the host's
  # init.defaultBranch, and carry a copy of the script under test at scripts/.
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

  # Deploy checkout = a clone that starts one commit behind origin/master.
  git clone -q "$SEED" "$DEPLOY"
  git -C "$DEPLOY" config user.email t@example.com
  git -C "$DEPLOY" config user.name tester
  printf 'v2\n' > "$SEED/tracked.txt"
  git -C "$SEED" commit -qam c2

  # An untracked file the guard must preserve (stands in for the real .venv).
  mkdir -p "$DEPLOY/.venv"
  printf 'keep-me\n' > "$DEPLOY/.venv/marker"
}

teardown() { rm -rf "$TMP"; }

run_deploy() {
  run env CCSWITCH_DEPLOY_TEST_STOP_AFTER_SYNC=1 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"
}

@test "dirty tracked tree is discarded and the fast-forward still lands" {
  printf 'LOCAL EDIT that would block git pull\n' > "$DEPLOY/tracked.txt"

  run_deploy

  [ "$status" -eq 0 ]
  [[ "$output" == *"discarding unexpected local changes"* ]]
  # Fast-forwarded to c2 (origin/master), proving the pull was not wedged.
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
  [ "$(cat "$DEPLOY/tracked.txt")" = "v2" ]
}

@test "a staged change also clears (guard catches the index, not just the worktree)" {
  printf 'staged edit\n' > "$DEPLOY/tracked.txt"
  git -C "$DEPLOY" add tracked.txt

  run_deploy

  [ "$status" -eq 0 ]
  [[ "$output" == *"discarding unexpected local changes"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}

@test "untracked files (the .venv) survive the guard" {
  printf 'LOCAL EDIT\n' > "$DEPLOY/tracked.txt"

  run_deploy

  [ "$status" -eq 0 ]
  [ -f "$DEPLOY/.venv/marker" ]
  [ "$(cat "$DEPLOY/.venv/marker")" = "keep-me" ]
}

@test "a clean tree fast-forwards without triggering the guard" {
  run_deploy

  [ "$status" -eq 0 ]
  [[ "$output" != *"discarding unexpected local changes"* ]]
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" = "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
