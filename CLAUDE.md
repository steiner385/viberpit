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
1. Branch off `master` — never commit feature work directly to `master`.
2. Commit (end message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`).
3. Push and open a PR via `gh pr create` (end body with the Claude Code trailer).
4. Merge to `master` once mergeable: `gh pr merge --merge --delete-branch`.
Only hold off if the user said it's a prototype/WIP, or if verification is failing.

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

### Before adding new code, find the existing source of truth

The most expensive bug shape is **N parallel implementations of one concept** (status maps, default
configs, enums, validation rules, design tokens, util helpers). **Before** adding a new constant,
helper, config list, or component, grep for what already exists and route through it. If it exists
but is wrong, fix it in place — adding a parallel "correct" copy *is* the anti-pattern.
- Python: search `server/` and `src/` for the symbol/string before writing a new helper or constant.
- Web: search `web/src/` (utils, lib, components, design tokens) before adding a new util or token.

Skill: `eliminating-parallel-implementations`.

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
