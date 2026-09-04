---
slug: git-worktree-isolation
title: Git Worktree Isolation
state: live
links:
  code:
    - src/ccswitch/worktrees.py
    - src/ccswitch/worktrees_janitor.py
    - src/ccswitch/gitrepos.py
    - server/api/repos.py
  prs: [425, 630, 678, 656, 625, 621, 590, 127]
---

# Git Worktree Isolation

**Problem** — Many `ccswitch` sessions can be working the same repository at the same time. If
they all shared one working tree, each session's branch checkout would clobber the others' —
Session A on `feature-x` and Session B on `feature-y` cannot coexist in one `~/GitHub/<repo>`
checkout, and concurrent `worktree add` / `fetch` calls race on `.git/config` and lose all-but-one
to lock errors. Left unmanaged, per-session working trees also accumulate forever, leaking disk
bytes and inodes until the host wedges.

**Target users** — Operators running parallel sessions and automations across one or more repos;
anyone who dispatches multiple concurrent sessions from the dashboard's repo/branch picker and
expects them not to trample each other's work.

**Desired outcome** — Each session gets its own isolated git worktree on its own branch, sharing
the repo's object store to save space, never touching the user's real checkout. Concurrent
creations against the same clone are serialized (unrelated repos stay parallel). Stale worktrees
are reclaimed automatically once clean, old, and fully pushed — while live and uncommitted work is
never destroyed — and worktree creation is refused up-front when the filesystem is out of bytes or
inodes.

**Success metrics** — (unknown — needs owner input) for a headline target. In-code signals the
team tracks: a sibling session never reaps a live worktree (#425); orphaned-worktree inode leak
closed by the janitor (#630); no `config.lock` race across concurrent per-clone git mutations
(#656); byte-and-inode preflight rejects creation before the host runs dry (#625, #621); dirty
worktrees are max-age evicted (stashing first) rather than kept forever (#678).

**Non-goals** — Not a general git GUI or a replacement for the user's own checkouts — `ccswitch`
worktrees live under a managed namespace (`<repo_root>/.ccswitch/worktrees/…`, clones under
`.ccswitch/clones/<owner>/<name>`) and never mutate the user's `~/GitHub/<repo>` HEAD or branch.
Not a credential manager beyond what git/gh operations need (tokens are injected per-op via env,
never written to `.git/config`).

**Notes**
- **Isolation.** `create_worktree()` (`src/ccswitch/worktrees.py`) builds a per-session worktree
  at `<repo_root>/.ccswitch/worktrees/<repo>/<short-session-id>`, off the shared clone's object
  store; a per-clone lock (`_clone_lock`) serializes concurrent creates on the same clone while
  leaving different repos parallel (#656). Fresh worktrees are locked (`_lock_worktree`,
  `git worktree lock`) so a stray global prune can't reap a live one (#425).
- **Janitor.** `sweep_orphaned_worktrees()` (`src/ccswitch/worktrees_janitor.py`) runs at startup
  then periodically; it reclaims a worktree only when it is **not** in the live-cwd set, old
  enough (default 24 h), clean (`git status --porcelain` empty), and fully pushed (or its upstream
  is `[gone]` after a squash-merge). Dirty, abandoned worktrees are stashed and force-evicted only
  after a max-dirty-age (default 7 days). `clear_stale_registration()` replaced the repo-global
  `git worktree prune` that was reaping live siblings (#425, #630).
- **Disk preflight.** `check_disk_headroom()` uses `os.statvfs()` to check both free bytes and
  free inodes before any git churn, raising `DiskSpaceError` with a stable marker; defaults are
  overridable via `CCSWITCH_MIN_FREE_BYTES` (2 GiB) and `CCSWITCH_MIN_FREE_INODES` (100k) (#625,
  #621).
- **Repo/clone management.** `gitrepos.py` lists gh-accessible repos, clones into the managed
  namespace (reusing a local reference clone via `--reference-if-able --dissociate` when found),
  fetches `origin` only (best-effort; auth failures recorded in a stale-credential registry the
  backlog runner consults), and injects per-owner GitHub App tokens via env (`_git_env`), stripping
  stray `GITHUB_TOKEN`/`GH_TOKEN` to prevent hijack. On a fetch failure it falls back to the local
  ref rather than failing fatally (#127). `server/api/repos.py` serves the repo list (annotated
  with `cloned`), branch lists, and credential-alert status.

**Evidence** — Lock session worktrees + scope cleanup so a sibling never reaps a live one (#425,
refs #177); daemon orphaned-worktree janitor closing the inode leak (#630, refs #591); max-age
eviction for dirty worktrees (#678); serialize per-clone git mutations to prevent the config.lock
race (#656); statvfs byte+inode preflight before `worktree add` (#625) and inode-exhaustion
detection surfacing 507 (#621); prune regenerable deps on same-device archive moves (#590);
fetch-failure fallback to local ref instead of fatal (#127). Code: `src/ccswitch/worktrees.py`,
`worktrees_janitor.py`, `gitrepos.py`; `server/api/repos.py`.
