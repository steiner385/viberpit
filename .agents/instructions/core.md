# cc-account-switcher — Agent Context

`ccswitch` is a multi-account AI coding-agent session manager: a CLI core plus a web-hosted,
mobile-accessible interface for running and switching between live agent sessions across
several accounts, with automatic usage-aware account rotation.

**Governance:** all work MUST comply with the project constitution at
`.specify/memory/constitution.md` (v1.2.0, Principles I–VII). Notably: never fork/patch a native
agent binary, and gate experimental vendor surfaces (I); account = one engine-scoped identity,
strict per-process isolation, at most one credential authority (II); verify against the real
binary/API, not assumptions (III); security-first, exposure-conditional auth (IV); TDD + quality
gates (V); single source of truth / no dead code (VI); modern vetted deps (VII).

Stack: Python FastAPI/Uvicorn + SQLAlchemy (SQLite in dev/tests, Postgres in prod) · React+Vite+TS
+ Tailwind/shadcn frontend · session engines implement the provider-neutral contract in
`server/sessions/engine.py`; Claude runs through `claude-agent-sdk`
(`server/sessions/claude_driver.py`), with normalized events bridged over WebSocket · Tailscale-identity
auth. Specs live under `specs/` (`001-ccswitch-web` is the base feature, `002-*` session continuity /
X display, `003-gh-projects-integration`); design notes and plans under `docs/`.

## Architecture map

- `src/ccswitch/` — CLI core: accounts, `CLAUDE_CONFIG_DIR` isolation, creds, ledger, worktrees. Entry points `ccswitch`, `ccrun`, `ccmax`
  (`pyproject.toml`). Tests in `tests/`.
- `server/app.py` — the **edge**: FastAPI `create_app()`; `app = create_app()` runs at import, so importing the module already reads
  `CCSWITCH_*` env (see the test note below).
- `server/ipc/` — the **session daemon** (`python -m server.ipc`, `ccswitch-daemon.service`). It owns every live `claude` process; the edge
  talks to it over mTLS RPC. Remote hosts (e.g. rocky) run the same daemon in worker role (`CCSWITCH_DAEMON_ROLE`).
- `server/sessions/` — `SessionOrchestrator` (`orchestrator.py`) + canonical engine registry (`engine.py`) + Claude adapter
  (`claude_driver.py`: SDK client, permissions, model switching). In remote mode the lifespan swaps `app.state.sessions` for
  `FleetOrchestrator` (`fleet_orchestrator.py`, routes via `fleet_router.py`) **after** routers are built — capture it lazily, never at
  router construction.
- `server/db/` — models, `init_db` reconcile, `clock.py` (see DB clock rule), `migrations/`.
- `server/deploy/` — deploy controller (`ccswitch-deploy-controller.service`); `server/sdlc/` — governance decisions enforced by `.claude/`
  (hook + managed deny rules + `REVIEW.md`).
- Other server packages: `api/` routers, `auth/`, `automations/`, `github/`, `pipeline/`, `pm/`, `library/`, `obs/`, `usage/`, `stt/`,
  `bridge/`.
- `web/src/` — `api/` (fetch + React Query hooks per domain), `session/` (event reducer + selectors), `shell/` (`AppShell`, nav, prompt
  dock), `components/`, `features/`, `ws/`.
- `deploy/` — systemd units + `deploy/README.md` (the ops runbook); `docs/runbooks/` for host procedures (Postgres cutover, onboarding a
  session runner, rocky).

## Completion workflow

When a unit of work is complete and verified (tests + lint green), finish it without being asked:
1. Branch off `origin/master` — `git fetch` first, then `git checkout -b <branch> origin/master`. NEVER branch off the local `master` ref:
   in a worktree it is a stale leftover (the deploy worktree owns `master`) and lags `origin/master` by hundreds of commits, so branching
   off it silently bases your work on ancient code. Never commit feature work directly to `master`.
2. Commit (end the message with the running model's `Co-Authored-By: Claude <model> <noreply@anthropic.com>` trailer). Write commit messages
   and PR bodies to a file and pass `-F` / `--body-file`: the Bash hook below denies any command whose *text* pairs a deploy verb with a
   prod word.
3. Push and open a PR via `gh pr create` (end body with the Claude Code trailer).
4. ALWAYS mark the PR ready for review (never leave it a draft) and ALWAYS flip auto-merge on once work is complete: open non-draft (omit
   `--draft`; if one slipped through, run `gh pr ready <#>`), then `gh pr merge <#> --squash --auto`. Both are required on every completed
   PR — a ready PR with auto-merge armed is the finished state.
Only hold off if the user said it's a prototype/WIP, or if verification is failing.

`master` is protected: PRs need the `lint-and-test` and `frontend` CI checks green and the branch
up to date with `master` before merging. `allow_update_branch` auto-rebases an out-of-date PR and
re-runs CI, so a green `--auto` PR lands without manual "Update branch" clicks. Branches are deleted
server-side on merge — don't pass `--delete-branch` (the `master` deploy worktree makes the local
delete fail). Merge queue is unavailable (user-owned repo); auto-merge + strict required checks
only *approximates* one, and only while master is quiet: a queue holds a serialized position and
lands the PR from that tested state, whereas `--auto` re-tests against a master free to move. Under
a busy cadence (master advancing faster than a CI cycle) a PR goes `BEHIND`, is rebased by
`allow_update_branch`, and may need several rebase-and-recheck cycles — each a fresh flaky-check
exposure — before it lands.
Admins may push directly in emergencies. If a possible blocker prompts substantive review of an
auto-merge-armed PR, disable auto-merge first and immediately re-read merged state; the stop can race.

## Dev / testing

- Tests: server → `pytest server/tests/`; web → the npm scripts CI runs, in `web/`: `npm run typecheck` (`tsc -b`), `npm run lint` (eslint
  with `--max-warnings 0` — a bare `eslint` can pass where CI fails), `npm run test` (vitest), `npm run build`. E2E: `npm run test:e2e`
  (Playwright). Prompt evals: `python scripts/run_evals.py` (own workflow, `evals.yml`).
- The `lint-and-test` gate is **four** steps, not one: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`
  (core), `pytest server/tests/`. A green `server/tests/` run is not a green gate — mypy in particular catches what no test does (e.g. a
  `min()` over an httpx `Timeout` leg, which is typed `float | None`). Run all four before pushing.
- CI runs the server suite as `pytest server/tests/ -n auto --dist loadfile --timeout=300`. Serial it takes ~25 min; run it in the
  background with output tee'd to `/tmp/`, and never two full runs at once — the load spike gets the Bash call SIGKILLed (exit 137).
- **Test-run slot cap (#822):** before a full `pytest server/tests/` run on rocky, acquire via `/api/test-slots` (`CCSWITCH_TEST_SLOT_CAP`,
  default 1/host; fail-open in `server/api/test_slots.py`). **Put the pytest command in the SAME Bash call / shell as the acquire loop and
  trap** — run the acquire as its own call and that shell exits the moment it finishes, firing the EXIT trap and freeing the slot before
  pytest ever starts, so the cap is a no-op:
  ```bash
  sid="${CCSWITCH_SESSION_ID:-$$}"; host="$(hostname -s)"; trap 'curl -sf -X POST http://127.0.0.1:8787/api/test-slots/release -H "Content-Type: application/json" -d "{\"sessionId\":\"$sid\"}" >/dev/null 2>&1||true' EXIT
  while :; do resp="$(curl -sf -X POST http://127.0.0.1:8787/api/test-slots/acquire -H 'Content-Type: application/json' -d "{\"sessionId\":\"$sid\",\"host\":\"$host\"}" 2>/dev/null)" || { echo "test-slot API unreachable — proceeding (fail-open)"; break; }; echo "$resp" | grep -qE '"granted": *true' && break; echo "$resp" | grep -qE '"degraded": *true' && { echo "test-slot control plane degraded — proceeding unchecked"; break; }; echo "waiting for test slot, $(echo "$resp" | grep -oE '"ahead": *[0-9]+' | grep -oE '[0-9]+') ahead"; sleep 15; done
  pytest server/tests/ …   # inside this same block: the trap releases only when THIS shell exits
  ```
- **Unset `CCSWITCH_SESSIOND_*` and `CCSWITCH_DAEMON_ROLE` before running `server/tests/` locally.** `server/app.py` builds the app at
  import; a live shell's sessiond client-cert vars make it demand certs, and a worker `CCSWITCH_DAEMON_ROLE` turns ~90 tests into "No
  session with id" failures.
- Run the **full** `npm run test`, not just changed files: ~47 web test files manually mock `@/api/hooks`, so adding a hook to a shared
  component (e.g. `SessionList`) breaks tests in files you never touched. CI catches it; a changed-files run does not.
- **Bash hook trap:** `.claude/settings.json` runs `python3 .claude/hooks/prod-deploy-gate.py` as a `PreToolUse` hook on every Bash call.
  (1) It matches the command *text* (deploy|release|rollout|promote + prod|production), so a commit message or `grep` pattern containing
  both words is denied — put the text in a file. (2) The path is cwd-relative: a persisted `cd web` makes every subsequent Bash call fail
  until you `cd` back; use `(cd web && …)` subshells.
- **DB clock rule:** never use `func.now()` / `func.current_timestamp()`; use `server/db/clock.py:db_utc_now()`. Every column is naive UTC,
  but Postgres `CURRENT_TIMESTAMP` is tz-aware and SQLite-only tests hide it — the last slip marked every prod session "Status unknown"
  (2026-09-03, #964).
- **Cancel rule:** never hand-roll `task.cancel()` + `await task` in a teardown path; use `server/aio.py:cancel_and_reap()`. `except
  (asyncio.CancelledError, Exception)` and `suppress(asyncio.CancelledError, …)` swallow the *caller's* cancellation as readily as the
  child's, so a SIGTERM drain keeps running instead of unwinding and eats the unit's `TimeoutStopSec` SIGKILL with live sessions attached
  (#1164). Blocked by `server/tests/test_no_swallowed_outer_cancellation.py`.
- A fresh git worktree has no built env (network blocks `uv sync` of the ~70MB claude-agent-sdk). Run against the main checkout's prebuilt
  env (`<main-checkout>/.venv/bin/python -m pytest …`) and symlink `web/node_modules` from the main checkout.
- **The shared `.venv` imports `ccswitch.*` from the DEPLOY worktree, not your checkout.** `site-packages/__editable__.ccswitch-0.1.0.pth`
  points at `cc-account-switcher-deploy/src`, so edits under your own `src/ccswitch/` are silently ignored by tests — they pass or fail
  against *production* code. `server/*` is unaffected (pytest's rootdir wins). When touching `src/ccswitch/`, run with
  `PYTHONPATH=<your-checkout>/src` and confirm with `python -c "import ccswitch.ledger as m; print(m.__file__)"`. CI is unaffected: it
  installs from the checkout under test.
- API auth tests 401 unless `CCSWITCH_TAILNET_ALLOWLIST` includes the test identity (the real shell env sets a live allowlist). Pin it
  per-test, or run the full suite with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest` (empty allowlist needs `CCSWITCH_DEV=1`).
- `create_all` never ALTERs existing tables, so `init_db` runs `_reconcile_columns` + `_assert_no_schema_drift` on **every** dialect (SQLite
  and Postgres) to ALTER-ADD missing columns. Add new model columns **nullable** and they are picked up automatically on both.
- A **NOT NULL** column still needs a real migration unless `_ddl_default_literal` can synthesize its default — reconcile logs and skips it,
  then the drift assert fails startup loudly.
- **Adding a MODEL (not just a column) means re-running the Postgres suite**, which is skipped unless `CCSWITCH_TEST_PG_URL` is set — so it
  is green locally and red in CI. Two hand-maintained lists in `server/tests/` enumerate every table and must gain an entry:
  `_REQUIRED_EXTRAS` in `test_postgres_compat.py` (NOT NULL columns the datetime sweep must supply) and, for a NOT NULL column with no
  synthesizable default, `RECONCILE_EXEMPT` in `test_schema_reconcile_coverage.py`.
- Nothing runs `alembic upgrade` at deploy time (and the prod DB has no `alembic_version` stamp — it was built by `create_all` at cutover).
  The revisions in `server/db/migrations/versions/` are the reviewable record of schema changes; reconcile is what actually applies them.
  Add a revision for any schema change anyway, and chain `down_revision` off the newest one.
- **`systemctl stop|restart ccswitch-daemon.service` touches every live session.** The daemon owns all running `claude` processes; a restart
  is never a "brief blip" for them. It drains in-flight turns on SIGTERM first (`server/ipc/drain_on_signal.py`, up to ~3 min / ~10 min
  mid-compaction), so the command blocks — expected, don't escalate. Prefer the deploy controller for code changes; for an env edit that
  needs a restart, do it when the Sessions page shows nothing mid-turn. Never restart a daemon whose installed unit still has
  `KillMode=control-group` while sessions are working — that kills them outright (`deploy/README.md` § "Stopping / restarting the daemon").
- **Fast local test loop (#823):** `pytest server/tests/ -m "not slow"` skips tests where a real `time.sleep`/`asyncio.sleep` (≳0.3s) or
  subprocess/daemon spawn IS the thing under test, not something pollable away (e.g. `test_teardown_watchdog.py`, `test_ipc_entrypoint.py`).
  CI never filters — every push runs the full set. Marker registered in `server/tests/conftest.py`; use it only for an intentional real-time
  wait, never for incidental slowness that should be fixed instead.

## Working norms

Concrete, operational discipline that backs constitution Principle VI (single source of truth / no
dead code) and IV/V (security-first, quality gates). Several map to installed skills — invoke the
skill, don't just recall the rule. `subagent-driven-development` ships with the `superpowers` plugin;
the others are user-local skills under `~/.claude/skills/` (if one is missing, follow the rule as
written here).

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

### Before implementing, check whether the same work is already in flight

The rule above has a multi-agent twin: several sessions work this repo at once, so the duplicate
may not be *in* the codebase yet — it may be an open PR. Check **before writing code**; the global
"list in-flight PRs" rule fires at `gh pr create`, too late to prevent the wasted cycle. Search by
the **files you are about to touch** and by the issue number, never by branch name — on 2026-09-05
#1142 and #1145 independently implemented #1131 nine minutes apart, same gate and same file.

```bash
gh api graphql -f query='{repository(owner:"steiner385",name:"ccswitch"){
  pullRequests(states:OPEN,first:40,orderBy:{field:UPDATED_AT,direction:DESC}){
    nodes{number title headRefName files(first:60){nodes{path}}}}}}' \
  --jq '.data.repository.pullRequests.nodes[] | select([.files.nodes[].path] | any(test("<path regex>"))) | "\(.number)\t\(.headRefName)\t\(.title)"'
```

Found one? **Review and extend it** rather than starting over. If yours is better in parts, combine
into one PR and close the other, crediting what you took and saying plainly which came first — the
session that duplicated owns the mistake, not the one it landed on.

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

- Never `git push --force` — use `git push --force-with-lease`, which fails fast if the remote tip moved (e.g. a teammate or a sibling
  worktree pushed since you branched).
- Before rebasing a shared/feature branch, check for remote-only commits with `git log origin/<branch>...<branch>`. Pull or cherry-pick them
  first so a force-push doesn't silently drop merged work.

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

GitHub Copilot Pro+ accounts run through the `whtsky/copilot2api` proxy binary, which translates
Anthropic API calls into Copilot API calls. Gated on `CCSWITCH_ENABLE_COPILOT=true` plus an
installed binary; experimental, with a ToS caveat — see
`docs/product/features/copilot-account-support.md` for provisioning, the add flow, and the caveat.

**Copilot sessions behave differently from Anthropic sessions:**
- Suggestions and auto-summaries are disabled (`_pre_record` skips `suggester`/`summarizer`).
- `ENABLE_TOOL_SEARCH=true` is injected — Claude Code disables MCP tool search for non-first-party base URLs, so this restores it for
  Copilot sessions.
- Usage is reported as premium-request quota (mapped to the `Usage` dataclass via `src/ccswitch/copilot.py:usage_from_proxy`), not
  session-reset tokens.
- The proxy runs as a supervised subprocess per account (see `CopilotProxyManager`), health-gated on `GET /usage`, with idle teardown after
  `CCSWITCH_COPILOT_IDLE_GRACE_S` (default 300 s).

## Server-side dictation (STT)

The mic button streams 16 kHz PCM to `/ws/stt` (`server/stt/`) where `faster-whisper`
(`base.en`, int8, CPU) transcribes it with Silero VAD endpointing; Web Speech is the fallback
when the edge does not advertise `sttEnabled` in `GET /api/config`.

- Enable with `CCSWITCH_ENABLE_STT=true` on the **edge** (needs the `stt` extra: the deploy controller installs `.[server,stt]`; CI/runners
  stay on `[server]`). First start downloads `Systran/faster-whisper-base.en` into the HuggingFace cache; the lifespan warms the model in a
  background thread.
- Knobs: `CCSWITCH_STT_MODEL` (`base.en`; `small.en` is ~1 s/tick on CPU — too slow), `CCSWITCH_STT_CPU_THREADS` (4), `CCSWITCH_STT_TICK_S`
  (1.0), `CCSWITCH_STT_MAX_CONCURRENT` (3), `CCSWITCH_STT_MAX_SESSION_S` (600).
- Policy lives in `server/stt/session.py` (pure, fake-engine tests); the real engine test is `server/tests/test_stt_whisper_integration.py`,
  gated on `CCSWITCH_TEST_STT=1`.
- Audio is never persisted or logged.

## AI assistant (drawer)

A right-side drawer backed by a real session of `kind == "assistant"`. Its only tools are an
in-process `ccswitch` MCP server (`server/assistant/tools.py`) where each handler calls exactly
**one ccswitch HTTP API endpoint over loopback** — never add business logic to a handler, add it to
the endpoint. Its `ccas1:` bearer token (`server/assistant/token.py`) is identity-bound, never
persisted, loopback-only. Kill switch `CCSWITCH_ASSISTANT_ENABLED`; evals under
`docs/superpowers/evals/assistant/`. Details: `docs/product/features/ai-assistant.md`.

## Self-directed model switching

A running session can ask for a different model by ending a message with
`<!-- ccswitch:model=<tier> reason="..." -->`. The contract lives in
`server/sessions/model_request.py` (marker + instruction + parser, one module so the
system-prompt half and the scan half cannot drift).

- **Downgrades auto-apply** on the next turn; **upgrades park** for human approval in the existing `pending_model` columns.
  `resolve_self_directed_model_policy` composes this with the tier denylist and takes the stricter outcome — a **blocked tier stays blocked
  even as a downgrade**.
- A parked hold can be approved **at a different model** (`POST /sessions/{id}/model-approval` with `{"decision": "approve", "model":
  "opus"}`). Only `block` refuses a counter-offer.
- **Never route an approval through `set_model_for_session()`** — it re-runs the policy and re-parks, so every approval would re-ask for
  approval. Approvals use the low-level `driver.set_model()` / `_spawn_session(model=…)` paths.
- Guards (`server/sessions/self_model_guards.py`): same-tier dedupe, a cap of `CCSWITCH_SELF_MODEL_SWITCH_CAP` (default 3) auto-applies per
  session, a flip-flop halt, and a drop once a human has amended a request for that tier.
- Kill switch: `CCSWITCH_SELF_MODEL_SWITCH_ENABLED=false` stops both the prompt instruction and any acting on a marker.
- Audit `model_approval.approve` carries `{requested, applied}` — requested ≠ applied is the metric for whether sessions self-assess
  accurately.

## Session tool surface

Every coding session mounts the in-process app MCP server (`server/sessions/tools/`; wire name from `ccswitch.brand`) with
`session_context`/`report_feedback`, both READ-class (no permission card).
The coding overlay is exactly `{"mcp_servers": …}` — never add `strict_mcp_config` or `system_prompt`. Kill switch
`CCSWITCH_SESSION_TOOLS_ENABLED`. Registry primitives live in `server/mcp_spec.py`; add a tool by adding a `ToolSpec` + handler + eval case.
Spec: `docs/superpowers/specs/2026-09-06-session-feedback-to-backlog-design.md`.

