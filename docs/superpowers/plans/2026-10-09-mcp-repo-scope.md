# MCP Per-Repo Scope + Live Session Toggle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an MCP server be off by default and turned on (or off) per repo, and let a running session switch any server on or off without a restart. The choice persists across resume.

**Architecture:**
- One pure resolver (session override → repo rule → server default → on) produces the disabled-server list.
- At every Claude driver build the orchestrator passes that list to `configdir.trust_project`, which writes the CLI's native `projects[<worktree>].disabledMcpServers`.
- A live toggle calls the SDK's `toggle_mcp_server()` through the same RPC chain as sub-project 1's reconnect, and records the choice in `sessions.mcp_overrides`.
- Rules live in two edge-managed tables behind a validated library API.

**Tech Stack:** Python 3.12 FastAPI + async SQLAlchemy + `claude-agent-sdk` (`toggle_mcp_server`); React + TS + React Query + vitest.

**Spec:** `docs/superpowers/specs/2026-10-09-mcp-repo-scope-design.md`. It depends on sub-project 1 (`2026-10-09-mcp-live-status-design.md`, PR #2872). Read both.

## Global Constraints

- **Precedence**, highest first: session override → repo rule (skipped for scratch sessions, `repo is None`) → server default → enabled. A server with no `mcp_server_scope` row defaults to enabled.
- **Server keys** are the names the CLI reports: bare for user scope, `plugin:<plugin>:<server>` for plugin servers. Map between UI short names and keys only with `viberpit.mcp_names.short_name`.
- **The CLI list** is written by `configdir.trust_project(...)` only, inside the existing compose → trust → isolate order. `disabled_mcp=None` leaves the key untouched.
- **Account-store reads** at driver build go through `account_store_io` (#2852). A rules read failure logs at DEBUG, writes no list, and never blocks the session start.
- **New tables:** `mcp_server_scope(server PK, default_enabled NOT NULL, updated_at)` and `mcp_repo_rule(repo, server, enabled NOT NULL, updated_at; UNIQUE(repo, server))`. **New column:** `sessions.mcp_overrides` (nullable JSON). `updated_at` comes from `db_utc_now()`; never use `func.now()`.
- **Migration** `0079_mcp_repo_scope`, `down_revision = '0078_mcp_server_status'`. Re-check the newest revision on `origin/master` before committing it.
- **Kill switch** `VIBERPIT_MCP_SCOPE_ENABLED` (default `true`). When off, no list is written, the rules API returns 404 `{"error": "mcp_scope_disabled"}`, the toggle route returns 404 the same way, and the UI hides the controls.
- **Toggle route:** `POST /api/sessions/{id}/mcp/{server}/toggle {enabled: bool}`, audited as `session.mcp_toggle`. It returns 404 when the session isn't live, 409 `MCP_TOGGLE_FAILED` on an SDK error, and 400 for an unknown server.
- **Rules API:**
  - `GET /api/library/mcp/scope`
  - `PUT /api/library/mcp/scope/{server}` `{defaultEnabled}`
  - `PUT` / `DELETE /api/library/mcp/scope/{server}/repos/{owner}/{name}` `{enabled}`
  - Audit type `library_mcp_scope_saved`, with name and repo only.
- **Validation:** `server` must be a configured key. The repo must be in the known-repos list (the same gh list `/api/github/repos` serves). Each path segment is validated on its own, and anything else returns 400.
- **Teardown** uses `cancel_and_reap`; there are no new background tasks expected.
- **Web:** lazy-load any new session-header weight. The bundle budget is at its ceiling (lesson from #2872).
- **Bash hook:** never a bare `cd web`; commit messages via `-F` file.
- **Tests:** run with `env -i PATH=$PATH HOME=/tmp/pytest-home-mcp LANG=C.UTF-8 VIBERPIT_DEV=1 PYTHONPATH=$PWD/src`, using the deploy venv python.

## Review Focus

1. **A session resumed after a repo rule changed** must start with the NEW rule. Its own earlier overrides still win. Pinned in Task 4: `test_resume_applies_current_rules_under_session_override`.
2. **A plugin server toggled from the session popover** (key `plugin:p:s`, short name shown) must persist under the full key, so the next build disables it. Pinned in Task 5: `test_toggle_persists_full_plugin_key`.
3. **A repo slug with a different case** (`Owner/Repo` vs `owner/repo`) must match one rule, because GitHub slugs are case-insensitive. Pinned in Task 2: `test_repo_rules_match_case_insensitively`.
4. **A rule for a server that was later deleted** must be ignored, not crash the build or show in the UI. Pinned in Task 3: `test_unknown_keys_in_rules_are_ignored`.
5. **Two sessions of the same account in different repos** must each get their own list. They share one `.claude.json` keyed by worktree, and neither may clobber the other's entry. Pinned in Task 4: `test_two_worktrees_same_account_keep_separate_lists`.

---

### Task 0: Stack the branch on sub-project 1

**Files:** none (git only).

- [ ] **Step 1: Find where sub-project 1 stands.** Run `gh api graphql -f query='{repository(owner:"steiner385",name:"viberpit"){pullRequest(number:2872){state mergedAt}}}'`.
- [ ] **Step 2: Rebase this branch.**
  - If #2872 is MERGED: `git fetch origin && git rebase origin/master` on `feat/mcp-repo-scope`.
  - Otherwise: `git fetch origin && git rebase origin/feat/mcp-live-status`, and note in the PR body that it stacks on #2872.
- [ ] **Step 3: Verify the base.** `git log --oneline -3` and `test -f server/mcp_health/probe.py && test -f src/viberpit/mcp_names.py` must succeed.

---

### Task 1: Real-binary verification (Principle III gate)

No production code. **If Q1 contradicts "Expected", STOP and report before Task 2.**

**Files:**
- Create: `docs/superpowers/spikes/2026-10-09-mcp-repo-scope-verification.md`
- Throwaway, not committed: `/tmp/mcp_scope_spike/spike.py`

- [ ] **Step 1: Make a scratch config dir.**
  - Copy ONE Anthropic account's cfg dir into `umask 077; mkdir -p /tmp/mcp_scope_spike/cfg`, with `cp -L` for `.claude.json` and `.credentials.json`.
  - Never write into `~/.claude-accounts/store/*` or a real `cfg/*` dir.
  - Add a working stdio server to the scratch `.claude.json` `mcpServers`: `{"command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"]}`, named `spike-ok`. If `npx` isn't available offline, pick any server already configured that reports `connected`.
- [ ] **Step 2: Write the spike** (it's throwaway).

```python
# /tmp/mcp_scope_spike/spike.py  — argv: cwd  disabled_csv  [toggle_name]
import asyncio, json, os, sys
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

cfg = os.environ["CLAUDE_CONFIG_DIR"]
cwd, disabled = os.path.abspath(sys.argv[1]), [s for s in sys.argv[2].split(",") if s]
p = os.path.join(cfg, ".claude.json")
d = json.load(open(p))
d.setdefault("projects", {}).setdefault(cwd, {}).update(
    {"hasTrustDialogAccepted": True, "disabledMcpServers": disabled})
json.dump(d, open(p, "w"))

async def statuses(c):
    for _ in range(10):
        st = (await c.get_mcp_status())["mcpServers"]
        if not any(s["status"] == "pending" for s in st):
            return st
        await asyncio.sleep(2)
    return st

async def main():
    async with ClaudeSDKClient(options=ClaudeAgentOptions(cwd=cwd, env={"CLAUDE_CONFIG_DIR": cfg})) as c:
        print("start", json.dumps([(s["name"], s["status"], s.get("scope")) for s in await statuses(c)]))
        if len(sys.argv) > 3:
            name = sys.argv[3]
            await c.toggle_mcp_server(name, True)
            print("after enable", json.dumps([(s["name"], s["status"]) for s in await statuses(c)]))
            await c.toggle_mcp_server(name, False)
            print("after disable", json.dumps([(s["name"], s["status"]) for s in await statuses(c)]))
    print("persisted", json.dumps(json.load(open(p))["projects"][cwd].get("disabledMcpServers")))

asyncio.run(main())
```

- [ ] **Step 3: Run it and record each answer.** Use `/home/tony/GitHub/viberpit-deploy/.venv-py314/bin/python -I`. Pass `cli_path` to the newest `~/.local/share/claude/versions/*` if the SDK can't find `claude`. Tee to `/tmp/mcp-scope-spike.log`.

| # | Question | Expected |
|---|---|---|
| 1 | `disabledMcpServers: ["spike-ok", "plugin:<p>:<s>"]` (one real plugin server key from a first run with an empty list) → status at start | both `disabled`, no `needs-auth`, no OAuth prompt |
| 2 | Exact key format for plugin servers: is `<p>` the plugin name without `@marketplace`? | `plugin:<plugin-name>:<server>` |
| 3 | `toggle_mcp_server("spike-ok", True)` while disabled → status | `connected` |
| 4 | `toggle_mcp_server("spike-ok", False)` → status | `disabled` |
| 5 | After the run, `projects[cwd].disabledMcpServers` in `.claude.json` | record it (does the CLI persist toggles?) |
| 6 | A project `.mcp.json` server in `cwd`: does `disabledMcpServers` disable it, or is `disabledMcpjsonServers` needed? (Put a `.mcp.json` in a scratch cwd.) | record it (input for 2b) |
| 7 | Toggling a name that isn't configured | record the exception type and message |

- [ ] **Step 4: Write it up and clean up.** Write the findings doc (table with Expected and Observed, redacted log excerpt, versions, and an "Implications" list naming any plan constraint a result changes). Run `rm -rf /tmp/mcp_scope_spike`. Commit with `git commit -F <file>`, subject `docs(spike): verify disabledMcpServers + toggle_mcp_server against CLI <version>`.

---

### Task 2: Tables, migration, store

**Files:**
- Modify: `server/db/models.py` (two models after `McpServerHealth`; `Session.mcp_overrides` column)
- Create: `server/db/migrations/versions/0079_mcp_repo_scope.py`
- Create: `server/mcp_scope/__init__.py` (empty), `server/mcp_scope/store.py`
- Modify: `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`), `server/tests/test_schema_reconcile_coverage.py` (`RECONCILE_EXEMPT`, only for NOT NULL columns without synthesizable defaults)
- Test: `server/tests/test_mcp_scope_store.py`

**Interfaces:**
- Produces:
  - `McpServerScope(server: str PK, default_enabled: bool, updated_at: datetime)`
  - `McpRepoRule(id PK, repo: str, server: str, enabled: bool, updated_at)` with `UniqueConstraint("repo","server", name="uq_mcp_repo_rule_key")`. `repo` is stored lower-cased.
  - `Session.mcp_overrides: dict | None`
  - In `server/mcp_scope/store.py`:
    - `async def server_defaults() -> dict[str, bool]`
    - `async def repo_rules(repo: str) -> dict[str, bool]`
    - `async def all_rules() -> list[dict]` (`[{repo, server, enabled}]`)
    - `async def set_default(server: str, enabled: bool) -> None`
    - `async def set_repo_rule(repo: str, server: str, enabled: bool) -> None`
    - `async def delete_repo_rule(repo: str, server: str) -> bool`
    - `async def delete_server(server: str) -> None` (deletes the scope row and every rule for that server)
    - `async def session_overrides(session_id: str) -> dict[str, bool]`
    - `async def set_session_override(session_id: str, server: str, enabled: bool) -> dict[str, bool]`

- [ ] **Step 1: Write the failing tests** (use the canonical `isolated_db` fixture)

```python
# server/tests/test_mcp_scope_store.py
import pytest
from server.mcp_scope import store


@pytest.mark.asyncio
async def test_defaults_roundtrip_and_upsert(isolated_db):
    await store.set_default("gh", False)
    await store.set_default("gh", True)
    assert await store.server_defaults() == {"gh": True}


@pytest.mark.asyncio
async def test_repo_rules_match_case_insensitively(isolated_db):
    await store.set_repo_rule("Owner/Repo", "gh", True)
    assert await store.repo_rules("owner/repo") == {"gh": True}
    await store.set_repo_rule("owner/REPO", "gh", False)          # same key, upsert
    assert await store.all_rules() == [{"repo": "owner/repo", "server": "gh", "enabled": False}]


@pytest.mark.asyncio
async def test_delete_server_drops_default_and_rules(isolated_db):
    await store.set_default("gh", False)
    await store.set_repo_rule("a/b", "gh", True)
    await store.delete_server("gh")
    assert await store.server_defaults() == {} and await store.all_rules() == []


@pytest.mark.asyncio
async def test_session_override_merges(isolated_db, make_session_row):   # helper: insert a minimal Session row; reuse the one other session-row tests use
    sid = await make_session_row()
    assert await store.set_session_override(sid, "gh", False) == {"gh": False}
    assert await store.set_session_override(sid, "plugin:p:s", True) == {"gh": False, "plugin:p:s": True}
    assert await store.session_overrides(sid) == {"gh": False, "plugin:p:s": True}
```

Find the existing helper that inserts a minimal `Session` row (`grep -rn "def .*session_row\|DbSession(" server/tests/conftest.py server/tests/*.py | head`) and reuse it. If none exists, define `make_session_row` as a fixture at the top of this file.

- [ ] **Step 2: Run them and confirm they fail** with `ImportError`.
- [ ] **Step 3: Implement**
  - **Models.**
    - Follow `McpServerHealth`'s style.
    - `updated_at` is NOT NULL. Set it explicitly from `select(db_utc_now())` in the store, like `server/mcp_health/store.py`.
    - Add `mcp_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)` to `Session`.
  - **Migration.** Model it on `0078_mcp_server_status.py`: `create_table` for both tables, plus `op.add_column('sessions', sa.Column('mcp_overrides', sa.JSON(), nullable=True))`. The downgrade reverses them.
  - **Store.** Write all upserts with `dialect_insert(...).on_conflict_do_update(...)` (helper in `server/db/__init__.py`, same pattern as `server/mcp_health/store.py`). Lower-case `repo` on every write and read.
  - **`set_session_override`.** Load the row, merge `{server: enabled}` into the existing dict (`None` → `{}`), assign a NEW dict so the JSON change is detected, and return it.
  - **Schema tests.** Add the models to `_REQUIRED_EXTRAS`. Add NOT NULL columns to `RECONCILE_EXEMPT` only if `test_schema_reconcile_coverage.py` demands it, and say so in the report.
- [ ] **Step 4: Run the tests and confirm they pass.** Run `server/tests/test_mcp_scope_store.py`, `test_schema_reconcile_coverage.py` and `test_postgres_compat.py`, then the Postgres compat run (see the `running-the-postgres-suite-locally` notes). Expected: PASS on both dialects.
- [ ] **Step 5: Commit** — "feat(db): MCP scope tables, repo rules and session overrides".

---

### Task 3: Pure resolver and configured-server keys

**Files:**
- Create: `server/mcp_scope/resolve.py`, `server/mcp_scope/servers.py`
- Test: `server/tests/test_mcp_scope_resolve.py`

**Interfaces:**
- Consumes: `server.library.scan.list_mcp_servers()` (entries have `name`, `source` = `"user"` or a plugin id, `readonly`) and Task 1's finding on the plugin key format.
- Produces:
  - `def effective_disabled(servers: list[str], *, server_defaults: dict[str, bool], repo_rules: dict[str, bool], session_overrides: dict[str, bool]) -> list[str]` (sorted)
  - `def server_key(entry: dict) -> str`: `entry["name"]` for user servers, `f"plugin:{plugin_name}:{entry['name']}"` for plugin servers, where `plugin_name` derives from `entry["source"]` per the Task 1 finding (e.g. `source.split("@", 1)[0]`)
  - `def configured_server_keys() -> list[str]` (sync, file reads; callers run it via `account_store_io` or `to_thread`)

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_mcp_scope_resolve.py
import pytest
from server.mcp_scope.resolve import effective_disabled
from server.mcp_scope.servers import server_key

ALL = ["a", "b", "c", "plugin:p:d"]


@pytest.mark.parametrize("defaults,rules,overrides,expected", [
    ({}, {}, {}, []),                                         # implicit: everything on
    ({"a": False}, {}, {}, ["a"]),                            # default off
    ({"a": False}, {"a": True}, {}, []),                      # repo rule beats default
    ({}, {"b": False}, {}, ["b"]),                            # repo rule turns a server off
    ({"a": False}, {"a": True}, {"a": False}, ["a"]),         # session beats repo
    ({}, {"b": False}, {"b": True}, []),                      # session beats repo (on)
    ({"plugin:p:d": False}, {}, {}, ["plugin:p:d"]),          # plugin keys are first-class
])
def test_precedence(defaults, rules, overrides, expected):
    assert effective_disabled(ALL, server_defaults=defaults, repo_rules=rules,
                              session_overrides=overrides) == expected


def test_unknown_keys_in_rules_are_ignored():
    assert effective_disabled(["a"], server_defaults={"gone": False}, repo_rules={"gone": True},
                              session_overrides={"gone": False}) == []


def test_output_is_sorted_and_deduplicated():
    assert effective_disabled(["b", "a", "a"], server_defaults={"a": False, "b": False},
                              repo_rules={}, session_overrides={}) == ["a", "b"]


def test_server_key_user_and_plugin():
    assert server_key({"name": "gh", "source": "user"}) == "gh"
    assert server_key({"name": "railway", "source": "railway@claude-plugins-official"}) == "plugin:railway:railway"
```

Adjust the plugin expectation to the exact format Task 1 recorded. If Task 1 found something else, follow it and cite the spike doc in a comment.

- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement**

```python
# server/mcp_scope/resolve.py
"""Effective MCP enablement for one session (spec 2026-10-09 repo-scope §2) — the single
source of truth for both the CLI's disabledMcpServers list and the UI's hints."""
from __future__ import annotations


def effective_disabled(servers: list[str], *, server_defaults: dict[str, bool],
                       repo_rules: dict[str, bool], session_overrides: dict[str, bool]) -> list[str]:
    off: set[str] = set()
    for key in set(servers):
        for source in (session_overrides, repo_rules, server_defaults):
            if key in source:
                if not source[key]:
                    off.add(key)
                break
    return sorted(off)
```

`servers.py` implements `server_key` per the spike, and `configured_server_keys()` returns `sorted({server_key(e) for e in scan.list_mcp_servers()["servers"]})`. Check `list_mcp_servers`'s current signature on the base branch: it may take `status_rows`, which this caller doesn't need, so pass nothing.

- [ ] **Step 4: Run the tests and confirm they pass.**
- [ ] **Step 5: Commit** — "feat(mcp): effective-disabled resolver and configured server keys".

---

### Task 4: Apply the list at every Claude driver build (plus the kill switch)

**Files:**
- Modify: `src/viberpit/configdir.py:140-157` (`trust_project(..., *, disabled_mcp=None)`)
- Modify: `server/sessions/orchestrator.py`: `_driver_config_dir` (~:2236-2259) and its four callers (~:5212 spawn, ~:18727 ensure_driver/resume, ~:19147 adopt, ~:22109 reassign); plus the trust call in the docstring example at ~:1766 if it's live code
- Modify: `server/config.py` (both settings classes) and `server/api/settings.py` (row): `mcp_scope_enabled`
- Create: `server/mcp_scope/apply.py`
- Test: `tests/test_configdir_disabled_mcp.py` (core suite) and `server/tests/test_mcp_scope_apply.py`

**Interfaces:**
- Consumes: Task 2's store, Task 3's `effective_disabled` and `configured_server_keys`
- Produces:
  - `configdir.trust_project(name: str, cwd: str, *, disabled_mcp: list[str] | None = None) -> None`
  - `async def disabled_for_session(*, repo: str | None, session_id: str | None) -> list[str] | None`. It returns `None` when the kill switch is off or on any read failure, at DEBUG.
  - `_driver_config_dir(engine, account, cwd, kind, *, disabled_mcp: list[str] | None = None)`. The callers compute it with `await disabled_for_session(repo=rec.repo, session_id=rec.id)` (or the new session's values at spawn) BEFORE calling it.
  - Settings `get_settings().mcp_scope_enabled: bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_configdir_disabled_mcp.py
import json
from viberpit import configdir, paths


def _cfg(tmp_path, monkeypatch, name="alice"):
    monkeypatch.setattr(paths, "account_cfg_dir", lambda n: tmp_path / n)
    (tmp_path / name).mkdir()
    return tmp_path / name / ".claude.json"


def test_writes_list(tmp_path, monkeypatch):
    p = _cfg(tmp_path, monkeypatch)
    configdir.trust_project("alice", "/w/1", disabled_mcp=["gh", "plugin:p:s"])
    assert json.loads(p.read_text())["projects"]["/w/1"]["disabledMcpServers"] == ["gh", "plugin:p:s"]


def test_none_leaves_existing_key(tmp_path, monkeypatch):
    p = _cfg(tmp_path, monkeypatch)
    p.write_text(json.dumps({"projects": {"/w/1": {"disabledMcpServers": ["x"]}}}))
    configdir.trust_project("alice", "/w/1")
    assert json.loads(p.read_text())["projects"]["/w/1"]["disabledMcpServers"] == ["x"]


def test_two_worktrees_same_account_keep_separate_lists(tmp_path, monkeypatch):
    p = _cfg(tmp_path, monkeypatch)
    configdir.trust_project("alice", "/w/1", disabled_mcp=["a"])
    configdir.trust_project("alice", "/w/2", disabled_mcp=["b"])
    projects = json.loads(p.read_text())["projects"]
    assert projects["/w/1"]["disabledMcpServers"] == ["a"] and projects["/w/2"]["disabledMcpServers"] == ["b"]
```

`server/tests/test_mcp_scope_apply.py` tests `disabled_for_session` with `isolated_db`, monkeypatching `configured_server_keys`:
- `test_resume_applies_current_rules_under_session_override`: default `gh` off, repo rule on for `a/b`, session override `plugin:p:s` off → `["plugin:p:s"]`. Change the rule to off → `["gh", "plugin:p:s"]`.
- Scratch session (`repo=None`) ignores repo rules.
- Kill switch off → `None`.
- A store read raising → `None`, with no WARNING logged (assert on caplog).

Add one orchestrator-level test using the existing spawn/resume harness (`grep -ln "_driver_config_dir\|trust_project" server/tests/*.py`). It should assert that `trust_project` receives `disabled_mcp` on spawn AND on the ensure_driver/resume path.

- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement**
  - **`trust_project`.** When `disabled_mcp is not None`, set `"disabledMcpServers": list(disabled_mcp)` in the entry; otherwise keep any existing value via `**existing_entry`.
  - **`apply.py`:**

```python
"""Compute the CLI disabledMcpServers list for a session build (spec repo-scope §3)."""
from __future__ import annotations

import logging

from server.config import get_settings
from server.mcp_scope import store
from server.mcp_scope.resolve import effective_disabled
from server.mcp_scope.servers import configured_server_keys
from server.sessions.account_store_io import account_store_io   # use the real import path — grep it

log = logging.getLogger(__name__)


async def disabled_for_session(*, repo: str | None, session_id: str | None) -> list[str] | None:
    if not get_settings().mcp_scope_enabled:
        return None
    try:
        servers = await account_store_io(configured_server_keys, default=None)
        if servers is None:
            return None
        return effective_disabled(
            servers,
            server_defaults=await store.server_defaults(),
            repo_rules=await store.repo_rules(repo) if repo else {},
            session_overrides=await store.session_overrides(session_id) if session_id else {},
        )
    except Exception:
        log.debug("mcp scope: rules unavailable repo=%s session=%s", repo, session_id, exc_info=True)
        return None
```

  - **`account_store_io`.** Check its real module and signature (Task 5 of sub-project 1 used it in `server/mcp_health/probe.py`) and match them.
  - **Orchestrator.** `_driver_config_dir` stays synchronous and gains `disabled_mcp`, forwarded to `configdir.trust_project(account, cwd, disabled_mcp=disabled_mcp)`. Each of the four async callers computes the list just before the call. At spawn, the session row may not exist yet, so pass `session_id=None` there (a new session has no overrides). Codex returns early and ignores the list.
  - **Settings:**
    - `server/config.py`, pydantic class: `mcp_scope_enabled: bool = Field(default=True, alias="VIBERPIT_MCP_SCOPE_ENABLED")`.
    - Dataclass fallback: the same `os.environ.get(...)` pattern as `mcp_status_enabled`.
    - `server/api/settings.py` row: `("mcp_scope_enabled", "VIBERPIT_MCP_SCOPE_ENABLED", "Per-repo MCP server scope and live session toggles")`.
- [ ] **Step 4: Run the tests and confirm they pass**: the core test with `PYTHONPATH=$PWD/src`, plus the server tests, ruff and mypy.
- [ ] **Step 5: Commit** — "feat(mcp): write per-session disabledMcpServers at every driver build".

---

### Task 5: Live toggle (driver → orchestrator → RPC → route)

**Files:**
- Modify: `server/sessions/engine.py` (Protocol `toggle_mcp_server`)
- Modify: `server/sessions/claude_driver.py` (implement), `server/codex/driver.py` (raise `NotImplementedError`)
- Modify: `server/sessions/sdk_contract.py` and `server/tests/fakes/fake_sdk.py` (`toggle_mcp_server` recorded in `.toggled`)
- Modify: `server/sessions/orchestrator.py` (`mcp_toggle`, next to `mcp_reconnect` ~:17942)
- Modify: `server/sessions/remote.py`, `server/ipc/orchestrator_rpc.py` (dedicated handler like `mcp_reconnect` ~:369), `server/sessions/fleet_orchestrator.py` (`_PER_SESSION` += `mcp_toggle`), `server/sessions/facade.py`
- Modify: `server/sessions/session_actions.py` (`mcp_toggle` action, audited `session.mcp_toggle`), `server/api/sessions.py` (route next to `session_mcp_reconnect` ~:1927)
- Modify: both facade test doubles (`server/tests/test_session_actions.py:FakeOrch`, `server/tests/test_sessions_actions_api.py:_FakeSessions`)
- Test: `server/tests/test_session_mcp_toggle.py`, plus driver cases in `server/tests/test_claude_driver_mcp_status.py`

**Interfaces:**
- Consumes: Task 2 `store.set_session_override`, and Task 3 `configured_server_keys`/`server_key` for validation
- Produces:
  - Driver: `async def toggle_mcp_server(self, name: str, enabled: bool) -> None`. It raises `RuntimeError("session not live")` on a dead client and re-polls status after.
  - `orch.mcp_toggle(session_id: str, server: str, enabled: bool) -> list[dict]`. It raises `LookupError` (not live) or `ValueError(msg[:300])` (SDK/driver failure), saves the override BEFORE returning, and returns fresh `mcp_status` wire dicts.
  - Route `POST /api/sessions/{id}/mcp/{server}/toggle` with body `{"enabled": bool}`, returning `{"servers": [...]}`.

- [ ] **Step 1: Write the failing tests.** Copy the harness from `server/tests/test_session_mcp_routes.py`, which sub-project 1 built for reconnect.
  - `test_toggle_calls_driver_persists_override_and_returns_status`
  - `test_toggle_persists_full_plugin_key`: the route receives `plugin:p:s` (URL-encoded); the override is saved under `plugin:p:s`.
  - `test_toggle_not_live_404`
  - `test_toggle_sdk_plain_exception_409`: the driver raises plain `Exception("Server not found: zz")` → 409 `MCP_TOGGLE_FAILED`, through the real facade AND the daemon-handler → RemoteOrchestrator round trip, with no WARNING logged.
  - `test_toggle_unknown_server_400`: not in `configured_server_keys()`.
  - `test_toggle_kill_switch_404`
  - `test_toggle_audited`
  - Driver: `test_toggle_calls_sdk_then_repolls` (FakeSDKClient `.toggled == [("a", False)]`) and `test_toggle_dead_client_raises`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.** Mirror `mcp_reconnect` exactly: the exception mapping (including the `"session not live"` → `LookupError` case), the daemon handler returning error-shaped dicts, `RemoteOrchestrator` re-raising, the `_PER_SESSION` entry, and the facade pass-through. Then:
  - The orchestrator saves the override with `await store.set_session_override(session_id, server, enabled)` AFTER the driver call succeeds, so a failed toggle never records a state the CLI doesn't have.
  - The route validates `server` against `configured_server_keys()`, run off-loop, before calling the action.
  - The route checks the kill switch first.
- [ ] **Step 4: Run the tests and confirm they pass**, along with `test_fleet_orchestrator.py`, `test_session_actions*.py`, `test_sessions_actions_api.py` and `test_ipc_cancel_does_not_strand.py`. Then run ruff and mypy.
- [ ] **Step 5: Commit** — "feat(sessions): live MCP server toggle with persisted session override".

---

### Task 6: Rules API (edge) + known-repos validation

**Files:**
- Modify: `server/api/repos.py`. Extract the gh-list-with-last-good-cache logic from `list_github_repos` into `async def known_repo_slugs(app_state) -> set[str]` (lower-cased `nameWithOwner`). `list_github_repos` keeps its behaviour and calls the shared fetch, so there is no parallel implementation.
- Modify: `server/api/library.py` (four routes)
- Test: `server/tests/test_mcp_scope_api.py`

**Interfaces:**
- Consumes: Task 2 store, Task 3 `configured_server_keys`
- Produces:
  - `GET /api/library/mcp/scope` → `{"servers": {key: {"defaultEnabled": bool}}, "rules": [{"repo", "server", "enabled"}]}`. Every configured key appears, with `defaultEnabled: true` when it has no row.
  - `PUT /api/library/mcp/scope/{server}` with `{"defaultEnabled": bool}` → 204
  - `PUT /api/library/mcp/scope/{server}/repos/{owner}/{name}` with `{"enabled": bool}` → 204
  - `DELETE /api/library/mcp/scope/{server}/repos/{owner}/{name}` → 204, or 404 if absent
  - `DELETE /api/library/mcp/{name}` (existing) also calls `store.delete_server` for that key

- [ ] **Step 1: Write the failing tests.**
  - GET merges defaults for unconfigured keys.
  - The PUT default round-trips.
  - The PUT repo rule round-trips, and slug case is normalised.
  - The DELETE rule returns 404 when absent.
  - Unknown server → 400 `unknown_server`. Unknown repo → 400 `unknown_repo`, with `known_repo_slugs` monkeypatched.
  - Traversal-shaped segments (`..`, `.`, `a%2Fb`, an empty owner) → 400 or 404, never reaching the store. A guard monkeypatch makes the store raise if called.
  - Kill switch off → 404 `mcp_scope_disabled`.
  - The audit row `library_mcp_scope_saved` carries `{server, repo}` only.
  - Deleting a server also deletes its scope rows.
  - `list_github_repos` behaviour is unchanged. Keep its existing tests green.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
  - Validate `owner` and `name` each against GitHub's slug charset `^[A-Za-z0-9_.-]+$`, excluding `.` and `..`.
  - Check membership in `known_repo_slugs` and `configured_server_keys()`. Run that off-loop with `asyncio.to_thread`.
  - Server path params may be `plugin:p:s`. Validate them by membership only, never as a path.
- [ ] **Step 4: Run the tests and confirm they pass**, along with the existing `test_library*` and repos tests, then ruff and mypy.
- [ ] **Step 5: Commit** — "feat(library): MCP scope rules API with known-repo validation".

---

### Task 7: Web data layer

**Files:**
- Modify: `web/src/api/types.ts`, `web/src/api/hooks.ts`
- Test: `web/src/api/__tests__/mcpScope.test.ts` (or the folder sibling hook tests use)

**Interfaces:**
- Produces:
  - `interface McpScope { servers: Record<string, { defaultEnabled: boolean }>; rules: { repo: string; server: string; enabled: boolean }[] }`
  - `useMcpScope()`, query key `QK.mcpScope`
  - `useSetMcpDefault()`: `{server, defaultEnabled}`
  - `useSetMcpRepoRule()`: `{server, repo, enabled}`
  - `useDeleteMcpRepoRule()`: `{server, repo}`
  - All three mutations invalidate `QK.mcpScope`. URLs encode `server` with `encodeURIComponent`; `repo` splits into `owner/name`, each encoded.
  - `useToggleSessionMcp(sessionId)`: `{server, enabled}`. It writes the returned `{servers}` into `['session-mcp', id]`, as `useReconnectSessionMcp` does.

- [ ] **Step 1: Write the failing tests.** URL shapes, including `plugin:p:s` encoding and a `repo` split. Cache invalidation and set-on-success.
- [ ] **Step 2: Run them and confirm they fail.** `(cd web && npx vitest run <file>)`
- [ ] **Step 3: Implement**, following the existing hook patterns.
- [ ] **Step 4: Run** `(cd web && npm run typecheck && npm run lint && npm run test)`. That's the FULL suite, teed to `/tmp`, checking `${PIPESTATUS[0]}`.
- [ ] **Step 5: Commit** — "feat(web): MCP scope and session toggle hooks".

---

### Task 8: Settings › MCP scope controls

**Files:**
- Create: `web/src/features/settings/sections/McpScopeControl.tsx`
- Modify: `web/src/features/settings/sections/McpSection.tsx` (render the control per server row, as a sibling of the card button; never nest interactive elements)
- Test: `web/src/features/settings/sections/McpScopeControl.test.tsx`

**Interfaces:**
- Consumes: Task 7 hooks, plus `useGithubRepos()` for the repo picker (existing)
- Produces: `<McpScopeControl serverKey={string} />`
  - A segmented control: "On for all repos" / "Off by default".
  - An "Exceptions" list showing each rule as `repo — on|off`, with a remove button.
  - An "Add exception" repo picker plus an on/off choice.
  - The hint "Applies to new and resumed sessions."
  - When scope is disabled, `GET` returns 404 `mcp_scope_disabled` and the control renders nothing.

- [ ] **Step 1: Write the failing tests.**
  - The default toggle calls `useSetMcpDefault`.
  - Adding an exception calls `useSetMcpRepoRule`.
  - Remove calls `useDeleteMcpRepoRule`.
  - It renders nothing on 404 `mcp_scope_disabled`, detected via `apiErrorCode`.
  - The hint text is present.
  - The picker offers only `useGithubRepos` slugs.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.** McpSection's rows key servers by `name` plus `source`, so compute the key for each row with the same rule as the server's `server_key`. Add a small pure `serverKey(s: LibraryMcpServer)` in `mcpStatus.ts` beside `shortName`, with a test; it must match Task 3's Python rule exactly. Rows wrap at 375 px (`flex-wrap`), and the picker has a `<label>`.
- [ ] **Step 4: Run** the full web gates (typecheck, lint, full test).
- [ ] **Step 5: Commit** — "feat(web): per-repo scope controls in Settings › MCP".

---

### Task 9: Session popover switches + `n/m` count

**Files:**
- Modify: `web/src/features/sessions/McpStatusReadout.tsx` (lazy-loaded via `DeferredMcpStatusReadout`; keep it that way)
- Test: `web/src/features/sessions/McpStatusReadout.test.tsx`

**Interfaces:**
- Consumes: `useToggleSessionMcp`, `McpServerState.status`
- Produces:
  - A switch per server, `role="switch"`, `aria-checked`, `aria-label="<shortName> enabled"`. It is checked when `status !== 'disabled'`, and toggling calls the mutation with the FULL `name`.
  - The label count is `MCP {connected}/{enabledTotal}`, where `enabledTotal` excludes `disabled` servers. The readout still renders nothing when there are no servers.
  - Toggle errors go through `extractMutationErrorMessage(err, 'Toggle failed')`, inline.
  - The kill switch: a 404 `mcp_scope_disabled` from the first toggle hides the switches for the rest of the session view.

- [ ] **Step 1: Write the failing tests.**
  - Switch state follows status.
  - A toggle sends the full `plugin:p:b` name.
  - The count excludes disabled servers: `[a connected, b disabled]` → `MCP 1/1`.
  - The error message renders.
  - Switches hide after a 404 `mcp_scope_disabled`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full web gates, then `(cd web && npm run build && npm run budget)`. The budget must pass without raising ceilings. If it doesn't, keep the switch code inside the lazy readout chunk, then remove `web/dist`.
- [ ] **Step 5: Commit** — "feat(web): live MCP server switches in the session readout".

---

### Task 10: Docs, gates, PR

**Files:**
- Modify: `docs/product/features/mcp-status.md`. Add a "Scope" section covering rules, precedence, live toggle, the kill switch, and "applies to new and resumed sessions".
- Modify: the spec. Set Status to implemented and add an "As built" section with the Task 1 findings.

- [ ] **Step 1: Run the gates** (with `tee`, inside a `systemd-run --user` unit with the test-slot acquire block):
  - `ruff check src/ server/`
  - `mypy server/ --ignore-missing-imports`
  - `PYTHONPATH=$PWD/src pytest tests/`
  - `pytest server/tests/ -n auto --dist loadfile --timeout=300`
  - the Postgres suite
  - `python scripts/brand_ratchet.py`
  - `(cd web && npm run typecheck && npm run lint && npm run test && npm run build && npm run budget)`, then remove `web/dist`
- [ ] **Step 2: Check for in-flight work.** Look for open PRs touching these files (the GraphQL query in core.md) and confirm `0079` is still free on `origin/master`.
- [ ] **Step 3: Open the PR.** Push, then `gh pr create --body-file` (note the stacking on #2872 if it hasn't merged), confirm it's not a draft, then `gh pr merge <#> --squash --auto`.
