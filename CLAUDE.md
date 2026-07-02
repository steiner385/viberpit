# cc-account-switcher — Agent Context

`ccswitch` is a multi-account Claude Max session manager: a CLI core plus a web-hosted,
mobile-accessible interface for running and switching between live `claude` sessions across
several accounts, with automatic usage-aware account rotation.

**Governance:** all work MUST comply with the project constitution at
`.specify/memory/constitution.md` (v1.1.0, Principles I–VII). Notably: never fork/patch the Claude
binary (I); account = `CLAUDE_CONFIG_DIR`, strict per-process isolation (II); verify against the
real binary/API, not assumptions (III); security-first, exposure-conditional auth (IV);
TDD + quality gates (V); single source of truth / no dead code (VI); modern vetted deps (VII).

<!-- SPECKIT START -->
Active feature: **001-ccswitch-web** — Multi-Account Claude Session Manager (Web).
- Spec: `specs/001-ccswitch-web/spec.md`
- Plan: `specs/001-ccswitch-web/plan.md`
- Research: `specs/001-ccswitch-web/research.md`
- Data model: `specs/001-ccswitch-web/data-model.md`
- Contracts: `specs/001-ccswitch-web/contracts/`
- Quickstart: `specs/001-ccswitch-web/quickstart.md`

Stack: Python FastAPI/Uvicorn + SQLAlchemy(SQLite) backend · React+Vite+TS + Tailwind/shadcn +
xterm.js frontend · sessions are native `claude` in PTYs bridged to the browser over WebSocket ·
Tailscale-identity auth (v1) with a passkey path deferred to public hosting.
Build phase-by-phase: P0 config-dir launch correctness → P1 backend engine+API → P2 frontend
(dashboard+terminal) → P3 structured view → P4 public-hosting hardening.
<!-- SPECKIT END -->

## Completion workflow

When a unit of work is complete and verified (tests + lint green), finish it without being asked:
1. Branch off `origin/master` — `git fetch` first, then `git checkout -b <branch> origin/master`. NEVER
   branch off the local `master` ref: in a worktree it is a stale leftover (the deploy worktree owns
   `master`) and lags `origin/master` by hundreds of commits, so branching off it silently bases your
   work on ancient code. Never commit feature work directly to `master`.
2. Commit (end message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`).
3. Push and open a PR via `gh pr create` (end body with the Claude Code trailer).
4. ALWAYS mark the PR ready for review (never leave it a draft) and ALWAYS flip auto-merge on
   once work is complete: open non-draft (omit `--draft`; if one slipped through, run
   `gh pr ready <#>`), then `gh pr merge <#> --squash --auto`. Both are required on every completed
   PR — a ready PR with auto-merge armed is the finished state.
Only hold off if the user said it's a prototype/WIP, or if verification is failing.

`master` is protected: PRs need the `lint-and-test` and `frontend` CI checks green and the branch
up to date with `master` before merging. `allow_update_branch` auto-rebases an out-of-date PR and
re-runs CI, so a green `--auto` PR lands without manual "Update branch" clicks. Branches are deleted
server-side on merge — don't pass `--delete-branch` (the `master` deploy worktree makes the local
delete fail). Merge queue is unavailable (user-owned repo), so auto-merge + strict required checks
is the equivalent. Admins may push directly in emergencies.

## Dev / testing

- Tests: server → `pytest server/tests/`; web (run in `web/`) → `vitest run`, `tsc --noEmit`, `eslint`.
- A fresh git worktree has no built env (network blocks `uv sync` of the ~70MB claude-agent-sdk).
  Run against the main checkout's prebuilt env (`<main-checkout>/.venv/bin/python -m pytest …`) and
  symlink `web/node_modules` from the main checkout.
- API auth tests 401 unless `CCSWITCH_TAILNET_ALLOWLIST` includes the test identity (the real shell
  env sets a live allowlist). Pin it per-test, or run the full suite with
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest` (empty allowlist needs `CCSWITCH_DEV=1`).
- Dev SQLite: `create_all` never ALTERs existing tables; `init_db` backfills missing nullable columns,
  so add new model columns nullable — a NOT NULL column needs a real migration (Alembic, Postgres/prod).

## Working norms

Concrete, operational discipline that backs constitution Principle VI (single source of truth / no
dead code) and IV/V (security-first, quality gates). Several map to installed skills — invoke the
skill, don't just recall the rule.

### Execute implementation plans subagent-driven — always

When a written plan reaches the execution-handoff step (brainstorming → writing-plans → execute),
**always** choose subagent-driven execution: a fresh subagent per task with review between tasks.
Never ask which mode to use and never default to inline batch execution. Skill:
`subagent-driven-development`.

### Before adding new code, find the existing source of truth

The most expensive bug shape is **N parallel implementations of one concept** (status maps, default
configs, enums, validation rules, design tokens, util helpers). **Before** adding a new constant,
helper, config list, or component, grep for what already exists and route through it. If it exists
but is wrong, fix it in place — adding a parallel "correct" copy *is* the anti-pattern.
- Python: search `server/` and `src/` for the symbol/string before writing a new helper or constant.
- Web: search `web/src/` (utils, lib, components, design tokens) before adding a new util or token.

Skill: `eliminating-parallel-implementations`.

### Don't re-derive a verdict one layer already computed — defer to it

A close cousin of the parallel-implementations bug: two layers each judge the *same outcome* from
raw fields and reach **different conclusions**. The classic shape is a client re-computing a
status the server already decided — e.g. the deploy backend deliberately treats a session that
can't be resumed as a non-fatal *warning* (keeps the phase `DONE`, never sets `error`, never rolls
back), but the frontend's `deployOutcome()` re-judged the raw `restore.failed` list and escalated
it to a hard "Upgrade failed" (#404). Backend said success-with-warning; frontend said failure;
the user saw a red modal over a deploy that actually completed.
- When one layer owns a decision (success/failure, valid/invalid, allowed/denied), downstream
  layers should **consume that verdict**, not recompute their own from the same inputs — the copies
  drift and disagree.
- The tell that you're about to add a second judge: you're reading the *same underlying fields* the
  authoritative layer read, to re-answer a question it already answered. Surface its answer instead.
- Sibling audit: when you find one, grep for other places the client re-derives server-owned status
  (`*Outcome`, `is*Failed`, status maps built from raw response fields) and route them through the
  authoritative field too.

### Purge replaced code in the same change — no half-dead code

When you replace a file, route, model, or function, delete the old version in the **same PR**. A
disconnected handler or orphaned module is a landmine: it gets imported by accident, drifts from the
new path, or is mistaken for canonical by the next grep. The only exception is a migration too large
for one PR — then mark the old path `# @deprecated — replaced by X, remove in #N` and track it in an
issue; otherwise, delete it.

### Audit recurring fix patterns — don't just patch

When you fix a bug whose *shape* could exist elsewhere (same anti-pattern, different file), capture
the pattern and sweep for siblings instead of fixing the one instance. The trigger is "this could
exist elsewhere," not "I'm sure it does" — false-positive audits are cheap; a missed second instance
of a silent bug is not. Skill: `capturing-fix-patterns`.

### Git safety

- Never `git push --force` — use `git push --force-with-lease`, which fails fast if the remote tip
  moved (e.g. a teammate or a sibling worktree pushed since you branched).
- Before rebasing a shared/feature branch, check for remote-only commits with
  `git log origin/<branch>...<branch>`. Pull or cherry-pick them first so a force-push doesn't
  silently drop merged work.

Skill: `safe-git-operations`.

### GitHub API — prefer GraphQL over REST for reads

`gh pr list` / `gh pr view` / `gh issue view` use REST under the hood and each costs a hit against
the shared 5,000-req/hr rate limit; a single `gh api graphql` query batches many reads into one
request. Use GraphQL for reads (`repo:steiner385/cc-account-switcher …`); reserve REST for mutations
that have no GraphQL path in `gh` (`gh pr create`, `gh pr merge`, `gh issue create/close`).
Skill: `github-via-graphql`.

### Issue auto-close keyword

GitHub closes a referenced issue on merge only when a closing keyword (`Fixes #N`, `Closes #N`,
`Resolves #N`) precedes the number in the **PR body or a commit message** — a bare `(#N)` links but
does NOT close. Put `Fixes #N` in the PR body for a full fix, `Refs #N` for a partial one.
