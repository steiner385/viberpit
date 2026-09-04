# Runbook — a shared clone was deleted and its session worktrees are orphaned

**Symptom.** Every git command in one or more session worktrees fails with:

```
fatal: not a git repository: /home/tony/GitHub/.ccswitch/clones/<owner>/<name>/.git/worktrees/<id>
```

**Cause.** Session worktrees under `~/GitHub/.ccswitch/worktrees/<repo>/<id>` hold only a `.git`
*pointer file* into the shared clone's `.git/worktrees/<id>`. The shared clone is therefore
load-bearing for every session on the host: delete it and every worktree under it dies at once
(issue #1003 — 130 worktrees across two hosts, from one deletion per host).

**Good news.** Only git metadata is lost. The working tree — committed files, uncommitted edits and
untracked files — is all still on disk, and is fully recoverable.

## 1. Confirm the blast radius

```bash
ccswitch clones doctor          # or: ccswitch clones doctor --json
```

Each line is one clone the host's worktrees depend on:

```
cairnea/KinDash: clone MISSING at ~/GitHub/.ccswitch/clones/cairnea/KinDash — 61 worktree(s), 61 broken
```

The daemon reports the same condition on its own: the worktree janitor emits one CRITICAL line per
sweep naming the missing clone (`worktree-janitor: orphaned=61 worktree(s) reference missing
clone(s) …`), and the count appears in every sweep's summary as `orphaned=N`.

## 2. Recover

```bash
ccswitch clones repair --dry-run    # report only
ccswitch clones repair
```

For each missing clone this re-clones into the *original path* and then, for every orphaned
worktree, recreates the git admin record (`gitdir`, `commondir`, `HEAD`), rebuilds the index with a
mixed `git reset`, normalises with `git worktree repair`, and re-locks the worktree (#177).

A repair also happens **automatically** the next time anything clones that repo — `ensure_clone`
re-links orphaned worktrees after creating a clone — so a new session on the affected repo
self-heals the whole host.

### What survives, and what doesn't

| | |
|---|---|
| Committed + modified + untracked files | **Intact** — never touched |
| Index (staged vs unstaged split) | Rebuilt from HEAD; staged-ness is lost, content is not |
| The branch the worktree was on | **Lost** — it lived only in the deleted admin record and the clone's local refs |

Recovered worktrees land **detached** at the remote default tip (override with
`--base-ref <ref>`). Re-attach a session's work with:

```bash
cd ~/GitHub/.ccswitch/worktrees/<repo>/<id>
git switch -c <branch>      # or: git switch -c ccswitch/<branch>-<id>
```

`git status` then shows the session's uncommitted work as a diff against that base.

> `git worktree repair` **alone cannot** do this. Once the clone is gone, a fresh clone has no
> `.git/worktrees/<id>` record to repair and the command fails with `unable to locate repository;
> .git file does not reference a repository`. That is why recovery writes the record first.

## 3. Don't delete a clone by hand

Use the guarded path, which refuses while any worktree still depends on the clone and journals
every outcome (caller, reason, referencing-worktree count):

```bash
ccswitch clones remove <owner>/<name> --reason "why"
```

Exit code 2 means the removal was refused; `--force` overrides it and is destructive — it kills the
git metadata of every session listed. A plain `rm -rf` of a clone bypasses all of this and is what
caused the 2026-09-03 outage.
