# MCP auth overhaul — Phase 1: truthful status — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Settings › MCP servers one truthful status per server. The status is derived
from the shared grant in the token store plus the observations that still count. The
per-account "Account to reauthorize" picker goes away.

**Architecture:** A new pure module, `server/mcp_health/overall.py`, does three things. It
reads a server's grant state from `store/mcp.shared.json`, using only the bundle that matches
the current config so orphans are ignored. It filters status rows down to the ones that count:
enabled accounts only, and session rows only while their session is live. It then derives one
`overall = {status, error, grant}`. `GET /api/library/mcp` attaches `overall` to each server
and a `counted` flag to each row. The web page renders `overall` and keeps the per-row
readings as Details.

**Tech Stack:** FastAPI + SQLAlchemy (server), React + TS + vitest (web).

**Spec:** `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md`. This plan covers §7
and "remove the picker" from §5; designer call 2 applies.

**Later phases** (each gets its own plan when it starts, because each depends on what the
previous one learned):

- Phase 2: keying + single writer (§2, §1)
- Phase 3: heal + alert (§4, push half of §5)
- Phase 4: one-tap sign-in (OAuth-client half of §5)
- Phase 5: distribution off the mount (§3, §6). This phase waits for the broker unmount.

## Global Constraints

- Never import the `claude` binary's internals or patch them (constitution I). Grant state is
  read only from `viberpit.creds.read_shared_mcp()`.
- Token values never leave `server/mcp_health/`. `overall.error` text never contains a token
  or a URL query.
- DB clock rule: never use `func.now()`. Any "now" for the DB comes from
  `server/db/clock.py:db_utc_now()`. Pure functions take `now_ms: int`.
- Grant states, exactly: `"never" | "dead" | "expired" | "expiring" | "live" | "none"`. In
  Phase 1, `"dead"` is never produced, because a cleared bundle is indistinguishable from
  never-signed-in until Phase 3's health record exists. The `expiring` lead is 10 min
  (600 000 ms), matching `mcp_oauth_refresh.LEAD_S`. Import that constant; don't copy it.
- Copy, exactly:
  - `never`: `"Not signed in — one sign-in covers every account and runner."`
  - `expired`: `"Sign-in expired {YYYY-MM-DD HH:MM} UTC — background refresh is failing; sign in again."`
  - `failed: token rejected`: `"Token rejected by the server while signed in ({marker})."`
- Before pushing, run all four lint-and-test steps: ruff, mypy, `pytest tests/`, and
  `pytest server/tests/`. Also run the full `npm run typecheck && npm run lint && npm run test && npm run build && npm run budget` in `web/`.

## Review Focus

1. **A bundle whose `serverUrl` doesn't match the configured `url`** (today's orphaned
   aws-oauth bundle). It must be ignored for grant state. An empty orphan must not make a live
   server read "Not signed in", and a live orphan must not make an unsigned server read
   "Connected". Task 1 covers this with `test_grant_ignores_orphan_bundle_with_other_url`.
2. **A disabled account's stale `needs-auth` session row** (today's exact false red). It must
   not affect the pill, and it must not reach the sign-in `preferred` list. Task 1
   (`test_disabled_account_row_not_counted`) and Task 2
   (`test_auth_start_prefers_only_enabled_accounts`) cover this.
3. **A probe row that says `needs-auth` while the store grant is live.** The probe overlays a
   possibly stale dir copy. It must read Connected, not "token rejected". Only a
   session-observed rejection (error starting `"Sign-in rejected"`) from a live session counts
   as an anomaly. Task 1 covers this with
   `test_probe_needs_auth_ignored_when_grant_live`.
4. **A server whose auth server is incompatible** (kindash: "does not support dynamic client
   registration") **and also has no grant.** It must read Failed with that reason, not Needs
   auth, because signing in can't fix it. Task 1 covers this with
   `test_config_failure_beats_missing_grant`.
5. **A stdio server or an http server without OAuth** (grant `"none"`). The page must never
   offer Fix auth, and status comes from the rows alone. Task 1
   (`test_non_oauth_server_uses_rows_only`) and Task 3 (`renders no Fix auth for grant none`)
   cover this.

---

### Task 1: `server/mcp_health/overall.py`, the pure derivation

**Files:**
- Create: `server/mcp_health/overall.py`
- Test: `server/tests/test_mcp_health_overall.py`

**Interfaces:**
- Consumes: `viberpit.mcp_oauth_refresh.LEAD_S` (seconds), and the store dict shape returned by
  `viberpit.creds.read_shared_mcp()`. That shape is `{key: {serverName, serverUrl,
  accessToken, refreshToken?, expiresAt(ms)}}`.
- Produces:
  - `Grant = Literal["never","dead","expired","expiring","live","none"]`
  - `def store_key_name(name: str, plugin_name: str | None) -> str`: returns `name` for user
    servers and `f"plugin:{plugin_name}:{name}"` for plugin servers.
  - `def grant_state(store: dict, *, key_name: str, url: str | None, now_ms: int) -> Grant`
  - `def counted(row: dict, *, enabled_accounts: set[str], live_session_ids: set[str]) -> bool`
  - `def derive_overall(*, grant: Grant, rows: list[dict], now_ms: int, expires_at_ms: int | None) -> dict`:
    returns `{"status": str | None, "error": str | None, "grant": Grant}`. `rows` are already
    filtered to counted rows.
  - `def best_expiry(store: dict, *, key_name: str, url: str | None) -> int | None`: the
    expiry used in the `expired` copy.

- [ ] **Step 1: Write the failing tests** (`now = 1_800_000_000_000`)

```python
from server.mcp_health import overall as o

NOW = 1_800_000_000_000
def b(name="aws", url="https://u?x=1", tok="t", exp=NOW + 3_600_000, rt="r"):
    return {"serverName": name, "serverUrl": url, "accessToken": tok, "expiresAt": exp, "refreshToken": rt}

def test_store_key_name():
    assert o.store_key_name("aws-oauth", None) == "aws-oauth"
    assert o.store_key_name("railway", "railway") == "plugin:railway:railway"

def test_grant_none_without_bundle():
    assert o.grant_state({}, key_name="aws", url="https://u?x=1", now_ms=NOW) == "none"

def test_grant_never_for_cleared_bundle():
    assert o.grant_state({"k": b(tok="", exp=0, rt=None)}, key_name="aws", url="https://u?x=1", now_ms=NOW) == "never"

def test_grant_live_expiring_expired():
    s = lambda exp: {"k": b(exp=exp)}
    assert o.grant_state(s(NOW + 3_600_000), key_name="aws", url="https://u?x=1", now_ms=NOW) == "live"
    assert o.grant_state(s(NOW + 60_000), key_name="aws", url="https://u?x=1", now_ms=NOW) == "expiring"
    assert o.grant_state(s(NOW - 1), key_name="aws", url="https://u?x=1", now_ms=NOW) == "expired"

def test_grant_ignores_orphan_bundle_with_other_url():
    store = {"orphan": b(url="https://u", tok="", exp=0, rt=None), "live": b()}
    assert o.grant_state(store, key_name="aws", url="https://u?x=1", now_ms=NOW) == "live"
    assert o.grant_state({"orphan": b(url="https://u")}, key_name="aws", url="https://u?x=1", now_ms=NOW) == "none"

def test_grant_best_bundle_wins():
    store = {"a": b(tok="", exp=0, rt=None), "b": b()}
    assert o.grant_state(store, key_name="aws", url="https://u?x=1", now_ms=NOW) == "live"

def test_disabled_account_row_not_counted():
    row = {"account": "old", "source": "session", "sessionId": "s1", "status": "needs-auth"}
    assert not o.counted(row, enabled_accounts={"a"}, live_session_ids={"s1"})

def test_ended_session_row_not_counted_probe_row_counted():
    sess = {"account": "a", "source": "session", "sessionId": "gone", "status": "connected"}
    probe = {"account": "a", "source": "probe", "sessionId": None, "status": "connected"}
    assert not o.counted(sess, enabled_accounts={"a"}, live_session_ids=set())
    assert o.counted(probe, enabled_accounts={"a"}, live_session_ids=set())

def test_never_reads_needs_auth():
    out = o.derive_overall(grant="never", rows=[{"status": "connected", "error": None, "source": "probe"}], now_ms=NOW, expires_at_ms=None)
    assert out == {"status": "needs-auth", "error": "Not signed in — one sign-in covers every account and runner.", "grant": "never"}

def test_expired_copy_names_utc_expiry():
    out = o.derive_overall(grant="expired", rows=[], now_ms=NOW, expires_at_ms=1_799_996_400_000)
    assert out["status"] == "needs-auth"
    assert out["error"] == "Sign-in expired 2027-01-15 07:00 UTC — background refresh is failing; sign in again."

def test_config_failure_beats_missing_grant():
    rows = [{"status": "failed", "error": "Incompatible auth server: does not support dynamic client registration", "source": "probe"},
            {"status": "needs-auth", "error": "Not signed in", "source": "probe"}]
    out = o.derive_overall(grant="never", rows=rows, now_ms=NOW, expires_at_ms=None)
    assert out["status"] == "failed" and "dynamic client registration" in out["error"]

def test_probe_needs_auth_ignored_when_grant_live():
    rows = [{"status": "needs-auth", "error": "Sign-in expired 2026-10-18 03:11 UTC — re-authenticate.", "source": "probe"},
            {"status": "connected", "error": None, "source": "probe"}]
    assert o.derive_overall(grant="live", rows=rows, now_ms=NOW, expires_at_ms=None)["status"] == "connected"

def test_session_rejection_while_live_is_failed():
    rows = [{"status": "needs-auth", "error": "Sign-in rejected by the server (TOKEN_EXPIRED) — re-authenticate.", "source": "session"}]
    out = o.derive_overall(grant="live", rows=rows, now_ms=NOW, expires_at_ms=None)
    assert out == {"status": "failed", "error": "Token rejected by the server while signed in (TOKEN_EXPIRED).", "grant": "live"}

def test_live_without_rows_is_unchecked():
    assert o.derive_overall(grant="live", rows=[], now_ms=NOW, expires_at_ms=None)["status"] is None

def test_non_oauth_server_uses_rows_only():
    rows = [{"status": "pending", "error": None, "source": "probe"}, {"status": "connected", "error": None, "source": "probe"}]
    assert o.derive_overall(grant="none", rows=rows, now_ms=NOW, expires_at_ms=None) == {"status": "connected", "error": None, "grant": "none"}
    assert o.derive_overall(grant="none", rows=[], now_ms=NOW, expires_at_ms=None)["status"] is None
```

The `2027-01-15 07:00` in `test_expired_copy_names_utc_expiry` was checked with
`date -u -d @1799996400`. These are fixed-`now` pure tests, so this is not a date-literal
time bomb.

- [ ] **Step 2: Run them and check they fail**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_overall.py -q`
Expected: collection error, `No module named 'server.mcp_health.overall'`.

- [ ] **Step 3: Implement `overall.py`**

Derivation order for `derive_overall`, which the tests pin:

1. Look for a counted row with `status == "failed"`. If one exists, return
   `failed` + its error. This is a configuration problem, and signing in can't fix it.
2. If the grant is `never`, `dead` or `expired`, return `needs-auth` + the copy from Global
   Constraints. `dead` uses the `never` copy.
3. If the grant is `live` or `expiring` and a counted **session** row has `needs-auth` with an
   error starting `"Sign-in rejected"`, return `failed` + the "token rejected" copy. Take the
   marker from the parentheses. Probe `needs-auth` rows are ignored for OAuth grants.
4. Otherwise, take the best of the remaining row statuses. Rank `connected` above `pending`,
   above `needs-auth` (`needs-auth` only when the grant is `none`), above unknown; this is
   best-wins, not worst-wins. If no row is left, the status is `None`.

`grant_state` matches bundles where `serverName == key_name` and (`url is None` or
`serverUrl == url`). It then returns the best state over the matching bundles, ranked
live > expiring > expired > never. `refreshToken` is not considered in Phase 1. Keep the
`_token` and `_expiry` helpers: import them from `server/mcp_health/status.py` rather than
duplicating them.

- [ ] **Step 4: Run the tests and check they pass**

Run: `.venv/bin/python -m pytest server/tests/test_mcp_health_overall.py server/tests/test_mcp_health_status.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**: `feat(mcp): derive one truthful status per server from the shared grant`

---

### Task 2: API: attach `overall` and `counted`, and filter sign-in preference to enabled accounts

**Files:**
- Modify: `server/api/library.py` (`get_mcp` near :207, `mcp_auth_start` near :398)
- Modify: `server/mcp_health/store.py` (`needs_auth_by_server`)
- Test: `server/tests/test_library_mcp_status.py`, `server/tests/test_mcp_auth_routes.py`

**Interfaces:**
- Consumes the Task 1 functions.
- Produces: each server in `GET /api/library/mcp` gains `"overall": {"status", "error", "grant"}`;
  each status row gains `"counted": bool`.
  `async def needs_auth_by_server(*, enabled_accounts: set[str] | None = None) -> dict[str, list[str]]`.
- New helper in `library.py`: `async def _live_session_ids() -> set[str]`. It returns the ids
  of `Session` rows whose `status != "ended"`, read from the DB. Don't go through the
  orchestrator, because remote orchestrators lack private seams.
- Enabled accounts are `{a["name"] for a in accounts.list_accounts() if a["enabled"]}`. Call
  it with `asyncio.to_thread`, because it takes the ledger lock.

- [ ] **Step 1: Write the failing tests**
  - `test_get_mcp_overall_ignores_disabled_account_row`: seed a user server `aws` with `url`
    `https://u?x=1` and a live store bundle for it. Write the store with
    `creds.write_json_secure(paths.<shared mcp path>, …)`, after checking `creds.py:105-131`
    for the path helper. Pass one `needs-auth` session row from a disabled account. Monkeypatch
    the enabled-accounts and live-session helpers. Assert `overall == {"status": None,
    "error": None, "grant": "live"}` and that the row's `counted is False`.
  - `test_get_mcp_overall_never_signed_in`: the store bundle has an empty token, and there is
    one counted probe row `connected`. Assert `overall["status"] == "needs-auth"` and
    `overall["grant"] == "never"`.
  - `test_auth_start_prefers_only_enabled_accounts`: `needs_auth_by_server(enabled_accounts={"a"})`
    over `needs-auth` rows for `a` and `old` returns `{"aws": ["a"]}`. The `mcp_auth_start`
    route passes only enabled accounts as `preferred`; assert this on the fake manager's
    recorded `preferred`.

- [ ] **Step 2: Run them and check they fail.**
  Run: `.venv/bin/python -m pytest server/tests/test_library_mcp_status.py server/tests/test_mcp_auth_routes.py -q`.
  The new tests should fail on a missing `overall` key or an unexpected `preferred`.

- [ ] **Step 3: Implement it**

`get_mcp` calls `scan.list_mcp_servers(...)` and then annotates each server:

- `key_name = store_key_name(name, pluginName)`
- `url = config.get("url")`
- `store = await asyncio.to_thread(creds.read_shared_mcp)`, read once per request
- `now_ms` from `time.time()`. This is pure overlay maths, not a DB timestamp.

Set `counted` on every row, then call `derive_overall` over the counted rows only. Leave
`scan.list_mcp_servers` itself unchanged. Its other caller (`mcp_auth_start`) doesn't need
`overall`.

- [ ] **Step 4: Run them and check they pass.** Same command as Step 2, plus
  `server/tests/test_library_api.py server/tests/test_mcp_health_store.py`; all should pass.

- [ ] **Step 5: Commit**: `feat(mcp): library API reports one overall status and ignores stale rows`

---

### Task 3: Web: render `overall`, drop the picker and `worstStatus`

**Files:**
- Modify: `web/src/api/types.ts` (`McpStatusRow`, `LibraryMcpServer`)
- Modify: `web/src/features/settings/sections/McpSection.tsx`
- Modify: `web/src/features/settings/sections/mcpStatus.ts` and `mcpStatus.test.ts` (delete
  `worstStatus` and its tests; this was its only caller, and core.md forbids leaving dead code)
- Modify: `web/src/features/sessions/rail/railAttention.ts:23` (the comment mentions
  `worstStatus`; reword it so it doesn't name a deleted function)
- Test: `web/src/features/settings/sections/McpSection.test.tsx`

**Interfaces:**
- Produces:
  - `export type McpGrant = 'never' | 'dead' | 'expired' | 'expiring' | 'live' | 'none'`
  - `export interface McpOverall { status: string | null; error: string | null; grant: McpGrant }`
  - `LibraryMcpServer.overall: McpOverall`
  - `McpStatusRow.counted: boolean`

- [ ] **Step 1: Update the fixtures in `McpSection.test.tsx` to include `overall` and
  `counted`, and add these failing tests:**
  - `shows the overall status, not the worst row`: `overall.status = 'connected'`, and one
    row `needs-auth` with `counted: false`. The pill reads `Connected`.
  - `offers one Fix auth with no account picker when grant is never`: there is no
    `Account to reauthorize` label, and `McpAuthFlow` is called with `accounts: []`. Assert
    the start mutation is called with `{ name, account: undefined }`.
  - `renders no Fix auth for grant none`: an http server with `overall.grant = 'none'` and
    `status: 'needs-auth'` from rows shows no Fix auth.
  - `shows overall.error under the card`: the error text is visible.
  - `marks uncounted rows in details`: after expanding Details, an uncounted row shows
    `not counted`.

- [ ] **Step 2: Run them and check they fail**
  Run: `(cd web && npx vitest run src/features/settings/sections/McpSection.test.tsx src/features/settings/sections/mcpStatus.test.ts)`.
  Expected: the new tests fail.

- [ ] **Step 3: Implement it**
  - Pill: `<McpStatusPill status={s.overall.status} />`.
  - Under the card, `s.overall.error` in `text-xs text-ink-muted break-words`.
  - Fix auth shows when `['never','dead','expired'].includes(s.overall.grant)` and the
    transport is http or sse. It renders `<McpAuthFlow server={s.name} accounts={[]} />`.
  - Keep the stdio note, triggered by `s.overall.status === 'needs-auth' && s.transport === 'stdio'`.
  - In Details, an uncounted row gets `opacity-60` and a trailing ` · not counted` after the
    source text.
  - Delete `needsAuthAccounts`, `ServerReauth` and `worstStatus`.

- [ ] **Step 4: Run the full web gate.**
  Run: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build && npm run budget) 2>&1 | tee /tmp/mcp-phase1-web.log; echo exit=${PIPESTATUS[0]}`.
  Expected: `exit=0`. Also `grep -rn "Account to reauthorize\|worstStatus" web/src web/e2e` finds nothing.

- [ ] **Step 5: Commit**: `feat(web): Settings › MCP shows one truthful status and a single sign-in`

---

### Task 4: Docs, the full gate, and the PR

**Files:**
- Modify: `docs/product/features/mcp-status.md:37-41`. Replace the "worst status across its
  rows" paragraph with the derivation order from Task 1. Also describe which rows count
  (enabled accounts; session rows only while the session is live) and the single Fix auth.
- Modify: `docs/superpowers/specs/2026-11-04-mcp-auth-overhaul-design.md`. In the Delivery
  order section, mark Phase 1 as "shipped in #<PR>" after the PR is opened.

- [ ] **Step 1: Edit the docs as above.**
- [ ] **Step 2: Run the server gate** in the background, with output tee'd:
  `env -u VIBERPIT_TAILNET_ALLOWLIST VIBERPIT_DEV=1 .venv/bin/ruff check src/ server/ && .venv/bin/mypy server/ --ignore-missing-imports && .venv/bin/python -m pytest tests/ -q && .venv/bin/python -m pytest server/tests/ -n 4 --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/mcp-phase1-server.log`
  Unset the `VIBERPIT_SESSIOND_*` and `VIBERPIT_DAEMON_ROLE` variables first (core.md).
  Expected: everything passes, and the log tail shows `passed` with no `failed`.
- [ ] **Step 3: Commit, push, and open a ready PR.** Its body cites the spec path and says
  "Phase 1 of 5". Arm
  `gh pr merge <#> --squash --auto`. Write the body to a file and pass it with `--body-file`.
