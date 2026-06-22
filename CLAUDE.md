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
4. Enable auto-merge so the PR lands itself once required checks pass: `gh pr merge <#> --squash --auto`.
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
