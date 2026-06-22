#!/usr/bin/env bats
# Regression for issue #95: the live server runs from a git WORKTREE, where ".git"
# is a FILE (a gitdir pointer), not a directory. The busy-defer state file must be
# resolved to a real writable dir (via `git rev-parse --absolute-git-dir`), not
# "$REPO/.git/ccswitch-deploy-defer" — that path treats a file as a directory, the
# write fails with "Not a directory", and the deploy timer wedges on every tick.
#
# Unlike auto-deploy-idle-gate.bats, this test does NOT set CCSWITCH_DEPLOY_DEFER_FILE,
# so it exercises the DEFAULT path — exactly what broke in production.

SCRIPT="${BATS_TEST_DIRNAME}/../auto-deploy.sh"

setup() {
  TMP="$(mktemp -d)"
  SEED="$TMP/seed"; MAIN="$TMP/main"; DEPLOY="$TMP/deploy"
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
  git clone -q "$SEED" "$MAIN"
  git -C "$MAIN" config user.email t@example.com
  git -C "$MAIN" config user.name tester
  # DEPLOY is a WORKTREE of MAIN → its .git is a FILE, mirroring the prod topology.
  git -C "$MAIN" worktree add -q --detach "$DEPLOY"
  # Advance origin so the worktree is behind origin/master once the script fetches.
  printf 'v2\n' > "$SEED/tracked.txt"
  git -C "$SEED" commit -qam c2
}
teardown() {
  git -C "$MAIN" worktree prune 2>/dev/null || true
  rm -rf "$TMP"
}

@test "busy-defer resolves a worktree-safe path when .git is a file" {
  # Precondition: the deploy checkout really is a worktree (.git is a file).
  [ -f "$DEPLOY/.git" ]

  run env CCSWITCH_DEPLOY_CI_OK=1 CCSWITCH_DEPLOY_BUSY=1 \
      CCSWITCH_DEPLOY_MAX_DEFER_SEC=1800 \
      CCSWITCH_HOST=127.0.0.1 CCSWITCH_PORT=0 \
      bash "$DEPLOY/scripts/auto-deploy.sh"

  [ "$status" -eq 0 ]
  [[ "$output" == *"deferring"* ]]
  [[ "$output" != *"Not a directory"* ]]

  # The marker landed inside git's real dir, not via the broken "$REPO/.git/…" path.
  marker="$(git -C "$DEPLOY" rev-parse --absolute-git-dir)/ccswitch-deploy-defer"
  [ -f "$marker" ]

  # Deferred ⇒ no fast-forward.
  [ "$(git -C "$DEPLOY" rev-parse HEAD)" != "$(git -C "$DEPLOY" rev-parse origin/master)" ]
}
