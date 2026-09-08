# Native vs Virtual Display Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session created from the runner host's own desktop gets `DISPLAY=:0` (native) and native browser launches; every other client keeps the pooled Xvfb display + VNC relay.

**Architecture:** The edge resolves the requesting client's tailnet node name (`tailscale whois … Node.ComputedName`) and passes it, plus an optional explicit `display` override, through `create_session`. The daemon that spawns the process resolves the mode with one pure function (`client_node == self_host_name()` and a native display is available), persists it on the session row, and a single `_display_env` picks the env at every driver build. A small `NativeDisplay` object owns the host desktop as a target (availability, env, managed Chromium launches, teardown).

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy (server), pytest + pytest-asyncio; React + TypeScript + vitest (web).

**Spec:** `docs/superpowers/specs/2026-09-06-native-display-routing-design.md`

## Global Constraints

- Constitution `.specify/memory/constitution.md` v1.2.0 applies. In particular: never fork/patch a native agent binary (I); TDD + quality gates (V); single source of truth, no dead code (VI).
- `server/config.py` defines every setting **twice** (pydantic branch ≈ line 60-230 and dataclass fallback ≈ line 340-510). Every new knob goes in both.
- New DB columns are **nullable** (`init_db` reconcile ALTER-ADDs them on both dialects); still add an alembic revision chained off `0028_assistant_tokens`.
- Never use `func.now()`; this plan adds no timestamp columns.
- Native display env is `{"DISPLAY": "<display>"}` with **no** `XAUTHORITY` (spec §2, verified on dobby).
- Run server tests from the worktree with the main checkout's venv, after stripping every `CCSWITCH_*` variable:
  ```bash
  cd /home/tony/GitHub/.ccswitch/worktrees/ccswitch/b1eacaf3
  for v in $(env | grep -o '^CCSWITCH_[A-Z0-9_]*'); do unset "$v"; done
  CCSWITCH_DEV=1 /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest <paths> -q --timeout=120
  ```
  (`env -u` does not glob; use the loop.) The `lint-and-test` gate is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- Web commands run in a subshell so the cwd never sticks in `web/` (the Bash hook path is cwd-relative): `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`. If `web/node_modules` is missing, symlink it from `/home/tony/GitHub/ccswitch/web/node_modules`. After `npm run build`, `rm -rf web/dist` before running server tests (the SPA mount breaks `test_library_api`).
- Commit messages go through a file (`git commit -F /tmp/msg.txt`) and end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Push after every commit (`git push`): the daemon's worktree relink drops unpushed commits.
- Branch: `native-display-routing` (already exists, based on `origin/master`, spec committed).

---

### Task 1: Config knobs (both settings branches)

**Files:**
- Modify: `server/config.py:121` (pydantic branch, after `enable_xvfb`) and `server/config.py:394` (dataclass branch, after `enable_xvfb`)
- Test: `server/tests/test_config_xvfb.py`

**Interfaces:**
- Produces: `Settings.native_display_enabled: bool` (env `CCSWITCH_NATIVE_DISPLAY_ENABLED`, default `True`), `Settings.native_display: str` (env `CCSWITCH_NATIVE_DISPLAY`, default `":0"`).

- [ ] **Step 1: Write the failing tests** — append to `server/tests/test_config_xvfb.py`:

```python
def test_native_display_defaults(monkeypatch):
    monkeypatch.delenv("CCSWITCH_NATIVE_DISPLAY_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_NATIVE_DISPLAY", raising=False)
    s = _fresh()
    assert s.native_display_enabled is True
    assert s.native_display == ":0"


def test_native_display_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY_ENABLED", "false")
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY", ":1")
    s = _fresh()
    assert s.native_display_enabled is False
    assert s.native_display == ":1"


def test_native_display_fallback_dataclass_branch(monkeypatch):
    """The dataclass fallback must define the same fields (omitting it is a silent
    AttributeError on hosts without pydantic-settings)."""
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY", ":7")
    import server.config as cfg
    importlib.reload(cfg)
    fallback_cls = getattr(cfg, "_FallbackSettings", None) or getattr(cfg, "Settings")
    inst = fallback_cls()
    assert inst.native_display == ":7"
    assert inst.native_display_enabled is True
```

Before writing the third test, open `server/config.py` and find the actual class name of the dataclass fallback (search for `dataclasses.field(` near line 340 and read the `class` line above it). Use that name instead of `_FallbackSettings` if it differs; if the fallback is only reachable via an import guard, construct it the same way the nearest existing test in this file does (search the test file for `dataclass`).

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_config_xvfb.py -q -k native_display`
Expected: FAIL — `AttributeError: … has no attribute 'native_display_enabled'`.

- [ ] **Step 3: Add the fields** — pydantic branch, directly after the `enable_xvfb` line (≈121):

```python
        # Route a session's GUI to the host's real X display when the requesting
        # client sits on that host (tailscale whois node == self_host_name()).
        # Gated at spawn time on the X socket actually existing, so a headless
        # worker never sees a native session. Design:
        # docs/superpowers/specs/2026-09-06-native-display-routing-design.md
        native_display_enabled: bool = Field(default=True, alias="CCSWITCH_NATIVE_DISPLAY_ENABLED")
        native_display: str = Field(default=":0", alias="CCSWITCH_NATIVE_DISPLAY")
```

Dataclass branch, directly after the `enable_xvfb` field (≈394):

```python
        native_display_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_NATIVE_DISPLAY_ENABLED", "true").lower() in ("1","true","yes","on")
        )
        native_display: str = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_NATIVE_DISPLAY", ":0")
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_config_xvfb.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/tests/test_config_xvfb.py
printf 'feat(config): CCSWITCH_NATIVE_DISPLAY_ENABLED / CCSWITCH_NATIVE_DISPLAY knobs\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 2: `display_policy.py` — the pure decision

**Files:**
- Create: `server/sessions/display_policy.py`
- Test: `server/tests/test_display_policy.py`

**Interfaces:**
- Produces: `DISPLAY_NATIVE = "native"`, `DISPLAY_VIRTUAL = "virtual"`, `DISPLAY_AUTO = "auto"`, `REQUESTABLE = (DISPLAY_AUTO, DISPLAY_NATIVE, DISPLAY_VIRTUAL)`, and
  `resolve_display_mode(*, requested: str | None, client_node: str | None, runner: str, native_available: bool) -> str` returning `"native"` or `"virtual"`.

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_display_policy.py`:

```python
"""resolve_display_mode: the one place 'native vs virtual' is decided (spec §4.2)."""
import pytest

from server.sessions.display_policy import (
    DISPLAY_AUTO, DISPLAY_NATIVE, DISPLAY_VIRTUAL, resolve_display_mode,
)


def _r(**kw):
    base = dict(requested=None, client_node="dobby", runner="dobby", native_available=True)
    base.update(kw)
    return resolve_display_mode(**base)


def test_local_client_auto_is_native():
    assert _r() == DISPLAY_NATIVE
    assert _r(requested=DISPLAY_AUTO) == DISPLAY_NATIVE


def test_remote_client_is_virtual():
    assert _r(client_node="phone") == DISPLAY_VIRTUAL


def test_unknown_client_is_virtual():
    assert _r(client_node=None) == DISPLAY_VIRTUAL


def test_explicit_virtual_wins_even_when_local():
    assert _r(requested=DISPLAY_VIRTUAL) == DISPLAY_VIRTUAL


def test_explicit_native_needs_no_locality_proof():
    assert _r(requested=DISPLAY_NATIVE, client_node="phone") == DISPLAY_NATIVE


def test_native_unavailable_beats_everything():
    assert _r(native_available=False) == DISPLAY_VIRTUAL
    assert _r(requested=DISPLAY_NATIVE, native_available=False) == DISPLAY_VIRTUAL


def test_node_comparison_is_case_insensitive():
    assert _r(client_node="Dobby", runner="dobby") == DISPLAY_NATIVE


@pytest.mark.parametrize("bad", ["", "desktop", "NATIVE "])
def test_unrecognised_request_is_treated_as_auto(bad):
    assert _r(requested=bad) == DISPLAY_NATIVE
    assert _r(requested=bad, client_node="phone") == DISPLAY_VIRTUAL
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_display_policy.py -q`
Expected: FAIL — `ModuleNotFoundError: server.sessions.display_policy`.

- [ ] **Step 3: Create the module**

```python
"""Native vs virtual display routing — the decision, and nothing else.

A session's GUI goes to the runner host's real X display when the browser that
created it runs on that same host; otherwise to a pooled Xvfb display viewed over
VNC. This module is pure so the rule can be tested exhaustively and so every
caller (the orchestrator's spawn, resume and adopt paths) decides the same way.
Design: docs/superpowers/specs/2026-09-06-native-display-routing-design.md §4.2.
"""
from __future__ import annotations

DISPLAY_NATIVE = "native"
DISPLAY_VIRTUAL = "virtual"
# Request-only value ("decide for me"); never persisted.
DISPLAY_AUTO = "auto"
REQUESTABLE = (DISPLAY_AUTO, DISPLAY_NATIVE, DISPLAY_VIRTUAL)


def resolve_display_mode(
    *,
    requested: str | None,
    client_node: str | None,
    runner: str,
    native_available: bool,
) -> str:
    """Return DISPLAY_NATIVE or DISPLAY_VIRTUAL.

    Rules, in order: an explicit "virtual" always wins; no native display means
    virtual; an explicit "native" needs no locality proof; otherwise native iff the
    requesting client's tailnet node IS the runner host. Anything unrecognised in
    ``requested`` (the API validates it, but daemons can be behind) reads as auto.
    """
    if requested == DISPLAY_VIRTUAL:
        return DISPLAY_VIRTUAL
    if not native_available:
        return DISPLAY_VIRTUAL
    if requested == DISPLAY_NATIVE:
        return DISPLAY_NATIVE
    if client_node and runner and client_node.strip().lower() == runner.strip().lower():
        return DISPLAY_NATIVE
    return DISPLAY_VIRTUAL
```

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_display_policy.py -q`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/display_policy.py server/tests/test_display_policy.py
printf 'feat(sessions): pure resolve_display_mode policy\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 3: Client node resolution (`identity.py`) + FastAPI dependency

**Files:**
- Modify: `server/auth/identity.py:144-182` (refactor `_whois_identity` over a shared `_whois`; add `client_node_name`)
- Modify: `server/auth/middleware.py:401` (add `current_client_node` next to `current_identity`)
- Test: `server/tests/test_auth_client_node.py`

**Interfaces:**
- Produces: `server.auth.identity.client_node_name(request) -> str | None` (lower-cased `Node.ComputedName`, never raises) and `server.auth.middleware.current_client_node(request) -> str | None` (FastAPI dependency).
- Existing behaviour of `verify_identity` / `_whois_identity` must be byte-for-byte unchanged (the tests in `test_auth.py` and `test_auth_identity_peer_trust.py` guard it).

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_auth_client_node.py`:

```python
"""client_node_name: which tailnet machine the browser is on (spec §4.1).

Same peer-trust rule as identity: X-Real-IP is only honoured from loopback /
CCSWITCH_TRUSTED_PROXY_IPS, and the whois subprocess is the only source of truth.
"""
import json
from unittest.mock import MagicMock

from starlette.requests import Request

from server.auth.identity import client_node_name

LOOPBACK = ("127.0.0.1", 51000)
UNTRUSTED = ("203.0.113.7", 51000)


def _headers(**kv: str) -> list[tuple[bytes, bytes]]:
    return [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in kv.items()]


def _http(client, **headers: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/sessions",
                    "headers": _headers(**headers), "client": client})


def _whois_ok(node_name="dobby"):
    def fake(cmd, **kw):
        fake.calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = json.dumps({
            "Node": {"Name": f"{node_name}.tail0dd570.ts.net.", "ComputedName": node_name,
                     "Addresses": ["100.69.70.109/32"]},
            "UserProfile": {"LoginName": "tony@example.com"},
        })
        return m
    fake.calls = []
    return fake


def test_resolves_node_from_x_real_ip_behind_loopback_proxy(monkeypatch):
    fake = _whois_ok("Dobby")
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    node = client_node_name(_http(LOOPBACK, x_real_ip="100.69.70.109"))
    assert node == "dobby"                      # lower-cased
    assert fake.calls[0][-1] == "100.69.70.109"  # the forwarded peer, after "--"


def test_untrusted_peer_cannot_forward_a_real_ip(monkeypatch):
    fake = _whois_ok()
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    client_node_name(_http(UNTRUSTED, x_real_ip="100.69.70.109"))
    assert fake.calls[0][-1] == "203.0.113.7"   # whois runs on the direct peer instead


def test_loopback_without_forwarded_ip_is_unknown(monkeypatch):
    """`curl 127.0.0.1` on the host must never count as 'local': whois has no peer."""
    def fail(cmd, **kw):
        m = MagicMock(); m.returncode = 1; m.stdout = ""; return m
    monkeypatch.setattr("server.auth.identity.subprocess.run", fail)
    assert client_node_name(_http(LOOPBACK)) is None


def test_whois_failure_is_none_not_an_error(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("tailscale not installed")
    monkeypatch.setattr("server.auth.identity.subprocess.run", boom)
    assert client_node_name(_http(LOOPBACK, x_real_ip="100.1.1.1")) is None


def test_non_ip_forwarded_value_never_reaches_argv(monkeypatch):
    fake = _whois_ok()
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    assert client_node_name(_http(LOOPBACK, x_real_ip="--flag")) is None
    assert fake.calls == []


def test_missing_computed_name_is_none(monkeypatch):
    def fake(cmd, **kw):
        m = MagicMock(); m.returncode = 0
        m.stdout = json.dumps({"UserProfile": {"LoginName": "x@y"}}); return m
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    assert client_node_name(_http(LOOPBACK, x_real_ip="100.1.1.1")) is None


def test_dependency_returns_none_without_raising():
    """current_client_node is a soft dependency: no 401, just None."""
    from server.auth.middleware import current_client_node
    req = _http(("testclient", 1))
    assert current_client_node(req) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_auth_client_node.py -q`
Expected: FAIL — `ImportError: cannot import name 'client_node_name'`.

- [ ] **Step 3: Refactor `identity.py`** — replace the whole `_whois_identity` function (lines 144-182) with:

```python
def _whois(request: Request | WebSocket) -> dict | None:
    """Run `tailscale whois --json` against the request's verified peer address.

    Returns the parsed JSON document, or None on any failure. This is the ONLY
    subprocess wrapper for whois: identity (LoginName) and client locality
    (Node.ComputedName) are both projections over it, so they can never disagree
    about which peer was asked.
    """
    import json
    try:
        direct_peer = request.client.host if request.client else None
        peer_ip: str | None = direct_peer
        if _peer_is_trusted(request):
            # Only honour X-Real-IP when something entitled to set it sits in front:
            # loopback, or an address in CCSWITCH_TRUSTED_PROXY_IPS. This is the same
            # predicate the identity-header gate uses — one peer-trust rule in this
            # module, not two that can drift apart.
            peer_ip = request.headers.get("X-Real-IP") or direct_peer
        if not peer_ip:
            return None
        # Validate peer_ip is a real IP address to prevent argv flag injection
        # (e.g. a value like "--flag" being misinterpreted by tailscale whois).
        try:
            ipaddress.ip_address(peer_ip)
        except ValueError:
            return None
        result = subprocess.run(
            ["tailscale", "whois", "--json", "--", peer_ip],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _whois_identity(request: Request | WebSocket) -> str | None:
    """Try to resolve identity via `tailscale whois <peer-ip>`.

    Best-effort fallback; the header path is preferred.
    tailscale whois --json → {"UserProfile": {"LoginName": "user@example.com", ...}}
    """
    data = _whois(request)
    if not data:
        return None
    login = (data.get("UserProfile") or {}).get("LoginName")
    return login or None


def client_node_name(request: Request | WebSocket) -> str | None:
    """The tailnet machine the caller's browser is on, or None if unknowable.

    ``Node.ComputedName`` is the MagicDNS short name ("dobby"), which is also the
    first label of the kernel hostname and therefore what ``server.hostname
    .self_host_name()`` returns on that machine — the comparison the display
    router makes. Lower-cased so the comparison is case-insensitive. Never raises;
    a loopback peer with no forwarded address has no whois entry and resolves to
    None, so a local ``curl 127.0.0.1`` never masquerades as the operator's desktop.
    """
    data = _whois(request)
    if not data:
        return None
    name = (data.get("Node") or {}).get("ComputedName")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().lower()
```

- [ ] **Step 4: Add the dependency** — in `server/auth/middleware.py`, immediately after `current_identity` (ends ≈ line 409):

```python
def current_client_node(request: Request) -> str | None:
    """FastAPI dependency — the tailnet node the caller's browser is on, or None.

    Soft: it never rejects a request. Only the session-create route asks for it (an
    extra whois subprocess per create, not per request), so it is a dependency
    rather than middleware state.
    """
    return client_node_name(request)
```

and add `client_node_name` to the existing `from server.auth.identity import …` line at the top of `middleware.py`.

- [ ] **Step 5: Run the new tests plus the identity guards**

Run: `… -m pytest server/tests/test_auth_client_node.py server/tests/test_auth.py server/tests/test_auth_identity_peer_trust.py -q`
Expected: all PASS (the refactor must not change identity behaviour).

- [ ] **Step 6: Commit**

```bash
git add server/auth/identity.py server/auth/middleware.py server/tests/test_auth_client_node.py
printf 'feat(auth): resolve the client tailnet node via a shared whois wrapper\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 4: `NativeDisplay` — the host desktop as a display target

**Files:**
- Create: `server/sessions/native_display.py`
- Test: `server/tests/test_native_display.py`

**Interfaces:**
- Consumes: `server.sessions.x11manager._terminate(proc, timeout)`, `resolve_chromium()`, `CHROMIUM_CANDIDATES`.
- Produces:
  ```python
  class NativeDisplay:
      display: str                                   # property
      def available(self) -> bool
      def env(self) -> dict[str, str]                # {"DISPLAY": display}
      async def launch_browser(self, session_id: str, url: str | None) -> bool
      async def close(self, session_id: str) -> None
      async def stop_all(self) -> None
      def metrics(self) -> dict                      # {"display", "available", "browsers"}
  def build_native_display(settings) -> NativeDisplay | None
  ```

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_native_display.py`:

```python
"""NativeDisplay: the host's real X display as a session target (spec §4.3)."""
import os

import pytest

from server.sessions.native_display import NativeDisplay, build_native_display


class FakeProc:
    def __init__(self):
        self.pid = 4242; self.returncode = None; self.terminated = False
    def terminate(self): self.terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode if self.returncode is not None else 0


def _mk(tmp_path, display=":0", **kw):
    sock_dir = tmp_path / "x11"
    sock_dir.mkdir(exist_ok=True)
    spawned = []
    async def fake_spawn(*argv, **kwargs):
        spawned.append((argv, kwargs)); return FakeProc()
    nd = NativeDisplay(display, run_dir=str(tmp_path), chromium_cmd="/usr/bin/chromium",
                       no_sandbox=False, max_browsers=2, spawn=fake_spawn,
                       socket_dir=str(sock_dir), **kw)
    return nd, sock_dir, spawned


def test_available_only_when_socket_exists(tmp_path):
    nd, sock_dir, _ = _mk(tmp_path)
    assert nd.available() is False
    (sock_dir / "X0").touch()
    assert nd.available() is True          # evaluated live, not cached at build


def test_blocked_range_makes_it_unavailable(tmp_path):
    nd, sock_dir, _ = _mk(tmp_path, display=":101", blocked_range=range(100, 112))
    (sock_dir / "X101").touch()
    assert nd.available() is False


def test_env_has_display_and_no_xauthority(tmp_path):
    nd, _, _ = _mk(tmp_path, display=":0")
    assert nd.env() == {"DISPLAY": ":0"}


@pytest.mark.asyncio
async def test_launch_browser_targets_native_display_with_throwaway_profile(tmp_path):
    nd, sock_dir, spawned = _mk(tmp_path)
    (sock_dir / "X0").touch()
    assert await nd.launch_browser("s1", "https://example.com") is True
    argv, kwargs = spawned[0]
    assert argv[0] == "/usr/bin/chromium"
    assert kwargs["env"]["DISPLAY"] == ":0"
    assert "XAUTHORITY" not in kwargs["env"] or kwargs["env"]["XAUTHORITY"] == os.environ.get("XAUTHORITY")
    assert any(a.startswith("--user-data-dir=") and "ccswitch-native-s1-" in a for a in argv)
    assert argv[-1] == "https://example.com"
    assert nd.metrics()["browsers"] == 1


@pytest.mark.asyncio
async def test_launch_browser_refuses_when_unavailable(tmp_path):
    nd, _, spawned = _mk(tmp_path)          # no X0 socket
    assert await nd.launch_browser("s1", None) is False
    assert spawned == []


@pytest.mark.asyncio
async def test_launch_browser_honours_cap(tmp_path):
    nd, sock_dir, _ = _mk(tmp_path)
    (sock_dir / "X0").touch()
    assert await nd.launch_browser("s1", None)
    assert await nd.launch_browser("s1", None)
    assert await nd.launch_browser("s1", None) is False


@pytest.mark.asyncio
async def test_close_terminates_only_that_sessions_browsers_and_profiles(tmp_path):
    nd, sock_dir, spawned = _mk(tmp_path)
    (sock_dir / "X0").touch()
    await nd.launch_browser("s1", None); await nd.launch_browser("s2", None)
    profile_s1 = next(a for a in spawned[0][0] if a.startswith("--user-data-dir=")).split("=", 1)[1]
    assert os.path.isdir(profile_s1)
    await nd.close("s1")
    assert not os.path.isdir(profile_s1)
    assert nd.metrics()["browsers"] == 1
    await nd.close("s1")                    # idempotent


@pytest.mark.asyncio
async def test_stop_all(tmp_path):
    nd, sock_dir, _ = _mk(tmp_path)
    (sock_dir / "X0").touch()
    await nd.launch_browser("s1", None); await nd.launch_browser("s2", None)
    await nd.stop_all()
    assert nd.metrics()["browsers"] == 0


class _S:
    def __init__(self, **kw):
        self.native_display_enabled = True; self.native_display = ":0"
        self.enable_xvfb = False; self.display_start = 100; self.display_count = 12
        self.max_browsers = 6; self.chromium_no_sandbox = False; self.browser_liveness_s = 0.0
        self.__dict__.update(kw)


def test_build_returns_none_when_disabled():
    assert build_native_display(_S(native_display_enabled=False)) is None


def test_build_blocks_pool_range_when_xvfb_on(caplog):
    nd = build_native_display(_S(enable_xvfb=True, native_display=":105"))
    assert nd is not None
    assert nd.available() is False
    assert "inside the Xvfb pool" in caplog.text


def test_build_rejects_malformed_display():
    assert build_native_display(_S(native_display="localhost:0.0")) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_native_display.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Create the module**

```python
"""The host's real X display as a session target (spec §4.3).

Counterpart of X11Manager (pooled Xvfb displays): this one never starts an X
server — it only checks that the operator's desktop is there, hands out the env
that points at it, and supervises the Chromium windows ccswitch itself opens on
it so they can be closed with the session. No XAUTHORITY is set: the daemon
runs as the desktop's user and the X server grants SI:localuser access (verified
on dobby; the reauth fallback has relied on the same posture since #331).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile

from server.sessions.x11manager import CHROMIUM_CANDIDATES, _terminate, resolve_chromium

log = logging.getLogger("server.sessions.native_display")

_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")


def _display_number(display: str) -> int | None:
    m = _DISPLAY_RE.match(display.strip())
    return int(m.group(1)) if m else None


class NativeDisplay:
    def __init__(self, display: str = ":0", *, run_dir: str = "/tmp", chromium_cmd: str,
                 no_sandbox: bool, max_browsers: int, browser_liveness_s: float = 0.0,
                 spawn=asyncio.create_subprocess_exec, socket_dir: str = "/tmp/.X11-unix",
                 blocked_range: range | None = None) -> None:
        self._display = display.strip()
        self._number = _display_number(self._display)
        self._run_dir = run_dir
        self._chromium_cmd = chromium_cmd
        self._no_sandbox = no_sandbox
        self._max_browsers = max_browsers
        self._browser_liveness_s = browser_liveness_s
        self._spawn = spawn
        self._socket_dir = socket_dir
        self._blocked_range = blocked_range
        self._browsers: dict[str, list] = {}          # session_id → managed Chromium procs
        self._profiles: dict[str, list[str]] = {}     # session_id → throwaway --user-data-dir dirs

    @property
    def display(self) -> str:
        return self._display

    def available(self) -> bool:
        """True when the X server socket exists right now and the display is outside
        the Xvfb pool range. Evaluated live: a desktop that logs in after the daemon
        booted must work, and one that logs out must stop being offered."""
        if self._number is None:
            return False
        if self._blocked_range is not None and self._number in self._blocked_range:
            return False
        return os.path.exists(os.path.join(self._socket_dir, f"X{self._number}"))

    def env(self) -> dict[str, str]:
        return {"DISPLAY": self._display}

    def metrics(self) -> dict:
        return {"display": self._display, "available": self.available(),
                "browsers": sum(len(v) for v in self._browsers.values())}

    async def launch_browser(self, session_id: str, url: str | None) -> bool:
        if not self.available():
            return False
        procs = self._browsers.setdefault(session_id, [])
        if len(procs) >= self._max_browsers:
            return False
        argv = [self._chromium_cmd, "--disable-dev-shm-usage", "--no-first-run", "--new-window"]
        if self._no_sandbox:
            argv.append("--no-sandbox")
        # Chromium enforces one process per --user-data-dir: a launch on the default
        # profile would hand off to the operator's own running Chrome and exit. A
        # throwaway profile per launch keeps ccswitch's windows ccswitch's.
        profile = tempfile.mkdtemp(prefix=f"ccswitch-native-{session_id}-", dir=self._run_dir)
        argv.append(f"--user-data-dir={profile}")
        if url:
            argv.append(url)
        # Inherit the daemon env and point DISPLAY at the desktop; deliberately no
        # XAUTHORITY (SI:localuser access — see the module docstring).
        env = dict(os.environ, **self.env())
        try:
            proc = await self._spawn(*argv, env=env,
                                     stdout=asyncio.subprocess.DEVNULL,
                                     stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.exception("native display: chromium binary %r missing", self._chromium_cmd)
            shutil.rmtree(profile, ignore_errors=True)
            return False
        if self._browser_liveness_s > 0:
            await asyncio.sleep(self._browser_liveness_s)
            if getattr(proc, "returncode", None) is not None:
                log.warning("native display: browser for session %s exited during startup (rc=%s)",
                            session_id, proc.returncode)
                shutil.rmtree(profile, ignore_errors=True)
                return False
        procs.append(proc)
        self._profiles.setdefault(session_id, []).append(profile)
        return True

    async def close(self, session_id: str) -> None:
        procs = self._browsers.pop(session_id, [])
        for p in procs:
            await _terminate(p)
        for d in self._profiles.pop(session_id, []):
            shutil.rmtree(d, ignore_errors=True)

    async def stop_all(self) -> None:
        for sid in list(self._browsers) + [s for s in self._profiles if s not in self._browsers]:
            await self.close(sid)


def build_native_display(settings) -> NativeDisplay | None:
    """None when the feature is off or the display string is not a local ':N'.

    A display inside the Xvfb pool range still returns an object, but one whose
    ``available()`` is always False, with one warning: X11Manager's orphan sweep
    reaps every Chromium in that range, so honouring it would kill the operator's
    windows on the next daemon boot (the collision spec 002 documents for
    CCSWITCH_REAUTH_DISPLAY).
    """
    if not getattr(settings, "native_display_enabled", True):
        return None
    display = str(getattr(settings, "native_display", ":0") or "").strip()
    number = _display_number(display)
    if number is None:
        log.warning("native display: CCSWITCH_NATIVE_DISPLAY=%r is not a local ':N' display; disabled", display)
        return None
    blocked: range | None = None
    if getattr(settings, "enable_xvfb", False):
        start = int(getattr(settings, "display_start", 100))
        count = int(getattr(settings, "display_count", 12))
        blocked = range(start, start + count)
        if number in blocked:
            log.warning(
                "native display: %s is inside the Xvfb pool [%d, %d); native routing disabled "
                "on this host — move CCSWITCH_NATIVE_DISPLAY or CCSWITCH_DISPLAY_START",
                display, start, start + count)
    chromium = resolve_chromium()
    if chromium is None:
        log.warning("native display: no Chromium-family binary on PATH %s — 'Open a browser' "
                    "will fail for native sessions", CHROMIUM_CANDIDATES)
    return NativeDisplay(
        display,
        chromium_cmd=chromium or "chromium",
        no_sandbox=bool(getattr(settings, "chromium_no_sandbox", False)),
        max_browsers=int(getattr(settings, "max_browsers", 6)),
        browser_liveness_s=float(getattr(settings, "browser_liveness_s", 0.3)),
        blocked_range=blocked,
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `… -m pytest server/tests/test_native_display.py -q`
Expected: 11 PASS. If `test_build_blocks_pool_range_when_xvfb_on` cannot see the log line, add `caplog.set_level("WARNING", logger="server.sessions.native_display")` at its top.

- [ ] **Step 5: Lint + commit**

```bash
/home/tony/GitHub/ccswitch/.venv/bin/ruff check server/sessions/native_display.py server/tests/test_native_display.py
git add server/sessions/native_display.py server/tests/test_native_display.py
printf 'feat(sessions): NativeDisplay — the host desktop as a session display target\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 5: Persistence — column, migration, record, serializers

**Files:**
- Modify: `server/db/models.py:195` (after `host`)
- Create: `server/db/migrations/versions/0029_session_display_mode.py`
- Modify: `server/sessions/types.py:113` (field), `:212` (`to_dict`), `:274` (`from_dict`)
- Modify: `server/sessions/orchestrator.py:8398` (`_record_from_row`)
- Modify: `server/api/sessions.py:223` (serializer)
- Test: `server/tests/test_session_display_mode_persistence.py`

**Interfaces:**
- Produces: `Session.display_mode: str | None`, `SessionRecord.display_mode: str | None = None`, wire/API key `displayMode`.

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_session_display_mode_persistence.py`:

```python
"""display_mode survives every hop: DB row → SessionRecord → RPC dict → API payload."""
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**kw):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="", cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_record_defaults_to_none():
    assert _rec().display_mode is None


def test_to_dict_from_dict_roundtrip():
    d = _rec(display_mode="native").to_dict()
    assert d["displayMode"] == "native"
    assert SessionRecord.from_dict(d).display_mode == "native"
    assert SessionRecord.from_dict({k: v for k, v in d.items() if k != "displayMode"}).display_mode is None


def test_db_model_has_nullable_column():
    from server.db.models import Session
    col = Session.__table__.c.display_mode
    assert col.nullable is True


def test_record_from_row_maps_display_mode():
    from types import SimpleNamespace
    from server.sessions.orchestrator import SessionOrchestrator
    row = SimpleNamespace(id="s1", name="n", account_name="a", engine="claude-code",
                          engine_session_id="", cwd="/tmp", repo=None, status="idle",
                          name_is_auto=True, permission_mode="interactive", created_at=None,
                          display_mode="native")
    rec = SessionOrchestrator._record_from_row(SessionOrchestrator.__new__(SessionOrchestrator), row)
    assert rec.display_mode == "native"


def test_api_serializer_emits_display_mode():
    from server.api.sessions import _serialize_session
    payload = _serialize_session(_rec(display_mode="virtual"))
    assert payload["displayMode"] == "virtual"


def test_migration_chain():
    import importlib
    m = importlib.import_module("server.db.migrations.versions.0029_session_display_mode")
    assert m.down_revision == "0028_assistant_tokens"
```

Before running: open `server/api/sessions.py` around line 205-230 and read the real name of the record→dict serializer that emits `"host": getattr(record, "host", None)`; replace `_serialize_session` in the test with that name (it may take extra keyword arguments — pass the minimum the signature requires, mirroring an existing call in `server/tests/test_api.py`). If `AgentEngine.CLAUDE_CODE.value` is not `"claude-code"`, use the real value in the fake row.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_session_display_mode_persistence.py -q`
Expected: FAIL on `display_mode` / `displayMode` / module import.

- [ ] **Step 3: Model column** — `server/db/models.py`, after the `host` column (line 195):

```python
    # Where this session's GUI renders: "native" (the runner host's real X display)
    # or "virtual" (a pooled Xvfb display viewed over VNC). Decided once at spawn from
    # the requesting client's locality and honoured on every rehydrate. NULL = created
    # before this column existed → treated as virtual. Nullable so init_db backfills
    # without a migration. Design: docs/superpowers/specs/2026-09-06-native-display-routing-design.md
    display_mode: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

- [ ] **Step 4: Migration** — create `server/db/migrations/versions/0029_session_display_mode.py`:

```python
"""Native display routing: sessions.display_mode ("native" | "virtual" | NULL).

Revision ID: 0029_session_display_mode
Revises: 0028_assistant_tokens
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
_reconcile_columns ALTER-ADDs the nullable column on both SQLite and Postgres.
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_session_display_mode"
down_revision = "0028_assistant_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("display_mode", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "display_mode")
```

Check the real table name: `grep -n "__tablename__" server/db/models.py | head -3` and use the `Session` model's value.

- [ ] **Step 5: SessionRecord** — `server/sessions/types.py`: after the `host` field (line 113) add

```python
    # "native" | "virtual" | None — where this session's GUI renders (spec
    # 2026-09-06-native-display-routing). Mirrors DB Session.display_mode; None on
    # pre-feature rows and reads as virtual everywhere.
    display_mode: str | None = None
```

In `to_dict` after `"host": self.host,` add `"displayMode": self.display_mode,`; in `from_dict` after `host=d.get("host"),` add `display_mode=d.get("displayMode"),`.

- [ ] **Step 6: Row loader + API** — `server/sessions/orchestrator.py` `_record_from_row`, after `host=getattr(row, "host", None),` add `display_mode=getattr(row, "display_mode", None),`. `server/api/sessions.py` serializer, after the `"host"` line add:

```python
        # "native" | "virtual" | null — drives the desktop badge and whether the
        # Browser tab offers a VNC canvas at all.
        "displayMode": getattr(record, "display_mode", None),
```

- [ ] **Step 7: Run the new tests + schema guards**

Run: `… -m pytest server/tests/test_session_display_mode_persistence.py server/tests/test_schema_reconcile_coverage.py server/tests/test_migrations*.py -q`
(if a migrations test file does not exist under that glob, run `ls server/tests | grep -i migrat` and use what is there; a test asserting exactly one alembic head must still pass.)
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0029_session_display_mode.py server/sessions/types.py server/sessions/orchestrator.py server/api/sessions.py server/tests/test_session_display_mode_persistence.py
printf 'feat(db): persist sessions.display_mode through record, RPC and API\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 6: Orchestrator threading — resolve, inject, persist, rehydrate, teardown, open_browser

**Files:**
- Modify: `server/sessions/orchestrator.py`:
  - `__init__` ≈566 (`self._native = None`)
  - `create_session` signature ≈1848-1868 and its `_spawn_session` call ≈1950
  - `_spawn_session` signature ≈2796-2821, display allocation ≈2890-2895, pre-record ≈2929-2944, row insert ≈3030-3052
  - `_session_factory` ≈1275
  - `ensure_driver` display allocation ≈10317-10321
  - `_teardown_driver_state` ≈3274-3278
  - new `open_browser` method (place right after `_teardown_driver_state`)
- Test: `server/tests/test_orchestrator_native_display.py`

**Interfaces:**
- Consumes: Task 2 `resolve_display_mode` / constants; Task 4 `NativeDisplay`; Task 5 `SessionRecord.display_mode`; `server.hostname.self_host_name`.
- Produces: `create_session(..., client_node: str | None = None, display: str | None = None)`; `orchestrator._native: NativeDisplay | None`; `orchestrator._display_env(session_id) -> dict[str, str]`; `async orchestrator.open_browser(session_id, url) -> dict` with `{"ok": bool, "reason": None | "no_display" | "cap" | "native_unavailable"}`.

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_orchestrator_native_display.py`:

```python
"""Native display routing inside the orchestrator (spec §4.6).

Pattern: server/tests/test_orchestrator_copilot_env.py — the fake SDK factory records
the extra_env the orchestrator hands it, which is where DISPLAY lands.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


class FakeNative:
    def __init__(self, available=True, display=":0"):
        self._available = available; self.display = display
        self.launched: list[tuple[str, str | None]] = []; self.closed: list[str] = []
    def available(self): return self._available
    def env(self): return {"DISPLAY": self.display}
    async def launch_browser(self, sid, url): self.launched.append((sid, url)); return True
    async def close(self, sid): self.closed.append(sid)
    async def stop_all(self): ...


class FakeX11:
    def __init__(self):
        self.started: list[str] = []; self.stopped: list[str] = []; self.launched = []
    async def start_display(self, sid): self.started.append(sid); return object()
    async def stop_display(self, sid): self.stopped.append(sid)
    def env_for(self, sid):
        return {"DISPLAY": ":100", "XAUTHORITY": f"/tmp/{sid}.Xauthority"} if sid in self.started else {}
    def display_for(self, sid): return object() if sid in self.started else None
    async def launch_browser(self, sid, url): self.launched.append((sid, url)); return True


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed(orch, sid, **kw):
    orch._records[sid] = SessionRecord(
        id=sid, name="t", account="acct", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="", cols=80, rows=24, cwd="/tmp", **kw)


# --- _display_env / _session_factory (unit, no DB) ---------------------------------

def test_factory_injects_native_display_for_native_record():
    seen = {}
    def rec_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        class _F:
            async def connect(self, *a, **k): ...
            async def disconnect(self): ...
        return _F()
    orch = SessionOrchestrator(sdk_factory=rec_factory, x11=FakeX11())
    orch._native = FakeNative()
    _seed(orch, "s1", display_mode="native")
    orch._session_factory("s1")("/tmp", "/cfg", None, "interactive", None)
    assert seen["extra_env"]["DISPLAY"] == ":0"
    assert "XAUTHORITY" not in seen["extra_env"]


def test_factory_uses_x11_env_for_virtual_record():
    seen = {}
    def rec_factory(cwd, cfg, cut, mode, resume, extra_env=None, *, on_pre_compact=None):
        seen["extra_env"] = extra_env
        class _F: ...
        return _F()
    x11 = FakeX11(); x11.started.append("s1")
    orch = SessionOrchestrator(sdk_factory=rec_factory, x11=x11)
    orch._native = FakeNative()
    _seed(orch, "s1", display_mode="virtual")
    orch._session_factory("s1")("/tmp", "/cfg", None, "interactive", None)
    assert seen["extra_env"]["DISPLAY"] == ":100"


def test_native_record_without_desktop_degrades_to_no_display():
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), x11=FakeX11())
    orch._native = FakeNative(available=False)
    _seed(orch, "s1", display_mode="native")
    assert orch._display_env("s1") == {}


# --- create_session end to end -----------------------------------------------------

@pytest.fixture
def spawn_env(tmp_path):
    shared = tmp_path / "dot-claude"; shared.mkdir()
    (shared / "settings.json").write_text("{}"); (shared / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "acct"; cfg_dir.mkdir(parents=True)
    (tmp_path / "repos").mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
    from ccswitch.selector import Picked
    patches = [
        patch.dict(os.environ, {"CCSWITCH_HOME": str(tmp_path / "home"),
                                "CLAUDE_CONFIG_DIR": str(shared), "CCSWITCH_DB_URL": db_url,
                                "CCSWITCH_HOST_NAME": "dobby"}),
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("acct")),
    ]
    for p in patches: p.start()
    yield db_url, str(tmp_path / "repos")
    for p in patches: p.stop()


async def _orch(spawn_env, x11, native):
    from server.db import init_db
    db_url, repo_root = spawn_env
    await init_db(db_url=db_url)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), x11=x11,
                               poll_interval=9999.0, usage_fn=_fake_usage, repo_root=repo_root)
    orch._native = native
    await orch.start()
    return orch


@pytest.mark.asyncio
async def test_local_client_gets_native_and_no_xvfb_lease(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        assert rec.display_mode == "native"
        assert x11.started == []
        assert orch._display_env(rec.id) == {"DISPLAY": ":0"}
        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            row = await db.get(DbSession, rec.id)
        assert row.display_mode == "native"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_remote_client_gets_virtual(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="phone")
        assert rec.display_mode == "virtual"
        assert x11.started == [rec.id]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_explicit_virtual_from_local_client(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby", display="virtual")
        assert rec.display_mode == "virtual" and x11.started == [rec.id]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_no_native_display_wired_means_virtual(spawn_env):
    x11 = FakeX11()
    orch = await _orch(spawn_env, x11, None)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        assert rec.display_mode == "virtual"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_close_tears_down_native_browsers(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        await orch.close_session(rec.id)
        assert native.closed == [rec.id]
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_open_browser_dispatches_by_mode(spawn_env):
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        nat = await orch.create_session("n", account="acct", client_node="dobby")
        vir = await orch.create_session("v", account="acct", client_node="phone")
        assert await orch.open_browser(nat.id, "https://a") == {"ok": True, "reason": None}
        assert native.launched == [(nat.id, "https://a")]
        assert await orch.open_browser(vir.id, None) == {"ok": True, "reason": None}
        assert x11.launched == [(vir.id, None)]
        assert await orch.open_browser("nope", None) == {"ok": False, "reason": "no_display"}
        native._available = False
        assert await orch.open_browser(nat.id, None) == {"ok": False, "reason": "native_unavailable"}
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_rehydrate_keeps_native_and_skips_lease(spawn_env):
    """A daemon restart must re-inject :0, not silently hand the session an Xvfb lease."""
    x11, native = FakeX11(), FakeNative()
    orch = await _orch(spawn_env, x11, native)
    try:
        rec = await orch.create_session("n", account="acct", client_node="dobby")
        sid = rec.id
    finally:
        await orch.stop()
    x11b, nativeb = FakeX11(), FakeNative()
    orch2 = await _orch(spawn_env, x11b, nativeb)
    try:
        await orch2.rehydrate()
        assert orch2._records[sid].display_mode == "native"
        drv = await orch2.ensure_driver(sid)
        assert drv is not None
        assert x11b.started == []
        assert orch2._display_env(sid) == {"DISPLAY": ":0"}
    finally:
        await orch2.stop()
```

If `rehydrate()` has a different name or needs arguments, read `grep -n "async def rehydrate" server/sessions/orchestrator.py` and adapt; if `close_session` requires an identity kwarg, pass the same the existing `test_orchestrator*.py` tests pass.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_orchestrator_native_display.py -q --timeout=120`
Expected: FAIL — `TypeError: create_session() got an unexpected keyword argument 'client_node'` and `AttributeError: _display_env`.

- [ ] **Step 3: Implement** — in `server/sessions/orchestrator.py`:

(a) imports near the other `server.sessions.*` imports:
```python
from server.hostname import self_host_name
from server.sessions.display_policy import DISPLAY_NATIVE, DISPLAY_VIRTUAL, resolve_display_mode
```
(`self_host_name` may already be imported for `local_host_stamp`; do not import twice.)

(b) `__init__` after `self._x11 = x11 …` (≈566):
```python
        # NativeDisplay | None — the host's real X display; wired by app.py / ipc.serve
        # exactly like _x11. Sessions created from that host's own desktop render here.
        self._native = None
```

(c) `create_session` signature: add after `engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,`:
```python
        client_node: str | None = None,
        display: str | None = None,
```
and forward both in the `_spawn_session(...)` call inside `create_session` (≈1950): `client_node=client_node, display=display,`. Search for every other `_spawn_session(` caller (`grep -n "_spawn_session(" server/sessions/orchestrator.py`); they are queue promotions / resumes that keep the defaults — leave them.

(d) `_spawn_session` signature: add after `engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,`:
```python
        client_node: str | None = None,
        display: str | None = None,
```

(e) Replace the display-allocation block (≈2890-2895) with:
```python
        # Decide where this session's GUI renders — ONCE, here — then either lease a
        # virtual Xvfb display or leave the host's real one to _display_env. A
        # previously-queued/resumed row keeps its persisted decision.
        display_mode = getattr(_prior, "display_mode", None) or resolve_display_mode(
            requested=display, client_node=client_node, runner=self_host_name(),
            native_available=self._native is not None and self._native.available())
        if display_mode == DISPLAY_VIRTUAL and self._x11 is not None:
            try:
                await self._x11.start_display(session_id)
            except Exception:
                log.exception("session.display_start_failed id=%s", session_id)
        elif display_mode == DISPLAY_NATIVE:
            log.info("session.display_native id=%s client_node=%s display=%s",
                     session_id, client_node, self._native.display)
```
`_prior` is defined a few lines below today (≈2912, `_host = getattr(_prior, "host", None) or …`); move the `_prior` lookup above this block if it is not already in scope — read the surrounding 40 lines first.

(f) Pre-record (≈2929): add `display_mode=display_mode,` next to `host=_host,`. Row insert (≈3030): add `display_mode=display_mode,` after `host=_host,`. If the queued-promotion branch just above builds a row update instead (the `else:` partner), set `display_mode` there too when the row's value is NULL.

(g) `_display_env` + `_session_factory`: add the method right above `_session_factory` and change line 1275:
```python
    def _display_env(self, session_id: str) -> dict[str, str]:
        """The DISPLAY/XAUTHORITY env for this session's process — the one place it
        is chosen. Native records get the host desktop (no XAUTHORITY, see
        NativeDisplay); everything else the Xvfb lease if one exists. A native record
        whose desktop is gone gets {} — the same degrade an exhausted pool gives."""
        rec = self._records.get(session_id)
        if rec is not None and rec.display_mode == DISPLAY_NATIVE:
            if self._native is not None and self._native.available():
                return self._native.env()
            return {}
        return self._x11.env_for(session_id) if self._x11 else {}
```
```python
            extra = {**self._display_env(session_id), **self._oat_env(session_id)}
```
and drop the now-unused `x11 = self._x11` local in `_session_factory`.

(h) `ensure_driver` (≈10317): wrap the lease in the mode check:
```python
            if self._x11 is not None and getattr(rec, "display_mode", None) != DISPLAY_NATIVE:
```

(i) `_teardown_driver_state` (≈3274): after the `stop_display` block add:
```python
        if self._native is not None:
            try:
                await self._native.close(session_id)
            except Exception:
                log.exception("session.native_display_cleanup_failed id=%s", session_id)
```

(j) `open_browser`, placed right after `_teardown_driver_state`:
```python
    async def open_browser(self, session_id: str, url: str | None) -> dict:
        """Launch a managed Chromium for this session on whichever display it owns.

        Returns {"ok", "reason"}; reasons: no_display (no such live session / no lease),
        cap (per-session browser limit), native_unavailable (the desktop went away).
        The daemon RPC ``x11_open_browser`` and the in-process API route both call
        this, so the native/virtual branch exists exactly once.
        """
        rec = self._records.get(session_id)
        if rec is not None and rec.display_mode == DISPLAY_NATIVE:
            if self._native is None or not self._native.available():
                return {"ok": False, "reason": "native_unavailable"}
            ok = await self._native.launch_browser(session_id, url)
            return {"ok": bool(ok), "reason": None if ok else "cap"}
        if self._x11 is None or self._x11.display_for(session_id) is None:
            return {"ok": False, "reason": "no_display"}
        ok = await self._x11.launch_browser(session_id, url)
        return {"ok": bool(ok), "reason": None if ok else "cap"}
```

- [ ] **Step 4: Run the new tests and the neighbours**

Run: `… -m pytest server/tests/test_orchestrator_native_display.py server/tests/test_orchestrator_copilot_env.py server/tests/test_x11_integration.py server/tests/test_session_id_persistence.py -q --timeout=180`
Expected: all PASS.

- [ ] **Step 5: mypy + ruff on the touched files, then commit**

```bash
/home/tony/GitHub/ccswitch/.venv/bin/ruff check server/sessions/orchestrator.py server/tests/test_orchestrator_native_display.py
/home/tony/GitHub/ccswitch/.venv/bin/mypy server/sessions/orchestrator.py --ignore-missing-imports
git add server/sessions/orchestrator.py server/tests/test_orchestrator_native_display.py
printf 'feat(sessions): resolve, persist and inject native vs virtual display per session\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 7: Wiring — edge app, daemon, RPC handler, shutdown

**Files:**
- Modify: `server/app.py:690-697` (build native next to x11), `:927-945` (`open_browser` route → orchestrator), `:544-560` (shutdown `stop_all`)
- Modify: `server/ipc/serve.py:94-96` (wire `_native`, pass orchestrator to `register_x11`), `:241-246` (shutdown)
- Modify: `server/ipc/x11_rpc.py:33` (`register_x11(app, x11, orchestrator=None)`), `:59-63` (`x11_open_browser` → `orchestrator.open_browser`)
- Test: `server/tests/test_x11_rpc.py` (adjust), `server/tests/test_app_x11_lifespan.py` (add), `server/tests/test_native_display_wiring.py` (new)

**Interfaces:**
- Consumes: Task 4 `build_native_display`, Task 6 `orchestrator.open_browser`, `orchestrator._native`.
- Produces: `app.state.native_display`; daemon RPC `x11_open_browser` reasons now include `native_unavailable`; HTTP 409 detail `"native display unavailable"`.

- [ ] **Step 1: Write the failing tests** — create `server/tests/test_native_display_wiring.py`:

```python
"""Both processes that own sessions wire NativeDisplay the same way X11Manager is wired."""
import pytest
from fastapi import FastAPI

from server.ipc.x11_rpc import register_x11


class _Orch:
    def __init__(self, result): self.result = result; self.calls = []
    async def open_browser(self, sid, url): self.calls.append((sid, url)); return self.result


def _daemon_app() -> FastAPI:
    app = FastAPI()
    app.state.rpc = {}          # what server.ipc.daemon.register_rpc writes into
    return app


@pytest.mark.asyncio
async def test_daemon_open_browser_rpc_delegates_to_orchestrator():
    app = _daemon_app()
    orch = _Orch({"ok": False, "reason": "native_unavailable"})
    register_x11(app, None, orchestrator=orch)
    handler = app.state.rpc["x11_open_browser"]
    assert await handler({"session_id": "S", "url": "u"}) == {"ok": False, "reason": "native_unavailable"}
    assert orch.calls == [("S", "u")]


@pytest.mark.asyncio
async def test_daemon_open_browser_without_orchestrator_keeps_legacy_no_display():
    app = _daemon_app()
    register_x11(app, None)
    handler = app.state.rpc["x11_open_browser"]
    assert await handler({"session_id": "S"}) == {"ok": False, "reason": "no_display"}
```

Add to `server/tests/test_app_x11_lifespan.py` (it already imports `create_app` and builds apps with a fake orchestrator — see `test_edge_startup_reaps_orphan_display_processes` at line 45 for the `create_app(orchestrator=orch)` pattern; reuse its fake orchestrator class):

```python
def test_native_display_is_built_in_process(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY_ENABLED", "true")
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY", ":0")
    import importlib, server.config as cfg
    importlib.reload(cfg)
    orch = _FakeOrch()          # the fake this file already defines for create_app
    app = create_app(orchestrator=orch)
    nd = getattr(app.state, "native_display", None)
    assert nd is not None and nd.display == ":0"
    assert orch._native is nd


def test_native_display_is_none_when_disabled(monkeypatch):
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY_ENABLED", "false")
    import importlib, server.config as cfg
    importlib.reload(cfg)
    app = create_app(orchestrator=_FakeOrch())
    assert app.state.native_display is None
```

Use the fake orchestrator class name that file actually defines (line 37 area) in place of `_FakeOrch`; if it is a plain object without `_native`, attribute assignment still works. Remote mode (`app.state.native_display = None`) is exercised by the existing remote-mode app tests once the code lands; no new remote fixture is needed here.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_native_display_wiring.py server/tests/test_app_x11_lifespan.py -q`
Expected: FAIL (`register_x11() got an unexpected keyword argument 'orchestrator'`, missing `native_display` state).

- [ ] **Step 3: `x11_rpc.py`** — signature and handler:

```python
def register_x11(app, x11, orchestrator=None) -> None:
    """Register the daemon /vnc WS endpoint and x11 RPCs against `x11` (may be None).

    `orchestrator` owns the native-vs-virtual branch for "Open a browser"; when it is
    absent (older callers, unit tests) the RPC keeps the virtual-only behaviour."""
```
```python
    async def x11_open_browser(params: dict) -> dict:
        session_id = params["session_id"]
        if orchestrator is not None and hasattr(orchestrator, "open_browser"):
            return await orchestrator.open_browser(session_id, params.get("url"))
        if x11 is None or x11.display_for(session_id) is None:
            return {"ok": False, "reason": "no_display"}
        ok = await x11.launch_browser(session_id, params.get("url"))
        return {"ok": bool(ok), "reason": None if ok else "cap"}
```

- [ ] **Step 4: `ipc/serve.py`** — after `orchestrator._x11 = build_x11_manager(get_settings())` (line 70):
```python
    from server.sessions.native_display import build_native_display
    orchestrator._native = build_native_display(get_settings())
```
Change `register_x11(app, app.state.x11)` (line 96) to `register_x11(app, app.state.x11, orchestrator=orchestrator)`. In the shutdown block after the x11 `stop_all` (≈246):
```python
        native = getattr(orchestrator, "_native", None)
        if native is not None:
            try:
                await asyncio.wait_for(native.stop_all(), timeout=_DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:
                log.warning("daemon shutdown: native display teardown failed", exc_info=True)
```

- [ ] **Step 5: `app.py`** — after the x11 block (≈697):
```python
    # Native display — the host desktop as a session target. Same ownership rule as
    # x11: in remote mode the daemon has it, the edge only proxies.
    from server.sessions.native_display import build_native_display
    if getattr(app.state, "remote_mode", False):
        app.state.native_display = None
    else:
        app.state.native_display = build_native_display(_gs())
        if orchestrator is not None:
            orchestrator._native = app.state.native_display
```
Replace the in-process branch of `open_browser` (≈939-945) with:
```python
        res = await app.state.sessions.open_browser(session_id, payload.get("url"))
        if not res.get("ok"):
            raise HTTPException(status_code=409, detail=_OPEN_BROWSER_DETAIL.get(
                res.get("reason"), "browser cap reached"))
        return {"ok": True}
```
and make the remote branch use the same mapping. Define once, above the route:
```python
    _OPEN_BROWSER_DETAIL = {
        "no_display": "no display for session",
        "cap": "browser cap reached",
        "native_unavailable": "native display unavailable",
    }
```
Shutdown (≈after the `reauth_x11` sweep):
```python
        _nd = getattr(app.state, "native_display", None)
        if _nd is not None:
            try:
                await asyncio.wait_for(_nd.stop_all(), timeout=_SHUTDOWN_SWEEP_TIMEOUT_S)
            except Exception:
                log.warning("shutdown: native display sweep incomplete", exc_info=True)
```
The in-process `app.state.sessions` is the `SessionOrchestrator`; check `server/sessions/facade.py` for an abstract method list or guard scan (`grep -n "open_browser\|x11_open_browser" server/sessions/facade.py server/tests/test_remote_orchestrator_guard*.py`) and, if the facade enumerates orchestrator methods, add `open_browser` there so the "unproxied method" scan does not flag it. Do **not** add `open_browser` to `RemoteOrchestrator`: remote mode keeps calling `x11_open_browser`.

- [ ] **Step 6: Run**

Run: `… -m pytest server/tests/test_native_display_wiring.py server/tests/test_app_x11_lifespan.py server/tests/test_x11_rpc.py server/tests/test_healthz_vnc.py server/tests/test_vnc_ws*.py server/tests/test_daemon_shutdown_x11.py -q --timeout=180`
Expected: all PASS (fix any `test_x11_rpc.py` case that asserted the old inline branch by passing `orchestrator=None`).

- [ ] **Step 7: Commit**

```bash
git add server/app.py server/ipc/serve.py server/ipc/x11_rpc.py server/tests/test_native_display_wiring.py server/tests/test_app_x11_lifespan.py server/tests/test_x11_rpc.py
printf 'feat(app,ipc): wire NativeDisplay on edge and daemon; open_browser via orchestrator\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 8: API field, fleet placement nudge, rolling-upgrade shim

**Files:**
- Modify: `server/api/sessions.py:80-95` (`display` field), `:560-565` (dependency), `:675-691` (forward)
- Modify: `server/sessions/fleet_orchestrator.py:206-226` (prefer nudge)
- Modify: `server/sessions/remote.py:530-562` (TypeError retry)
- Test: `server/tests/test_api_sessions_display.py` (new), `server/tests/test_fleet_orchestrator.py` (add), `server/tests/test_remote_orchestrator_x11.py` (add)

**Interfaces:**
- Consumes: Task 3 `current_client_node`; Task 2 `REQUESTABLE`; Task 6 `create_session(client_node=, display=)`.
- Produces: `CreateSessionRequest.display: Literal["auto","native","virtual"] | None`.

- [ ] **Step 1: Write the failing tests**

`server/tests/test_api_sessions_display.py` (model the client/fixture on the nearest create test in `server/tests/test_api.py` — search for `"/api/sessions"` with `json=` and reuse its `client` fixture and identity env):

```python
"""POST /api/sessions carries client_node + display to the orchestrator (spec §4.8)."""
import pytest


@pytest.mark.asyncio
async def test_create_forwards_client_node_and_display(client, monkeypatch):
    seen = {}
    async def fake_create(**kw):
        seen.update(kw)
        from server.tests.test_session_display_mode_persistence import _rec
        return _rec(id="s-new", display_mode="native")
    app = client.app
    monkeypatch.setattr(app.state.sessions, "create_session", fake_create)
    monkeypatch.setattr("server.auth.middleware.client_node_name", lambda req: "dobby")
    r = await client.post("/api/sessions", json={"display": "auto"})
    assert r.status_code == 201, r.text
    assert seen["client_node"] == "dobby" and seen["display"] == "auto"
    assert r.json()["displayMode"] == "native"


@pytest.mark.asyncio
async def test_create_rejects_unknown_display_value(client):
    r = await client.post("/api/sessions", json={"display": "desktop"})
    assert r.status_code == 422
```

Append to `server/tests/test_fleet_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_local_client_prefers_its_own_host(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec(f"sess-{self.name}")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = {}
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None):
        got["prefer"] = prefer; return ["dobby", "rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(client_node="dobby")
    assert got["prefer"] == "dobby"


@pytest.mark.asyncio
async def test_explicit_host_and_explicit_virtual_beat_the_nudge(monkeypatch):
    rocky, dobby = _FakeOrch("rocky"), _FakeOrch("dobby")
    async def create_session(self, **kw): return _Rec("s")
    _FakeOrch.create_session = create_session
    router = _FakeRouter({"rocky": rocky, "dobby": dobby}); got = []
    async def pick(*, prefer=None, engine=None, expected_contract_sha256=None):
        got.append(prefer); return ["rocky"]
    router.pick = pick
    fo = FleetOrchestrator(router, primary="rocky")
    monkeypatch.setattr(fo, "_stamp_host", lambda sid, host: _acoro(None))
    await fo.create_session(prefer="rocky", client_node="dobby")
    await fo.create_session(client_node="dobby", display="virtual")
    await fo.create_session(client_node="laptop")          # not a fleet host
    assert got == ["rocky", None, None]
```

Append to `server/tests/test_remote_orchestrator_x11.py`:

```python
@pytest.mark.asyncio
async def test_create_session_retries_without_display_kwargs_on_old_daemon(caplog):
    from server.ipc.client import DaemonError
    class _Old:
        def __init__(self): self.calls = []
        async def call(self, method, **params):
            self.calls.append((method, params))
            if "client_node" in params:
                raise DaemonError("TypeError",
                                  "create_session() got an unexpected keyword argument 'client_node'")
            return {"id": "s1", "name": "n", "account": "a", "engine": "claude-code",
                    "engineSessionId": "", "cols": 80, "rows": 24, "cwd": "/tmp"}
    c = _Old()
    rec = await RemoteOrchestrator(c).create_session(name="n", client_node="dobby", display="auto")
    assert rec.id == "s1"
    assert len(c.calls) == 2 and "client_node" not in c.calls[1][1] and "display" not in c.calls[1][1]
    assert "predates display routing" in caplog.text
```

Check what shape `RemoteOrchestrator.create_session` expects back from `call` (read lines 555-575); if it wraps the record under a key, return that shape.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_api_sessions_display.py server/tests/test_fleet_orchestrator.py server/tests/test_remote_orchestrator_x11.py -q`
Expected: the three new tests FAIL.

- [ ] **Step 3: API** — `server/api/sessions.py`: add `from typing import Literal` if absent, `from server.auth.middleware import current_client_node` alongside `current_identity`. In `CreateSessionRequest` after `host`:
```python
    # Where the session's GUI renders. None/"auto" → decided from the requesting
    # client's locality (native when the browser is on the runner host); "native" /
    # "virtual" force it. Only the resolved mode is persisted (Session.displayMode).
    display: Literal["auto", "native", "virtual"] | None = None
```
Route signature: add `client_node: str | None = Depends(current_client_node),` after `identity`. Forward in `orch.create_session(...)`: `client_node=client_node, display=body.display,`.

- [ ] **Step 4: Fleet nudge** — `fleet_orchestrator.py`, after the assistant pin (≈226):
```python
        # A client sitting at a fleet host's own desktop should land there, or "route
        # to the native display" can never hold. An explicit host still wins, and an
        # explicit "virtual" request has no reason to move.
        if (prefer is None and kwargs.get("display") != "virtual"
                and kwargs.get("client_node") in self._router.names()):
            prefer = kwargs["client_node"]
```

- [ ] **Step 5: Remote shim** — `remote.py`, wrap the `self._client.call("create_session", **kwargs)`:
```python
        try:
            r = await self._client.call("create_session", **kwargs)
        except DaemonError as exc:
            if (exc.err_type == "TypeError"
                    and ("client_node" in exc.message or "'display'" in exc.message)
                    and ("client_node" in kwargs or "display" in kwargs)):
                # Rolling upgrade: this daemon predates display routing and rejects
                # the additive kwargs at call binding, before any work. Retry without
                # them — the session is virtual there, which is what it would have
                # been anyway. Same posture as the protocol-v1 `engine` pop above.
                log.warning("daemon predates display routing; creating without "
                            "client_node/display (session will use a virtual display)")
                stripped = {k: v for k, v in kwargs.items() if k not in ("client_node", "display")}
                r = await self._client.call("create_session", **stripped)
            else:
                raise
        …existing error reconstruction continues on `exc` for the non-retry path…
```
Integrate with the existing `except DaemonError as exc:` block so `AllAccountsBusy` / clone-error reconstruction still runs for every other error type (put the retry check first, then fall through to the existing branches). Make sure `log` exists in `remote.py` (`grep -n "^log\b\|getLogger" server/sessions/remote.py`).

- [ ] **Step 6: Run**

Run: `… -m pytest server/tests/test_api_sessions_display.py server/tests/test_fleet_orchestrator.py server/tests/test_remote_orchestrator_x11.py server/tests/test_api.py -q --timeout=180`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add server/api/sessions.py server/sessions/fleet_orchestrator.py server/sessions/remote.py server/tests/test_api_sessions_display.py server/tests/test_fleet_orchestrator.py server/tests/test_remote_orchestrator_x11.py
printf 'feat(api,fleet): display override, client-node placement nudge, old-daemon shim\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 9: Web — types, native Browser tab, desktop badge

**Files:**
- Modify: `web/src/api/types.ts:296` (after `host`), `web/src/api/hooks.ts:335` (after `host`)
- Modify: `web/src/components/BrowserTab.tsx:26-30` (props), `:113-125` (connect effect), `:182+` (native panel)
- Modify: `web/src/components/session/SessionBody.tsx:310` (pass props)
- Modify: `web/src/components/SessionCard.tsx:3,435-446`; `web/src/components/session/SessionMeta.tsx:40,206-214`
- Test: `web/src/components/BrowserTab.test.tsx`, `web/src/components/__tests__/SessionCard.meta.test.tsx`

**Interfaces:**
- Consumes: API `displayMode` (Task 5).
- Produces: `Session.displayMode?: 'native' | 'virtual' | null`; `BrowserTab` props `displayMode?: 'native' | 'virtual' | null`, `host?: string | null`; `SessionMeta` prop `displayMode?: 'native' | 'virtual' | null`.

- [ ] **Step 1: Write the failing tests** — append to `BrowserTab.test.tsx`:

```tsx
describe('BrowserTab native display', () => {
  it('shows the desktop notice instead of a VNC canvas and never connects', () => {
    setCoarsePointer(false)
    const spy = vi.spyOn(globalThis, 'WebSocket' as never)
    render(<BrowserTab sessionId="s" statusState="idle" displayMode="native" host="dobby" />)
    expect(screen.getByText(/opens windows on dobby's desktop/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /open a browser/i })).toBeInTheDocument()
    expect(document.querySelector('canvas')).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('falls back to "the server" when the host is unknown', () => {
    setCoarsePointer(false)
    render(<BrowserTab sessionId="s" statusState="idle" displayMode="native" host={null} />)
    expect(screen.getByText(/opens windows on the server's desktop/i)).toBeInTheDocument()
  })
})
```

Append to `SessionCard.meta.test.tsx`:

```tsx
describe('SessionCard desktop badge', () => {
  it('shows a desktop badge for a native-display session', () => {
    renderCard({ ...base, displayMode: 'native', host: 'dobby' })
    expect(screen.getByTitle(/windows open on the host's desktop/i)).toBeInTheDocument()
  })
  it('shows no badge for virtual or legacy sessions', () => {
    renderCard({ ...base, displayMode: 'virtual' })
    expect(screen.queryByTitle(/windows open on the host's desktop/i)).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/components/BrowserTab.test.tsx src/components/__tests__/SessionCard.meta.test.tsx)`
Expected: FAIL (typecheck on the unknown prop / missing text).

- [ ] **Step 3: Types** — `types.ts` after `host`:
```ts
  /** Where the session's GUI renders: 'native' = the runner host's real desktop,
   *  'virtual' = a pooled Xvfb display viewed over VNC. Null for pre-feature rows
   *  (read as virtual). */
  displayMode?: 'native' | 'virtual' | null
```
`hooks.ts` `CreateSessionInput` after `host`:
```ts
  /** Force where the GUI renders; omitted/'auto' → decided from client locality. */
  display?: 'auto' | 'native' | 'virtual'
```

- [ ] **Step 4: BrowserTab** — props:
```ts
interface Props {
  sessionId: string
  statusState: string
  initialState?: VncState
  displayMode?: 'native' | 'virtual' | null
  host?: string | null
}
export function BrowserTab({ sessionId, statusState, initialState, displayMode, host }: Props) {
  const native = displayMode === 'native'
```
Connect effect guard: `if (initialState || native) return` and add `native` to the dependency array. Early return **after all hooks** (just before the main `return (`):
```tsx
  if (native) {
    const where = host ? `${host}'s desktop` : "the server's desktop"
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-gray-600 dark:text-gray-300">
        <Monitor size={28} aria-hidden="true" className="text-gray-400" />
        <p>This session opens windows on {where}. There is nothing to stream here.</p>
        <button type="button" onClick={openBrowser}
          className="rounded-md border border-edge px-3 py-1.5 text-sm font-medium hover:bg-surface-2">
          Open a browser
        </button>
        {openError && <p role="alert" className="text-xs text-red-600">{openError}</p>}
      </div>
    )
  }
```
Import `Monitor` from `lucide-react` (add to the file's existing lucide import or create one). Extend the `openBrowser` 409 mapping with `body?.detail === 'native display unavailable' ? 'The host desktop is not available right now (is anyone logged in?).'`.

- [ ] **Step 5: SessionBody** — line 310:
```tsx
<BrowserTab sessionId={rt.sessionId} statusState={rt.state.status.state}
            displayMode={sessionRecord?.displayMode} host={sessionRecord?.host} />
```

- [ ] **Step 6: Badges** — `SessionCard.tsx`: add `Monitor` to the lucide import; inside the host chip fragment (after the `{session.host}` span, still inside the `session.host &&` block is wrong because primary-local rows have no host — so place it **after** the whole host block, before the `<Sep />` at line 447):
```tsx
        {session.displayMode === 'native' && (
          <>
            <Sep />
            <span data-testid="card-desktop" className="flex-shrink-0 inline-flex items-center gap-1 text-gray-500 dark:text-gray-400"
                  title="Windows open on the host's desktop">
              <Monitor size={12} aria-hidden="true" />
              <span>desktop</span>
            </span>
          </>
        )}
```
`SessionMeta.tsx`: add prop `displayMode?: 'native' | 'virtual' | null` at line 40, and after the Runner-host block:
```tsx
        {displayMode === 'native' && (
          <span className="inline-flex items-center gap-1" aria-label="Display" title="Windows open on the host's desktop">
            <span className="text-gray-400 dark:text-gray-500">display</span>
            <span className="font-medium text-gray-900 dark:text-gray-100">desktop</span>
          </span>
        )}
```
and pass `displayMode={sessionRecord?.displayMode}` from `SessionBody.tsx` line 85 area.

- [ ] **Step 7: Full web gate**

Run: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`; then `rm -rf web/dist`.
Expected: all green, `--max-warnings 0` respected. Run the **full** vitest (this command does), not just the two files.

- [ ] **Step 8: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/components/BrowserTab.tsx web/src/components/BrowserTab.test.tsx web/src/components/session/SessionBody.tsx web/src/components/SessionCard.tsx web/src/components/session/SessionMeta.tsx web/src/components/__tests__/SessionCard.meta.test.tsx
printf 'feat(web): native-display notice in the Browser tab and a desktop badge\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 10: Docs + full verification gate

**Files:**
- Modify: `docs/product/features/remote-desktop-vnc-x11.md` (new section + code links)
- Modify: `specs/002-session-x-display/quickstart.md:36-60` (config table + range note)

- [ ] **Step 1: Feature doc** — append to `remote-desktop-vnc-x11.md` (and add `server/sessions/native_display.py`, `server/sessions/display_policy.py` under `links.code`):

```markdown
## Native display routing (2026-09-06)

A session created from the runner host's **own desktop** does not get a virtual display:
its `DISPLAY` is the host's real X display (`CCSWITCH_NATIVE_DISPLAY`, default `:0`) and
"Open a browser" launches Chromium there. Every other client — any other tailnet device,
automations, the assistant — keeps the pooled Xvfb display and VNC relay.

**Detection.** The create route resolves the requesting browser's tailnet node with
`tailscale whois` (the same peer-verified path identity uses) and the daemon that spawns
the process compares it with its own `self_host_name()`. Native only when they match
*and* the X socket (`/tmp/.X11-unix/X<n>`) exists on that host. A local client also
*prefers* its own host for placement so the rule can hold across the fleet; an explicit
host choice still wins. The decision is made once at spawn, persisted as
`Session.displayMode`, and honoured on rehydrate. Attaching later from another device
does not move a session.

**Override.** `POST /api/sessions` accepts `display: "auto" | "native" | "virtual"`.

**Requirements / limits.** The daemon runs as the desktop user and the X server must
accept `SI:localuser:<user>` (GDM's default; otherwise `xhost +SI:localuser:<user>` in the
session startup). No `XAUTHORITY` is set. `CCSWITCH_NATIVE_DISPLAY` must not fall inside
the Xvfb pool range or native routing disables itself with a warning (the orphan sweep
reaps that range). Behind `tailscale serve` (no `X-Real-IP`) locality resolves to unknown →
virtual. Kill switch: `CCSWITCH_NATIVE_DISPLAY_ENABLED=false`.
```

- [ ] **Step 2: Quickstart** — in the config block add after `CCSWITCH_VNC_IDLE_TIMEOUT_S=1800`:
```
# Route sessions created from this host's own desktop to its real X display
CCSWITCH_NATIVE_DISPLAY_ENABLED=true
CCSWITCH_NATIVE_DISPLAY=:0
```
and extend the range-conflict note to read: "ensure `CCSWITCH_REAUTH_DISPLAY` and `CCSWITCH_NATIVE_DISPLAY` (both default `:0`) do not fall within …".

- [ ] **Step 3: Full gate (server)** — run all four steps, tee'd:

```bash
cd /home/tony/GitHub/.ccswitch/worktrees/ccswitch/b1eacaf3
for v in $(env | grep -o '^CCSWITCH_[A-Z0-9_]*'); do unset "$v"; done
V=/home/tony/GitHub/ccswitch/.venv/bin
$V/ruff check src/ server/ 2>&1 | tee /tmp/ndr-ruff.log
$V/mypy server/ --ignore-missing-imports 2>&1 | tee /tmp/ndr-mypy.log
$V/python -m pytest tests/ -q 2>&1 | tee /tmp/ndr-core.log
CCSWITCH_DEV=1 $V/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/ndr-server.log
```
Expected: ruff clean, mypy clean, both suites green (a lone `test_lifespan_shutdown_bounded_…` failure on this box is the known worktree-janitor artefact; a lone `GeneratorExit` is #1134 — re-run that file alone).

- [ ] **Step 4: Full gate (web)** — `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`; `rm -rf web/dist`.

- [ ] **Step 5: Commit docs**

```bash
git add docs/product/features/remote-desktop-vnc-x11.md specs/002-session-x-display/quickstart.md
printf 'docs: native display routing — feature doc and quickstart knobs\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

- [ ] **Step 6: PR** — check in-flight PRs touching the same files first (`gh api graphql …` filter on `identity.py|x11manager|orchestrator.py|api/sessions.py|BrowserTab`), then open non-draft, arm auto-merge:

```bash
cat > /tmp/ndr-pr-body.md <<'EOF'
Sessions created from the runner host's own desktop now render on its native X display; every other client keeps the pooled Xvfb display + VNC relay.

- Locality: `tailscale whois` node of the requesting browser vs the spawning daemon's `self_host_name()`; native only when they match and the X socket exists. Verified against the real binary and dobby's X seat.
- Decided once at spawn, persisted as `Session.display_mode`, honoured on rehydrate; `display: auto|native|virtual` override on create.
- Local clients prefer their own host for placement (explicit host still wins); old daemons get a retry without the new kwargs.
- UI: Browser tab shows a desktop notice for native sessions (no VNC), "Open a browser" launches on the desktop; desktop badge on card + meta strip.

Spec: docs/superpowers/specs/2026-09-06-native-display-routing-design.md
Plan: docs/superpowers/plans/2026-09-06-native-display-routing.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
gh pr create --title "feat(sessions): route native vs virtual display by client locality" --body-file /tmp/ndr-pr-body.md
gh pr merge --squash --auto
```
