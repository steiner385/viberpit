# A2.3: Remote by Default + Deploy Classification + Cutover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The edge runs against the daemon **by default** when a compatible one is listening at the well-known socket, falling back to in-process otherwise; the deploy restarts only the edge for edge-only changes (sessions survive in the daemon) and idle-gates daemon restarts.

**Architecture:** `create_app` probes `/run/ccswitch/sessiond.sock` (sync `GET /info`); a protocol-compatible daemon → remote, else in-process (today's behavior). An explicit `CCSWITCH_SESSIOND_SOCKET` still forces remote. `auto-deploy.sh` classifies the deploy range as daemon-vs-edge and gates accordingly.

**Tech Stack:** Python 3.12 · FastAPI · httpx (sync UDS probe) · bash/systemd.

## Global Constraints

- **Fallback-first safety:** with NO compatible daemon at the probed path, the edge MUST be byte-identical to today (in-process). Deploying this changes nothing until a daemon is enabled.
- An explicit `CCSWITCH_SESSIOND_SOCKET` forces remote UNCONDITIONALLY (no probe) — preserves existing remote-mode tests that point at a dead socket.
- An injected `orchestrator=` forces in-process.
- The probe is SYNC (create_app is sync), short timeout (1.0s), swallows all errors → False.
- Deploy classification must fail SAFE: if it can't determine the change type, treat as `daemon_changed` (idle-gate + restart both). All new deploy logic guarded so a host without the daemon enabled behaves as today.
- Do NOT change `server/bridge/ws.py` or the orchestrator internals.
- Run tests: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest <args>`
- CI gates: `ruff check .`, `mypy server/ --ignore-missing-imports`. Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Remote-by-default with in-process fallback (probe)

**Files:**
- Modify: `server/app.py` (`create_app` mode selection; add `_daemon_compatible` helper)
- Test: `server/tests/test_remote_default.py` (create)

**Interfaces:**
- Consumes: `server.ipc.paths.DEFAULT_SOCKET_PATH`, `server.ipc.protocol.PROTOCOL_VERSION`.
- Produces: `_daemon_compatible(path: str) -> bool` (sync probe). `create_app` default path → remote iff a compatible daemon answers `DEFAULT_SOCKET_PATH`, else in-process. `app.state.daemon_protocol: int | None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_remote_default.py
import os
from unittest import mock
import pytest
from server.app import create_app, _daemon_compatible
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.remote import RemoteOrchestrator
from server.ipc.daemon import create_daemon_app
from server.ipc.paths import DEFAULT_SOCKET_PATH
from server.tests.ipc_harness import serve_daemon_uds


def _clean_env():
    e = {k: v for k, v in os.environ.items() if k != "CCSWITCH_SESSIOND_SOCKET"}
    return e


def test_default_in_process_when_no_daemon():
    # No daemon at the well-known path → in-process fallback (byte-identical to today).
    with mock.patch.dict(os.environ, _clean_env(), clear=True), \
         mock.patch("server.app._daemon_compatible", return_value=False):
        app = create_app()
    assert app.state.remote_mode is False
    assert isinstance(app.state.sessions, InProcessOrchestratorAdapter)


def test_default_remote_when_compatible_daemon_present():
    with mock.patch.dict(os.environ, _clean_env(), clear=True), \
         mock.patch("server.app._daemon_compatible", return_value=True):
        app = create_app()
    assert app.state.remote_mode is True
    assert isinstance(app.state.sessions, RemoteOrchestrator)
    assert app.state.orchestrator is None


def test_explicit_env_forces_remote_without_probe():
    # Explicit flag → remote unconditionally, even with NO daemon (probe not consulted).
    with mock.patch.dict(os.environ, {"CCSWITCH_SESSIOND_SOCKET": "/tmp/dead.sock"}), \
         mock.patch("server.app._daemon_compatible", return_value=False) as probe:
        app = create_app()
    assert app.state.remote_mode is True
    probe.assert_not_called()


def test_injected_orchestrator_forces_in_process():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator()
    with mock.patch("server.app._daemon_compatible", return_value=True):
        app = create_app(orchestrator=orch)
    assert app.state.remote_mode is False


@pytest.mark.asyncio
async def test_daemon_compatible_probe_real_socket():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        assert _daemon_compatible(sock) is True
    # After the daemon stops, the socket is gone → not compatible.
    assert _daemon_compatible("/nonexistent/ccsd.sock") is False
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_default.py -v`
Expected: FAIL — `_daemon_compatible` missing; default path doesn't probe.

- [ ] **Step 3: Add `_daemon_compatible` to `server/app.py`**

Near `running_git_sha` (module level), add:

```python
def _daemon_compatible(socket_path: str) -> bool:
    """True iff a protocol-compatible session daemon answers /info at socket_path.
    Synchronous + short-timeout (create_app is sync); any failure → False (in-process fallback)."""
    import httpx
    from server.ipc.protocol import PROTOCOL_VERSION
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=socket_path),
                          base_url="http://daemon", timeout=1.0) as c:
            info = c.get("/info").json()
        return info.get("protocol") == PROTOCOL_VERSION
    except Exception:  # noqa: BLE001 — unreachable / bad response → fall back to in-process
        return False
```

- [ ] **Step 4: Wire the default-path probe into `create_app`**

Replace the mode-selection block (currently `socket_path = os.environ.get(...)` through the in-process `else`) with:

```python
    from server.sessions.facade import InProcessOrchestratorAdapter

    socket_path = os.environ.get("CCSWITCH_SESSIOND_SOCKET")
    app.state.daemon_protocol = None
    if orchestrator is None and not socket_path:
        # Remote by default: auto-detect a compatible daemon at the well-known path.
        # No daemon (or a version-incompatible one) → in-process fallback (today's behavior).
        from server.ipc.paths import DEFAULT_SOCKET_PATH
        if _daemon_compatible(DEFAULT_SOCKET_PATH):
            socket_path = DEFAULT_SOCKET_PATH
            from server.ipc.protocol import PROTOCOL_VERSION
            app.state.daemon_protocol = PROTOCOL_VERSION
            logging.getLogger("server").info(
                "session daemon detected at %s — running in remote mode", DEFAULT_SOCKET_PATH)

    if orchestrator is None and socket_path:
        from server.ipc.client import DaemonClient
        from server.sessions.remote import RemoteOrchestrator
        app.state.remote_mode = True
        app.state.orchestrator = None
        app.state.sessions = RemoteOrchestrator(DaemonClient(socket_path))
    else:
        if orchestrator is None:
            orchestrator = SessionOrchestrator()
        app.state.remote_mode = False
        app.state.orchestrator = orchestrator
        app.state.sessions = InProcessOrchestratorAdapter(orchestrator)
```

(Leave the x11 block that follows unchanged.)

- [ ] **Step 5: Run, verify pass + regression**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_remote_default.py server/tests/test_remote_mode_wiring.py server/tests/test_remote_mode_edge.py server/tests/test_healthz.py server/tests/test_security_hardening.py -q
```
Expected: PASS. The in-process default tests (no daemon) must stay green — proving byte-identical fallback. Then `ruff check server/app.py server/tests/test_remote_default.py` clean; `mypy server/ --ignore-missing-imports` Success.

NOTE: existing tests that call `create_app()` with no env and no injected orchestrator and expect IN-PROCESS will now run the real probe against `DEFAULT_SOCKET_PATH` (`/run/ccswitch/sessiond.sock`). On a CI/test host with no daemon there, the probe returns False in ≤1s → in-process, so they still pass — but confirm none of them are slowed unacceptably or accidentally hit a real daemon. If any test environment HAS a daemon at that path, pin `mock.patch("server.app._daemon_compatible", return_value=False)` in the affected fixture. Report if you had to.

- [ ] **Step 6: Commit**

```bash
git add server/app.py server/tests/test_remote_default.py
git commit -m "feat(edge): remote by default — probe well-known daemon socket, in-process fallback (A2.3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Deploy classification — edge-only vs daemon change

**Files:**
- Modify: `scripts/auto-deploy.sh`
- Test: `server/tests/test_deploy_classification.py` (create)

**Interfaces:**
- Produces: in `auto-deploy.sh`, a `daemon_changed` computation over `$LOCAL..$REMOTE` and a `remote_active` probe of the edge `/healthz`; the busy/idle gate applies only when `daemon_changed || ! remote_active`; the daemon restart happens only when `daemon_changed` and the unit is enabled.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_deploy_classification.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "auto-deploy.sh"


def test_classification_helpers_present():
    s = SCRIPT.read_text()
    # Classifies the deploy range and probes remote-mode.
    assert "daemon_changed" in s
    assert "remote_active" in s
    # Daemon-relevant paths the classifier keys on.
    assert "server/ipc/" in s and "server/sessions/" in s
    # The idle gate is now conditional on a session-disrupting restart.
    assert "is-enabled ccswitch-daemon.service" in s


def test_fail_safe_defaults_to_daemon_changed():
    # If the diff can't be computed, the script must treat it as daemon_changed (safe).
    s = SCRIPT.read_text()
    # A comment or default assignment documenting the fail-safe.
    assert "fail" in s.lower() and "daemon_changed" in s


def test_shellcheck_clean_if_available():
    import shutil
    if not shutil.which("shellcheck"):
        return
    r = subprocess.run(["shellcheck", "-S", "error", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
```

- [ ] **Step 2: Run, verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_deploy_classification.py -v`
Expected: FAIL — no classification in the script.

- [ ] **Step 3: Add classification to `scripts/auto-deploy.sh`**

Read the whole script first. Two edits, both careful:

**(a)** Right after `REMOTE="$(git rev-parse origin/master)"` is known and BEFORE the busy/idle defer gate, compute the classification (the diff is between the current `$LOCAL` and `$REMOTE`):

```bash
# Classify the pending change: does it touch code the DAEMON runs (orchestrator/IPC/session
# logic)? If only edge code changed AND the edge is running in remote mode, restarting the edge
# does NOT disrupt sessions (the daemon holds them), so we can skip the idle gate entirely.
# Fail SAFE: if the diff can't be computed, assume daemon_changed (gate + restart both).
if daemon_files="$(git diff --name-only "$LOCAL" "$REMOTE" 2>/dev/null)"; then
  if printf '%s\n' "$daemon_files" | grep -qE '^(server/ipc/|server/sessions/|src/ccswitch/)'; then
    daemon_changed=1
  else
    daemon_changed=0
  fi
else
  daemon_changed=1   # fail-safe: cannot classify → treat as a daemon change
fi

# Is the running edge in remote mode? Its /healthz reports "daemon":"ok" only then.
remote_active=0
if [ -z "${CCSWITCH_DEPLOY_REMOTE_ACTIVE:-}" ]; then
  _hz="$(curl -fsS -m 3 "http://${CCSWITCH_HOST:-127.0.0.1}:${CCSWITCH_PORT:-8787}/healthz" 2>/dev/null || true)"
  grep -q '"daemon":"ok"' <<<"$_hz" && remote_active=1
else
  remote_active="$CCSWITCH_DEPLOY_REMOTE_ACTIVE"   # test seam
fi
```

**(b)** Wrap the EXISTING busy/idle/VNC defer computation so it only runs when a restart will disrupt sessions. Find the block that computes `defer_reason` (busy/vnc) and the defer-or-proceed decision; guard the whole defer decision:

```bash
# Only idle-gate when a session-disrupting restart will happen: a daemon change, or an
# in-process edge (where any restart kills sessions). A remote edge + edge-only change is
# non-disruptive → never defer.
if [ "$daemon_changed" = "1" ] || [ "$remote_active" != "1" ]; then
  ... (the existing defer_reason busy/vnc gate + the defer-and-exit logic, unchanged) ...
else
  echo "[deploy] edge-only change, remote mode — non-disruptive restart, skipping idle gate"
fi
```

**(c)** The daemon restart block (added in A4, after the edge health gate) gains a `daemon_changed` condition:

```bash
if [ "$daemon_changed" = "1" ] && systemctl is-enabled ccswitch-daemon.service >/dev/null 2>&1; then
  echo "[deploy] daemon code changed — restarting ccswitch-daemon.service"
  sudo systemctl restart ccswitch-daemon.service || \
    echo "[deploy] WARNING: daemon restart failed (edge already live)" >&2
fi
```

Do NOT alter the CI gate, fast-forward, dep-sync, frontend build, edge restart, edge health gate, or rollback. Only the defer-gate guard, the new classification block, and the daemon-restart condition.

- [ ] **Step 4: Run, verify pass + shellcheck**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$(pwd)/src /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/.venv/bin/python -m pytest server/tests/test_deploy_classification.py server/tests/test_deploy_daemon.py -v`
If `shellcheck` is installed, run `shellcheck -S error scripts/auto-deploy.sh` and fix any NEW error-level issues your edits introduced (note pre-existing ones in the report).

- [ ] **Step 5: Commit**

```bash
git add scripts/auto-deploy.sh server/tests/test_deploy_classification.py
git commit -m "feat(deploy): classify edge-only vs daemon change; skip idle gate for remote edge-only restarts (A2.3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Plan Self-Review

**Spec coverage:** remote-by-default-with-fallback + probe + handshake enforcement → Task 1; deploy classification (daemon-vs-edge, conditional idle gate, conditional daemon restart) → Task 2. The cutover (enable daemon, restart edge, verify, reversibility) is a controller-performed deploy-verify step after merge, not a code task. healthz needs no change (§3). ✓
**Placeholder scan:** complete code in each step. The Task 1 Step 5 NOTE and Task 2's "read the script first" are verify-against-reality guards, not logic gaps. ✓
**Type/name consistency:** `_daemon_compatible`, `DEFAULT_SOCKET_PATH`, `PROTOCOL_VERSION`, `app.state.daemon_protocol`/`remote_mode`, and the bash `daemon_changed`/`remote_active`/`CCSWITCH_DEPLOY_REMOTE_ACTIVE` consistent across tasks. The fallback (no daemon → in-process) is the invariant guarding production safety. ✓
