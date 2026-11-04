# MCP auth overhaul — Phase 2: keying + single writer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each configured MCP server has exactly one live grant, stored under the key the
`claude` CLI looks up. When a server's config is edited, the grant follows it. Superseded and
empty leftover bundles disappear from the store and from every account dir. Only the edge
writes the store.

**Architecture:**
- **Key formula.** A new `viberpit.mcp_keys` module reproduces the CLI's key formula, which
  has been verified against the installed binary 2.1.293 and against live data.
- **Single writer.** `creds.update_shared_mcp`, the one locked store writer, becomes a no-op on
  a worker.
- **Re-keying.** A pure `plan_rekey` decides what to do: move a grant to its server's current
  key when both have the same (authorization server, resource) identity, then drop superseded
  and empty leftovers.
- **Edge loop.** Each refresher tick runs `reconcile_grants()` first. It builds the wanted keys
  from the configured servers, applies the plan under the store lock, and hands the dropped
  keys to `run_once`, so `_publish_all` purges them from every account dir.
- **Status.** Phase 1's `grant_state` switches from name + URL matching to an exact key lookup.

**Tech Stack:** Python (src/viberpit CLI core + FastAPI edge).

**Spec:** `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md` §1, §2, designer
calls 3 and 6. Runner capture over RPC (§6) is Phase 5, not this plan.

**Precondition:** PR #3256 (Phase 1) has merged. Branch off `origin/master` after it lands;
Task 5 edits Phase 1's `server/mcp_health/overall.py`.

## Global Constraints

- **Exact CLI key formula** (binary 2.1.293, function `Ol`):
  `key = f"{server_name}|{sha256(json.dumps({'type': t, 'url': u, 'headers': h or {}}, separators=(',', ':'))).hexdigest()[:16]}"`.
  Key order is exactly `type, url, headers`. The URL is the raw configured URL, query
  included. `server_name` is the configured name for user servers and
  `plugin:<pluginName>:<name>` for plugin servers.
- **Verified vectors**, which must be asserted verbatim:
  - `aws-oauth` + `{"type":"http","url":"https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize"}` → `aws-oauth|c6c1c83162f8ce2d`
  - `aws-oauth` + `{"type":"http","url":"https://aws-mcp.us-east-1.api.aws/mcp"}` → `aws-oauth|751831b0c9c1bf84`
  - `kindash-mcp` + `{"type":"http","url":"https://qa.kindash.com/mcp"}` → `kindash-mcp|ff3c9dc630867870`
  - `plugin:railway:railway` + `{"type":"http","url":"https://mcp.railway.com"}` → `plugin:railway:railway|b12ecd269c942ba3`
- **Grant identity** = `(authorization_server, resource)`. Both come from an RFC 9728
  protected-resource metadata (PRM) document:
  - `authorization_servers[0]` and `resource` are normalised with
    `mcp_oauth_refresh._url_href`. Promote that helper to a public `url_href` in place and
    update its callers; don't copy it.
  - A donor's PRM URL is `discoveryState.resourceMetadataUrl`. A wanted server's PRM URL is
    `prm_url_for(url)` = `origin + "/.well-known/oauth-protected-resource" + path`, where an
    empty path or `/` adds nothing and the query is dropped.
  - Unknown identity (no PRM URL, fetch failed, missing fields) means **no move**. Never guess.
- **Drop rules.** These are the only bundles ever deleted:
  - (a) **superseded**: `serverName` equals a wanted server's `store_name` but the key is not
    that server's wanted key;
  - (b) **empty**: the key is not wanted and the bundle has neither an `accessToken` nor a
    `refreshToken`.

  An unwanted bundle that holds any token and whose `serverName` is not configured is
  **kept**, because it may be a repo `.mcp.json` server, which is out of scope.
- **Single writer:** the guard checks `viberpit.fleet_role.is_worker()` (reads
  `VIBERPIT_DAEMON_ROLE`). On a worker, `update_shared_mcp(update)` doesn't call `update`,
  doesn't write, logs at DEBUG once per process (not per call), and returns the current store.
- Token values never appear in logs or in test failure output. Log keys and counts only.
- Before pushing, run all four lint-and-test steps (ruff, mypy, `pytest tests/` with
  `PYTHONPATH=$PWD/src`, `pytest server/tests/`), with `web/dist` absent.

## Review Focus

1. **A wanted server whose identity can't be fetched** (offline or PRM 404). Its old-key grant
   must stay where it is, and nothing is moved or dropped for that server except bundles that
   are empty. Task 3 covers this with `test_unknown_identity_never_moves`.
2. **A server whose URL changed to a different provider under the same name.** Identities
   differ, so there is no move. The old bundle is dropped as superseded (it can't serve the
   new URL), and the server reads Not signed in. Task 3 covers this with
   `test_same_name_different_identity_drops_not_moves`.
3. **A repo `.mcp.json` server with a token**, which is unconfigured and unwanted. It must
   survive reconcile in both the store and the dirs. Task 3 covers this with
   `test_unconfigured_bundle_with_token_kept`.
4. **A dropped key still present in an account dir.** It must not be captured back into the
   store, and it must be removed from the dir on the next publish. Task 4 covers this with
   `test_dropped_key_not_recaptured_and_purged_from_dir`.
5. **A worker process running compose or sync_back** (rocky through sshfs). The store must stay
   unchanged. Task 2 covers this with `test_worker_never_writes_store`.

---

### Task 1: `viberpit.mcp_keys`: the CLI key formula and the PRM URL

**Files:**
- Create: `src/viberpit/mcp_keys.py`
- Test: `tests/test_mcp_keys.py`

**Interfaces:**
- Produces:
  - `def cli_oauth_key(server_name: str, config: dict) -> str`
  - `def prm_url_for(server_url: str) -> str | None`, which returns None for a non-http(s) URL
  - `def store_name(name: str, plugin_name: str | None) -> str`. **Move** Phase 1's
    `server/mcp_health/overall.py:store_key_name` here, and make overall import it. One
    implementation only.

- [ ] **Step 1: Write the failing tests.**
  - `test_cli_oauth_key_matches_binary_vectors`: the four vectors from Global Constraints, as
    a parametrized test.
  - `test_cli_oauth_key_headers_default_and_order`: `{"url":…, "type":"http"}` gives the same
    key as `{"type":"http","url":…,"headers":{}}`. A non-empty `headers` dict changes the key.
  - `test_prm_url_for`:
    - `https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize` → `https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp`
    - `https://mcp.railway.com` → `https://mcp.railway.com/.well-known/oauth-protected-resource`
    - `stdio-ish` → None
  - `test_store_name`.
- [ ] **Step 2: Run them and check they fail.**
  Run: `env -i HOME=$(mktemp -d) PATH=/usr/bin:/bin PYTHONPATH=$PWD/src <venv>/python -m pytest tests/test_mcp_keys.py -q`.
  Expected: `ModuleNotFoundError`.
- [ ] **Step 3: Implement it.** Move `store_key_name` out of `overall.py` and update its import
  and test.
- [ ] **Step 4: Run the tests and check they pass**, together with
  `server/tests/test_mcp_health_overall.py`.
- [ ] **Step 5: Commit:** `feat(mcp): reproduce the CLI's MCP OAuth key formula`

---

### Task 2: Single writer: workers never write the store

**Files:**
- Modify: `src/viberpit/creds.py:131` (`update_shared_mcp`)
- Test: `tests/test_creds.py`

**Interfaces:**
- Produces: the same `update_shared_mcp` signature, now with the worker guard. All existing
  writers (`merge_shared_mcp`, `capture_cfg_mcp`, compose, sync_back, the refresher CAS and
  `mcp_auth`) inherit the guard with no change at their call sites.

- [ ] **Step 1: Write the failing tests.**
  - `test_worker_never_writes_store`: with `VIBERPIT_DAEMON_ROLE` set to the worker value
    (read `fleet_role.py` for the exact string), the `merge_shared_mcp`, `capture_cfg_mcp`
    and `sync_cfg_mcp_oauth` capture paths leave the store file byte-identical, and the
    update callback is never called.
  - `test_worker_publish_still_updates_dir`: the dir publish half of `sync_cfg_mcp_oauth`
    still works on a worker.
  - `test_primary_writes_store`: the existing behaviour is unchanged when the role is unset.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement the guard.** Add a module-level `_warned_worker` flag for the
  log-once behaviour. Update the docstring: "the one locked writer of the store, and only
  on the refresh authority".
- [ ] **Step 4: Run the tests and check they pass**, together with `tests/test_creds.py`,
  `tests/test_creds_mcp_refresh.py`, `tests/test_mcp_oauth_refresh.py` and
  `server/tests/test_mcp_auth_helpers.py`.
- [ ] **Step 5: Commit:** `fix(mcp): only the refresh authority writes the shared MCP store`

---

### Task 3: `viberpit.mcp_rekey`: the pure re-key plan and grant identity

**Files:**
- Create: `src/viberpit/mcp_rekey.py`
- Modify: `src/viberpit/mcp_oauth_refresh.py`. Rename `_url_href` → `url_href`, add
  `grant_identity`, and reuse `_default_fetch_json`. Add a `_identity_cache` alongside
  `_resource_cache`.
- Test: `tests/test_mcp_rekey.py`

**Interfaces:**
- Consumes: `cli_oauth_key`, `prm_url_for` (Task 1).
- Produces:
  - `@dataclass(frozen=True) class WantedServer: key: str; store_name: str; url: str`
  - `def grant_identity(prm_url: str, *, fetch_json: FetchJson | None = None) -> tuple[str, str] | None`,
    in `mcp_oauth_refresh.py`. Results are cached per `prm_url`; failures are not cached.
  - `@dataclass(frozen=True) class RekeyPlan: store: dict; moved: dict[str, str]; dropped: frozenset[str]`.
    `moved` maps old key → new key.
  - `def plan_rekey(store: dict, wanted: list[WantedServer], identity_of: Callable[[str], tuple[str, str] | None]) -> RekeyPlan`

**Algorithm (pinned):**
1. For each wanted server whose key is missing from the store, or whose bundle has no
   `refreshToken` and no non-empty `accessToken`:
   - Find donors: unwanted bundles that hold a `refreshToken` and have a `resourceMetadataUrl`.
   - Compute the identity of the wanted server (`identity_of(prm_url_for(w.url))`) only if at
     least one donor exists. Then compute each donor's identity.
   - The donor with an equal identity and the latest `expiresAt` moves to `w.key`, with
     `serverName = w.store_name` and `serverUrl = w.url`. Its old key is removed and recorded
     in `moved`.
2. Then drop every bundle matching drop rule (a) or (b) from Global Constraints. Record its
   key in `dropped`.
3. `identity_of` is never called when no wanted server needs a donor. That keeps the network
   out of a steady-state tick.

- [ ] **Step 1: Write the failing tests.** Use a fake `identity_of` dict lookup and token
  placeholders such as `"t"`/`"r"`; no real values.
  - `test_moves_grant_to_new_key_after_url_edit`: the aws-oauth case. The live bundle sits
    under the old key with the old URL, the wanted key is the `?oauth=initialize` key, and the
    identities are equal. Expect it moved, `serverUrl` updated, and the old key gone.
  - `test_unknown_identity_never_moves`: `identity_of` returns None. Nothing moves, and the
    wanted key stays absent.
  - `test_same_name_different_identity_drops_not_moves`: the old bundle is dropped as
    superseded, and nothing is moved.
  - `test_rename_follows_identity`: the wanted store name is `aws2`, and the donor's
    serverName is `aws-oauth`, which is unconfigured, has a token and the same identity.
    Expect it moved.
  - `test_drops_empty_unwanted_bundles`: the linear, notion and stripe shapes, with an empty
    `accessToken` and no `refreshToken`, are dropped.
  - `test_unconfigured_bundle_with_token_kept`
  - `test_wanted_live_key_untouched_and_no_identity_calls`: `identity_of` must not be called.
  - `test_grant_identity_normalises_and_caches`: a fake `fetch_json` returns
    `{"resource": "https://mcp.railway.com", "authorization_servers": ["https://backboard.railway.com"]}`.
    Expect `("https://backboard.railway.com/", "https://mcp.railway.com/")`. A second call
    doesn't fetch. A failure returns None and isn't cached.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests and check they pass**, with `tests/test_mcp_oauth_refresh.py`
  as well (because of the `url_href` rename).
- [ ] **Step 5: Commit:** `feat(mcp): plan moving a grant to its server's current key`

---

### Task 4: Edge reconcile: apply the plan each tick, and purge dropped keys from the dirs

**Files:**
- Create: `server/mcp_health/grants.py`
- Modify: `src/viberpit/creds.py` (`publish_mcp`, `capture_cfg_mcp`, `sync_cfg_mcp_oauth` gain
  `drop: frozenset[str] = frozenset()`)
- Modify: `src/viberpit/mcp_oauth_refresh.py` (`run_once(..., drop_keys: frozenset[str] = frozenset())`
  → `_publish_all(names, drop=…)`)
- Modify: `server/app.py:568-588` (the loop: `drop = await asyncio.to_thread(grants.reconcile_grants)`,
  then `run_once(drop_keys=drop)`)
- Test: `server/tests/test_mcp_grants.py`, `tests/test_creds.py`

**Interfaces:**
- Consumes: `plan_rekey`, `WantedServer`, `grant_identity`, `cli_oauth_key`, `store_name`.
- Produces:
  - `def wanted_servers() -> list[WantedServer]` in `grants.py`. It is built from
    `server.library.scan.list_mcp_servers()["servers"]` and covers http/sse entries only,
    with `key = cli_oauth_key(store_name(name, pluginName), config)`.
  - `def reconcile_grants() -> frozenset[str]`. It applies `plan_rekey` **inside**
    `creds.update_shared_mcp`, re-planning against the locked fresh copy, and passes
    `grant_identity` as `identity_of`. It logs one INFO line when anything moved or dropped:
    `"mcp grants reconciled moved=%s dropped=%s"`, with keys only. It returns `dropped`.
    It never raises: on an exception it logs a WARNING and returns `frozenset()`.
  - **It returns `dropped | set(moved)`.** A moved bundle's old key must also be purged from
    the dirs. Otherwise capture would offer the dir's copy of the old key back into the store
    as "a key the store lacks".
  - `publish_mcp(..., drop)` removes the `drop` keys from the result. `capture_cfg_mcp(dir_mcp, drop)`
    never offers a key in `drop`.

- [ ] **Step 1: Write the failing tests.**
  - `test_dropped_key_not_recaptured_and_purged_from_dir`: a dir holds an empty orphan key.
    `sync_cfg_mcp_oauth(dir, drop={key})` removes it from the dir's `.credentials.json`, and the
    store still lacks it.
  - `test_reconcile_grants_moves_and_reports`: seed the shared `claude.json` with aws-oauth at
    the new URL, and seed the store with the live bundle under the old key plus an empty
    linear orphan. Monkeypatch `grant_identity` to return equal identities. Expect the store
    to have the live bundle under `aws-oauth|c6c1c83162f8ce2d`, and the returned drop set to
    contain the old aws key and the linear key.
  - `test_reconcile_grants_swallows_errors`: `list_mcp_servers` raises, and the result is
    `frozenset()` with a WARNING logged.
  - `test_run_once_threads_drop_keys_to_publish`
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.** In `app.py`, keep the loop's existing authority and kill-switch
  gate. `reconcile_grants` runs only inside it.
- [ ] **Step 4: Run the tests and check they pass**, together with the Task 2/3 test files
  and `server/tests/test_mcp_after_login.py`.
- [ ] **Step 5: Commit:** `feat(mcp): edge reconciles grants to current keys and purges leftovers`

---

### Task 5: Status reads the exact key

**Files:**
- Modify: `server/mcp_health/overall.py` (`grant_state` and `best_expiry` take `key: str`
  instead of `key_name`/`url`)
- Modify: `server/api/library.py` (`get_mcp` computes
  `key = cli_oauth_key(store_name(name, pluginName), config)` for http/sse servers; stdio
  servers get grant `"none"` without a lookup)
- Test: `server/tests/test_mcp_health_overall.py`, `server/tests/test_library_mcp_status.py`

**Interfaces:**
- Produces: `grant_state(store, *, key: str, now_ms: int) -> Grant` and
  `best_expiry(store, *, key: str) -> int | None`.

- [ ] **Step 1: Update the tests to the new signatures and add these:**
  - `test_grant_state_uses_exact_cli_key`: a bundle with the right serverName and URL but
    stored under a different key reads `"none"`.
  - In the API test, the aws-oauth fixture's store key is
    `aws-oauth|c6c1c83162f8ce2d`.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.** Delete the name + URL matching code; it becomes dead code.
- [ ] **Step 4: Run the tests and check they pass.**
- [ ] **Step 5: Commit:** `refactor(mcp): status reads the grant at the CLI's exact key`

---

### Task 6: Docs, the full gate, and the PR

**Files:**
- Modify: `docs/product/features/mcp-status.md`. In the token refresh section, describe
  reconcile: keys, moves, drop rules, and that only the edge writes. Replace the "matched by
  server name and the configured `url`" wording with "at the CLI's exact key".
- Modify: the spec's Delivery order. Item 2 gets a link to this plan.

- [ ] **Step 1: Make the doc edits.**
- [ ] **Step 2: Run the server gate** in the background with output tee'd to
  `/tmp/mcp-phase2-server.log`, with `web/dist` absent and the `VIBERPIT_SESSIOND_*` and
  `VIBERPIT_DAEMON_ROLE` variables unset. Use `-n 4`. If anything fails, show it is
  pre-existing with a run on a detached `origin/master`.
- [ ] **Step 3: Open a ready PR** that cites the spec and says "Phase 2 of 5", and arm
  `--squash --auto`.
