# MCP auth overhaul — Phase 5: runner distribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runner gets every MCP grant's public view from the edge over the `creds_sync` push
it already receives, writes it into its own account dirs, and hands a terminal `claude mcp
login` made on the runner back to the edge — so MCP auth on rocky no longer depends on the
sshfs mount of dobby's `~/.claude-accounts`.

**Architecture:** The edge's `FleetOrchestrator.push_credentials` (every 30 s, #2629) carries
two new fields beside the Claude envelopes: `mcp_bundles` (the shared store's public view,
keyed exactly as the store) and `mcp_drop` (the grant reconcile's live purge keys). The runner
daemon caches them host-locally (`McpViewStore`, beside the envelope cache) and, on a worker,
`creds.read_shared_mcp()` answers from that cache, so `compose` and `sync_cfg_mcp_oauth` need
no change. When the runner's account tree is **provably local** (not a network mount), the
`creds_sync` handler also publishes the view into each local account dir and returns the
dirs' refresh-token-bearing bundles as `mcp_offers`, which the edge merges under its store
lock. While the tree is still the sshfs mount the runner only caches: the edge's own publish
covers the shared tree (designer call 1).

**Tech Stack:** Python; existing daemon RPC (`server/ipc/daemon.py:register_rpc`,
`server/sessions/remote.py`), `server/fsmounts.py` / `server/obs/logdir.py:is_shared_dir`,
`viberpit.credential_envelope.state_dir`.

**Spec:** `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md` §3 (runners) and
§6 (runner capture), plus "Calls made by the designer" 1 and 4. Broker spec
`docs/superpowers/specs/2026-10-02-claude-credential-broker-design.md` §4 names the gap this
fills. Issue #3287.

**Precondition:** #3295 has merged (it has).

## Global Constraints

- Constitution II: a runner never holds an MCP refresh token longer than the window between
  a terminal login and the next publish. The pushed view is public-view only; the runner
  strips `refreshToken` again on receipt.
- Single writer stands: only the refresh authority writes `store/mcp.shared.json`. A runner
  offer is merged by the **edge** through `creds.merge_shared_mcp` (newer-expiry-wins; an
  explicit login is not implied). Parity with the edge's own dir capture: no (issuer,
  resource) check is added here, the reconcile tick files misplaced keys (designer call 4 is
  deferred with the same reasoning the edge's capture already relies on).
- Wire names (RPC `creds_sync`): request `mcp_bundles: dict[str, dict]`, `mcp_drop:
  list[str]`; response `mcp_offers: dict[str, dict]`. All optional both ways: an old edge
  sends neither and an old runner returns none; nothing may raise on absence.
- The runner writes account dirs only when `fleet_role.is_worker()` and
  `server.obs.logdir.is_shared_dir(paths.cfg_root())` is False (fails closed: an unreadable
  mount table means "do not write").
- Host-local cache: `credential_envelope.state_dir() / "mcp-view.json"`, `0600`, written
  only when the content changed. No lease: a stale access token in a dir is the same
  harmless state the dir would be in anyway.
- Never log token values. Logs carry keys, counts and host names.
- All account-dir and mount-table IO in the daemon handler runs in `asyncio.to_thread`
  (#2852).
- New settings, if any, take the `VIBERPIT_` prefix (none are planned).
- Spec §3's "a dead grant clears the dir's stale access token" is **not** in this plan: the
  cleared shape is indistinguishable from a never-signed-in discovery stub in the store, so
  publishing it would wipe a live access-token-only terminal login. Record it in the PR as
  left open.

## Review Focus

1. **An old edge pushes to a new runner** (no `mcp_bundles`): acks as before, no dir
   touched, no offer. Task 4: `test_handler_without_mcp_fields_is_envelopes_only`.
2. **A new edge pushes to an old runner** (response without `mcp_offers`): the push still
   returns acks and merges nothing. Task 3: `test_push_tolerates_runner_without_offers`.
3. **The runner's account tree is the sshfs mount:** the view is cached, nothing is written,
   nothing offered. Task 4: `test_shared_mount_tree_is_never_written`.
4. **A pushed bundle carries a refresh token by mistake:** the cache never holds it. Task 1:
   `test_sync_strips_refresh_token`.
5. **A terminal login in a runner dir:** offered on the next sync and, once the view holds
   that key, stripped from the dir on the following publish — even if the edge kept an
   older bundle. Task 4: `test_local_login_is_offered_then_stripped`.

---

### Task 1: `McpViewStore` and the worker's `read_shared_mcp`

**Files:**
- Create: `src/viberpit/mcp_view_store.py`
- Modify: `src/viberpit/creds.py:163-166` (`read_shared_mcp`)
- Test: `tests/test_mcp_view_store.py` (new), `tests/test_creds_mcp_refresh.py`

**Interfaces:**
- Consumes: `viberpit.credential_envelope.state_dir()`, `viberpit.creds.public_view`,
  `viberpit.fleet_role.is_worker()`.
- Produces:
  - `class McpViewStore(directory: Path | None = None)` with
    `sync(bundles: Mapping[str, object], drop: Iterable[str] = ()) -> bool` (True when the
    cache file was rewritten), `bundles() -> dict | None` (deep copy; None until the first
    sync), `drop_keys() -> frozenset[str]`.
  - `runner_view() -> McpViewStore` (process-wide, lazy) and `reset_runner_view() -> None`.
  - `creds.read_shared_mcp()` on a worker returns `runner_view().bundles()` when it is not
    None, else the file as today.

- [ ] **Step 1: Write the failing tests** in `tests/test_mcp_view_store.py` (unittest or
  pytest style, matching `tests/test_credential_envelope.py`):

```python
def test_sync_persists_and_reloads(tmp_path):
    store = McpViewStore(tmp_path)
    assert store.bundles() is None
    assert store.sync({"k1": {"serverName": "aws", "accessToken": "at", "expiresAt": 5}}, ["old"]) is True
    assert McpViewStore(tmp_path).bundles() == {"k1": {"serverName": "aws", "accessToken": "at", "expiresAt": 5}}
    assert McpViewStore(tmp_path).drop_keys() == frozenset({"old"})
    assert oct(os.stat(tmp_path / "mcp-view.json").st_mode & 0o777) == "0o600"

def test_sync_strips_refresh_token(tmp_path):
    store = McpViewStore(tmp_path)
    store.sync({"k1": {"accessToken": "at", "refreshToken": "rt"}})
    assert store.bundles() == {"k1": {"accessToken": "at"}}
    assert "rt" not in (tmp_path / "mcp-view.json").read_text()

def test_sync_writes_only_on_change(tmp_path):
    store = McpViewStore(tmp_path)
    assert store.sync({"k1": {"accessToken": "a"}}) is True
    assert store.sync({"k1": {"accessToken": "a"}}) is False

def test_non_dict_bundles_are_ignored(tmp_path):
    store = McpViewStore(tmp_path)
    store.sync({"k1": "junk", "k2": {"accessToken": "a"}})
    assert store.bundles() == {"k2": {"accessToken": "a"}}

def test_bundles_returns_a_copy(tmp_path):
    store = McpViewStore(tmp_path)
    store.sync({"k1": {"accessToken": "a"}})
    store.bundles()["k1"]["accessToken"] = "x"
    assert store.bundles()["k1"]["accessToken"] == "a"
```

  And in `tests/test_creds_mcp_refresh.py`, using that file's existing `VIBERPIT_HOME`
  fixture idiom and `monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")`,
  `monkeypatch.setenv("VIBERPIT_CLAUDE_CREDS_DIR", str(tmp_path / "state"))` and
  `reset_runner_view()` before and after:

```python
def test_worker_reads_pushed_view_when_held(...):
    # store file holds {"file": {...}}; runner_view().sync({"pushed": {"accessToken": "p"}})
    assert read_shared_mcp() == {"pushed": {"accessToken": "p"}}

def test_worker_falls_back_to_file_when_never_synced(...):
    assert read_shared_mcp() == {"file": {...}}

def test_primary_ignores_the_runner_view(...):
    # role unset (primary), a synced view present: read_shared_mcp() == the file
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=$PWD/src .venv-or-main/.venv/bin/python -m pytest tests/test_mcp_view_store.py tests/test_creds_mcp_refresh.py -q`
Expected: ImportError / AssertionError on the new tests only.

- [ ] **Step 3: Implement `src/viberpit/mcp_view_store.py`.** Mirror `EnvelopeStore`'s
  shape: a `threading.Lock`, `_load()` tolerant of a missing or torn file (`OSError`,
  `ValueError`, `KeyError`, `TypeError` → empty), `_persist()` with `os.open(..., 0o600)` +
  `os.replace`, parent `0o700`. Payload `{"bundles": {...}, "drop": [...]}`. `sync` keeps
  only dict values, applies `creds.public_view` (lazy import inside the function —
  `creds` will import this module), compares against the held state and persists only on
  change. Module docstring: why there is no lease, and that the file holds access tokens
  (hence `0600`).

- [ ] **Step 4: Change `creds.read_shared_mcp()`** to consult the view on a worker
  (`fleet_role.is_worker()` is already imported there). Update its docstring: "on a worker
  that has received a push, the authority's view as pushed (#3287); the file otherwise".

- [ ] **Step 5: Run the two test files and `tests/test_creds.py`, `tests/test_configdir.py`**

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/viberpit/mcp_view_store.py src/viberpit/creds.py tests/test_mcp_view_store.py tests/test_creds_mcp_refresh.py
git commit -F /tmp/phase5-t1-msg.txt   # "feat(mcp): runner-side MCP view cache; a worker reads the pushed view (#3287)"
```

---

### Task 2: Pure `offerable_mcp` and `merge_mcp_bundles` in `creds.py`

**Files:**
- Modify: `src/viberpit/creds.py:191-250` (`merge_shared_mcp`, `capture_cfg_mcp`)
- Test: `tests/test_creds_mcp_refresh.py`

**Interfaces:**
- Produces:
  - `creds.offerable_mcp(dir_mcp: dict, store: dict, *, drop: frozenset[str] = frozenset()) -> dict`
    — the bundles a dir would offer the store: exactly the `held` rule now inside
    `capture_cfg_mcp` (refresh-bearing always; refresh-less only for a key the store lacks
    and that is not in `drop`). Pure, no role check.
  - `creds.merge_mcp_bundles(existing: dict, incoming: dict, *, authenticated_keys: set[str] | None = None) -> dict`
    — the per-key policy fold now inside `merge_shared_mcp.merge`. Pure.
  - `capture_cfg_mcp` and `merge_shared_mcp` keep their signatures and now call these.

- [ ] **Step 1: Write the failing tests**

```python
def test_offerable_mcp_rule():
    store = {"have": {"accessToken": "s"}}
    dir_mcp = {"have": {"accessToken": "d"},                 # store has it, no refresh: not offered
               "rt": {"accessToken": "d", "refreshToken": "r"},  # always offered
               "new": {"accessToken": "d"},                  # store lacks it: offered
               "dropped": {"accessToken": "d"}}              # refresh-less + in drop: not offered
    assert offerable_mcp(dir_mcp, store, drop=frozenset({"dropped"})) == {
        "rt": dir_mcp["rt"], "new": dir_mcp["new"]}

def test_merge_mcp_bundles_is_the_store_policy():
    existing = {"k": {"accessToken": "a", "expiresAt": 10}}
    assert merge_mcp_bundles(existing, {"k": {"accessToken": "b", "expiresAt": 5}}) == existing
    assert merge_mcp_bundles(existing, {"k": {"accessToken": "b", "expiresAt": 5}},
                             authenticated_keys={"k"})["k"]["accessToken"] == "b"
    assert merge_mcp_bundles(existing, {"k": "junk"}) == existing
```

- [ ] **Step 2: Run to verify they fail** (NameError on import).

- [ ] **Step 3: Extract both functions** and route `capture_cfg_mcp` / `merge_shared_mcp`
  through them. No behaviour change: the existing capture and merge tests in
  `tests/test_creds_mcp_refresh.py` and `tests/test_creds.py` must stay green unchanged.

- [ ] **Step 4: Run `tests/test_creds_mcp_refresh.py tests/test_creds.py tests/test_configdir.py`** — PASS.

- [ ] **Step 5: Commit** — "refactor(mcp): expose the dir-offer rule and bundle merge as pure helpers (#3287)".

---

### Task 3: Edge push carries the MCP view and merges runner offers

**Files:**
- Modify: `server/mcp_health/grants.py:121-126` (public `purge_keys`)
- Modify: `server/sessions/remote.py:1501-1503` (`creds_sync` signature)
- Modify: `server/sessions/fleet_orchestrator.py:929-962` (`push_credentials`)
- Test: `server/tests/test_creds_broker_transport.py`, `server/tests/test_creds_push_unreachable_debounce.py`
  (its `_Runner.creds_sync` double must accept the new kwargs), `server/tests/test_mcp_grants.py`

**Interfaces:**
- Consumes: `creds.read_shared_mcp`, `creds.public_view`, `creds.merge_shared_mcp` (Task 2
  leaves its signature intact).
- Produces:
  - `grants.purge_keys(store: dict, *, clock: Callable[[], float] = time.monotonic) -> frozenset[str]`
    — the live tombstones not back in *store*; the public face of `_live_tombstones`.
  - `RemoteOrchestrator.creds_sync(self, envelopes: list[dict], lease_s: float, mcp_bundles: dict | None = None, mcp_drop: list[str] | None = None) -> dict`
    passing both through to `self._client.call("creds_sync", ...)` **only when not None**
    (so the wire to an old daemon is unchanged when the edge has nothing to send — but the
    edge always sends them once this lands; the "only when not None" rule exists for tests
    and future callers).
  - `push_credentials` reads the store once per tick (`asyncio.to_thread`), sends
    `mcp_bundles={k: public_view(v) for dict v}` and `mcp_drop=sorted(purge_keys(store))`,
    and for each runner response merges `res.get("mcp_offers")` when it is a non-empty
    dict via `await asyncio.to_thread(creds.merge_shared_mcp, offers)`, logging
    `fleet.creds_sync host=%s offered mcp bundle(s) keys=%s` at INFO. A merge failure is
    logged WARNING and does not affect the acks.

- [ ] **Step 1: Write the failing tests** in `server/tests/test_creds_broker_transport.py`
  (extend `_Runner.creds_sync` to `(self, envelopes, lease_s, **kw)` recording `kw`; and in
  `test_creds_push_unreachable_debounce.py` make its double accept `**kw`):

```python
def test_push_carries_mcp_public_view_and_drop(monkeypatch):
    # store: {"k": {"accessToken": "a", "refreshToken": "r", "expiresAt": 1}, "junk": 3}
    # grants.purge_keys -> {"gone"}
    # assert ok.calls[0] kw == {"mcp_bundles": {"k": {"accessToken": "a", "expiresAt": 1}}, "mcp_drop": ["gone"]}
    # assert "r" not in json.dumps(kw)

def test_push_merges_runner_offers(monkeypatch, caplog):
    # runner answers {"acks": {...}, "mcp_offers": {"k2": {"accessToken": "x", "refreshToken": "y"}}}
    # merge_shared_mcp monkeypatched to record; assert called once with that dict
    # assert "offered mcp bundle(s) keys=['k2']" in caplog.text and "y" not in caplog.text

def test_push_tolerates_runner_without_offers(monkeypatch):
    # runner answers {"acks": {...}} only -> merge never called, acks returned
```

  And in `server/tests/test_mcp_grants.py`:

```python
def test_purge_keys_is_live_tombstones_minus_present():
    # seed grants._tombstones = {"a": now+100, "b": now-1}; purge_keys({"a": {...}}) == frozenset()
    # purge_keys({}) == {"a"} (b expired)
```

- [ ] **Step 2: Run them to verify they fail.**

- [ ] **Step 3: Implement** the three changes. Keep the imports of `creds` and `grants`
  lazy inside `push_credentials`, as `mint_envelopes` already is.

- [ ] **Step 4: Run** `server/tests/test_creds_broker_transport.py server/tests/test_creds_push_unreachable_debounce.py server/tests/test_mcp_grants.py server/tests/test_fleet_orchestrator.py` — PASS.

- [ ] **Step 5: Commit** — "feat(mcp): creds_sync carries the MCP public view and merges runner offers (#3287)".

---

### Task 4: Runner handler caches the view, publishes locally, returns offers

**Files:**
- Create: `server/ipc/runner_mcp.py`
- Modify: `server/ipc/creds_rpc.py`
- Test: `server/tests/test_runner_mcp_view.py` (new)

**Interfaces:**
- Consumes: `McpViewStore` / `runner_view()` (Task 1), `creds.offerable_mcp`,
  `creds.merge_mcp_bundles` (Task 2), `creds.sync_cfg_mcp_oauth`, `creds.read_json`,
  `configdir.CREDENTIALS_NAME`, `server.obs.logdir.is_shared_dir`, `paths.cfg_root()`,
  `fleet_role.is_worker()`.
- Produces:
  - `runner_mcp.owns_account_dirs(*, mounts: Mounts | None = None) -> bool` — worker AND
    `cfg_root()` provably local.
  - `runner_mcp.sync_local_dirs(*, mounts: Mounts | None = None) -> dict` — returns the
    offers. Blocking; callers thread it. When `owns_account_dirs` is False or the view has
    never synced, returns `{}` without touching the filesystem (beyond the mount table).
    Otherwise for every child dir of `cfg_root()` holding `CREDENTIALS_NAME`: read its
    `mcpOAuth`, fold `offerable_mcp(dir_mcp, view, drop=view.drop_keys())` into the offers
    with `merge_mcp_bundles`, then `sync_cfg_mcp_oauth(dir, drop=view.drop_keys())`. Each
    dir's failure is contained (DEBUG with `exc_info`). One INFO line per call when
    `written or offers`: `runner mcp view published dirs=%d offered=%d` (counts only).
  - `creds_sync` handler: after the envelope sync, if `"mcp_bundles" in p`:
    `runner_view().sync(p["mcp_bundles"], p.get("mcp_drop") or [])` then
    `offers = await asyncio.to_thread(sync_local_dirs)`; response gains `"mcp_offers": offers`
    only when `offers` is non-empty. Without `mcp_bundles` the response is exactly
    `{"acks": acks}` as today.

- [ ] **Step 1: Write the failing tests** in `server/tests/test_runner_mcp_view.py`. Fixture:
  `VIBERPIT_HOME=tmp/home`, `VIBERPIT_CLAUDE_CREDS_DIR=tmp/state`,
  `VIBERPIT_DAEMON_ROLE=worker`, `reset_runner_view()` + `ce.reset_runner_store()` around
  each test, `cfg/<acct>/.credentials.json` written with `creds.write_json_secure`. Mount
  tables: `LOCAL = [("/", "ext4")]`, `SHARED = [("/", "ext4"), (str(home), "fuse.sshfs")]`.

```python
def test_handler_without_mcp_fields_is_envelopes_only(...):
    res = asyncio.run(app.state.rpc["creds_sync"]({"envelopes": [_wire()], "lease_s": 60}))
    assert res == {"acks": {"a": 1}}; dir file unchanged; runner_view().bundles() is None

def test_view_is_published_into_local_dirs(...):
    # two account dirs; push mcp_bundles={"k": {...accessToken "at", expiresAt 9e12}}; mounts=LOCAL (monkeypatch runner_mcp.is_shared_dir)
    # both dirs' mcpOAuth["k"] == the bundle; claudeAiOauth untouched; res has no "mcp_offers"

def test_drop_keys_are_purged_from_local_dirs(...):
    # dir holds refresh-less "old"; push mcp_drop=["old"] -> "old" gone from dir

def test_local_login_is_offered_then_stripped(...):
    # dir holds {"k": {"accessToken": "d", "refreshToken": "rt", "expiresAt": 9e12}}; view has no "k"
    # first sync -> res["mcp_offers"] == {"k": that bundle}; dir still holds it (view lacks "k")
    # second sync with mcp_bundles={"k": {"accessToken": "edge", "expiresAt": 1}} (edge kept an OLDER one)
    #   -> dir "k" has no refreshToken; dir "k" accessToken is "d"? NO: publish_mcp replaces a
    #      refresh-bearing dir copy with the public view, so accessToken == "edge". Assert that.

def test_shared_mount_tree_is_never_written(...):
    # mounts=SHARED; dir holds a refresh-bearing bundle; push a view
    # runner_view().bundles() == the view (cached); dir file byte-identical; no "mcp_offers"

def test_primary_never_publishes_from_a_push(...):
    # role unset: sync_local_dirs() == {} and no dir written even with mounts=LOCAL

def test_owns_account_dirs_fails_closed_on_unknown_mounts(...):
    # worker, mounts=[] -> False
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement `server/ipc/runner_mcp.py` and extend `creds_rpc.py`.** Module
  docstring cites spec §3/§6, #3287, and designer call 1 (mounted tree → cache only).

- [ ] **Step 4: Run** `server/tests/test_runner_mcp_view.py server/tests/test_creds_broker_transport.py server/tests/test_ipc_serve.py` — PASS.

- [ ] **Step 5: Commit** — "feat(mcp): runner caches the pushed MCP view, publishes locally, offers terminal logins (#3287)".

---

### Task 5: Docs

**Files:**
- Modify: `docs/product/features/mcp-status.md` ("Token refresh", after **Single writer**)
- Modify: `docs/runbooks/rocky-session-host.md` §4.0
- Modify: `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md` "Delivery order" item 5 (plan link)

- [ ] **Step 1: `mcp-status.md`** — add a **Runners** paragraph: the view rides `creds_sync`
  every 30 s; cache path; a worker's `read_shared_mcp` is the pushed view; local publish +
  offers only when the account tree is provably local (`is_shared_dir`); while mounted the
  edge's publish covers it; offers merge under the edge's lock with the ordinary policy;
  the refresh token leaves the runner dir on the publish after the view holds the key.
- [ ] **Step 2: runbook §4.0** — one paragraph after the Claude envelope one: MCP public
  views ride the same push into `$XDG_STATE_HOME/viberpit/state/claude-creds/mcp-view.json`;
  what to verify before unmounting (the edge log shows no `creds_sync … failed`, and after
  the unmount a session on rocky connects to an OAuth MCP server).
- [ ] **Step 3: spec delivery order** — append `— plan: docs/superpowers/plans/2026-11-05-mcp-auth-overhaul-phase5-runner-distribution.md`.
- [ ] **Step 4: Commit** — "docs(mcp): phase 5 runner distribution (#3287)".
