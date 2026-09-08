# Rebrand `ccswitch` → **ViberPit** — design

Status: design, awaiting owner review. Date: 2026-09-06.

`ccswitch` (repo `steiner385/ccswitch`, formerly `cc-account-switcher`) becomes **ViberPit**.
This document is the rename plan, ordered outside-in: first every surface a person or another
program sees, then wire contracts, then hosts, then the on-disk namespace inside user repos,
then internal identifiers, then docs. It is a plan for a *series* of PRs, not one.

## 1. Name forms (single source of truth)

| Context | Value | Notes |
|---|---|---|
| Display name | `ViberPit` | UI, titles, prose, `Description=` lines, GitHub comments |
| Slug | `viberpit` | CLI binary, Python package, MCP server name, repo slug, unit-name stem, dir names |
| Env prefix | `VIBERPIT_` | replaces `CCSWITCH_`; old prefix stays readable (§4) |
| Header stem | `X-ViberPit-` | replaces `X-CCSwitch-` |
| Marker stem | `<!-- viberpit:… -->` | replaces `<!-- ccswitch:… -->` |
| Legacy slug | `ccswitch` | kept only in compat readers, referenced by one constant |

Two constants own these: `src/viberpit/brand.py` (Python) and `web/src/brand.ts` (web). Every
display string, prefix, key, and path stem is derived from them. No other module spells the
brand. This is the eliminating-parallel-implementations rule applied before the rename, so each
later phase changes one line, not five.

Deliberately **not** renamed (with reason):

| Item | Reason |
|---|---|
| `~/.claude-accounts` config root (`CCSWITCH_HOME` default) | already brand-free; it is an sshfs mount shared with rocky |
| `ccrun`, `ccmax` entry points | "cc" = Claude Code, the engine, not the brand; keep as-is |
| `ccas1:` / `ccas2:` assistant token prefixes | wire format held by external clients; not the brand name |
| mTLS CA `CN=ccswitch-ca` | allowlists pin `CN=edge`, not the CA; reissue at the next rotation |
| unix user `ccswitch-auth` + `/var/lib/ccswitch-codex-auth` | 0700 state holding OpenAI refresh tokens; rename with the next credential rotation |
| Postgres role/db `ccswitch` | a name only; optional `ALTER ROLE … RENAME` later |
| `docs/superpowers/**`, `docs/audits/**`, `docs/2026-06-19-ccswitch-*.md`, `specs/**` | dated historical records; rewriting history is churn |
| `specs/001-ccswitch-web/` directory | referenced by `.specify/feature.json` and live code comments |
| `.specify/memory/constitution.md` | governance-locked; needs a human "Owner acknowledgment" line — the owner edits it |

Trademark note: "Viber" is a Rakuten messaging product. ViberPit is distinct as a compound, but
the owner should be aware before the GitHub slug and any public assets change.

## 2. Inventory summary

| Layer | Distinct surfaces | Compat needed? |
|---|---|---|
| Web UI display | `<title>`, meta, `APP_NAME`, TopBar wordmark, help/overlay/adopt copy, service-worker title | no |
| CLI display | `prog=`, ~45 `ccswitch: …` stderr prefixes, `ccswitch mcp` banner | no |
| Outbound identity | `User-Agent: ccswitch/<ver>` (Anthropic OAuth), `user-agent: ccswitch` (GitHub), FastAPI titles, MCP client `name`/`title` to Codex | no |
| Text posted to GitHub | 5 Backlog Runner comment templates, label descriptions, CI/CD Doctor label + issue body | verify claim-comment read-back (§4.6) |
| Agent-facing prose | assistant system prompt, `<ccswitch-context>` header (regex-parsed), skill-author prompt, backlog prompt, sdlc prompts, model-request instruction | yes — pairs with markers |
| Markers | `<!-- ccswitch:complete -->`, `:no-pr`, `:model=` | **yes** — parsers accept both forever |
| MCP tool namespace | server name `ccswitch`, prefix `mcp__ccswitch__` in **5** copies, permission gates fail closed | **yes** |
| HTTP | header `X-CCSwitch-Via`, `X-CCSwitch-Assistant-Session`, cookie `ccswitch_session` | **yes** — cookie is the auth path |
| Browser storage | 13 `ccswitch.*` / `ccswitch:*` keys | migrate-on-read |
| Env vars | 239 distinct `CCSWITCH_*`, read via pydantic `Settings` + ~120 scattered `os.environ` + shell; dynamic `CCSWITCH_OAT_<acct>` | **yes** — `/etc/ccswitch/*.env` is uncommitted root state holding secrets |
| systemd | 12 units + 2 drop-ins, `SyslogIdentifier=`, deploy controller restarts units (and itself) by literal name | **yes** — two-phase |
| Host paths | `/etc/ccswitch`, `/run/ccswitch/*.sock`, `/var/lib/ccswitch`, `/var/log/ccswitch`, `~/.cache/ccswitch`, `~/.local/lib/ccswitch`, `/opt/ccswitch-codex-auth`, checkout `cc-account-switcher-deploy` | symlink + rename |
| Repo namespace | `<repo>/.ccswitch/{worktrees,clones,scratch}`, `.ccswitch-codex-attachments`, branch prefix `ccswitch/`, orchestrator's `".ccswitch" in cwd.parts` delete guard, `repoFromCwd.ts` | **yes** — live worktrees with absolute gitdir pointers |
| DB rows | `Session.cwd`, `Session.branch`, `FleetHost.endpoint` (`unix:///run/ccswitch/…`), `WorkItem.dedupe_key` (`ccswitch:feature-map` …), `WorkItem.repo` slug, marker text in transcripts | aliases in readers; one UPDATE for PM dedupe keys and repo slug |
| Python | package `src/ccswitch` (29 modules, 1036 import lines / 234 files), `pyproject` name + scripts, `uv.lock`, `alembic.ini`, 4 loggers, 2 ContextVars, 18 test names, editable `.pth` in the shared venv | shim package for one release |
| Web internals | `MCP_CCSWITCH_PREFIX`, eslint rule ns `ccswitch/no-raw-palette`, SVG gradient id, one test asserting exact localStorage keys | no |
| Agent roles | `ccswitch-{implementer,reviewer}` in `.claude/agents`, `.codex/agents`, `.agents/roles` + `contract.json` + `agent-contract.yml` | rename all copies in one PR |
| Docs (living) | README, AGENTS (generated from `.agents/instructions/core.md`), deploy/README, 5 runbooks, 21 feature docs, ACCESSIBILITY, SMOKE, evals/README, `.claude/README|REVIEW` | rewrite |

## 3. Phasing

Order is outside-in, as requested. Each phase is independently shippable and leaves the fleet
working. Phase 0 is not a rename; it is the de-duplication that makes every later phase a
one-line change.

### Phase 0 — Prep: one source of truth per brand-bound concept (2 PRs)

- **0a `brand.py` / `brand.ts`.** Add the constants from §1 with the *old* values first
  (`NAME="ccswitch"`), and route every existing display string, prefix, and key through them.
  Behaviour-neutral; tests prove no wire string changed. Collapse the 5 copies of
  `mcp__ccswitch__` into `brand.MCP_PREFIX`. Collapse the 4 path modules' literals
  (`server/ipc/paths.py`, `server/deploy/serve.py`, `server/config.py`, `server/sessions/*`) onto
  `brand.SLUG`.
- **0b env-prefix mirror.** Add `viberpit.env_compat.mirror_legacy_env()`: for every
  `CCSWITCH_<X>` in `os.environ`, set `VIBERPIT_<X>` if unset, and the reverse. Call it first
  thing in every entrypoint (`server/app.py` import, `server/ipc/__main__`, `server/deploy`,
  `server/codex/auth_main`, CLI `main`s, `alembic env.py`, pydantic `Settings` init). This makes
  the 765 scattered reads renamable by find-and-replace while `/etc/ccswitch/*.env` keeps
  working unchanged. Covers the dynamic `CCSWITCH_OAT_<acct>` prefix for free. Shell scripts get
  a `${VIBERPIT_X:-${CCSWITCH_X:-default}}` idiom.

Flip the constants to the new values in Phase 1; nothing else in Phase 0 is user-visible.

### Phase 1 — External surfaces (3 PRs, ship first, zero compat risk)

- **1a Web + CLI + outbound identity.** Flip `brand.ts`/`brand.py` display values:
  `<title>`, meta description, `APP_NAME`, TopBar wordmark, DeployOverlay, AdoptPanel, HelpSheet,
  navDefs help, AboutSection path text, `sw.js` title/tag, FastAPI titles, MCP client identity
  to Codex, `ArgumentParser(prog=)`, the ~45 CLI prefixes, `ccswitch mcp` banner, both
  `User-Agent`s, push-notification titles, snapshot download filename, Prometheus help text,
  `Description=` lines in unit files (text only, filenames unchanged until Phase 3).
  **Logo:** keep the existing mark (two session lanes) and change only the wordmark, unless the
  owner supplies a ViberPit mark; `favicon.svg` and `BrandMark.tsx` stay in sync.
- **1b Text we post to GitHub.** Backlog Runner comment templates, label descriptions,
  CI/CD Doctor label + issue body, janitor commit identity → `ViberPit`. Pre-req: confirm the
  orphan-claim heal at `backlog_github.py:1073` matches claims by structure, not by the literal
  "ccswitch Backlog Runner"; if it matches the literal, accept both.
- **1c Agent-facing prose (paired with Phase 2a).** Assistant system prompt identity, tool
  descriptions, skill-author prompt, backlog/sdlc prompts, the model-request instruction, and the
  `<ccswitch-context>` header emit the new name. Ships in the same PR as the marker dual-accept
  (2a) because a prompt that teaches `<!-- viberpit:complete -->` to an edge that only parses
  `ccswitch:` strands every session.

**GitHub repo rename** (`steiner385/ccswitch` → `steiner385/viberpit`) is an external surface
but an ops event: it changes remotes, the deploy checkout, `CCSWITCH_DAEMON_SSH_REPO`, and
triggers the daemon's clone-relink (which broke worktree gitdir links at the last rename). It is
scheduled inside the Phase 3 maintenance window, with a runbook, and followed by one `UPDATE`
of `WorkItem.repo` and the hard-coded slugs in `deploy_envs.py` / `githubUrl.ts` / fixtures.
GitHub redirects the old slug, so nothing breaks in between.

### Phase 2 — Wire contracts: emit new, accept both (3 PRs)

- **2a Markers + MCP namespace.** `completion.py`, `model_request.py`, `claude_driver.py`
  parse `(ccswitch|viberpit):` and emit `viberpit:`. Permanent, because stored transcripts are
  re-parsed on replay. MCP server name → `viberpit`, `brand.MCP_PREFIX` → `mcp__viberpit__`;
  `permissions.py`, `events.py`, `claude_driver.py`, `codex/driver.py`, `options_codex.py`, and
  `appActions.ts` accept both prefixes (resumed Codex threads keep old `dynamicTools` names).
  Includes 1c.
- **2b HTTP + browser.** Cookie `viberpit_session` written, both read for one release. Headers
  `X-ViberPit-*` sent, both accepted by `auth/middleware.py`. localStorage: migrate-on-read
  using the existing `notifyPref.ts` pattern (read legacy → write new → remove legacy); update
  the `listFilters.test.ts` exact-keys assertion. Export format id `viberpit.normalized-events/v1`
  written, both accepted on import.
- **2c DB-persisted strings.** Branch prefix: create `viberpit/<base>-<id>`, match
  `^(ccswitch|viberpit)/` in `worktrees.py:510`, `pr_status.py`, janitor. PM `dedupe_key` /
  `origin_ref`: one migration `UPDATE … SET dedupe_key = replace(dedupe_key,'ccswitch:','viberpit:')`
  plus emitter flip (no alias — a dual key would re-file every item). Add the alembic revision
  per the repo rule; reconcile applies nothing for a data-only change, so the migration runs as a
  one-shot script in the Phase 3 window.

### Phase 3 — Hosts and ops (2 PRs + 1 maintenance window)

- **3a Code tolerates both unit names.** `server/deploy/{steps,runners,__main__}.py`,
  `server/aio.py`, `ipc/drain_on_signal.py`, `codex/auth_main.py`, health-check script: resolve
  the unit name at runtime (`viberpit-daemon.service` if `systemctl cat` finds it, else
  `ccswitch-daemon.service`). Socket and state paths go through `brand.SLUG` with a
  legacy-path fallback if the new path does not exist. Tests that assert literal unit names move
  to the resolver.
- **3b Unit files renamed in `deploy/`** (12 units, 2 drop-ins, nginx site, sysusers, scripts/*),
  `SyslogIdentifier=viberpit*`, `RuntimeDirectory=viberpit`, `StateDirectory=viberpit`,
  `EnvironmentFile=/etc/viberpit/viberpit.env`. `deploy/README.md` and the runbooks rewritten in
  the same PR (living docs, lockstep with the units).
- **Maintenance window (runbook, one host at a time, rocky first):** when the Sessions page shows
  nothing mid-turn. Steps: `mv /etc/ccswitch /etc/viberpit && ln -s`; same for `/var/lib`,
  `/var/log`, `~/.cache`, `~/.local/lib`; `ln -s /run/ccswitch /run/viberpit` (tmpfs, per boot →
  `tmpfiles.d` entry); rename the deploy checkout dir and edit `WorkingDirectory=`/`ExecStart=`;
  install new units with `Alias=ccswitch-*.service` for one release; `systemctl disable` old,
  `enable` new, daemon-reload, restart via the deploy controller; `UPDATE fleet_hosts SET
  endpoint` for the socket path; GitHub repo rename + remote fix + worktree relink repair;
  `pip install -e` so the `.pth` points at the renamed checkout. Verify `/healthz`, journal tags,
  a full turn on each host, a Codex session, and the assistant drawer.

### Phase 4 — Namespace inside user repos (1 PR)

- `<repo>/.ccswitch/` → `<repo>/.viberpit/` for **new** clones/worktrees only. A resolver
  `repo_namespace(repo_root)` returns `.viberpit` unless only `.ccswitch` exists, so one repo
  never has two namespaces and live worktrees (absolute `gitdir:` pointers, `Session.cwd` rows)
  are untouched. Janitor, `clone_doctor`, `server/api/repos.py`, the orchestrator's
  `permanent_delete` guard, and `web/src/lib/repoFromCwd.ts` accept both segment names.
  `.ccswitch-codex-attachments` → `.viberpit-codex-attachments` same rule. Worktree lock reason
  and janitor identity flip. Sweep for siblings of the `".ccswitch" in parts` guard pattern.

### Phase 5 — Internal identifiers (3 PRs)

- **5a Python package.** `git mv src/ccswitch src/viberpit`; sed the 1036 import lines;
  `pyproject` `name = "viberpit"`, scripts `viberpit = viberpit.cli:main` **plus** the old
  `ccswitch` entry point kept as an alias for one release (`ccrun`/`ccmax` unchanged);
  `uv.lock`, `alembic.ini`. A thin `src/ccswitch/__init__.py` shim that installs a meta-path
  importer redirecting `ccswitch.*` → `viberpit.*` for one release (the shared venv's editable
  `.pth` resolves from the deploy checkout, so any un-rebuilt worktree would otherwise
  `ModuleNotFoundError`). Loggers `viberpit.*`, ContextVars, the 18 test names, CI Postgres
  `viberpit_test`, `CCSWITCH_TEST_PG_URL`.
- **5b Web internals.** `MCP_VIBERPIT_PREFIX`, eslint plugin namespace `viberpit/no-raw-palette`
  (config + rule file + the source comments that cite it), SVG gradient id, remaining code
  comments in `pipeline/*` ("ccswitch blend"), `.env.example`.
- **5c Agent roles.** `ccswitch-{implementer,reviewer}` → `viberpit-*` in `.claude/agents`,
  `.codex/agents`, `.agents/roles`, `.agents/contract.json`, the SDD skill that dispatches by
  name, and `agent-contract.yml`, all in one PR so no client dispatches asymmetrically.

### Phase 6 — Living docs (1 PR)

README (H1 + CLI examples), `.agents/instructions/core.md` then `agentctl render` (320-line cap),
`CLAUDE.md`, `docs/product/features/*` prose, `docs/ACCESSIBILITY.md`, `docs/SMOKE.md`,
`evals/README.md`, `.claude/README.md`, `.claude/REVIEW.md`, `.env.example`. Historical docs
untouched; add one line at the top of `README.md`: "formerly ccswitch / cc-account-switcher".
Constitution edit handed to the owner with the exact diff.

### Phase 7 — Remove scaffolding (1 PR, ≥ 4 weeks after Phase 5)

Drop: the `ccswitch` import shim and entry point, cookie/header/localStorage legacy reads,
`Alias=` lines in units, `/run` and `/etc` symlinks (runbook), the env mirror's reverse
direction (keep `CCSWITCH_*` → `VIBERPIT_*` for ops sanity, or drop after `/etc/viberpit/*.env`
is confirmed migrated on every host). Keep forever: marker and MCP-prefix dual-accept
(historical transcripts), branch-prefix dual-match (old branches on remotes), namespace
resolver (old repos on disk).

## 4. Risks and their controls

1. **systemd rename while the deploy controller restarts itself by name** → Phase 3a lands and
   deploys *before* 3b; `Alias=` covers the overlap; controller's transient
   `--unit=ccswitch-controller-refresh` renamed with it.
2. **MCP prefix mismatch fails closed** (every assistant tool call denied) → single constant
   (0a) plus a test that the server name, the SDK-emitted tool name, and each permission matcher
   agree, for both prefixes.
3. **Env rename silently reverting `CCSWITCH_MASTER_KEY` / `SESSION_SECRET` / `DB_URL` to
   defaults** → mirror runs before any read; a startup assertion logs which prefix supplied each
   secret-bearing var.
4. **Live worktrees orphaned** → Phase 4 never moves existing namespaces; resolver prefers the
   existing one.
5. **Every browser logged out** → cookie read-both for one release.
6. **Shared venv `.pth` hazard** → import shim + coordinated `pip install -e` on dobby and rocky
   in the Phase 3 window; verify with `python -c "import viberpit.ledger as m; print(m.__file__)"`.
7. **In-flight sessions prompted with old markers** → parsers accept both permanently.
8. **Backlog Runner orphan-heal keyed on the literal comment** → verified in 1b before the
   template changes.
9. **Repo rename relinks worktrees** → done inside the maintenance window with the repair recipe
   from the previous rename; not a separate event.
10. **Concurrent sessions** → each phase is one or two files' worth of sweep; check in-flight PRs
    by file before each sweep, and land sweeps when master is quiet.

## 5. Testing

- Phase 0: snapshot tests that every wire string (markers, prefix, cookie, headers, keys, unit
  names, paths) is byte-identical before/after routing through `brand.*`.
- Phase 2: table-driven parser tests for old and new forms; a `test_brand_wire_contracts.py`
  that asserts the emitted form is new and the accepted set is both.
- Phase 3: `test_deploy_*` use the resolver; a fake `systemctl` returning either unit set.
- Phase 4: worktree/clone tests run against both namespace names; the delete guard accepts both.
- Phase 5: full four-step gate (`ruff`, `mypy`, `pytest tests/`, `pytest server/tests/`) with
  `PYTHONPATH=$PWD/src`; full `npm run typecheck|lint|test|build`; the `agent-contract` and
  `constitution` workflows.
- Each phase: grep gate `git grep -n 'ccswitch' -- ':!docs/superpowers' ':!docs/audits'
  ':!specs' ':!docs/2026-*'` shrinks monotonically, and the remaining hits are only in
  `brand.py`/`brand.ts` compat constants and the alias readers.

## 6. Decisions assumed (owner may override)

| # | Decision | Assumed |
|---|---|---|
| D1 | Slug spelling | `viberpit` (no hyphen, no camel) |
| D2 | Keep `ccrun`/`ccmax` | yes, unchanged |
| D3 | Logo | keep the mark, change the wordmark |
| D4 | GitHub slug | `steiner385/viberpit`, renamed in the Phase 3 window |
| D5 | Historical docs and `specs/` | untouched |
| D6 | Token prefixes `ccas1:`/`ccas2:`, CA CN, unix user, PG role | untouched this round |
| D7 | Env alias direction | both directions during transition; old→new kept longest |
| D8 | Sunset for shims | 4 weeks after Phase 5 lands |

Sizing: 16 PRs across 8 phases plus one per-host maintenance window. Phases 0–2 are pure code
and can ship this week; Phase 3 needs the owner on the hosts.
