# Desktop-parity D-18: dev-server detection and per-viewer preview — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal.** Detect dev servers that a session starts, list them in the session's Browser tab, and open one on whichever display suits the person looking:
- If the viewer is on the session's own host, open `http://localhost:<port>` in their own browser.
- If the dev server is reachable over the tailnet, open a tailnet link.
- Otherwise, open the URL in the session's virtual display (VNC), starting it on demand.

There is no proxy.

**Facts (binding).** `docs/superpowers/specs/2026-11-05-d18-dev-server-preview-verification.md`, especially §A (Browser tab), §B (detection), §E (security) and §F (code map).

**Owner decision (2026-11-05): per-click routing.** The display is chosen when the viewer clicks Open, from the viewer's current device. It follows them from desk to phone and is never fixed at spawn. The existing spawn-time native/virtual setting keeps governing every other GUI window the agent opens.

**Controller rulings (2026-11-05), from the spike's recommendations:**
- **No proxied preview.** Proxying needs isolated origins and a cross-host streaming proxy. That's a possible later slice.
- **No element select in D-18.** It's a later slice, built on Chrome's own inspect picker over CDP in the VNC browser, never by script injection.
- **Ports that count:**
  - The listener is owned by the session's process subtree, or it sits in the daemon's cgroup outside every subtree with a longest-`cwd` `match_session` hit inside the session's worktree. An unattributed port is never offered.
  - Port ≥ 1024.
  - Not one of the session's MCP servers and not a Chromium `--remote-debugging-port`.
  - It answers an HTTP `GET /` with `text/html` within 1 s. Probe only after attribution passes.
  - Ports are never entered by hand.
- **`0.0.0.0` binds** are listed with the note `Also reachable from your network`, and they qualify for the tailnet-link route.

**Depends on.** The security PR on branch `fix/ws-origin-and-browser-open`. It adds the Origin allowlist, hardens `POST /api/sessions/{id}/browser/open` (URL validation, `--` before the URL, `require_human`), and routes `/ws/vnc` by owning host. Rebase onto master once it merges. Don't duplicate its changes; call the hardened `open_browser`.

**Tech stack.** Python, FastAPI, pytest; React + TS, vitest, Playwright.

## Global Constraints

- **Detection runs on the owning daemon.**
  - Add a per-session RPC `dev_servers(session_id)`, registered in `_PER_SESSION` and in the RPC `_SIMPLE` list, with remote and facade parity.
  - Implement it with the existing `/proc` helpers in `server/sessions/memory_watch.py`: `scan_proc`, `read_proc_children`, `_subtree`, `match_session`. Don't add psutil or a parallel scanner.
  - Read `/proc/net/tcp` and `tcp6`, keep LISTEN entries, map inode → pid through `/proc/<pid>/fd`, then attribute by the rules above.
  - Connect to the **detected** address. Never assume `127.0.0.1`.
  - Probe with a 1 s timeout and `Accept: text/html`, and probe nothing outside the session.
  - Label each entry from `/proc/<pid>/cmdline`, using the background-task command (`background_tasks.OpenBgTask`) when the pid descends from a background shell.
  - Cache the result per session for a few seconds so polling stays cheap.
  - Returns `[{port, address, bind: "loopback"|"all", pid, label, title?}]`. `title` is the HTML `<title>` if it's cheap to get; it's untrusted text.
- **Per-click routing at the edge.**
  - `GET /api/sessions/{id}/dev-servers` returns the detected list. Each entry gains an `open` object, computed per request from the **requesting** client's tailnet node, using the same node resolution `display_policy._is_local` relies on. Don't add a second locality rule; reuse or extend `display_policy`.
  - The three routes, in order:
    1. **`{kind: "local-link", url: "http://localhost:<port>/"}`** when the viewer's node is the session's host. Use the detected loopback address form. For an IPv6-only bind, use `http://[::1]:<port>/`.
    2. **`{kind: "tailnet-link", url: "http://<host-tailnet-name>:<port>/", note}`** when bind is `all`. Get the host's tailnet DNS name from fleet host info (grep `fleet` and `tailnet`).
    3. **`{kind: "vnc"}`** otherwise.
  - Links open in a new tab with `rel="noopener noreferrer"`. The URL is built server-side from detected data only, never from client input.
- **Opening in VNC.**
  - `POST /api/sessions/{id}/dev-servers/{port}/open` re-runs detection and refuses ports that aren't currently detected. It then:
    - ensures a virtual display for the session, **starting one on demand even for a native-mode session** (ruling: preview only, with no change to the session's `display_mode`);
    - opens `http://<detected address>:<port>/` through the hardened `open_browser`.
  - Require a person (`require_human`), write an audit row (`session.preview_open`), and log expected refusals at INFO.
  - The on-demand display must be torn down with the session's existing display lifecycle. Check `x11manager` idle teardown, and that a native session's preview display gets reaped.
- **Engines.** Detection applies to Claude and Codex alike, since it's process-based. Check that the Codex driver's process tree is also found by `match_session`.
- **Gates.**
  - Server: ruff, mypy, focused pytest, parity tests, the full `server/tests -n 4` with `PYTHONPATH=$PWD/src` in a clean env, and core `tests/`.
  - Web: typecheck, lint, full vitest, build, budget, e2e.
  - A no-model contract-style test that starts a real `python -m http.server` on 127.0.0.1 in a fake session subtree and detects it, plus a raw socket that must not be listed.

## Review Focus

1. A port outside the session (another session's server, Postgres, the VM's VNC, a detached process with no worktree cwd) is never listed and never probed (Task 1).
2. `POST …/open` refuses a port that isn't currently detected, so the URL can't be steered (Task 1).
3. The routing: the same session viewed from its own host gets `local-link`, from elsewhere gets `vnc` or `tailnet-link`, and a viewer can't spoof locality (Task 1).
4. A worker-host (rocky) session's detection and VNC open run on rocky (Task 1).
5. The UI never builds a URL itself, and titles and labels render as text (Task 2).

---

### Task 1: Server — detection RPC, routing, open-in-VNC

**Files:**
- New `server/sessions/dev_servers.py`: the scan, attribution, probe and cache.
- `orchestrator.py`: the `dev_servers` and `open_dev_server` methods.
- RPC, remote, fleet and facade wiring.
- `display_policy.py`: routing reuse.
- `api/sessions.py`: the routes.
- `session_actions`.

**Tests:** `server/tests/test_dev_servers.py`.

- [ ] **Step 1: Failing tests.** Every bullet above, plus Review Focus 1–4.
- [ ] **Step 2–4: Implement and verify.** Then run focused tests, parity, ruff and mypy.
- [ ] **Step 5: Commit** `feat(sessions): detect a session's dev servers and route Open to the viewer's display (D-18)`

### Task 2: Web — "Dev servers" in the Browser tab

**Files:**
- `browser/BrowserTab.tsx` and `DeferredBrowserTab.tsx`. Make the Browser tab listed when the session has detected dev servers even if X11 is disabled, so a desk viewer can still use links.
- A `DevServerList` that polls while visible (e.g. every 5 s) and shows label, port, a bind note and an Open action per route:
  - `local-link` / `tailnet-link`: open the server-provided URL in a new tab.
  - `vnc`: post to `/open`, then switch the tab to the VNC view.
- An empty state: `No dev servers detected — when the session starts one (e.g. npm run dev), it shows up here.`
- Optionally, a small "Dev server on :5173" chip in the session header when one is detected and the Browser tab isn't open (ruling: include it if it's cheap).
- Hooks, kept lazy where possible because budget headroom is about 0.3 kB, plus mocks.

**Tests:** unit tests, and the e2e `web/e2e/dev-servers.spec.ts` (route-mocked, at 1440 and 390).

- [ ] **Step 1: Failing tests.** The list renders; each route's action does the right thing; VNC posts and switches; Review Focus 5; the empty state.
- [ ] **Step 2–4: Implement and verify.** Then run the full web gates and e2e.
- [ ] **Step 5: Commit** `feat(web): dev servers in the Browser tab, opened on the viewer's display (D-18)`

### Task 3: Benchmark and docs

- **Benchmark.** Set `Preview browser with dev-server detection` → Y, with the note "detection + per-viewer open (local link / tailnet / VNC); no proxy, no element select". Regenerate §3 and report the target (D-17: 95%).
- **Spec §8/§10/§11.** Cover routing, detection rules, the rulings, and the follow-ups (proxy, element select). Mark D-18 and **sub-spec D complete**.
- **Product docs.** Browser tab / display doc.
- **Ceilings.** Measured + 0.3, only if they moved.
- [ ] Commit `docs: D-18 benchmark rerun and dev-server preview docs`.
