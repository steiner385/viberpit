# D-16c residuals (#3314) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push plugin changes into running sessions on every host (opt-in), verify a Directory install's review state against the live Directory API, pin marketplace clones to the DNS answer the SSRF check saw, and record the same-host tampering ruling.

**Architecture:** The live push is a raw `reload_plugins` control request on the Claude driver (the `apply_flag_settings` seam), enumerated per host by the orchestrator, fanned out by the fleet like `plugin_op_everywhere`, exposed as `POST /library/plugins/reload`, and surfaced as a checkbox on the consent card plus a section button. The Directory check adds one pure comparison to `plugin_policy` and one httpx fetch module. DNS pinning adds a resolved pin to the add policy and a per-run `GIT_CONFIG_GLOBAL` file in `plugin_cli._env`.

**Tech Stack:** Python FastAPI + claude-agent-sdk control channel, httpx, git `http.curloptResolve`; React + TanStack Query + vitest.

**Spec:** `docs/superpowers/specs/2026-11-05-d16c-residuals-design.md` (and the probe record `docs/superpowers/specs/2026-11-05-d16c-residuals-verification.md`, written in Task 8).

## Global Constraints

- Never pass `-y`, `--accept-command` or any flag in `plugin_cli.FORBIDDEN_FLAGS`; never write managed settings; no root.
- Expected refusals log at INFO (a WARNING auto-files an issue). Use `log.info` for every policy/busy/unavailable refusal.
- `server/library/plugin_cli.py` stays the only writer of plugin state; the reload writes nothing.
- Every new route that changes live state needs `require_human(..., trust_local_host=False)` via `_plugin_human_gate` and an audit row.
- New env/settings take the `VIBERPIT_` prefix (none are added here).
- Web copy is React text only; untrusted strings (session names, plugin names, messages) go through `clip()`.
- Run the four gates before the PR: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, focused `pytest server/tests/...`; web `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`, `npm run budget` in `web/`.
- Tests run as `env -i HOME=<tmp> PATH=$PATH VIBERPIT_DEV=1 <main>/.venv/bin/python -m pytest …` with `VIBERPIT_SESSIOND_*`/`VIBERPIT_DAEMON_ROLE` unset. The venv is `/home/tony/GitHub/viberpit/.venv` (or `/home/tony/GitHub/viberpit-deploy/.venv`); `web/node_modules` is a symlink to the main checkout's.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and are written to a file and passed with `-F`.

## Review Focus

1. A reload sent while a turn is running would swap tools under the model — the driver must refuse `RELOAD_BUSY` whenever `rewind_busy()` is true (Task 1 test `test_reload_refuses_while_busy`).
2. A host whose daemon predates the RPC must show up as one failed row, not vanish from the result (Task 3 test `test_reload_everywhere_labels_dead_and_old_hosts`).
3. A forged cache that maps a reviewed Directory id to a different repository URL must be refused even though the id's review state is `done` (Task 5 test `test_directory_listing_refuses_a_repointed_source`).
4. A Directory API outage must refuse the install (503), never fall back to the cache (Task 5 test `test_directory_install_fails_closed_when_the_api_is_down`).
5. The pin file must still include the user's global git config, or a private https marketplace loses its credential helper (Task 6 test `test_env_git_pin_writes_a_global_config_that_includes_the_users`).

---

### Task 1: Driver `reload_plugins` + `plugin_reload` helpers

**Files:**
- Create: `server/sessions/plugin_reload.py`
- Modify: `server/sessions/claude_driver.py` (next to `apply_flag_settings`, ~line 2401)
- Test: `server/tests/test_plugin_reload_driver.py`

**Interfaces:**
- Consumes: `ClaudeAgentDriver.rewind_busy()`, `_is_control_error_reply`, `CLIConnectionError`, `server/tests/test_fast_mode.py::FakeControl` + `_driver` pattern.
- Produces (`server/sessions/plugin_reload.py`):
  - `RELOAD_UNAVAILABLE = "RELOAD_UNAVAILABLE"`, `RELOAD_BUSY = "RELOAD_BUSY"`, `RELOAD_REFUSED = "RELOAD_REFUSED"`, `RELOAD_FAILED = "RELOAD_FAILED"`, `RELOAD_CONTROL_TIMEOUT_S = 60.0`
  - `class ReloadError(Exception)` with `.code: str`, `.message: str`
  - `def request(hold_on_cache_impact: bool) -> dict` → `{"subtype": "reload_plugins", "hold_on_cache_impact": bool}`
  - `def outcome(reply: object) -> dict` → `{"outcome": "held"|"applied", "plugins": [str], "errorCount": int, "cacheImpact": dict|None}` (`held` iff `reply.get("held") is True`; `plugins` = each `plugins[i]["name"]` that is a str; `cacheImpact` = `reply["cache_impact"]` when a dict else absent key; a non-dict reply → applied with empty lists and errorCount 0)
  - `ClaudeAgentDriver.reload_plugins(self, *, hold_on_cache_impact: bool = True) -> dict` (the `outcome(...)` dict; raises `ReloadError`)

- [ ] **Step 1: Write the failing tests** in `server/tests/test_plugin_reload_driver.py`, reusing `FakeControl`/`_driver` from `test_fast_mode.py` (import them):

```python
async def test_reload_sends_the_raw_control_request_and_reduces_the_reply():
    control = FakeControl({"commands": [], "agents": [], "plugins": [{"name": "hello", "path": "/p"}],
                           "mcpServers": [], "error_count": 0})
    d = _driver(control); await d.start()
    try:
        assert await d.reload_plugins(hold_on_cache_impact=True) == {
            "outcome": "applied", "plugins": ["hello"], "errorCount": 0}
    finally:
        await d.aclose()
    assert control.sent == [{"subtype": "reload_plugins", "hold_on_cache_impact": True}]
    assert control.timeouts == [pr.RELOAD_CONTROL_TIMEOUT_S]

async def test_reload_reports_a_held_reply():  # held: True + cache_impact passthrough → outcome "held", cacheImpact kept
async def test_reload_refuses_while_busy():    # d._turn_active = True → ReloadError code RELOAD_BUSY, nothing sent
async def test_reload_refuses_when_not_live(): # before start() → RELOAD_UNAVAILABLE
async def test_reload_maps_control_error_and_no_reply():  # FakeControl raising a control error → RELOAD_REFUSED; a TimeoutError → RELOAD_FAILED
def test_outcome_is_total_over_garbage():      # outcome(None) == {"outcome": "applied", "plugins": [], "errorCount": 0}
```

Also assert, with `caplog`, that no refusal logs at WARNING (mirror `_no_warnings` from `test_fast_mode.py`).

- [ ] **Step 2: Run to verify failure** — `pytest server/tests/test_plugin_reload_driver.py -q` → ImportError on `plugin_reload`.
- [ ] **Step 3: Implement** `plugin_reload.py` (pure) and `ClaudeAgentDriver.reload_plugins` modelled on `apply_flag_settings`: refuse unavailable (no client / stream ended / no `_send_control_request`), refuse busy via `rewind_busy()`, send `request(...)` with `timeout=RELOAD_CONTROL_TIMEOUT_S`, map exceptions like `apply_flag_settings` (`_is_control_error_reply` → `RELOAD_REFUSED`, `CLIConnectionError` → `RELOAD_UNAVAILABLE`, else `RELOAD_FAILED`), log INFO `driver.reload_plugins session=%s outcome=%s plugins=%d`.
- [ ] **Step 4: Run** the test file → PASS; `ruff check server/sessions/plugin_reload.py server/sessions/claude_driver.py`; `mypy server/sessions/plugin_reload.py --ignore-missing-imports`.
- [ ] **Step 5: Commit** `feat(plugins): driver reload_plugins control request (#3314)`.

### Task 2: Orchestrator `reload_plugins_live` + RPC registration

**Files:**
- Modify: `server/sessions/orchestrator.py` (after `plugin_op`, ~line 22306)
- Modify: `server/ipc/orchestrator_rpc.py` `_SIMPLE` (after `"plugin_op"`, line 398)
- Test: `server/tests/test_plugin_reload_orchestrator.py`

**Interfaces:**
- Consumes: Task 1 `ClaudeAgentDriver.reload_plugins`, `ReloadError`; `self._drivers`, `self._records`, `self_host_name()` (already imported in orchestrator).
- Produces: `SessionOrchestrator.reload_plugins_live(self, hold_on_cache_impact: bool = True) -> list[dict]`, rows `{"sessionId", "name", "host", "outcome": "applied"|"held"|"busy"|"skipped"|"failed", "failureCode"?, "message"?, "plugins"?, "cacheImpact"?}`. `busy` ⇐ `ReloadError.code == RELOAD_BUSY`; `skipped` ⇐ driver has no `reload_plugins` attribute (Codex) or `RELOAD_UNAVAILABLE`; `failed` ⇐ `RELOAD_REFUSED`/`RELOAD_FAILED`/any other exception (`failureCode` = code or `"error_internal"`). Sessions run concurrently with `asyncio.gather`; a driver without a record is skipped silently. Rows sorted by `name`.

- [ ] **Step 1: Write the failing tests** (build a `SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=lambda n: None)`, create two sessions through `create_session`, then monkeypatch each driver's `reload_plugins` with a stub: one returns an applied outcome, one raises `ReloadError(RELOAD_BUSY, ...)`; add a third fake driver object with no `reload_plugins` into `orch._drivers` with a record):

```python
async def test_reload_live_reports_every_live_session_on_this_host(): ...
    rows = await orch.reload_plugins_live(hold_on_cache_impact=True)
    assert [(r["outcome"], r.get("failureCode")) for r in rows] == [("applied", None), ("busy", "RELOAD_BUSY"), ("skipped", None)]  # in name order
    assert all(r["host"] == self_host_name() for r in rows)
async def test_reload_live_isolates_an_unexpected_exception():  # a stub raising RuntimeError → that row failed/error_internal, others fine
def test_reload_rpc_is_registered():  # "reload_plugins_live" in the _SIMPLE list (read orchestrator_rpc source like test_plugin_op_rpc_runs_the_wrapper_on_the_daemon does)
```

- [ ] **Step 2: Run** → AttributeError.
- [ ] **Step 3: Implement** the method and add `"reload_plugins_live"` to `_SIMPLE` with a one-line comment (`# D-16c follow-up (#3314): push reload_plugins into this host's live Claude sessions`).
- [ ] **Step 4: Run** the test file + `server/tests/test_plugin_marketplace.py::test_plugin_op_rpc_runs_the_wrapper_on_the_daemon` → PASS; ruff + mypy on the two files.
- [ ] **Step 5: Commit** `feat(plugins): per-host live reload of plugins (#3314)`.

### Task 3: Fleet `reload_plugins_everywhere`

**Files:**
- Modify: `server/sessions/fleet_orchestrator.py` (after `plugin_op_everywhere`, ~line 948)
- Test: `server/tests/test_plugin_reload_fleet.py`

**Interfaces:**
- Consumes: `FleetOrchestrator._fanout_names`, `self._router`, `plugin_cli.host_outcome`/`unreachable` (reuse their error mapping: `daemon_outdated`, `host_timeout`, `host_unreachable`, `daemon_error`, `error_internal`).
- Produces: `FleetOrchestrator.reload_plugins_everywhere(self, hold_on_cache_impact: bool = True) -> list[dict]`: the concatenation of every reachable host's `reload_plugins_live` rows; a host that fails yields exactly one row `{"host": name, "outcome": "failed", "failureCode": <code>, "message": ...}` (no `sessionId`). A host reply that is not a list is one `unparseable_result` row.

- [ ] **Step 1: Write the failing tests** with the `_HostOrch`/`_Router` fakes from `test_plugin_marketplace.py` (give `_HostOrch` a `reload_plugins_live` that returns `self.behaviour` the same way `plugin_op` does; copy the two fakes into the new file rather than importing a test module):

```python
async def test_reload_everywhere_labels_dead_and_old_hosts():
    # dobby → [row applied], rocky → DaemonUnavailable, old → DaemonError("UnknownMethod", ...), slow → DaemonTimeout, gone → not reachable
    rows = await fleet.reload_plugins_everywhere()
    assert {(r["host"], r["outcome"], r.get("failureCode")) for r in rows} == {("dobby","applied",None), ("rocky","failed","host_unreachable"), ("old","failed","daemon_outdated"), ("slow","failed","host_timeout"), ("gone","failed","host_unreachable")}
async def test_reload_everywhere_rejects_a_non_list_reply():  # a host returning {"x": 1} → one unparseable_result row
```

- [ ] **Step 2: Run** → AttributeError.
- [ ] **Step 3: Implement** mirroring `plugin_op_everywhere` (a small local `_one` that awaits `self._router.get(name).reload_plugins_live(hold_on_cache_impact=...)` inside the same `try/except` ladder as `host_outcome`; factor the ladder into `plugin_cli.host_failure(host, exc) -> dict | None` if that avoids duplicating it — keep one source of truth).
- [ ] **Step 4: Run** the new file + `test_plugin_marketplace.py -k fleet` → PASS; ruff/mypy.
- [ ] **Step 5: Commit** `feat(plugins): fleet-wide live reload fan-out (#3314)`.

### Task 4: Route `POST /library/plugins/reload` + web hook, consent checkbox, section button

**Files:**
- Modify: `server/api/library.py` (after `uninstall_plugin`, ~line 295)
- Modify: `web/src/api/types.ts` (after `PluginOpResult`, ~line 961), `web/src/api/pluginHooks.ts`, `web/src/features/settings/sections/plugins/pluginText.ts`, `web/src/features/settings/sections/plugins/ConsentDialog.tsx`, `web/src/features/settings/sections/PluginsSection.tsx`
- Create: `web/src/features/settings/sections/plugins/SessionOutcomes.tsx`
- Test: `server/tests/test_plugin_marketplace.py` (extend; reuse the `env` fixture and `_Fleet` — give `_Fleet` a `reload_plugins_everywhere(hold_on_cache_impact)` recording calls and returning `self.reload_reply`), `web/src/features/settings/sections/PluginsSection.test.tsx` (extend)

**Interfaces:**
- Consumes: Task 3 `reload_plugins_everywhere`, `_plugin_human_gate`, `record_audit`.
- Produces:
  - Route body model `ReloadBody(holdOnCacheImpact: bool = True)`; response `{"op": "reload_plugins", "ok": bool, "sessions": list[dict]}` where `ok` = rows non-empty and no row has `outcome == "failed"`; audit type `library_plugins_reloaded`, detail `{"holdOnCacheImpact": bool, "sessions": rows}`.
  - `web/src/api/types.ts`: `PluginSessionOutcome { host: string; sessionId?: string; name?: string; outcome: 'applied'|'held'|'busy'|'skipped'|'failed'; failureCode?: string; message?: string; plugins?: string[]; cacheImpact?: Record<string, unknown> }`, `PluginReloadResult { op: 'reload_plugins'; ok: boolean; sessions: PluginSessionOutcome[] }`.
  - `pluginHooks.ts`: `useReloadPlugins()` → `usePluginOp<{ holdOnCacheImpact: boolean }, PluginReloadResult>(…post('/library/plugins/reload', body))`.
  - `pluginText.ts`: `RELOAD_LABEL = 'Also reload running sessions now'`, `RELOAD_HELP = 'A session that has started talking and whose tools would change keeps its plugins until it restarts, so its prompt cache is kept. Sessions mid-turn are skipped.'`, `RELOAD_BUTTON = 'Reload running sessions'`.
  - `SessionOutcomes({ result }: { result: PluginReloadResult })`: a `role="group" aria-label="Session results"` list, one `<li>` per row: `clip(name ?? host)`, a Badge per outcome (`applied` success, `held` warning, `busy`/`skipped` neutral, `failed` danger), code/message like `HostOutcomes`. Zero rows → "No running session to reload."
  - `ConsentDialog`: a checkbox (`aria-label={RELOAD_LABEL}`) default unchecked, disabled while pending; on install/update `onSuccess` with `result.ok || any host ok`, if checked call `reload.mutate({ holdOnCacheImpact: true })` and render `<SessionOutcomes>` under the host outcomes; the Install/Update button's `note` stays `RESTART_COPY`.
  - `PluginsSection`: a "Reload running sessions" `Button` (secondary, sm) in the header row next to the intro copy; it calls the hook and renders `<SessionOutcomes>` plus a failure line under the intro.

- [ ] **Step 1: Server tests** (extend `test_plugin_marketplace.py`):

```python
def test_reload_route_fans_out_and_audits(env):  # POST {"holdOnCacheImpact": False} → fleet.calls == [("reload", False)], 200 body op/ok/sessions, audit row library_plugins_reloaded
def test_reload_route_ok_is_false_on_a_failed_row(env)
```
  Add `("POST", "/api/library/plugins/reload", {})` to the `test_every_mutation_is_human_only` parametrize list, and assert in `test_kill_switch` (or a new case) that the reload route still answers 200 with the switch off.
- [ ] **Step 2: Run** → 404/405.
- [ ] **Step 3: Implement the route.**
- [ ] **Step 4: Run** `pytest server/tests/test_plugin_marketplace.py -q` → PASS; ruff/mypy.
- [ ] **Step 5: Web tests** (extend `PluginsSection.test.tsx`; `api.post` is a `vi.fn()` — route on URL):

```ts
it('Install with "Also reload running sessions now" ticked posts the reload after the install and lists each session', …)
  // tick the checkbox, click Install → posts: [install, reload with {holdOnCacheImpact: true}]; dialog shows "worker-1" with "held" and "alpha-session" with "applied"
it('the reload checkbox is off by default and nothing is reloaded when it stays off', …)
it('the section button reloads running sessions and lists outcomes; zero sessions says so', …)
```
- [ ] **Step 6: Run** `(cd web && npx vitest run src/features/settings/sections/PluginsSection.test.tsx)` → FAIL, then implement types, hook, copy, `SessionOutcomes`, dialog and section changes.
- [ ] **Step 7: Run** the file → PASS; then `(cd web && npm run typecheck && npm run lint)`.
- [ ] **Step 8: Commit** `feat(plugins): live reload route, consent opt-in and section button (#3314)`.

### Task 5: Directory review verified against the live API

**Files:**
- Create: `server/library/directory_api.py`
- Modify: `server/library/plugin_policy.py` (new code + pure check), `server/api/library.py::_check_install` (~line 214)
- Test: `server/tests/test_plugin_marketplace.py` (extend), `server/tests/test_directory_api.py`

**Interfaces:**
- Produces:
  - `plugin_policy.DIRECTORY_UNAVAILABLE = "PLUGIN_DIRECTORY_UNAVAILABLE"`; `plugin_policy.REASON_DIRECTORY_MISMATCH = "directory_mismatch"`.
  - `plugin_policy.check_directory_listing(cached: Mapping, live: object) -> None`: raises `PolicyRefusal(PLUGIN_UNREVIEWED, …, reason=REASON_DIRECTORY_MISMATCH)` unless `live` is a dict, `live["id"] == cached["id"]`, `is_reviewed(live)`, and `_normalize_url(live.source.repository.url) == _normalize_url(cached.source.repository.url)` with both present (a listing whose `source` is `{"type": "directory"}` with no repository on **both** sides passes on id + review alone).
  - `directory_api.DIRECTORY_URL = "https://api.anthropic.com/api/directory/plugins"`, `DIRECTORY_TIMEOUT_S = 15.0`; `async def fetch_listing(listing_id: str) -> dict | None` (GET `{DIRECTORY_URL}/{listing_id}` with `httpx.AsyncClient(timeout=…)`, header `User-Agent: viberpit`; returns the JSON dict on 200, `None` on any status, transport error, timeout or non-dict body; `listing_id` must match `^[A-Za-z0-9._-]{1,120}$` else `None`). Module-level `_fetch = fetch_listing` is NOT the seam; tests monkeypatch `directory_api.fetch_listing`.
  - `_check_install`: after the cache check passes for a Directory plugin, `live = await directory_api.fetch_listing(listing["id"])`; `None` → `HTTPException(503, {"code": DIRECTORY_UNAVAILABLE, "message": "The Anthropic Directory could not be reached to verify this listing's review; try again."})` (log INFO); else `plugin_policy.check_directory_listing(listing, live)` via `_policy_http`.

- [ ] **Step 1: Pure tests** in `test_plugin_marketplace.py`:

```python
def test_directory_listing_passes_when_id_review_and_repo_match()
def test_directory_listing_refuses_a_repointed_source()      # live url github.com/a/b vs cached github.com/evil/b → PLUGIN_UNREVIEWED, reason directory_mismatch
def test_directory_listing_refuses_unreviewed_live_or_wrong_id_or_garbage()  # parametrize: live state none; live id differs; live = "nope"
```
  Route tests (the `env` fixture: `monkeypatch.setattr(directory_api, "fetch_listing", <async stub>)` — default stub returns the cached listing itself so existing tests keep passing; set that default inside the fixture):
```python
def test_directory_install_verifies_the_listing_live(env, monkeypatch)   # stub records the id "id_reviewed"; install 200
def test_directory_install_refuses_a_forged_cache(env, monkeypatch)      # stub returns state none → 409 PLUGIN_UNREVIEWED reason directory_mismatch, fleet.calls == []
def test_directory_install_fails_closed_when_the_api_is_down(env, monkeypatch)  # stub returns None → 503 PLUGIN_DIRECTORY_UNAVAILABLE, fleet.calls == []
def test_curated_marketplace_installs_never_call_the_directory(env, monkeypatch)
```
  `test_directory_api.py`: `fetch_listing` against `httpx.MockTransport` (200 dict → dict; 404 → None; a `httpx.ConnectError` → None; a bad id → None without a request).
- [ ] **Step 2: Run** → failures.
- [ ] **Step 3: Implement.** `fetch_listing` accepts an optional `transport` kwarg for the mock.
- [ ] **Step 4: Run** `pytest server/tests/test_plugin_marketplace.py server/tests/test_directory_api.py -q` → PASS; ruff/mypy.
- [ ] **Step 5: Commit** `fix(plugins): verify Directory review state live before install (#3314)`.

### Task 6: DNS pinning for marketplace add/update

**Files:**
- Modify: `server/library/plugin_policy.py` (resolve pin), `server/library/plugin_cli.py` (`_env`, `build_argv`/`run_op`)
- Test: `server/tests/test_plugin_marketplace.py` (extend), `server/tests/test_plugin_cli_contract.py` (one new `slow` test behind `VIBERPIT_TEST_NETWORK=1`)

**Interfaces:**
- Produces:
  - `plugin_policy.resolved_pin(source: str) -> str | None`: for an `https://` source that `check_marketplace_source(source)` accepts, `"<host>:443:<addr1>,<addr2>…"` (addresses from `_resolve(host)`, sorted, de-duplicated, host lower-cased); `None` for `owner/repo`. Raises the same `PolicyRefusal` the check raises. Resolution happens once: refactor `_check_host` to return `(why, addrs)` internally, or add `_resolved_host_check(host) -> list[str]` that the check and the pin share — one DNS call per op.
  - `plugin_policy.pin_for_normalized(normalized: str | None) -> str | None`: `https:<host>[:port]<path>` → `resolved_pin("https://<host>[:port]<path>")`; anything else `None`.
  - `plugin_cli.git_pin(op: str, args: dict) -> str | None`: `marketplace_add` → `resolved_pin(args["source"])`; `marketplace_update` → `pin_for_normalized(args.get("expected_source"))`; else `None`. A `PolicyRefusal` propagates (run_op turns it into a `failed` outcome with the refusal code, before the CLI runs).
  - `plugin_cli._env(nonce: str, *, git_pin: str | None = None) -> dict[str, str]` plus `plugin_cli.git_pin_config(pin: str) -> str` returning exactly `"[include]\n\tpath = ~/.gitconfig\n[http]\n\tcurloptResolve = <pin>\n"`. With a pin, `_env` writes it to `tempfile.NamedTemporaryFile(prefix="viberpit-git-pin.", suffix=".gitconfig", delete=False)` and sets `GIT_CONFIG_GLOBAL`; `run_op` removes the file in a `finally`.

- [ ] **Step 1: Tests** (`test_plugin_marketplace.py`, using the autouse `_dns` fixture):

```python
def test_resolved_pin_for_https_sources_and_none_for_github(_dns)   # _dns["git.example.com"] = ["203.0.113.5", "203.0.113.4"] → "git.example.com:443:203.0.113.4,203.0.113.5"; "acme/m" → None
def test_resolved_pin_refuses_what_the_check_refuses(_dns)          # internal address → PolicyRefusal
def test_git_pin_for_ops()                                          # add → pin; update with expected_source "https:git.example.com/m" → pin; install → None; update with github: → None
def test_env_git_pin_writes_a_global_config_that_includes_the_users(monkeypatch, tmp_path)  # GIT_CONFIG_GLOBAL set, file text == git_pin_config(pin); without a pin no key
def test_run_op_removes_the_pin_file_and_passes_it_to_the_cli(shared, monkeypatch)  # fake runner captures env; file existed during the run, gone after
def test_marketplace_update_with_an_internal_source_is_refused_before_the_cli(shared, monkeypatch)
```
  Contract (`test_plugin_cli_contract.py`, `@pytest.mark.skipif(os.environ.get("VIBERPIT_TEST_NETWORK") != "1", reason="network")`): `test_marketplace_add_clone_honours_the_pin`: run the raw CLI `marketplace add https://github.com/octocat/Hello-World` with `plugin_cli._env(nonce, git_pin="github.com:443:127.0.0.1")` → outcome `failed`, `failureCode != "manifest_missing"`, and `"unable to access"` in the message.
- [ ] **Step 2: Run** → failures.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest server/tests/test_plugin_marketplace.py -q` → PASS; `VIBERPIT_TEST_NETWORK=1 pytest server/tests/test_plugin_cli_contract.py -q -k pin` → PASS on dobby; ruff/mypy.
- [ ] **Step 5: Commit** `fix(plugins): pin marketplace clones to the checked DNS answer (#3314)`.

### Task 7: Binary contract test for `reload_plugins`

**Files:**
- Create: `server/tests/test_plugin_reload_cli_contract.py`

**Interfaces:**
- Consumes: `test_settings_cli_contract._Cli` (import), `test_plugin_cli_contract.BINARY`, the local marketplace fixture shape from `test_plugin_cli_contract.sandbox` (copy the marketplace-writing lines into a local helper taking `tmp_path`).
- Produces: a `slow` test that never runs a turn.

- [ ] **Step 1: Write** `test_reload_plugins_reply_shape_and_hold_passthrough`: build a throwaway cfg dir; run `claude plugin marketplace add <mkt> --scope user --json` and `claude plugin install hello@<mkt> --scope user --json` with `subprocess.run` and the `_Cli`-style env (same `HOME`/`CLAUDE_CONFIG_DIR`); then open `_Cli(binary)` **with that cfg dir** (extend `_Cli.__init__` with an optional `cfg: str | None` kwarg — modify `test_settings_cli_contract.py` minimally), `initialize`, send `plugin_reload.request(True)` through `cli.ok(...)`, and assert: reply has `commands`, `plugins`, `error_count`; `plugin_reload.outcome(reply)["outcome"] == "applied"` (no output tokens → never held, per the spec); `"hello"` in the outcome's `plugins`; some command name startswith `hello:`; a second reload with `hold_on_cache_impact: False` also `applied`; a malformed request `{"subtype": "reload_plugins", "hold_on_cache_impact": "yes"}` → `cli.ask(...)["subtype"] == "error"`.
- [ ] **Step 2: Run** `pytest server/tests/test_plugin_reload_cli_contract.py -q` with `VIBERPIT_CLAUDE_BIN=/mnt/ssk-ssd/tony/cache/.local/share/claude/versions/2.1.318` → PASS (skip without a binary).
- [ ] **Step 3: Commit** `test(plugins): reload_plugins contract against the installed binary (#3314)`.

### Task 8: Docs and probe record

**Files:**
- Create: `docs/superpowers/specs/2026-11-05-d16c-residuals-verification.md`
- Modify: `docs/product/features/plugin-marketplace.md` (§ How it works, § Security, § Not built), `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` lines 365 and 434 (the "Follow-up (not built)" / "Follow-up:" clauses), `docs/superpowers/specs/2026-11-04-d16-verification.md` Gaps (the `reload_plugins` line → resolved, pointer to the new record)

- [ ] **Step 1: Write the probe record** from the design doc's "Facts" section, in the format of `2026-11-04-d16-verification.md` §0 (binary 2.1.318 sha prefix `708c77b204db3008`, offsets: `reload_plugins` schema `@283016339`, handler `@320846464`, `$te` `@304823890`, `B_` = `costLedger.totalOutputTokens()` `@280189494`, `Jg` exemption `@284153823`, `GCr` `@284110796`), the curl results (status 200, 100/page, `next_page`, `/{id}` 200, `?slug=`/`?id=` not filters), the git probe (the CLI's own `GIT_CONFIG_COUNT=40`, `GIT_CONFIG_GLOBAL` honoured, pin to 127.0.0.1 → clone fails before GitHub; `followRedirects=false` breaks GitLab), and Gaps (a held reply was never observed: needs output tokens).
- [ ] **Step 2: Update the feature doc**: How it works gains "Reload running sessions (opt-in)" and "Directory installs are verified live"; Security: rewrite the review-cache bullet (browse badge from cache, install verified live; a host's own re-check still reads its cache), rewrite the DNS-rebinding bullet (closed for add/update by `http.curloptResolve`; plugin-entry git sources and auto-update clones are not pinned), keep same-host tampering with the ruling text from the design doc § 4; Not built: drop the reload line; keep "#3314 holds the same-host ruling".
- [ ] **Step 3: Update the parity spec** clauses and the D-16 verification Gaps line.
- [ ] **Step 4: Commit** `docs(plugins): D-16c residuals — probe record and feature doc (#3314)`.
