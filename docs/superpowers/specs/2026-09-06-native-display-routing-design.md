# Native vs virtual display routing by client locality — design

**Date:** 2026-09-06
**Status:** approved under stated assumptions (non-interactive session; see §9)
**Extends:** spec 002 (`specs/002-session-x-display/`), the reauth virtual-display design
(`docs/superpowers/specs/2026-06-26-reauth-virtual-display-design.md`)

## 1. Problem

Every session today gets a pooled Xvfb display (or no display at all). That is right for a
phone on the tailnet, which can only see the agent's GUI through the noVNC relay. It is
wrong for the operator sitting at the edge host's own keyboard: a browser the agent opens
lands in a hidden virtual framebuffer they must then watch through VNC, on the very machine
whose real desktop is in front of them.

ccswitch should detect which of the two situations a session is created in and route GUI
output accordingly:

- **Client on the runner host** (the browser driving ccswitch runs on the same machine that
  will run the session, e.g. dobby's own desktop) → the session's `DISPLAY` is the host's
  native X display (`:0`), and "Open a browser" launches Chromium there.
- **Everyone else** (any other tailnet device, automations, the assistant) → today's
  virtual Xvfb display and VNC relay, unchanged.

## 2. Verified facts the design rests on

Checked against the real binaries and hosts on 2026-09-06 (constitution Principle III):

| Fact | Evidence |
|---|---|
| `tailscale whois --json <own tailnet IP>` succeeds and returns `Node.ComputedName` equal to the first hostname label. | On rocky: `Node.ComputedName == "rocky"`, `LoginName` present. `whois 127.0.0.1` → "peer not found". |
| The prod front (`deploy/ccswitch-tailnet.nginx`) strips identity headers and sets `X-Real-IP`, so `verify_identity` already runs whois on every request. | `server/auth/identity.py:144-179`; only `UserProfile.LoginName` is kept. |
| A browser on dobby reaching `https://dobby.tail0dd570.ts.net` connects from dobby's own tailnet IP, so nginx's `$remote_addr` is dobby's node. | MagicDNS resolves to the 100.x address; whois on it works (row 1). |
| dobby runs a real X11 seat on `:0`, and a process owned by `tony` with **no** `XAUTHORITY` can open it: the server grants `SI:localuser:tony`. | `loginctl` type `x11` on seat0; `env -u XAUTHORITY DISPLAY=:0 xdpyinfo` succeeds; `xhost` lists `SI:localuser:tony`. |
| The daemon (`ccswitch-daemon.service`, `User=tony`) has no `DISPLAY`/`XAUTHORITY` in its environment. | `/proc/<MainPID>/environ`. |
| Bare `DISPLAY=:0` with no `XAUTHORITY` is the already-shipped reauth fallback posture. | `server/reauth.py:200-202`. |

## 3. Goals / non-goals

**Goals**
1. Auto-detect client locality at session creation with no user action.
2. Native sessions get `DISPLAY=<native display>` in the agent process env (so anything the
   agent launches — Playwright, chrome-devtools MCP, `xdg-open` — appears on the desktop) and
   "Open a browser" launches Chromium on that display.
3. The decision is persisted and survives daemon restart / rehydrate.
4. Remote clients see no behavior change.
5. An explicit per-request override (`display: native | virtual`) for when auto is wrong.
6. The UI says where a session's windows go, and never offers a VNC canvas for a native
   session.

**Non-goals**
- Moving a *running* session between displays when a different client attaches later. The
  agent process's `DISPLAY` is fixed at spawn; a "move to my desktop" action would be a
  respawn and is a separate feature.
- VNC access to the operator's real desktop (explicitly a non-goal of spec 002 too).
- Wayland-native display routing. dobby is X11; a Wayland host still exposes `:0` via
  Xwayland, which this design would use unchanged, but that is not verified here.
- `tailscale serve` fronting. It sets `X-Forwarded-For`, not `X-Real-IP`; locality would
  resolve to "unknown" → virtual, which is safe. Documented as a limitation.

## 4. Architecture

```
browser on dobby ──HTTPS──▶ nginx (X-Real-IP=100.x dobby) ──▶ edge (server/app.py)
                                                                │  POST /api/sessions
                                                                │  identity + client_node ("dobby")
                                                                ▼
                                        FleetOrchestrator.create_session(prefer=client_node…)
                                                                │  RPC create_session(client_node="dobby", display="auto")
                                                                ▼
                                  daemon SessionOrchestrator._spawn_session
                                    resolve_display_mode(requested, client_node,
                                                         runner=self_host_name(),
                                                         native_available=NativeDisplay.available())
                                    ├─ "native"  → no Xvfb lease; env {DISPLAY: ":0"}
                                    └─ "virtual" → x11.start_display(); env from X11Manager
                                    persist Session.display_mode
```

Three pieces are new; everything else is a threading change.

### 4.1 `server/auth/identity.py` — client node resolution

- Refactor `_whois_identity` into a shared `_whois(request) -> dict | None` (peer-IP
  resolution + argv validation + subprocess + JSON parse, unchanged semantics) and keep
  `_whois_identity` as the `LoginName` projection over it. One peer-trust rule, one
  subprocess wrapper.
- New `client_node_name(request) -> str | None`: `_whois(request)["Node"]["ComputedName"]`,
  lower-cased, or None. Never raises. Loopback peers with no forwarded IP resolve to None
  (whois has no peer), so a `curl 127.0.0.1` never counts as "local" by accident.
- New FastAPI dependency `current_client_node(request) -> str | None` in
  `server/auth/middleware.py`, next to `current_identity`. It is **not** computed by the
  middleware for every request: only the create route asks, so the extra whois subprocess
  is paid once per session creation.

### 4.2 `server/sessions/display_policy.py` — the decision (pure)

```python
DISPLAY_NATIVE = "native"
DISPLAY_VIRTUAL = "virtual"
DISPLAY_AUTO = "auto"           # request-only; never persisted

def resolve_display_mode(*, requested: str | None, client_node: str | None,
                         runner: str, native_available: bool) -> str:
```

Rules, in order:
1. `requested == "virtual"` → virtual.
2. `not native_available` → virtual (covers headless hosts, feature disabled, display
   number colliding with the Xvfb pool range).
3. `requested == "native"` → native (explicit override needs no locality proof).
4. `requested in (None, "auto")` and `client_node == runner` (case-insensitive) → native.
5. Otherwise virtual.

An unknown `requested` value is rejected at the API boundary (pydantic `Literal`), so the
resolver treats anything else as auto. The runner's name is `server.hostname.self_host_name()`
on the daemon that spawns — the same name the fleet registry uses for that host — so the
comparison is always "client node == the host that will own the process".

### 4.3 `server/sessions/native_display.py` — the host desktop as a display target

```python
class NativeDisplay:
    def __init__(self, display: str = ":0", *, run_dir="/tmp", chromium_cmd, no_sandbox,
                 max_browsers: int, browser_liveness_s: float = 0.0,
                 spawn=asyncio.create_subprocess_exec, socket_dir="/tmp/.X11-unix",
                 blocked_range: range | None = None): ...
    @property
    def display(self) -> str
    def available(self) -> bool          # socket exists AND display number not in blocked_range
    def env(self) -> dict[str, str]      # {"DISPLAY": display}; NO XAUTHORITY (§2)
    async def launch_browser(self, session_id, url) -> bool
    async def close(self, session_id) -> None      # terminate this session's browsers, rm profiles
    async def stop_all(self) -> None

def build_native_display(settings) -> NativeDisplay | None   # None when disabled
```

- `available()` is evaluated at each spawn, not cached: an X session that starts after the
  daemon booted must work, and one that logs out must stop being offered.
- `blocked_range` is the Xvfb pool `[display_start, display_start + display_count)` when
  `enable_xvfb` is on. A native display inside it is refused with one warning at build time,
  because `X11Manager.sweep_orphan_processes` reaps every in-range Chromium — the hazard spec
  002 §config already documents for `CCSWITCH_REAUTH_DISPLAY`.
- `launch_browser` reuses `resolve_chromium()` and the same argv posture as
  `X11Manager.launch_browser` (throwaway `--user-data-dir`, optional `--no-sandbox`), with a
  per-session process list so `close()` can terminate them. It shares the `max_browsers` cap.
  No debug-port support (that is the virtual display's CDP feature; not needed here).
- Native browsers on `:0` are outside the managed range, so the orphan sweep never touches
  them; `stop_all()` at daemon shutdown is their only reaper. `close()`/`stop_all()` use the
  existing `_terminate` helper from `x11manager.py` (imported, not copied).

### 4.4 Config (`server/config.py`, BOTH branches — pydantic and dataclass fallback)

| Var | Default | Meaning |
|---|---|---|
| `CCSWITCH_NATIVE_DISPLAY_ENABLED` | `true` | Master switch. Off → every session is virtual, `client_node` is still resolved (cheap) but ignored. |
| `CCSWITCH_NATIVE_DISPLAY` | `:0` | The X display string used for native sessions. |

Default-on is safe because `NativeDisplay.available()` is gated on the X socket: a headless
worker never has one. `CCSWITCH_REAUTH_DISPLAY` stays as is — reauth is a master-only flow
with its own "fallback when virtual is unavailable" semantics, and the two knobs default to
the same value. `server/tests/test_config_xvfb.py` gains cases for both new fields so the
dataclass branch cannot be forgotten.

### 4.5 Persistence

- `Session.display_mode: Mapped[str | None]` — nullable String, `None` = created before this
  feature (treated as virtual everywhere). Nullable so `init_db`'s reconcile ALTER-ADDs it on
  both dialects; no `_REQUIRED_EXTRAS`/`RECONCILE_EXEMPT` entry needed.
- Migration `0029_session_display_mode.py`, `down_revision = "0028_assistant_tokens"`
  (reviewable record only, per the repo rule).
- `SessionRecord.display_mode: str | None = None`; `to_dict`/`from_dict` key `displayMode`;
  `_record_from_row` maps it; the API serializer in `server/api/sessions.py` emits
  `displayMode`.

### 4.6 Orchestrator threading (`server/sessions/orchestrator.py`)

- `create_session(..., client_node: str | None = None, display: str | None = None)` and
  `_spawn_session(..., client_node=None, display=None)`.
- New `self._native: NativeDisplay | None` (wired in `server/app.py` in-process mode and
  `server/ipc/serve.py`, mirroring `_x11`).
- In `_spawn_session`, right where the Xvfb lease is taken today:
  ```python
  mode = resolve_display_mode(requested=display, client_node=client_node,
                              runner=self_host_name(),
                              native_available=self._native is not None and self._native.available())
  if mode == DISPLAY_VIRTUAL and self._x11 is not None:
      start_display(...)            # unchanged
  ```
  `mode` goes on the pre-record and the persisted row.
- `_display_env(session_id) -> dict`: native record and `self._native` available → `self._native.env()`; otherwise `x11.env_for(session_id)` (or `{}`). `_session_factory` calls this instead of `x11.env_for` directly — the single place DISPLAY is chosen for a process.
- `ensure_driver` (rehydrate/resume): skip `start_display` when the record is native. If the
  native display is gone by then, the env is `{}` — the same degrade as an exhausted pool
  today. The persisted mode is not rewritten.
- `adopt_session`: unchanged (virtual). Adoption has no browser client; it can be extended
  later if wanted.
- `_teardown_driver_state`: `await self._native.close(session_id)` next to `stop_display`.
- New `open_browser(session_id, url) -> dict` on the orchestrator: dispatches to
  `NativeDisplay.launch_browser` for native records, `X11Manager.launch_browser` otherwise,
  returning `{"ok", "reason"}` with reasons `no_display | cap | native_unavailable`. The
  daemon RPC `x11_open_browser` and the in-process branch of `POST …/browser/open` both call
  it, so the display-mode branch exists once. `RemoteOrchestrator` keeps its existing
  `x11_open_browser` proxy (no new unproxied method — see the "green tests hide unproxied
  orch methods" lesson).

### 4.7 Fleet and RPC

- `FleetOrchestrator.create_session`: if `prefer is None`, `display != "virtual"` and
  `kwargs.get("client_node")` names a registered host (`self._router.names()`), set
  `prefer = client_node`. An explicit `host` in the request still wins. If the preferred host
  is unhealthy the router falls through to the next candidate and the daemon there resolves
  virtual (client ≠ runner) — correct by construction.
- `server/ipc/orchestrator_rpc.create_session` splats params; nothing to change.
- Rolling-upgrade shim in `RemoteOrchestrator.create_session`: a daemon that predates this
  change raises `TypeError` at call binding on the unknown kwargs (before any work starts).
  On `DaemonError(err_type="TypeError")` whose message names `client_node` or `display`,
  retry once without them and log a warning. Same spirit as the existing `engine` pop for
  protocol-v1 daemons.

### 4.8 API

- `CreateSessionRequest.display: Literal["auto", "native", "virtual"] | None = None`.
- The create route takes `client_node: str | None = Depends(current_client_node)` and passes
  `client_node=client_node, display=body.display`.
- `POST /api/sessions/{id}/browser/open` 409 detail gains `"native display unavailable"`.
- Session payloads carry `displayMode`.

### 4.9 Web

- `Session.displayMode?: 'native' | 'virtual' | null` in `web/src/api/types.ts`;
  `CreateSessionInput.display?` (API parity; no form control — YAGNI).
- `SessionBody` passes `displayMode` and `host` to `BrowserTab`. For a native session
  `BrowserTab` does **not** open the VNC socket; it renders a notice ("This session opens
  windows on {host}'s desktop." — `host` falls back to "the server" for primary-local rows,
  whose `host` is NULL by design) and keeps the "Open a browser" button, which hits the same
  endpoint.
- `SessionCard` and `SessionMeta`: a small "desktop" badge (Monitor icon, title "Windows
  open on the host's desktop") next to the existing host chip when `displayMode === 'native'`.
- Tests: BrowserTab native state (no VncClient constructed, notice shown, button works),
  SessionCard badge, `types` compile. Run the **full** vitest, not changed files.

### 4.10 Docs

- `docs/product/features/remote-desktop-vnc-x11.md`: new "Native display routing" section
  (detection rule, override, limitations, the `xhost SI:localuser` requirement).
- `specs/002-session-x-display/quickstart.md` config table: the two new vars and the
  pool-range collision note now covering `CCSWITCH_NATIVE_DISPLAY` too.

## 5. Data flow (native case, end to end)

1. Operator on dobby's desktop dispatches a session from the web UI.
2. nginx forwards with `X-Real-IP = dobby's tailnet IP`; middleware verifies identity as
   today (whois → LoginName).
3. The create route's `current_client_node` dependency runs whois once more →
   `"dobby"`.
4. `FleetOrchestrator` sets `prefer="dobby"`; router ranks dobby first; RPC to dobby's
   daemon carries `client_node="dobby", display=None`.
5. `_spawn_session` resolves `native` (runner `"dobby"`, socket `/tmp/.X11-unix/X0`
   present, feature enabled), skips the Xvfb lease, persists `display_mode="native"`.
6. `_session_factory` → `_display_env` → `{"DISPLAY": ":0"}` merged into the SDK env.
   Chrome the agent launches appears on dobby's screen.
7. UI shows the "desktop" badge; the Browser tab shows the notice instead of a canvas;
   "Open a browser" → `open_browser` → `NativeDisplay.launch_browser` → Chromium on `:0`.
8. Daemon restart: `rehydrate` → `_record_from_row` restores `display_mode="native"`;
   `ensure_driver` skips the lease and injects `:0` again.
9. Close/archive: `NativeDisplay.close(session_id)` terminates that session's Chromium
   windows and removes their throwaway profiles.

## 6. Error handling

- whois failure/timeout → `client_node=None` → virtual. Never blocks creation.
- Native display socket missing at spawn → virtual, one info log line.
- Native display inside the Xvfb pool range → `build_native_display` returns a
  `NativeDisplay` whose `available()` is always False, with one warning at boot.
- Chromium missing / dies in the liveness window → `open_browser` returns
  `{"ok": False, "reason": "cap"}` semantics unchanged from the virtual path; 409 to the UI.
- Old daemon on the far side of the RPC → shim retry without the new kwargs (§4.7); the
  session is virtual and a warning names the host so the operator knows to upgrade it.
- `display_mode` NULL on old rows → every consumer treats it as virtual.

## 7. Security

- Locality is derived from `tailscale whois` on a peer address the existing peer-trust rule
  already vets (`_peer_is_trusted` + `X-Real-IP`), never from a client-supplied header. A
  remote client cannot claim to be local.
- The explicit `display: "native"` override is available to any authenticated user. It only
  changes which display a process the same user could already spawn renders on, and only
  when that host has a desktop; no new capability is exposed. Windows on the operator's
  desktop from a remote requester are a UX concern, not a boundary crossing, and the
  override is opt-in per request.
- No new network exposure: native sessions have *less* surface (no x11vnc, no RFB port).

## 8. Testing

Server (`server/tests/`):
- `test_auth_client_node.py`: `client_node_name` with a fake `subprocess.run` (loopback +
  `X-Real-IP`, untrusted peer ignores the header, whois failure → None, argv injection
  guard). Reuse the request builders in `test_auth_identity_peer_trust.py`.
- `test_display_policy.py`: the five rules, case-insensitivity, None/auto equivalence.
- `test_native_display.py`: `available()` on socket presence and range block; `env()` has
  no `XAUTHORITY`; `launch_browser` argv/env with a fake spawn; `close` terminates only that
  session's procs; `stop_all`.
- `test_orchestrator_native_display.py` (pattern: `test_orchestrator_copilot_env.py`):
  native create skips `start_display` and the fake factory receives `DISPLAY=:0`; remote
  client gets the Xvfb env; explicit `display="virtual"` from a local client stays virtual;
  rehydrate of a native row re-injects `:0` without a lease; `open_browser` dispatch.
- `test_config_xvfb.py`: both settings branches parse the new vars.
- `test_remote_orchestrator_x11.py` or a sibling: the TypeError retry shim.
- `test_fleet_orchestrator*`: `prefer` nudge from `client_node`, and that an explicit host
  wins.
- Migration/reconcile: the existing schema-drift and reconcile-coverage tests pick the new
  nullable column up automatically; run them.

Web (`web/`): full `npm run typecheck`, `npm run lint`, `npm run test`, `npm run build`.

## 9. Assumptions made without the owner (flag on review)

1. **Creator decides, not attacher.** Chosen because `DISPLAY` is fixed at spawn.
2. **Local clients prefer their own host for placement.** Chosen because "always on
   dobby:0" is unachievable otherwise. Reversible by deleting one branch in
   `FleetOrchestrator.create_session`.
3. **Feature default-on**, gated on X-socket presence, because the request is for the
   detection to just work on dobby. `CCSWITCH_NATIVE_DISPLAY_ENABLED=false` turns it off.
4. **No `XAUTHORITY` for native**, matching the verified host posture and the reauth
   fallback. A host without `SI:localuser` access would need `xhost +SI:localuser:<user>`
   in its session startup; documented, not automated.
5. Adopted sessions stay virtual.
