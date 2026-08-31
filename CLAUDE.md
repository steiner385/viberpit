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
- The `lint-and-test` gate is **four** steps, not one: `ruff check src/ server/`, `mypy server/
  --ignore-missing-imports`, `pytest tests/` (core), `pytest server/tests/`. A green `server/tests/`
  run is not a green gate — mypy in particular catches what no test does (e.g. a `min()` over an
  httpx `Timeout` leg, which is typed `float | None`). Run all four before pushing.
- A fresh git worktree has no built env (network blocks `uv sync` of the ~70MB claude-agent-sdk).
  Run against the main checkout's prebuilt env (`<main-checkout>/.venv/bin/python -m pytest …`) and
  symlink `web/node_modules` from the main checkout.
- **The shared `.venv` imports `ccswitch.*` from the DEPLOY worktree, not your checkout.**
  `site-packages/__editable__.ccswitch-0.1.0.pth` points at
  `cc-account-switcher-deploy/src`, so edits under your own `src/ccswitch/` are silently ignored
  by tests — they pass or fail against *production* code. `server/*` is unaffected (pytest's
  rootdir wins). When touching `src/ccswitch/`, run with
  `PYTHONPATH=<your-checkout>/src` and confirm with
  `python -c "import ccswitch.ledger as m; print(m.__file__)"`. CI is unaffected: it installs from
  the checkout under test.
- API auth tests 401 unless `CCSWITCH_TAILNET_ALLOWLIST` includes the test identity (the real shell
  env sets a live allowlist). Pin it per-test, or run the full suite with
  `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest` (empty allowlist needs `CCSWITCH_DEV=1`).
- `create_all` never ALTERs existing tables, so `init_db` runs `_reconcile_columns` +
  `_assert_no_schema_drift` on **every** dialect (SQLite and Postgres) to ALTER-ADD missing columns.
  Add new model columns **nullable** and they are picked up automatically on both.
- A **NOT NULL** column still needs a real migration unless `_ddl_default_literal` can synthesize its
  default — reconcile logs and skips it, then the drift assert fails startup loudly.
- **Adding a MODEL (not just a column) means re-running the Postgres suite**, which is skipped unless
  `CCSWITCH_TEST_PG_URL` is set — so it is green locally and red in CI. Two hand-maintained lists in
  `server/tests/` enumerate every table and must gain an entry: `_REQUIRED_EXTRAS` in
  `test_postgres_compat.py` (NOT NULL columns the datetime sweep must supply) and, for a NOT NULL
  column with no synthesizable default, `RECONCILE_EXEMPT` in `test_schema_reconcile_coverage.py`.
- Nothing runs `alembic upgrade` at deploy time (and the prod DB has no `alembic_version` stamp — it was
  built by `create_all` at cutover). The revisions in `server/db/migrations/versions/` are the reviewable
  record of schema changes; reconcile is what actually applies them. Add a revision for any schema change
  anyway, and chain `down_revision` off the newest one.
- **`systemctl stop|restart ccswitch-daemon.service` touches every live session.** The daemon owns all
  running `claude` processes; a restart is never a "brief blip" for them. It drains in-flight turns on
  SIGTERM first (`server/ipc/drain_on_signal.py`, up to ~3 min / ~10 min mid-compaction), so the
  command blocks — expected, don't escalate. Prefer the deploy controller for code changes; for an env
  edit that needs a restart, do it when the Sessions page shows nothing mid-turn. Never restart a daemon
  whose installed unit still has `KillMode=control-group` while sessions are working — that kills them
  outright (`deploy/README.md` § "Stopping / restarting the daemon").

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

## Copilot account type

ccswitch supports GitHub Copilot Pro+ accounts via the `whtsky/copilot2api` proxy binary, which
translates Anthropic API calls into Copilot API calls. Key operational notes:

**Provisioning the binary:** `CCSWITCH_ENABLE_COPILOT=true` must be set and the proxy binary must
be installed before adding Copilot accounts.

```bash
bash scripts/fetch-copilot-proxy.sh   # installs to ~/.local/lib/ccswitch/copilot2api
```

Or point to a custom binary: `CCSWITCH_COPILOT_PROXY_BIN=/path/to/copilot2api`.

**Copilot sessions behave differently from Anthropic sessions:**
- Suggestions and auto-summaries are disabled (`_pre_record` skips `suggester`/`summarizer`).
- `ENABLE_TOOL_SEARCH=true` is injected — Claude Code disables MCP tool search for non-first-party
  base URLs, so this restores it for Copilot sessions.
- Usage is reported as premium-request quota (mapped to the `Usage` dataclass via
  `src/ccswitch/copilot.py:usage_from_proxy`), not session-reset tokens.
- The proxy runs as a supervised subprocess per account (see `CopilotProxyManager`), health-gated
  on `GET /usage`, with idle teardown after `CCSWITCH_COPILOT_IDLE_GRACE_S` (default 300 s).

**ToS caveat:** routing Claude sessions through the Copilot API may violate GitHub's ToS or
Anthropic's usage policies. This feature is provided for personal experimentation. Users are
responsible for ensuring their usage complies with applicable terms.

**Adding a Copilot account:** open the Add Account dialog → toggle to "GitHub Copilot" → enter a
name → click Add → follow the device-flow code at `github.com/login/device`.
