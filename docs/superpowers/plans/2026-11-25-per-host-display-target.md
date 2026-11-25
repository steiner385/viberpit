# Per-host Display Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fleet host can be configured so its sessions' GUI programs open as real windows on another
fleet host's desktop (e.g. rocky → dobby), via waypipe (Wayland) + an SSH-forwarded X11 display.

**Architecture:**
- A pure policy (`display_policy.py`) gains a `remote` mode.
- The **target** daemon (dobby) runs a `DisplayBridge`: one `ssh` to the source host carrying both legs,
  driven by an idempotent desired-state RPC (`display_bridge_ensure`).
- The **edge** computes the desired set from the DB and asserts it on spawn, archive and every probe tick.
- The **source** daemon (rocky) injects env pointing at deterministic socket paths, after a local
  liveness check.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / asyncio; waypipe 0.11 (Ubuntu archive); OpenSSH;
React + TS (Fleet settings).

**Spec:** `docs/superpowers/specs/2026-11-25-per-host-display-target-design.md`

## Global Constraints

- Constitution v1.2.0. Principle III: the waypipe/SSH invocation is taken from Task 1's verified findings,
  never from docs alone.
- DB columns are all nullable:
  - `Host.display_target` (String; NULL = `local`).
  - `Host.display_ssh_host` (String; falls back to `ssh_host`).
  - `Session.display_target_host` (String; NULL unless `display_mode == "remote"`).
- Alembic revision `0115_display_target.py`, `down_revision = "0114_session_launch_credential"`
  (re-check the newest revision at implementation time and chain off it).
- `display_target` grammar: `local` | `host:<name>`. Normalize `""`/`local` to NULL on write. No
  `windows:` value, enum member or flag (spec §7, Principle VI).
- Settings go in BOTH `Settings` classes in `server/config.py` (pydantic `Field(alias=…)` and the
  dataclass fallback):
  - `display_bridge_enabled: bool = True` (`VIBERPIT_DISPLAY_BRIDGE_ENABLED`). This is the kill switch:
    false means every host behaves as `local`.
  - `display_bridge_accept_from: str = ""` (`VIBERPIT_DISPLAY_BRIDGE_ACCEPT_FROM`): comma-separated
    source host names allowed to draw here.
  - `display_bridge_x_start: int = 150` (`VIBERPIT_DISPLAY_BRIDGE_X_START`).
  - `display_bridge_x11: bool = True` (`VIBERPIT_DISPLAY_BRIDGE_X11`).
  - `display_bridge_start_timeout_s: float = 15.0` (`VIBERPIT_DISPLAY_BRIDGE_START_TIMEOUT_S`).
  - `display_bridge_grace_s: float = 60.0` (`VIBERPIT_DISPLAY_BRIDGE_GRACE_S`).
- Fixed paths on the source host (one target per source host, so no index is needed):
  - Wayland socket: `$XDG_RUNTIME_DIR/wayland-viberpit-bridge`.
  - X11 forward: `$XDG_RUNTIME_DIR/viberpit-bridge/X11`, exported as the path display
    `DISPLAY=$XDG_RUNTIME_DIR/viberpit-bridge/X11` (never `/tmp/.X11-unix` / `:N`: its abstract
    `@/tmp/.X11-unix/X<N>` twin is bindable by any local user; spec §8 fix round 1).
  - Cookie file: `$XDG_RUNTIME_DIR/viberpit-bridge/xauth`, mode 0600, in a 0700 directory.
- Fallback reasons (exact strings, stored in `display_fallback_reason`): `bridge_disabled`,
  `bridge_not_accepted`, `bridge_unavailable`, `bridge_wrong_user`, `target_desktop_unavailable`.
- The X11 cookie and SSH keys never appear in argv, log records, RPC responses or API responses.
- Teardown uses `server/aio.py:cancel_and_reap()`. Never use `func.now()` (use `db_utc_now()`).
- Gates before every push:
  - `ruff check src/ server/`
  - `mypy server/ --ignore-missing-imports`
  - `pytest tests/`
  - `pytest server/tests/ -n 4` (run in the background, tee'd to `/tmp/`, under `env -i` with an
    isolated HOME per memory)
  - In `web/`: `npm run typecheck && npm run lint && npm run test && npm run build && npm run budget`

## Review Focus

1. **dobby logs out and back in while a bridge is up.** The Xwayland cookie and socket rotate, so the
   bridge must re-push the cookie and restart the X11 leg within one resolver TTL. Windows opened after
   re-login must work. (Task 4 test `test_cookie_rotation_repushes_and_restarts_x11_leg`.)
2. **The target host is disabled or deleted while sources still point at it.** Spawn must still succeed:
   virtual, with reason `bridge_unavailable`. It must not raise. (Task 6 test
   `test_target_host_disabled_falls_back_virtual`.)
3. **The edge goes away.** The target tears bridges down after 3 missed ensures (fail closed). When the
   edge returns, the next tick re-establishes them. (Task 4
   `test_silence_fails_closed_after_three_intervals`; Task 6
   `test_reconcile_reasserts_desired_set_after_restart`.)
4. **Two sources target one desktop at once** (rocky and a future runner both → dobby). They need
   independent bridges and target-side socket names keyed by source, and tearing one down must not touch
   the other. (Task 4 `test_two_sources_are_independent`.)
5. ~~**`display_bridge_x_start` lands inside the Xvfb pool range.**~~ Moot since spec §8 fix round 1:
   the X11 forward is a path display in the private dir, with no display number to collide. (Task 5
   `test_x11_leg_follows_only_its_setting`.)

---

### Task 1: Verify waypipe 0.11 + SSH forwarding on the real hosts (spike, no product code)

**Files:**
- Modify: `docs/superpowers/specs/2026-11-25-per-host-display-target-design.md` (append a
  "§8 Verified invocation" section)

**Interfaces:**
- Produces: the exact argv for the target-side client, the remote server command and the SSH flags.
  Also a yes/no on whether one `waypipe server` serves many clients. Task 4 builds argv from this
  section verbatim.

- [ ] **Step 1:** Confirm `waypipe --version` prints 0.11.x on dobby and on `ssh rocky`. If it is
  missing, stop and hand back: the operator runs `sudo apt install waypipe` on both hosts.
- [ ] **Step 2:** From dobby, using the cookie/`WAYLAND_DISPLAY` that
  `server.sessions.desktop_session.resolve_desktop_session(uid=os.getuid())` returns:
  - Start `waypipe --socket <dobby sock> client`.
  - Run one `ssh -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=yes -o BatchMode=yes -R <rocky sock>:<dobby sock> -R /tmp/.X11-unix/X150:/tmp/.X11-unix/X0 rocky 'waypipe --socket <rocky sock> server --display wayland-viberpit-bridge -- sleep infinity'`.
  - Iterate until that works. Record any flag that differs.
- [ ] **Step 3:** On rocky, with `WAYLAND_DISPLAY=wayland-viberpit-bridge`, launch two Wayland clients
  at once (`foot`, `gtk4-demo` or `chromium --ozone-platform=wayland`). Record whether both appear on
  dobby. This is the multi-client answer.
- [ ] **Step 4:** On rocky, `xauth -f <file> add :150 MIT-MAGIC-COOKIE-1 <cookie>` (cookie via ssh
  stdin), then `DISPLAY=:150 XAUTHORITY=<file> xeyes`. Record whether it appears on dobby.
- [ ] **Step 5:** Write §8 with the exact argv, the multi-client result and, if multi-client fails, which
  fallback from spec §2 is taken. Screenshot evidence goes under `/tmp/display-bridge-spike/` and is
  referenced, not committed.
- [ ] **Step 6:** Commit:
  `git add docs/superpowers/specs/2026-11-25-per-host-display-target-design.md && git commit -m "docs(spec): verified waypipe/ssh invocation for display bridge"`

### Task 2: Display policy gains `remote`

**Files:**
- Modify: `server/sessions/display_policy.py`
- Test: `server/tests/test_display_policy.py` (extend; create it if absent)

**Interfaces:**
- Produces:
  - `DISPLAY_REMOTE = "remote"`.
  - `resolve_display_decision(*, requested: str | None, client_node: str | None, runner: str, native_available: bool, native_unavailable_reason: str | None, remote_target: str | None, remote_available: bool, remote_unavailable_reason: str | None) -> DisplayDecision`.
  - The existing `resolve_display_mode` and `native_fallback` become thin wrappers over it, or are
    deleted with their callers moved, in the same task (Principle VI: one decision function).

- [ ] **Step 1: Write failing tests** for the precedence (spec §1):
  - `test_explicit_virtual_beats_remote` → `("virtual", None)`.
  - `test_explicit_native_available_beats_remote` → `("native", None)`.
  - `test_local_client_native_beats_remote` (auto, client == runner, native available) →
    `("native", None)`.
  - `test_remote_target_available_goes_remote` → `("remote", None)`.
  - `test_remote_target_unavailable_falls_back_with_reason` (`remote_unavailable_reason="bridge_unavailable"`)
    → `("virtual", "bridge_unavailable")`.
  - `test_no_target_keeps_todays_rules`: a parametrized copy of every existing
    `resolve_display_mode`/`native_fallback` case, with `remote_target=None`, gives identical results.
  - `test_unknown_requested_reads_as_auto`, including `requested="remote"` (a client cannot force
    remote).
- [ ] **Step 2:** Run `pytest server/tests/test_display_policy.py -v`. Expect a FAIL (ImportError).
- [ ] **Step 3:** Implement `resolve_display_decision` with the precedence above. A wanted-native fallback
  keeps its existing reason, and a wanted-remote fallback uses `remote_unavailable_reason`. Move
  orchestrator callers (`_resolve_display_mode_for`, ~line 4921) onto it, passing
  `remote_target=None, remote_available=False`. Task 5 wires the real values.
- [ ] **Step 4:** Run `pytest server/tests/test_display_policy.py server/tests -k "display" -v`. Expect a
  PASS.
- [ ] **Step 5:** Commit: `feat(display): remote mode in the display decision`.

### Task 3: Columns, settings, Fleet API

**Files:**
- Modify: `server/db/models.py` (`Host`, `Session`), `server/config.py` (both classes),
  `server/api/fleet.py`, `server/api/sessions.py` (session wire)
- Create: `server/db/migrations/versions/0115_display_target.py`
- Test: `server/tests/test_fleet_api_display_target.py`, `server/tests/test_display_bridge_settings.py`

**Interfaces:**
- Produces:
  - Wire fields `displayTarget: string | null` and `displaySshHost: string | null` on fleet host
    GET/PUT/POST.
  - Read-only `displayBridge: {accepts: string[], active: object[]} | null` on GET, from a nullable JSON
    column `Host.display_bridge` (the probe snapshot, which Task 4 writes).
  - Session wire `displayTargetHost: string | null`.
  - `server/sessions/display_target.py:parse_display_target(value: str | None) -> str | None`. It returns
    the target host name, or None for local, and raises `ValueError` on bad grammar. This is the one
    parser; Tasks 5 and 6 use it.
  - `server/sessions/display_target.py:accepted_sources(settings) -> frozenset[str]`, parsing
    `display_bridge_accept_from` (lower-cased, stripped).

- [ ] **Step 1: Write failing tests:**
  - `test_put_display_target_roundtrips` (`host:dobby` on rocky).
  - `test_display_target_self_rejected_422` (`code == "invalid_display_target"`).
  - `test_display_target_unknown_host_rejected_422`.
  - `test_display_target_local_normalizes_to_null`.
  - `test_display_target_windows_prefix_rejected_422`.
  - `test_display_ssh_host_validated_with_SSH_HOST_RE`.
  - `test_parse_display_target_grammar` (table).
  - `test_accepted_sources_parsing`.
  - `test_settings_defaults` (the six values in Global Constraints, both Settings classes).
- [ ] **Step 2:** Run them. Expect a FAIL.
- [ ] **Step 3:** Add the columns, the migration (`op.add_column` ×4, nullable, including `hosts.display_bridge` JSON), the settings, the parser
  module and the `_PATCHABLE` entries plus validation in `fleet.py` (pattern: the existing `ssh_host`
  branch). Add `displayTargetHost` to the session serializer next to `displayMode`
  (`server/api/sessions.py:300`).
- [ ] **Step 4:** Run the new tests, `pytest server/tests -k "schema or reconcile or postgres or fleet" -v`,
  and the brand ratchet test. Expect a PASS.
- [ ] **Step 5:** Commit: `feat(fleet): per-host display target setting`.

### Task 4: `DisplayBridge` on the target daemon + RPC + `/info`

**Files:**
- Create: `server/sessions/display_bridge.py`, `server/ipc/display_bridge_rpc.py`
- Modify: `server/ipc/serve.py` (register, next to `register_x11` at line 129), `server/ipc/daemon.py`
  (`/info` key `displayBridge`, next to `PLATFORMS_INFO_KEY` ~line 480), the daemon lifespan (construct
  and `stop_all()` on shutdown), and `server/sessions/host_probe.py` (persist `/info.displayBridge` into
  `Host.display_bridge` on every probe outcome, like `_platforms_snapshot`)
- Test: `server/tests/test_display_bridge.py`, `server/tests/test_display_bridge_rpc.py`

**Interfaces:**
- Consumes: `NativeDisplay` (its resolver/cache gives the target's `DesktopSession`: display, xauthority,
  wayland_display, kind), `accepted_sources`, and Task 1 §8 argv.
- Produces:
  - `class DisplayBridge`, constructed as
    `DisplayBridge(*, native: NativeDisplay, accept_from: frozenset[str], ssh_dest: Callable[[str], str | None], x11_enabled: bool, x_start: int, grace_s: float, silence_s: float, spawn=asyncio.create_subprocess_exec, clock=time.monotonic)`.
  - `async def ensure(self, sources: list[str], ssh_dests: dict[str, str]) -> list[dict]`. It returns
    one dict per known source:
    `{"source": str, "state": "up"|"starting"|"stopping"|"not_accepted"|"desktop_unavailable"|"failed", "legs": ["wayland","x11"], "since": float|None}`.
  - `async def stop_all(self) -> None`.
  - `def info(self) -> dict`, which returns `{"accepts": sorted, "active": [...]}`.
  - RPC `display_bridge_ensure`. Params `{"sources": [...], "sshDests": {source: dest}}`; returns
    `{"bridges": [...]}`.
  - `/info` key `displayBridge` = `info()`.
- `silence_s` = 3 × the probe interval (90 s). With no `ensure` call for `silence_s`, the desired set
  is treated as empty.

- [ ] **Step 1: Write failing tests** (fake spawn records argv and env, fake clock, fake native):
  - `test_ensure_starts_one_ssh_per_source_with_verified_argv`: argv equals §8 and contains no cookie.
  - `test_not_accepted_source_never_spawns`: state is `not_accepted`.
  - `test_desktop_unavailable_never_spawns`.
  - `test_unlisted_source_torn_down_after_grace`: not before `grace_s`, yes after.
  - `test_relisted_within_grace_keeps_process` (no respawn).
  - `test_silence_fails_closed_after_three_intervals`.
  - `test_two_sources_are_independent`.
  - `test_crash_restarts_with_backoff_while_desired`.
  - `test_cookie_rotation_repushes_and_restarts_x11_leg`.
  - `test_cookie_sent_via_stdin_never_argv_or_logs` (caplog plus argv scan for the cookie hex).
  - `test_x11_leg_absent_when_disabled_or_target_is_x11_only_for_wayland_leg`: an X11-only target gets
    no waypipe leg; `x11_enabled=False` gets no `-R` X11 forward.
  - `test_stop_all_reaps_everything`.
  - RPC: `test_rpc_roundtrip_and_info_key`.
  - Probe: `test_probe_persists_display_bridge_snapshot`.
- [ ] **Step 2:** Run `pytest server/tests/test_display_bridge*.py -v`. Expect a FAIL.
- [ ] **Step 3:** Implement. One supervisor task per source, holding one `ssh` (+ local
  `waypipe client`) process group.
  - Cookie push: a second short `ssh <dest> 'umask 077; mkdir -p …/viberpit-bridge; xauth -f …/xauth add :N MIT-MAGIC-COOKIE-1 -'`,
    with the cookie on stdin (adjust to what §8 verified).
  - Rotation: detect it by comparing `DesktopSession.xauthority`/display on each `ensure` and on the
    supervisor tick.
  - Bridge log lines name only the source, leg and state.
- [ ] **Step 4:** Run the tests. Expect a PASS. Then run
  `pytest server/tests -k "ipc or info or serve" -n 4`. Expect a PASS.
- [ ] **Step 5:** Commit: `feat(display): target-side display bridge with desired-state RPC`.

### Task 5: Source-daemon env injection and remote decision

**Files:**
- Create: `server/sessions/display_bridge_env.py`
- Modify: `server/sessions/orchestrator.py`:
  - `_display_env` ~2839.
  - `_resolve_display_mode_for` / `_display_decision` ~4921.
  - The `create_session` kwargs ~4466 (new kwarg `display_target: str | None = None`).
  - Persist `display_target_host` wherever `display_mode` is persisted (~5013, 5079, 5138, 5168, 6674,
    6804, 6836).
  - The `open_browser` native/virtual branch ~7236.
- Test: `server/tests/test_display_bridge_env.py`, `server/tests/test_orchestrator_remote_display.py`

**Interfaces:**
- Consumes: `resolve_display_decision` and `DISPLAY_REMOTE` (Task 2), `parse_display_target` (Task 3).
- Produces:
  - `remote_env(*, runtime_dir: str, x_display: int | None, uid: int) -> dict[str, str] | str`. It
    returns the env dict (`WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, plus `DISPLAY`/`XAUTHORITY` when the X11
    socket and cookie file are live), or a fallback-reason string (`bridge_unavailable` |
    `bridge_wrong_user`).
  - Live means the socket exists, is owned by `uid` and accepts `connect()` within 0.5 s. It runs in a
    worker thread from async callers.
  - `x_display` is None when `display_bridge_x11` is false or the start is inside the Xvfb pool.

- [ ] **Step 1: Write failing tests** (tmp_path sockets; a real `socket.socket(AF_UNIX)` listener):
  - `test_remote_env_both_legs_live`.
  - `test_remote_env_wayland_only_when_x11_absent`.
  - `test_remote_env_dead_socket_is_bridge_unavailable`.
  - `test_remote_env_foreign_uid_is_wrong_user`.
  - `test_x_start_inside_pool_disables_x11_leg`.
  - Orchestrator:
    - `test_create_session_with_target_and_live_bridge_is_remote_and_persists_target_host`.
    - `test_create_session_target_but_dead_bridge_is_virtual_with_reason`.
    - `test_kill_switch_off_ignores_target`: `bridge_disabled` is NOT recorded as a fallback. The kill
      switch means local, so it is equivalent to no target.
    - `test_resume_keeps_persisted_remote_decision`.
    - `test_display_env_remote_branch_returns_bridge_env`.
    - `test_open_browser_remote_uses_bridge_env_and_ozone_wayland`.
- [ ] **Step 2:** Run them. Expect a FAIL.
- [ ] **Step 3:** Implement. Read the bridge env through the same "decide once at birth, persist, env
  read live" shape native uses. Old daemons ignore the unknown `display_target` kwarg: check that
  `create_session` signature changes are mirrored in the IPC params schema and in both session-facade
  test doubles (memory: FakeOrch AND _FakeSessions).
- [ ] **Step 4:** Run the new tests plus `pytest server/tests -k "display or native or x11 or orchestrator" -n 4`.
  Expect a PASS.
- [ ] **Step 5:** Commit: `feat(display): sessions on a targeted host open on the remote desktop`.

### Task 6: Edge broker (desired set, spawn wait, reconcile)

**Files:**
- Create: `server/sessions/display_bridge_broker.py`
- Modify: `server/sessions/fleet_orchestrator.py` (`create_session` ~617: look up the chosen host's
  `display_target`, call the broker, pass `display_target` to the daemon; archive path calls
  `broker.reassert()`), `server/sessions/host_probe.py` (`probe_loop` ~302: call `broker.reassert()`
  each tick)
- Test: `server/tests/test_display_bridge_broker.py`

**Interfaces:**
- Consumes: the RPC `display_bridge_ensure` (Task 4), `parse_display_target` (Task 3) and the
  `Session.display_target_host`/`display_mode` columns.
- Produces:
  - `class DisplayBridgeBroker(*, clients: Callable[[str], DaemonClient | None], hosts: Callable[[], list[Host]], timeout_s: float)`.
  - `async def prepare(self, source: str) -> str | None`. It returns the target host name when that
    target reports `state == "up"` for `source` within `timeout_s`, else None.
  - `async def reassert(self) -> None`.
  - `def desired(self) -> dict[str, list[str]]`: target → sources, from the DB plus in-flight
    `prepare`s.
- The edge passes `display_target=<target or None>` into the daemon `create_session`. A None from
  `prepare` (timeout, disabled host, not accepted) means the daemon gets the target anyway and its own
  liveness check records the precise reason. Pass the target even when `prepare` timed out.

- [ ] **Step 1: Write failing tests** (fake clients, fake DB rows via `isolated_db`):
  - `test_prepare_returns_target_when_up`.
  - `test_prepare_times_out_returns_none_within_timeout`.
  - `test_target_host_disabled_falls_back_virtual` (end to end through `FleetOrchestrator.create_session`
    with fakes).
  - `test_desired_groups_live_remote_sessions_by_target`.
  - `test_reassert_sends_empty_list_to_target_with_no_sessions`.
  - `test_reconcile_reasserts_desired_set_after_restart` (new broker instance, same DB → same calls).
  - `test_kill_switch_off_never_calls_ensure`.
- [ ] **Step 2:** Run them. Expect a FAIL.
- [ ] **Step 3:** Implement. Capture `app.state.sessions` lazily (core.md: the FleetOrchestrator is
  swapped in after routers are built). A failure to reach a target daemon is logged at INFO, not
  WARNING: it is an expected degrade and WARNING auto-files issues (memory).
- [ ] **Step 4:** Run the tests plus `pytest server/tests -k "fleet or probe" -n 4`. Expect a PASS.
- [ ] **Step 5:** Commit: `feat(display): edge brokers display bridges as desired state`.

### Task 7: Web: Fleet target select, status chip, session label

**Files:**
- Modify:
  - `web/src/api/fleet.ts` (types + PUT body).
  - `web/src/api/types.ts` (`displayTargetHost`).
  - `web/src/features/settings/sections/FleetSection.tsx` (Display column).
  - `web/src/features/sessions/displayFallback.ts` (labels for the five new reasons).
  - `web/src/features/sessions/rail/EnvironmentCard.tsx` ("On dobby's desktop" for `remote`).
  - Hide the VNC/browser panel for `remote`, the same as `native`, in `SessionBody.tsx`/`BrowserTab.tsx`.
- Test: `FleetSection.test.tsx`, `displayFallback` tests, `EnvironmentCard` tests (extend existing)

**Interfaces:**
- Consumes:
  - Fleet wire `displayTarget`/`displaySshHost` (Task 3).
  - Fleet wire `displayBridge` (Tasks 3 and 4).
- Exact copy:
  - Select options: "This host (default)" and "{name}'s desktop".
  - Chips:
    - "→ {target}: bridge up"
    - "→ {target}: desktop logged out"
    - "→ {target}: not accepted"
    - "→ {target}: wrong user"
    - "→ {target}: unavailable"
  - Help text: "Programs on this host open as windows on the chosen desktop. X11 programs can read that
    desktop's input while connected; Wayland programs cannot."

- [ ] **Step 1: Write failing tests:**
  - `selecting a target PUTs displayTarget host:dobby`.
  - `self is not offered`.
  - `chip shows not accepted when target accepts lacks this host`.
  - `remote session shows "On dobby's desktop"`.
  - `each new fallback reason has a label`.
- [ ] **Step 2:** Run `(cd web && npx vitest run <files>)`. Expect a FAIL.
- [ ] **Step 3:** Implement on the existing FleetSection table chassis. No new component library.
- [ ] **Step 4:** Run the full web gate (Global Constraints), including the full `npm run test`. Grep
  `web/e2e` for any changed strings. Expect a PASS.
- [ ] **Step 5:** Commit: `feat(web): per-host display target in Fleet settings`.

### Task 8: Runbook, real verification, ship

**Files:**
- Create: `docs/runbooks/display-bridge.md`
- Modify: `deploy/README.md` (pointer), `docs/product/features/` (a short feature note if the directory
  has a pattern for it)

- [ ] **Step 1:** Write the runbook (spec §5):
  - Install waypipe on both hosts.
  - SSH trust direction (target → source, as the session user).
  - `VIBERPIT_DISPLAY_BRIDGE_ACCEPT_FROM=rocky` on dobby's daemon env.
  - Select the target in Fleet.
  - A troubleshooting table keyed by chip state.
  - The X11 security note.
- [ ] **Step 2:** Run all gates (Global Constraints). Expect everything green; record the output paths
  under `/tmp/`.
- [ ] **Step 3:** Open the PR. Branch `feat/per-host-display-target`, body via `--body-file`, `Refs`
  nothing (no issue), ready (not draft), then `gh pr merge <#> --squash --auto`. First check for in-flight
  PRs touching `display_policy.py`, `fleet.py` and `FleetSection.tsx` (core.md GraphQL query).
- [ ] **Step 4:** Do the real verification after deploy. This needs the operator to set
  `ACCEPT_FROM` on dobby and pick the target, so hand back for it:
  - A rocky session runs `foot`, `xeyes` and headed Chromium; each appears as its own window on dobby.
  - Logout → the next session is virtual with `target_desktop_unavailable`.
  - The bridge is gone ~60 s after the last session ends.
  - Attach screenshots to the PR.
