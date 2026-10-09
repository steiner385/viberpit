# Wayland coexistence — audit and plan

**Date:** 2026-10-09 · **Status:** approved — §4 decided 2026-10-09: Q1 Wayland-native (`auto`), Q2 auto-route to virtual + show the reason

## 1. What changed on the host

dobby's desktop is now a GNOME/Mutter **Wayland** session (logind `Type=wayland`, session 45).
X clients are served by **rootless Xwayland**:

```
/usr/bin/Xwayland :0 -rootless -noreset … -auth /run/user/1000/.mutter-Xwaylandauth.N7R0W3 …
```

Verified facts (Principle III — probed on dobby, 2026-10-09):

| Probe | Result |
|---|---|
| `env -u XAUTHORITY DISPLAY=:0 xhost` | **`Authorization required`** — Mutter's Xwayland does *not* grant `SI:localuser:tony` |
| `XAUTHORITY=<previous login's cookie> … xdpyinfo` | **`Invalid MIT-MAGIC-COOKIE-1 key`** — the cookie path is randomized and rotates on every login; old files linger in `/run/user/1000` |
| `env -i XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show-environment` | `DISPLAY=:0`, `XAUTHORITY=…N7R0W3` (current), `WAYLAND_DISPLAY=wayland-0`, `XDG_SESSION_TYPE=wayland` — discoverable from a scrubbed, daemon-like env |
| `pgrep -u 1000 -a Xwayland` | argv carries `:0` and `-auth <cookie>` — second discovery path |
| `env -i DISPLAY=:0 XAUTHORITY=<discovered> xdpyinfo` | connects |
| `/tmp/.X11-unix/` | `X0` (Xwayland), `X1` (Mutter's on-demand reservation), `X2`–`X7` **stale, no listener** |
| `/run/user/1000/gdm/Xauthority` | gone (the old X11-era path) |

## 2. Audit — every X11 dependency, triaged

### BREAKS

| # | Where | Assumption | Effect today |
|---|---|---|---|
| B1 | `server/sessions/native_display.py:1-8, 63-64, 86-88` | `:0` grants `SI:localuser`, so no `XAUTHORITY` is needed | Every native-session Chromium (and every X client the agent runs, e.g. headful `chrome-devtools-mcp`) is refused by Xwayland |
| B2 | `native_display.py:53-61` `available()` | "socket file exists" == "display usable" | Reports **available** while every launch fails → sessions keep being routed native; the UI offers a broken "Open a browser" |
| B3 | `server/reauth.py:192-206`, `config.py:109/448` (`VIBERPIT_REAUTH_DISPLAY=:0`) | Same bare-`:0` posture for the no-Xvfb fallback | Reauth fallback browser can't open |
| B4 | `orchestrator.py:5788-5789` | A failed native launch is reported as reason `"cap"` | Misleading error — hides B1 |
| B5 | Host: live `chrome-devtools-mcp` processes carry `XAUTHORITY=…UP53W3` (previous login) | A cookie captured once stays valid | Any long-lived process that snapshotted the env breaks on relogin |

### DEGRADES

| # | Where | Note |
|---|---|---|
| D1 | `x11manager.py:70-71` ready-check, `alloc()` | Socket-exists is used for readiness and allocation never checks `/tmp/.X{n}-lock`; stale sockets (X2–X7 exist now) make "exists" meaningless. Pool `:100–:111` doesn't collide today — pre-existing fragility, same shape as B2 |
| D2 | `x11manager.py:341`, `claude_sdk_factory.py:96-98`, `native_display.py:88` | Children inherit the full `os.environ`; `WAYLAND_DISPLAY`/`XDG_SESSION_TYPE` are never scrubbed and no launch pins `--ozone-platform`. Harmless while the daemon is a system unit, but if it ever inherits a desktop env, a *virtual* session's Chrome would auto-select Wayland and draw on the operator's real desktop instead of its Xvfb |
| D3 | `native_display.py:76-83` | Native Chromium runs as an Xwayland client: blurry under fractional scaling, clipboard bridged |
| D4 | `x11manager.py:434` orphan sweep matches only `chromium*` | `resolve_chromium()` can pick `google-chrome` (it does on dobby), so leaked Chrome on Xvfb isn't reaped. Not Wayland-caused; same file, fix alongside |
| D5 | Host: `godot` MCP server (shared `.claude.json`) | GUI on whatever `DISPLAY` it inherits — inherits B1's fate |

### FINE (no change needed)

- Entire **virtual** path: Xvfb pool + per-session xauth cookie + lazy `x11vnc` + `/ws/vnc` relay + noVNC UI + X keysyms (`web/src/vnc/keyRelay.ts`). Self-contained; independent of the host compositor.
- Nothing screen-captures or VNCs `:0` (rootless Xwayland has no root-window content — this would have been a hard break, but no code does it). No xdotool/xrandr/xclip/XTEST anywhere.
- Deploy units, runbooks (Xvfb install), Playwright e2e (headless), session `mcp_servers` overlay (only `viberpit`), `src/viberpit/auth_login.py` (D-Bus keyring works the same under Wayland).
- Web `displayMode: 'native' | 'virtual'` — the UI never assumed X11 for native sessions; it only says "opens windows on {host}'s desktop".

### Stale docs / memory

- `docs/superpowers/specs/2026-09-06-native-display-routing-design.md:34-36, 56-57, 326-328` (explicitly deferred Wayland: "would use `:0` unchanged, not verified"), `docs/product/features/remote-desktop-vnc-x11.md:89-90`, `docs/superpowers/specs/2026-06-26-reauth-virtual-display-design.md:94`.
- Agent memory notes that hardcode `DISPLAY=:0` or `/run/user/1000/gdm/Xauthority` (several, across accounts/projects; listed in the PR description).

## 3. Design — "desktop session" as the single source of truth

The bug's shape is **N places that each assume what the desktop is** (native display, reauth fallback,
the env handed to agents). Replace them with one resolver, evaluated live per use, because the cookie
rotates at every login.

### 3.1 `server/sessions/desktop_session.py` (new, pure-ish)

```python
@dataclass(frozen=True)
class DesktopSession:
    kind: Literal["x11", "wayland"]
    display: str                 # ":0"  — always present: Xwayland on Wayland hosts
    xauthority: str | None       # live cookie path, validated to exist
    wayland_display: str | None  # "wayland-0" (kind == "wayland" only)
    xdg_runtime_dir: str         # "/run/user/1000"

    def env(self, *, prefer_wayland: bool) -> dict[str, str]: ...

def resolve_desktop_session(uid: int, *, configured_display: str) -> DesktopSession | Unavailable: ...
```

Discovery order (all three verified from a scrubbed env above):
1. **systemd user manager env** (`systemctl --user show-environment` with `XDG_RUNTIME_DIR=/run/user/<uid>`): compositor-agnostic — GNOME and KDE both import `DISPLAY`/`XAUTHORITY`/`WAYLAND_DISPLAY`/`XDG_SESSION_TYPE` at each login, so it reflects the *current* login.
2. **Xwayland argv** (`/proc/*/cmdline` for uid's `Xwayland`, read `:N` and `-auth`): fallback if the user manager is unreachable.
3. **Legacy X11**: `XAUTHORITY` unset + `SI:localuser` — keeps an Xorg host (or a future revert) working.

`Unavailable` carries a **reason** (`no_graphical_session`, `cookie_missing`, `connect_refused`, …) so B4's
"cap" mislabel goes away and health/UI can say *why*.

Cache with a short TTL (≈5 s) keyed on nothing global that tests can latch — see memory
"global TTL cache latches fake subprocess verdicts": inject the clock/probe, never a module global.

### 3.2 Consumers

| Consumer | Change |
|---|---|
| `NativeDisplay.available()` | Resolver + a **real connect probe** (open the Unix socket and complete an X11 setup handshake with the cookie — or `xdpyinfo` with a 2 s timeout). Replaces socket-exists (B2) |
| `NativeDisplay.env()` / `launch_browser` | Resolver env (fixes B1). Chromium launch flag per decision §4-Q1 |
| `orchestrator._display_env` (native branch) | Same env → the agent's own X/Wayland clients (headful `chrome-devtools-mcp`, godot) work in native sessions (B1, D5) |
| `reauth.py` `:0` fallback | Resolver instead of `reauth_display` default; keep `VIBERPIT_REAUTH_DISPLAY` as an explicit override (B3) |
| Virtual (Xvfb) env builder | **Scrub** `WAYLAND_DISPLAY`, `XDG_SESSION_TYPE`, `WAYLAND_SOCKET` and pin `--ozone-platform=x11` on managed Chromium (D2) |
| `x11manager` | Ready-check = connect probe; `alloc()` skips numbers with a live lock/listener and reclaims stale sockets in-range only (D1); orphan sweep matches the resolved Chromium basename (D4) |
| Metrics / `/healthz` / session payload | `nativeDisplay: {available, kind, reason}`; web shows "Wayland desktop" / "X11 desktop" and the unavailable reason instead of a dead button |

`display_mode` in the DB stays `native | virtual` — the compositor is a property of the *host at launch
time*, not of the session, so it belongs in metrics, not a column (no migration).

### 3.3 Config

- `VIBERPIT_NATIVE_DISPLAY` becomes **optional** (default: auto-discover). An explicit value still wins.
- New `VIBERPIT_NATIVE_BROWSER_PLATFORM = auto | wayland | x11` (default per §4-Q1). `VIBERPIT_` prefix per the brand ratchet.
- Add both to `.env.example` and the dataclass fallback in `config.py` (it duplicates the pydantic model — keep them in step; consolidating them is out of scope).

### 3.4 Host-side (not repo code — runbook + one-off ops)

- Restart the stale `chrome-devtools-mcp` processes holding the old cookie (B5) — they die with their sessions anyway.
- Clean stale `/tmp/.X11-unix/X2`–`X7` (no listener; verify with `ss -xlp` first).
- Runbook section in `docs/runbooks/` "Wayland desktop hosts": how discovery works, how to diagnose (`systemctl --user show-environment`, `pgrep -a Xwayland`), and that a relogin rotates the cookie.
- Fix the stale memory notes (bare `DISPLAY=:0`, `gdm/Xauthority`) → point them at the discovery recipe.

## 4. Decisions needed

**Q1 — Native browser: Wayland-native or Xwayland?**
- *(Recommended)* **`auto` → Wayland-native** (`--ozone-platform=wayland` + `WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`) on a Wayland host: crisp under scaling, native clipboard, no Xwayland dependency. `DISPLAY`+`XAUTHORITY` are still exported so X-only tools the agent spawns work.
- **Xwayland**: smallest change (just add the cookie), behaves exactly like the old X11 world, but blurry under fractional scaling.

**Q2 — Fail mode when the desktop is locked / logged out:** route to `virtual` silently (today's
behaviour for a missing socket) vs. surface "desktop unavailable: <reason>" and let the user choose.
Recommended: auto-route to virtual **and** show the reason on the session badge.

## 5. Task breakdown (TDD; subagent-driven execution)

1. `desktop_session.py` + tests: resolver over injected `run`/`read_proc`/`probe`; fixtures for Wayland (systemd env), Wayland (argv fallback), Xorg (no cookie), stale cookie, logged-out.
2. `NativeDisplay` on the resolver: `available()` connect probe, env, platform flag; replace `"cap"` with the reason. Update `test_native_display*.py`, `test_orchestrator_native_display.py`.
3. Reauth fallback on the resolver; `test_reauth_manager.py`.
4. Virtual env scrub + `--ozone-platform=x11`; regression test asserting no `WAYLAND_DISPLAY` reaches an Xvfb child even when the daemon env has one.
5. `x11manager` connect-probe readiness, lock-aware `alloc()`, sweep basename fix.
6. Metrics/healthz/session payload `{available, kind, reason}` + web badge/disabled-state copy (+ `web/e2e` grep for changed strings).
7. Docs: update the three stale specs, feature doc, new runbook section; `.env.example`.
8. Host ops (§3.4) after deploy.

Each task is independently shippable; 1→2→3 are ordered, 4–5 are independent of 1–3.
