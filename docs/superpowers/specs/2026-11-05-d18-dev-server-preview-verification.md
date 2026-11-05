# D-18 "Dev-server detection + preview": verification spike

Date: 2026-11-05. Constitution III: every claim below was checked in one of three ways: in ViberPit's code, against
an installed binary (CLI 2.1.318, Claude Desktop, Chrome), or by a throwaway local probe. Live hosts were read
only. Claims that were not checked are listed under **Gaps**.
Parent spec: `2026-11-01-ui-desktop-parity-design.md:373` (the "Dev-server preview" row, `[A-D19]`: "Detect listening
dev servers started by the session and offer them in the Browser tab (VNC) or a proxied preview with element
select.") and `:437` (`D-18 | Dev-server detection + preview`). Benchmark:
`docs/audits/2026-11-01-claude-desktop-code-benchmark.md:189` ("Preview browser with dev-server detection", ◐, "VNC
Browser tab, no dev-server detect") and owner decision D19 at `:287`.
Earlier spikes: `2026-11-05-d17-move-session-verification.md` (format and harness).

**Headline findings.**
1. **"Open a URL in the Browser tab" already exists server-side and is routed to the session's host.**
   `POST /api/sessions/{id}/browser/open {url}` → `x11_open_browser` (a `_PER_SESSION` fleet method) → the owning
   daemon starts a Chromium on that session's display with the URL. The web UI never sends a `url`. So the
   no-proxy half of D-18 is mostly detection plus one button.
2. **The Browser tab cannot stream a worker host's display.** `/ws/vnc` in remote mode always proxies to the
   **primary** daemon (`app.state.daemon_endpoint`), never to the session's host. A rocky session's VNC therefore
   lands on dobby's daemon, which has no display for it (close 4404). Today all rocky sessions are `virtual` and all
   dobby ones `native`, so **no live session can use the VNC Browser tab at all**. This must be fixed before
   "Open in Browser tab" is worth anything for rocky.
3. **Two security defects sit on the path D-18 would widen.**
   - **Flag injection.** Both browser launchers append the caller's `url` to Chromium's argv unvalidated. Chrome 157
     treats a positional `--version` as a switch, and honours `--` as the switch terminator (probe §A.3).
   - **No Origin check on any WebSocket** (and no CSRF guard on the HTTP API). `/ws` accepts `prompt` frames from
     any Origin, and identity is re-derived per request from the Tailscale header. Any page the user's tailnet
     browser loads can open `wss://dobby…:8443/ws` as the user (probe §E.2). A preview of arbitrary dev-server
     JS makes this the first thing to fix, whatever the origin layout.
4. **Detection by process tree is cheap and works**, with one known hole (§B.2): a daemon-wide `/proc` pass takes
   15–25 ms and maps each listening socket to a session through the same tree `memory_watch` already walks.

## 0. Versions, hosts and probes

- **CLI.** `~/.local/bin/claude` → `versions/2.1.318` (sha256 prefix `708c77b204db3008`, the D-14 to D-17 file).
  Every `@N` offset below is a byte offset into this file.
- **Claude Desktop.** `/usr/lib/claude-desktop/resources/{app.asar,ion-dist.asar}` (Nov 3 build), read-only by byte
  search; the running app was not touched. Offsets are into those files.
- **Chrome** `157.0.8103.38` (`/usr/bin/google-chrome`); dobby also has `/usr/local/bin/chromium`. rocky has
  `google-chrome` only, which `resolve_chromium` picks (`x11manager.py:21-33`).
- **Vite** `8.0.16`, run from `/home/tony/GitHub/viberpit-deploy/web/node_modules/vite` (read-only use).
- **Fleet at the time** (`GET 127.0.0.1:8787/api/fleet/hosts`, `/api/sessions`): dobby primary and rocky worker,
  both at `bac0b655`. Live sessions: dobby 6, all `displayMode: native`; rocky 5, all `virtual`. rocky ran
  `Xvfb :100`, `:101` and `:110` for three of them. `GET /api/x11/metrics` on the edge reported
  `displays_allocated: 0` (the primary's view only, §A.1).
- **Probes** (`/tmp/d18-probe`, `/tmp/d18-scripts`, deleted afterwards):
  - P1, listener attribution: a stand-in "CLI" shell started `python -m http.server` on `127.0.0.1:47811` and
    `0.0.0.0:47812`, a raw TCP listener on `127.0.0.1:47813`, and Vite on `127.0.0.1:47814`. `listeners.py` (a
    `/proc`-only scanner, §B.1) mapped them back to the root pid and sent one HTTP probe each.
  - P2, Vite behaviour behind a proxy: Host-header check and HMR WebSocket handshake with a foreign Origin.
  - P3, Chromium argv: `google-chrome --headless=new --dump-dom --version` with and without a `--` terminator,
    each with a throwaway `--user-data-dir`.
  - P4, edge Origin handling: a raw WS upgrade to `127.0.0.1:8787/ws/vnc?session=d18-probe-nonexistent` with
    `Origin: https://evil.example`. The session id does not exist, so the daemon answered 4404 and nothing was
    attached.
  - Read-only `/proc` scans of both daemons' trees. rocky's ran by piping the script into `ssh rocky python3 -I -`
    (no file written on rocky).
  - No model calls, no daemon restarts, no session touched, nothing bound beyond `127.0.0.1` except P1's
    `0.0.0.0:47812` for a few seconds, which was the bind-address check itself.

## A. The Browser tab today

### A.1 How it works

- **One virtual X display per session, on the session's host.** `X11Manager` (`server/sessions/x11manager.py:146`)
  leases `:N` from a pool (`VIBERPIT_DISPLAY_START` 100, `_COUNT` 12, `server/config.py:235-236`). `start_display`
  (`:190`) writes a per-session MIT cookie and spawns `Xvfb :N -auth <cookie> -nolisten tcp` (`:224-236`).
  - It is started at spawn for `display_mode == virtual` (`orchestrator.py:6086-6092`), and torn down with the
    driver (`:6684-6690`, in the per-session teardown). **A detached idle session has no display.**
  - The `claude` process gets `DISPLAY`/`XAUTHORITY` through `_display_env` (`orchestrator.py:2526-2537`), so the
    agent's own browsers (Playwright, chrome-devtools MCP) draw there too. This is the spec 002 design: "one
    framebuffer per session" (`specs/002-session-x-display/spec.md:16-23`).
- **VNC.** `x11vnc -rfbport <5900+N…> -localhost -nopw -auth <cookie> -shared` is spawned lazily on the first attach
  (`ensure_vnc`, `x11manager.py:350-372`) and stopped after 30 min with no client (`:392-397`,
  `VIBERPIT_VNC_IDLE_TIMEOUT_S`).
- **Transport.** The web viewer is noVNC `^1.7.0` (`web/package.json:18`) on `/ws/vnc?session=<id>`
  (`BrowserTab.tsx:118-120`). The edge relays to the daemon's `/vnc` (`server/bridge/vnc_ws.py:147-176`), which
  relays to the local RFB port (`server/ipc/x11_rpc.py:39-57`). No websockify, no window manager.
- **Which browser.** "Open a browser" spawns the first of `chromium`, `chromium-browser`, `google-chrome`,
  `google-chrome-stable` (`x11manager.py:21`) with `--ozone-platform=x11`, a **fresh throwaway profile per launch**
  and the URL as a positional argument (`:399-456`). The cap is `VIBERPIT_MAX_BROWSERS` = 6 per session
  (`config.py:237`); each launch is a new Chromium process.
- **Native mode** (the operator is at the runner host's desktop): `NativeDisplay.launch_browser`
  (`server/sessions/native_display.py:155-201`) opens the window on the real desktop (dobby is Wayland,
  `kind: wayland` in `/api/x11/metrics`). The Browser tab shows only "This session opens windows on <host>'s
  desktop" and an **Open a browser** button (`BrowserTab.tsx:191-200`). Nothing is streamed.
- **Tab gating.** `SessionBody` shows the Browser tab when `GET /api/x11/metrics` is not `{enabled: false}`
  (`SessionBody.tsx:136-146`). In remote mode that RPC goes to the **primary** daemon only (`x11_metrics` is not
  in `_PER_SESSION`; it falls through `FleetOrchestrator.__getattr__` to the primary, `fleet_orchestrator.py:1293-1300`),
  so the metrics never describe rocky.

### A.2 Which host the browser runs on — and the VNC routing gap

- **The browser launch is routed correctly.** `x11_open_browser` is in `_PER_SESSION` (`fleet_orchestrator.py:243`)
  and in `_MOVE_BLOCKED` (`:260`), so the edge sends it to `sessions.host` (`remote.py:1659-1660` →
  `x11_rpc.py:68-80` → `SessionOrchestrator.open_browser`, `orchestrator.py:6699-6718`). **The Chromium runs on the
  session's host, so `http://127.0.0.1:<port>` in it reaches that host's dev server.**
- **The VNC stream is not routed.** `build_vnc_ws_router` is given `lambda: app.state.daemon_endpoint`
  (`server/app.py:1176-1181`), which is the primary daemon's endpoint (`:868`). Only the reauth display is pinned
  differently (`_local_vnc_x11`, `:1167-1174`). The daemon's `/vnc` looks only in its own `X11Manager`
  (`x11_rpc.py:42`). So a rocky session's Browser tab connects to dobby's daemon and gets 4404 "no display", while
  its Xvfb and Chromium run on rocky. This dates from before the fleet: #204 made the WS "transport-agnostic"
  for the single-daemon remote mode, and #733 moved reauth back to the master.
  - Verified by code reading only; no live WS was opened for a real rocky session, because attaching spawns
    `x11vnc` on that session's display (Gaps).
  - Fix shape: route `/ws/vnc` by `sessions.host` like any `_PER_SESSION` call, using the fleet router's per-host
    `DaemonClient.endpoint` and SSL context (`server/ipc/client.py:388-404`, `fleet_router.py:72`).

### A.3 Can the tab navigate programmatically?

- **To a new window: yes, today.** `POST /api/sessions/{id}/browser/open` with `{"url": …}` (`app.py:1254-1265`)
  launches a Chromium on the session's display with that URL. The UI posts no body (`BrowserTab.tsx:147`), so the
  feature is unused.
- **No input validation.** `url` goes straight into argv (`x11manager.py:433-434`, `native_display.py:175-176`).
  - **P3:** `google-chrome --headless=new … --dump-dom --version` printed `Google Chrome 157.0.8103.38`, so a
    positional `--…` is parsed as a switch. With `-- --version` it printed an empty DOM, so `--` is honoured as
    the switch terminator.
  - Impact: an authenticated caller can pass arbitrary Chromium switches, including ones that name a command to
    run. The route is JSON-bodied (a cross-origin form cannot reach it without a CORS preflight, and there is no
    CORS middleware), so this is an authenticated-caller bug, not a drive-by. It has **no `require_human` gate**.
  - Fix: accept only `http:`/`https:` URLs (D-18 needs only `http://127.0.0.1|localhost|[::1]:<detected port>`),
    and insert `--` before the URL in both launchers.
- **Into the running browser: no.** There is no CDP, xdotool or extension path:
  - `launch_browser(debug_port=True)` adds `--remote-debugging-port=<rfbport+1000> --remote-allow-origins=*`
    (`x11manager.py:418-422`), but **nothing in production passes it**, and `debug_port_for` (`:471`) has no
    caller (Principle VI: dead option; either use it in D-18 or delete it).
  - xdotool is on dobby (`/usr/bin/xdotool`), not on rocky.
  - Each "open" is a new process and profile, so six previews exhaust the cap. Reusing one profile per session
    would hand the URL to the running instance (Chromium's singleton opens a tab and the second process exits),
    but `_await_browser_alive` (`VIBERPIT_BROWSER_LIVENESS_S` 0.3 s, `config.py:246`) would then report that quick
    exit as a failed launch. CDP `Target.createTarget` on one debug-enabled browser per session is the clean way.

## B. Detecting dev servers

### B.1 Mechanism (P1, measured)

- **No psutil.** The shared venv has no `psutil` (`ModuleNotFoundError`), and `pyproject.toml` does not list it.
  ViberPit already reads `/proc` directly in three places; reuse them rather than add a dependency:
  - `server/sessions/memory_watch.py`: `scan_proc` (`:95`, pid/ppid/rss/comm for every process),
    `read_proc_children` (`:138`), `_subtree` (`:161`) and **`match_session` (`:186-204`), which maps a daemon child
    to a session by its `--resume` id, then by longest `cwd` prefix**. The daemon wires it with
    `SessionKey(r.id, r.engine_session_id, r.cwd)` for every record (`server/ipc/serve.py:289-291`).
  - `server/sessions/adopt.py`: `_read_proc` (`:107`), `terminate_process_tree` (`:303`).
  - The module docstring states why: "The SDK never hands the driver the CLI pid … so polling our own children is
    the hook" (`memory_watch.py:35-37`).
- **The scan.** Read `/proc/net/tcp` and `tcp6`, keep state `0A` (LISTEN), index by inode; walk the session's
  subtree; read each pid's `/proc/<pid>/fd/*` links for `socket:[<inode>]`. This is exactly what the CLI itself does
  for an unrelated diagnostic (`Kr`/`Vr`, `@312217185`: `/proc/net/tcp{,6}` → inode → `/proc/*/fd` → pid → comm).
- **P1 result** (stand-in root, 6 pids, 38 listening sockets on the host, **17.5 ms**):

  | pid | listener | HTTP probe `GET /` (`Accept: text/html`, 1 s) |
  |---|---|---|
  | python3 | `127.0.0.1:47811` | `200 text/html; charset=utf-8` |
  | python3 | `0.0.0.0:47812` | `200 text/html; charset=utf-8` |
  | python3 (raw socket) | `127.0.0.1:47813` | timeout (accepts, never answers) |
  | node (Vite) | `127.0.0.1:47814` | `200 text/html` |

- **Live daemons (read-only).** dobby's daemon subtree: 46–50 pids, 34 host listeners, 22–24 ms; rocky's: 20 pids,
  29 host listeners, 15.5 ms. **Neither had a session dev server listening at the time**; each tree's only
  listener was the daemon's own `:9443`.
- **Background tasks are in the subtree.** A live dobby CLI (`2127282`) had `bash → sleep` children for its
  background shells and `npm exec chrome-devtools-mcp` for an MCP server. So `run_in_background` processes are
  descendants of the CLI, and so are MCP servers, which must be filtered out (below).

### B.2 What the SDK and CLI report

- **Background Bash tasks carry no pid and no port.** `TaskStartedMessage` has `task_id, description, uuid,
  session_id, tool_use_id, task_type` (`claude_agent_sdk/types.py:1080-1094`); the notification adds
  `output_file` and `summary` (`:1115-1137`). ViberPit's own scanner records the command and the
  `<id>.output` path from the transcript (`background_tasks.py:28-33`, `OpenBgTask` `:81`). The command line is a
  useful **label** for a detected port (`npm run dev`), not a detector.
- **The CLI has no dev-server detection that ViberPit can use.**
  - `localhost:` appears 59 times and `dev server` 38, almost all in bundled Bun runtime strings
    (`bun dev`, "Development - full-stack dev server with hot reload", `@2210331`).
  - `listening_ports`/`newPorts` (`@331735029`) is an `mcp serve` CPU-report counter for remote tool containers
    ("mcp serve: … is left off the CPU report"), never emitted to the SDK.
  - The **preview tunnel** (`tengu_preview_tunnel_*`, handler `@303537098`) proxies `httpRequest`/`wsOpen` frames
    from Anthropic's bridge relay to `http://127.0.0.1:<port>`. It refuses ports outside 1024–65535 and any port
    "not one this tunnel may reach" (an allowlist), and requires the `Host` header be `127.0.0.1:<port>`. It is a
    claude.ai/Desktop remote surface, not an SDK one; Principle I rules it out. **Its allowlist and Host rewrite are
    the right precedent** for ViberPit's own proxy.
  - `preview_start` (`@300305693`) is one of the `host:claude_browser` tools that exist only when the Claude
    Desktop app hosts the session.
- **Holes in tree attribution.**
  - **Detached processes escape.** In P1 the `setsid`'d stand-in was reparented to **pid 1** (`ppid=1`), not to an
    ancestor: neither the CLI nor the daemon is a child subreaper (no `PR_SET_CHILD_SUBREAPER` in `server/` or
    `src/`). `nohup npm run dev &` inside a foreground Bash call behaves the same way.
  - The **cgroup** survives the escape: P1's processes, like every session's, were in
    `0::/system.slice/viberpit-daemon.service`. That separates "a session made it" from "a host service" (rocky
    has Postgres `:5432`/`:55432`, the Windows VM's VNC on `:5900`, `:5572` and more on 127.0.0.1), but it is
    shared by every session on the host.
  - So the order is: subtree owner, then (for a listener in the daemon's cgroup but outside every subtree) the
    longest `cwd` match through `match_session`, else "unattributed". Never offer an unattributed port.
- **Telling a dev server from other listeners.**
  - An HTTP `GET /` with `Accept: text/html` and a 1 s timeout separates them (P1): Vite and `http.server` answer
    `200 text/html`; a raw listener times out. Probe only after attribution, so nothing outside the session is
    ever probed.
  - Exclude known non-preview listeners before probing: the session's MCP servers (by subtree position under an
    MCP launcher, or their configured port) and any Chromium `--remote-debugging-port` (by cmdline).
  - Label from `/proc/<pid>/cmdline` and the matching `OpenBgTask.input.command` when the pid descends from a
    background shell.
- **Bind address.** Vite 8's default (`localhost`) bound `127.0.0.1` on dobby, because `localhost` resolves to
  `127.0.0.1` on both hosts (`getent hosts localhost`). `[::1]` was refused. Other stacks (Node ≥ 17 on a host where
  `localhost` → `::1`) can bind IPv6 only, so connect to the **detected** address, never an assumed `127.0.0.1`.
  A `0.0.0.0` bind (`47812`) is reachable on the LAN and tailnet too; detection should flag it, not use it.
- **Cross-host.** Detection must run on the owning daemon: a new per-session RPC (`dev_servers`, in `_PER_SESSION`
  and the RPC `_SIMPLE` list, `orchestrator_rpc.py:332`), answered from that daemon's `/proc`. The edge cannot
  see a worker's processes.

## C. Proxied preview

### C.1 What an edge reverse proxy needs

- **Nothing to reuse at the edge.** The edge has no HTTP reverse proxy; the only `StreamingResponse`s are its own
  downloads (`api/snapshots.py:59`, `api/pipeline.py:530`, `api/sessions.py:3096`). The only byte relay is VNC
  (`vnc_ws.py` `_relay`/`_WSAdapter`, a flow-controlled WS↔TCP and WS↔WS pump), which covers the WebSocket half.
  `httpx` and `websockets` are already dependencies (`pyproject.toml:33-34`).
- **Cross-host path.** Dev servers bind loopback on the worker, so the edge cannot reach them over the tailnet,
  and must not ask anyone to re-bind them. The only existing channel to a worker is the daemon's mTLS listener
  (`:9443`, `server/ipc/daemon.py` routes; `daemon_ws_connect`, `client.py:262`). A preview needs a new **daemon
  route** (`/preview/{sid}/{port}/…`, HTTP and WS) that re-checks "is this port owned by this session now" and
  relays to the detected address; the edge relays to it like `/vnc`. Today the RPC client is JSON-only, so the
  HTTP half needs a streaming passthrough over the same mTLS endpoint.
- **Path-prefix serving breaks real dev servers (P2).**
  - Vite's `index.html` loads `/@vite/client` and `/main.js`: **absolute paths**. Under
    `/api/sessions/{id}/preview/{port}/` they resolve to the ViberPit origin root and miss.
  - The HMR client builds its socket from `import.meta.url`'s host and port plus `base` (`/`):
    `` `${hostname}:${hmrPort || port}${"/"}` `` (`/@vite/client` lines 852-865), so under a prefix it dials the edge
    root, and its fallback is `127.0.0.1:<port>` from the user's device, which fails.
  - Fixing this means rewriting HTML and JS on the fly, or asking every project to set `base`. Neither is
    acceptable for arbitrary projects. **A preview must be served at an origin's root** (a dedicated origin per
    preview, §E.3), which makes absolute paths and `import.meta.url` work unchanged.
- **Host header.** Vite 8 refuses a foreign `Host`: `Host: dobby.tail0dd570.ts.net` → **403** "Blocked request. This
  host (…) is not allowed". `Host: 127.0.0.1:47814` and `localhost:47814` → 200. The proxy must rewrite `Host` to
  the detected `addr:port`, as the CLI's tunnel does.
- **HMR WebSocket.** With the page's token, the upgrade succeeded (**101**) with both `Origin: http://127.0.0.1:47814`
  and `Origin: https://dobby.tail0dd570.ts.net`; Vite checks the token, not the Origin. So a root-mounted proxy
  passes HMR through unchanged, provided it relays the `vite-hmr` subprotocol.

### C.2 Alternative: open the port in the session's VNC browser (no proxy)

- **Works for any host once §A.2 is fixed**, because the Chromium runs on the session's host and `127.0.0.1:<port>`
  is local to it. No path rewriting, no Host rewriting, HMR works natively, and the dev server's JS runs in a
  browser on the runner host, never in the user's browser and never under the ViberPit origin.
- **Limits.**
  - Virtual sessions only. A **native** session opens the page on the runner host's real desktop
    (`native_display.py:155`), which helps only a user sitting at that host. On a phone it shows nothing.
  - It is a framebuffer: pixel streaming over VNC, no copy and paste of text, no devtools, and a 1280×800 Xvfb.
  - The display exists only while the driver is live (§A.1), but so does the dev server, which dies with the
    driver.
  - Each open costs a Chromium process until CDP reuse exists (§A.3).

## D. Element select

- **What Desktop does** (Claude Desktop bundle, read-only):
  - The Browser pane's selection mode (Cmd/Ctrl+Shift+S) is labelled "Select element", with the detail **"Sends
    Claude a screenshot plus the element's HTML, styles, selector and any React component details."**
    (`ion-dist.asar@108986800`).
  - The renderer pushes the pick into the composer as context (`onElementSelected` → `…push(I,{…t,origin:{previewId,
    tabId, pickId}})`, `ion-dist.asar@43743356`); a variant attaches `selected-element-<ts>.png` with the text as
    a `contextNote` (`@108982951`).
  - The main process wires `setElementSelectedCallback` → `dispatchElementSelected` (`app.asar@23267502`). The
    preview is an Electron `WebContentsView` that Desktop controls, so no page proxy is involved.
  - Desktop's "detection" is **not** port scanning: `detectServerCandidates` reads configured services and
    `package.json` scripts, and `detectServerCandidatesWithAgent` runs a model with read-only tools and the prompt
    "You are detecting a project's dev-server command(s) for a preview pane…" (`app.asar@10754316`, `@9197094`).
    Desktop then **starts** the server itself (`addServerFromCandidate`, `.claude/launch.json`). The docs inventory
    marks server cards "local only" and the preview "not SSH"
    (`docs/audits/2026-11-01-claude-desktop-evidence/desktop-docs-inventory.md:53,173`).
- **Through a proxy (injected script).** An injected overlay script would compute a selector, `outerHTML` and
  computed styles on click and `postMessage` them to the ViberPit parent, which appends them to the composer. This
  needs the preview framed by ViberPit and an HTML-rewriting proxy (to inject the script), on a separate origin
  (§E.3). Problems:
  - The injected script runs **in the dev server's origin, beside its JS**, so the page can forge or read
    selections. The parent must treat every message as untrusted text (Markdown-escape, size cap, origin check on
    `event.origin`), and must never act on it beyond filling the draft.
  - HTML rewriting breaks CSP-strict apps, streaming SSR and non-HTML entry points, and adds a parser to the
    proxy.
  - React component details need the app's own React devtools hook; Desktop has them because it owns the
    renderer.
- **Through CDP in the VNC browser (no injection, no proxy).** The DevTools protocol already has a picker:
  `Overlay.setInspectMode` → `Overlay.inspectNodeRequested(backendNodeId)` → `DOM.getOuterHTML`,
  `CSS.getComputedStyleForNode` and `Page.captureScreenshot` with a clip. The daemon on the session's host drives
  it over the browser's debug port (the dormant `debug_port=True`, §A.3), and returns the pick as composer
  context. The page never sees ViberPit, ViberPit never runs the page's JS, and the user clicks in the VNC canvas.
  - Costs: a CDP client in the daemon (a new dependency, or `websockets` plus a small hand-written client for six
    methods), and `--remote-allow-origins=*` must narrow to a fixed value, because any page in that Chromium could
    otherwise try the debug port.
  - Same uid: another process of the same user on that host (any session) can reach a loopback debug port. That is
    no new exposure, since same-uid processes can already ptrace each other.
  - Not verified against Chrome 157 in this spike (Gaps).

## E. Security

### E.1 Exposure

- A preview proxy turns "loopback services on a runner host" into "anything reachable through the edge". rocky has
  Postgres (`127.0.0.1:5432`, `:55432`), the Windows VM's VNC (`127.0.0.1:5900`), `:5572` and more on loopback
  (`ss -ltn` on rocky). **The proxy must never take a free-form port.** It must take a port the owning daemon
  attributes to this session's process tree **at request time**, re-checked on every HTTP request and WS open
  (a port can be freed and re-bound by something else). The CLI tunnel's 1024–65535 range check plus allowlist
  (§B.2) is the precedent.
- Constitution IV (`.specify/memory/constitution.md:94-118`): "Every endpoint and WebSocket MUST be gated by an
  identity control"; "this service … is a remote-code-execution surface and MUST be treated as one". A preview
  origin is a new endpoint and needs the same Tailscale gate. Being identity-gated is necessary but not
  sufficient, because the danger is the **content** served, not who requests it (§E.2).

### E.2 Auth model today, and the missing Origin check (pre-existing)

- **Identity is per request, from the Tailscale header.** `tailscale serve` maps `https://dobby.tail0dd570.ts.net:8443`
  → `127.0.0.1:8787` (`tailscale serve status`); the middleware derives identity from `Tailscale-User-Login`
  (falling back to `tailscale whois`) on every request (`server/auth/middleware.py:387-396`,
  `server/auth/identity.py:4`). The signed `HttpOnly; SameSite=Strict` cookie (`middleware.py:422-427`) is
  bookkeeping: "re-verified from the header on every request".
- **Consequence: any page in a tailnet browser can act as the user**, whatever its origin, because the browser's
  device is what Tailscale stamps.
  - **WebSocket (CSWSH).** `require_ws_identity` (`middleware.py:540`) and `/ws` (`server/bridge/ws.py:605-610`)
    check no `Origin`, and `/ws` accepts `prompt` frames (`ws.py:212`, `:1945`). **P4:** a raw upgrade to
    `127.0.0.1:8787/ws/vnc` with `Origin: https://evil.example` got `101 Switching Protocols` (then 4404 for the
    nonexistent session), the same as with the real origin.
  - **HTTP.** There is no CORS middleware and no Origin or `Sec-Fetch-Site` check. JSON-bodied routes are safe
    from simple cross-site requests (FastAPI rejects a `text/plain` body for a model), but a body-less mutating
    `POST` is a CSRF target.
- **Why this is D-18's problem.** Today an attacker needs the user to visit a hostile page. A preview proxy serves
  **the dev server's JS, and every npm dependency it bundles**, into the user's browser on a tailnet origin by
  design. Whatever origin it gets, that JS can open `wss://dobby…:8443/ws` and send prompts to any session, which
  is RCE on every host. **An Origin allowlist on every WS upgrade and every non-GET request is a prerequisite of
  any proxied preview**, and is worth shipping on its own now (a separate fix PR, not part of D-18).

### E.3 Origin isolation options for a proxied preview

| Option | Isolation | Feasibility here |
|---|---|---|
| Same origin, path prefix (`/api/sessions/{id}/preview/{port}/`) | **None.** Dev-server JS gets ViberPit's DOM, storage and same-origin API: stored XSS by design | Also breaks absolute paths and HMR (§C.1). **Reject.** |
| Same origin, `<iframe sandbox="allow-scripts">` without `allow-same-origin` | Opaque origin, so no ViberPit DOM, storage or cookies | HMR, `localStorage` and cookie-based app auth break inside the opaque origin. Requests still reach the edge, so the CSWSH hole remains. Weak. |
| **Separate port on the edge host** (e.g. `tailscale serve --https=8444` → a preview listener) | Different origin: no DOM, storage or same-origin API access to `:8443`. Same site (cookies ignore port; the ViberPit cookie is `HttpOnly` and identity is the header anyway) | One port serves one preview at the root, so it needs a **port per live preview** or a host-keyed router. A small pool of `tailscale serve` ports is an operator change on dobby (`deploy/README.md`). With the §E.2 Origin allowlist, its JS cannot drive the API. |
| Separate hostname (a Tailscale Service or second node name, e.g. `viberpit-preview.tail…ts.net`, one subdomain per preview) | Different site: strongest | MagicDNS has no wildcard subdomains, so per-preview hostnames are not available; one extra node name gives one origin, back to the port problem. Most ops work. |

- **Inside any of them:** authenticate the preview origin separately (a short-lived, single-use, per-(session,
  port) capability minted by the edge and exchanged for a cookie scoped to that origin), send
  `Content-Security-Policy: frame-ancestors https://dobby…:8443` (the edge sets no CSP or `X-Frame-Options` today:
  no match for either in `server/`), and strip the ViberPit cookie from proxied requests.
- **Cookies of the proxied app.** On its own origin root, the app's cookies work unchanged (`Path=/`). Under a
  shared origin they would collide across previews and with ViberPit's.

## F. Code map (file:line)

| Surface | Server | Web |
|---|---|---|
| Virtual display | `x11manager.py:21` (browser candidates), `:146` (`X11Manager`), `:190` (`start_display`), `:350` (`ensure_vnc`), `:392` (idle teardown), `:399-456` (`launch_browser`; URL argv `:433-434`; dormant `debug_port` `:418-422`), `:471` (`debug_port_for`, no caller), `:475` (`metrics`); `config.py:235-249` | `browser/BrowserTab.tsx:34`, `:118-120` (VNC URL), `:144-162` (`openBrowser`, no body), `:191-200` (native copy); `browser/DeferredBrowserTab.tsx:18-30` |
| Native display | `native_display.py:155-201` (URL argv `:175-176`); `display_policy.py:1-40` | — |
| Display decision and env | `orchestrator.py:4492` (`_display_decision`), `:6086-6092` (start at spawn), `:6684-6696` (teardown), `:2526-2537` (`_display_env`), `:6699-6718` (`open_browser`) | — |
| VNC transport | `bridge/vnc_ws.py:128-197` (edge, **primary-only** in remote mode `:155-163`); `ipc/x11_rpc.py:39-57` (daemon `/vnc`), `:59-80` (RPCs); `app.py:1164-1181` (wiring), `:1197-1208` (`/api/x11/metrics`), `:1244-1265` (`/browser/open`) | `SessionBody.tsx:136-146` (tab gate), `:228` (tab list), `:415-419` (mount) |
| Fleet routing | `fleet_orchestrator.py:243` (`x11_open_browser` per-session), `:260` (`_MOVE_BLOCKED`), `:1293-1300` (`__getattr__`: `x11_metrics` → primary); `remote.py:1656-1660`; `fleet_router.py:72` (`get`); `ipc/client.py:262` (`daemon_ws_connect`), `:388-404` (endpoint, SSL) | — |
| Process tree | `memory_watch.py:95` (`scan_proc`), `:138` (`read_proc_children`), `:161` (`_subtree`), `:186-204` (`match_session`); `ipc/serve.py:289-291` (session keys); `adopt.py:107,303` | — |
| Background tasks | `background_tasks.py:28-33` (output-file ack), `:81` (`OpenBgTask`), `:219` (`scan_open_background_tasks`); SDK `types.py:1080-1137` | — |
| Per-session RPC | `ipc/orchestrator_rpc.py:332` (`_SIMPLE`), `:158,301` (`get_session` pattern) | `api/hooks.ts` (a `useDevServers(sessionId)` query, polled while the tab is open) |
| Auth | `auth/middleware.py:387-396` (per-request identity), `:422-427` (cookie), `:540` (`require_ws_identity`, no Origin); `auth/identity.py:438` (`human_sender`); `session_actions.py:159` (`require_human`); `bridge/ws.py:605` (`/ws`) | — |

**Where a "Preview" affordance goes.**
- The Browser tab's empty and native states (`BrowserTab.tsx:191-200` and the empty overlay `:441-460`) gain a
  "Dev servers" list: `name (command) · 127.0.0.1:5173 · Open`. "Open" posts `/browser/open {url}`.
- A small chip in the session header or Details tab ("1 dev server") deep-links to the Browser tab.
- Copy for a `0.0.0.0` bind: "Also reachable from your network."

In-flight work: no open PR touches `x11manager`, `vnc_ws`, `browser/`, `BrowserTab`, `SessionBody`, `memory_watch`
or anything named preview (GraphQL check, 2026-11-05). No open issue matches "dev server preview".

## G. Recommended design and owner decisions

### G.1 Minimal safe design (D-18a + D-18b), no proxy

1. **Prerequisite fixes (each its own PR, each useful alone):**
   - Route `/ws/vnc` to the session's host (§A.2). Without it, nothing below works for rocky.
   - Validate `url` (http/https only) and add `--` before it in both launchers (§A.3); add `require_human` to
     `/browser/open`.
   - An Origin allowlist on every WS upgrade and on non-GET HTTP (§E.2). Not strictly needed for a no-proxy
     D-18, but it is a live hole and becomes critical the moment a preview origin exists.
2. **Detection** (server, D-18a):
   - A pure `dev_servers` module that reuses `memory_watch`'s `/proc` readers (no psutil; Principle VI), adds
     socket-inode resolution, and attributes listeners: subtree first, then same-cgroup + `match_session` cwd,
     else drop.
   - Exclusions: MCP servers, Chromium debug ports, the daemon's own ports, ports < 1024.
   - Probe: `GET /` with `Accept: text/html`, 1 s timeout, to the **detected** address, only after attribution.
     Label from cmdline and the matching `OpenBgTask`.
   - Per-session RPC `dev_servers(session_id)` on the owning daemon (`_PER_SESSION`, `_SIMPLE`); `GET
     /api/sessions/{id}/dev-servers` → `[{port, address, pid, label, html, exposed}]`. Polled while the Browser
     tab is open (≈ 5 s); no background loop.
   - Kill switch `VIBERPIT_DEV_SERVER_DETECT_ENABLED`.
3. **Open in Browser tab** (web, D-18b): the list and button above; Open posts `{url: "http://<addr>:<port>/"}`
   and switches to the Browser tab. A native session opens on that host's desktop with the existing copy.
4. **Verification gate (Principle III):** a live smoke on a **virtual rocky session**: start `npm run dev` in its
   worktree through the agent, see it listed, open it, and see it in the VNC canvas.

### G.2 Later, gated slices

- **D-18c, element select through CDP in the VNC browser** (§D): one debug-enabled Chromium per session, reused
  through `Target.createTarget`. This also fixes the six-launch cap, and removes `debug_port`'s dead-option
  status.
- **D-18d, proxied preview (only if chosen):** a separate-origin preview listener on the edge, relayed over the
  daemon's mTLS to the worker, root-mounted, `Host` rewritten, port re-attributed on every request and WS open,
  capability-cookie auth, `frame-ancestors`. Hard prerequisite: the Origin allowlist.

### G.3 Owner decisions

**D1. Proxy, or VNC only?**
- (a) **(Recommended)** **VNC only for D-18.** "Open in Browser tab" runs the page in a Chromium on the runner
  host, and it closes the benchmark row (◐ → ✓ for "with dev-server detection"). Zero new attack surface, and
  HMR works. Cost: pixels, not a real browser on the phone; native sessions get a desktop window instead.
- (b) VNC now, plus the proxy (D-18d) as a follow-up slice. A real browser tab with devtools on the user's
  device, at the cost of a new origin, a streaming relay over mTLS and an Origin-hardening prerequisite.
- (c) Proxy only. Rejected: it is the largest and riskiest piece, and the VNC path is nearly built.

**D2. If a proxy is built, how is the origin isolated?**
- (a) **(Recommended)** **A separate `tailscale serve` HTTPS port per preview** (a small pool, e.g. 8450–8459),
  root-mounted, capability-cookie auth, plus the Origin allowlist. Cheap and standard. Cost: a few
  operator-provisioned ports, and a cap on concurrent previews.
- (b) A second Tailscale node name (a Tailscale Service) for previews. Different **site**, which is stronger. It
  is still one origin per name, so it still needs ports, and it adds ops work.
- (c) Same origin with a sandboxed iframe. Rejected: it breaks HMR and app storage, and leaves the API reachable.

**D3. Element select?**
- (a) **(Recommended)** **Out of D-18, as a follow-up via CDP in the VNC browser** (D-18c). No script runs in the
  user's browser, nothing is injected into the page, and it works on any host.
- (b) In D-18 through an injected script in a proxied preview. Desktop-like UX, but it needs the proxy, an HTML
  rewriter and an untrusted `postMessage` channel.
- (c) Never. Detection plus open is the parity row; element select is the "with element select" half of the
  spec row only.

**D4. Which ports count?**
- (a) **(Recommended)** **Listeners in the session's process subtree, or in the daemon's cgroup with a `cwd` inside
  the session's worktree; HTTP `text/html` at `/`; ports ≥ 1024; MCP and debug ports excluded.** Never a
  free-form port. Cost: a server whose `cwd` is outside the worktree and that escaped the tree is missed.
- (b) Subtree only. Simplest and strictest, but it misses every `nohup`/`setsid` server (P1).
- (c) Any HTTP listener on the host's loopback. Rejected: it offers rocky's Postgres-adjacent and VM services,
  and other sessions' servers.

**D5. `0.0.0.0` binds?**
- (a) **(Recommended)** **List them with an "also reachable from your network" note**; open through loopback like
  any other.
- (b) Hide them. Safer-looking, but it hides a real exposure the user should know about.

### G.4 Split

- **PR 0a** (fix): `/ws/vnc` routed by `sessions.host`. **PR 0b** (fix): `url` validation, `--`, `require_human`.
  **PR 0c** (security): Origin allowlist on WS and non-GET HTTP. 0a and 0b are small; 0c needs care with the
  CLI's `ccas2:` WS clients (bearer, no Origin) and the Assistant's loopback calls.
- **D-18a** (server): detection module, RPC, API, kill switch, tests against a fake `/proc` (like `memory_watch`'s)
  plus one real-`/proc` test that binds a loopback listener in a child.
- **D-18b** (web): the Dev servers list, Open, and the header chip, plus the full `npm run test` and the e2e spec.
- **D-18c** (gated): CDP element select. **D-18d** (gated, D1 b only): proxied preview.

## Gaps (not verified)

- **A live rocky-session VNC attach** (the §A.2 routing gap) was not exercised: attaching would spawn `x11vnc` on a
  real session's display. Code reading only.
- **A real session's dev server**: neither daemon had one during the spike, so P1 used a stand-in tree. Not
  checked: Next, Astro and other frameworks' bind and Host rules; servers started through `docker compose`
  (a container's listener belongs to `docker-proxy` or `rootlesskit`, outside the subtree and the cgroup).
- **CDP picker against Chrome 157** (`Overlay.setInspectMode`, `inspectNodeRequested`) was not driven.
- **`tailscale serve` multi-port** for a preview pool was not configured (it would change host config).
- **FastAPI's body handling for every mutating route** (which routes are body-less and so CSRF-able) was not
  enumerated.
- **HMR through an actual relay** was checked only as a direct handshake with a foreign `Origin`, not through a
  proxy.
- **rocky's unit sandbox**: dobby's `viberpit-daemon.service` has `ProtectProc=default`, `ProcSubset=all` (`systemctl
  show`), so the daemon sees every same-uid `/proc/<pid>/fd`, and the P1 scan ran from a shell inside that same
  cgroup. rocky's unit settings were not read; the read-only scan there ran over `ssh`, outside the unit.
