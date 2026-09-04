# Reauth Profile Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the account add/reauth Chrome open with the user's Google session intact and the 1Password extension available, instead of an effectively fresh browser.

**Architecture:** Two independent fixes in the existing cookie-copy flow. (1) `capture_login()` injects the user's D-Bus session bus address so the reauth Chrome can reach gnome-keyring and decrypt the copied `v11` cookies. (2) `prepare_reauth_profile()` additionally copies a curated extension set (default: 1Password) plus `Login Data` and writes a minimal `Preferences` that registers only those extensions. A new `CCSWITCH_REAUTH_EXTENSIONS` setting overrides the id list.

**Tech Stack:** Python 3.12 stdlib (`pathlib`, `shutil`, `json`, `socket` in tests), pytest, pydantic-settings (`server/config.py`), bash launcher unchanged.

**Spec:** `docs/superpowers/specs/2026-09-04-reauth-profile-fidelity-design.md`

## Global Constraints

- Only a missing source `Cookies` may raise from `prepare_reauth_profile`; every other missing piece is logged at INFO and skipped (spec D4).
- Never copy `Secure Preferences`, `Web Data`, or `NativeMessagingHosts/*` (spec D2/D3).
- The minimal `Preferences` must contain only `{"extensions": {"settings": {...}}}` — nothing that names a Google account (spec D2).
- Explicit `env_extra` values passed to `capture_login` always win over injected bus variables (spec D1).
- Default extension id list lives in ONE place: `ccswitch.auth_login.REAUTH_EXTENSION_IDS`. `server/config.py` stores `None` for "use the default"; it never repeats the id.
- Run core tests with the checkout on the path, because the shared venv imports `ccswitch.*` from the deploy worktree (see CLAUDE.md):
  `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/...`
  Confirm with `PYTHONPATH=$PWD/src .../python -c "import ccswitch.auth_login as m; print(m.__file__)"` before trusting a green run.
- Server tests: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_SOCKET -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/...`
- The full gate before pushing is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- Branch off `origin/master` (`git fetch && git checkout -b fix/reauth-profile-fidelity origin/master`), never local `master`.

---

## File map

| File | Responsibility after this plan |
|---|---|
| `src/ccswitch/auth_login.py` | `session_bus_env()` (new), `REAUTH_EXTENSION_IDS` (new), `_copy_extensions()` (new), `prepare_reauth_profile(..., extension_ids=None)` (widened), `capture_login()` (merges bus env) |
| `tests/test_session_bus_env.py` | Pure tests for `session_bus_env()` (new file) |
| `tests/test_capture_login.py` | Integration: the spawned `claude` sees the bus variables; explicit `env_extra` wins |
| `tests/test_reauth_profile.py` | Extension/Login Data copy, minimal Preferences, skip-on-missing, override list |
| `server/config.py` | `reauth_extension_ids` + `reauth_extension_id_items` on BOTH `Settings` classes |
| `server/tests/test_config_xvfb.py` | Setting default/override/empty parsing |
| `server/reauth.py` | Runner passes `extension_ids=settings.reauth_extension_id_items` |
| `server/tests/test_reauth_manager.py` | Runner forwards the configured id list |
| `specs/002-session-x-display/quickstart.md` | Document `CCSWITCH_REAUTH_EXTENSIONS` next to the other reauth knobs |

---

### Task 1: `session_bus_env()` helper

**Files:**
- Modify: `src/ccswitch/auth_login.py` (add after `_under_reauth_tmp`, before `prepare_reauth_profile`)
- Create: `tests/test_session_bus_env.py`

**Interfaces:**
- Produces: `session_bus_env(environ: Mapping[str, str] | None = None, *, uid: int | None = None, run_root: str = "/run/user") -> dict[str, str]`. Returns `{}` when no bus socket exists; otherwise the subset of `{"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}` that is not already present in `environ`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_session_bus_env.py
"""session_bus_env(): give a service-launched Chrome a route to gnome-keyring.

ccswitch.service has no DBUS_SESSION_BUS_ADDRESS, so Chrome cannot fetch the
"Chrome Safe Storage" key and silently drops every v11 cookie copied from the
real profile (spec 2026-09-04-reauth-profile-fidelity-design.md).
"""
import socket

from ccswitch.auth_login import session_bus_env


def _bus(tmp_path, uid=1000):
    runtime = tmp_path / str(uid)
    runtime.mkdir()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(runtime / "bus"))
    return runtime, s


def test_injects_bus_and_runtime_dir_when_socket_exists(tmp_path):
    runtime, s = _bus(tmp_path)
    try:
        out = session_bus_env({}, uid=1000, run_root=str(tmp_path))
    finally:
        s.close()
    assert out == {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        "XDG_RUNTIME_DIR": str(runtime),
    }


def test_empty_when_no_socket(tmp_path):
    assert session_bus_env({}, uid=1000, run_root=str(tmp_path)) == {}


def test_plain_file_named_bus_is_not_a_socket(tmp_path):
    runtime = tmp_path / "1000"
    runtime.mkdir()
    (runtime / "bus").write_text("")
    assert session_bus_env({}, uid=1000, run_root=str(tmp_path)) == {}


def test_does_not_override_existing_values(tmp_path):
    runtime, s = _bus(tmp_path)
    try:
        out = session_bus_env(
            {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/elsewhere", "XDG_RUNTIME_DIR": str(runtime)},
            uid=1000, run_root=str(tmp_path),
        )
    finally:
        s.close()
    assert out == {}


def test_honours_xdg_runtime_dir_from_environ(tmp_path):
    runtime, s = _bus(tmp_path, uid=4242)
    try:
        out = session_bus_env({"XDG_RUNTIME_DIR": str(runtime)}, uid=1000, run_root="/nonexistent")
    finally:
        s.close()
    assert out == {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_session_bus_env.py -v`
Expected: FAIL with `ImportError: cannot import name 'session_bus_env'`

- [ ] **Step 3: Implement the helper**

In `src/ccswitch/auth_login.py`, change the typing import to `from typing import Mapping, Sequence` and add after `_under_reauth_tmp`:

```python
def session_bus_env(
    environ: Mapping[str, str] | None = None,
    *,
    uid: int | None = None,
    run_root: str = "/run/user",
) -> dict[str, str]:
    """Env vars a keyring-backed Chrome needs when launched from a bus-less service.

    The reauth Chrome runs ``--password-store=gnome-libsecret`` so it can decrypt the
    v11 cookies copied from the real profile. systemd services do not inherit the
    user's session bus, so without these two variables Chrome cannot reach
    gnome-keyring and silently drops every keyring-encrypted cookie — Google looks
    logged out. Returns only the variables *environ* lacks; never overrides.
    """
    env = os.environ if environ is None else environ
    uid = os.getuid() if uid is None else uid
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"{run_root}/{uid}"
    bus = Path(runtime_dir) / "bus"
    if not bus.is_socket():
        return {}
    out: dict[str, str] = {}
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        out["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    if "XDG_RUNTIME_DIR" not in env:
        out["XDG_RUNTIME_DIR"] = runtime_dir
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_session_bus_env.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/auth_login.py tests/test_session_bus_env.py
git commit -m "feat(reauth): session_bus_env() locates the user's D-Bus session bus for keyring access"
```

---

### Task 2: `capture_login()` merges the bus env under `env_extra`

**Files:**
- Modify: `src/ccswitch/auth_login.py` — `capture_login`, the `env = {...}` line (currently line 140)
- Modify: `tests/test_capture_login.py`

**Interfaces:**
- Consumes: `session_bus_env()` from Task 1.
- Produces: no signature change. The `claude` subprocess env is `{**os.environ, **session_bus_env(), "CLAUDE_CONFIG_DIR": capture_dir, **env_extra}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_capture_login.py`:

```python
import json
import os
import socket


def _env_dumping_fake(tmp_path):
    """A fake `claude` that records its environment, then behaves like the real fake."""
    fake = tmp_path / "fake_claude_env.py"
    fake.write_text(
        "import json, os, sys\n"
        "cfg = os.environ['CLAUDE_CONFIG_DIR']\n"
        "os.makedirs(cfg, exist_ok=True)\n"
        "json.dump(dict(os.environ), open(os.path.join(cfg, '..', 'seen-env.json'), 'w'))\n"
        "json.dump({'claudeAiOauth': {'accessToken': 'a', 'refreshToken': 'r',\n"
        "           'expiresAt': 9999999999000, 'subscriptionType': 'max'}},\n"
        "          open(os.path.join(cfg, '.credentials.json'), 'w'))\n"
        "sys.exit(0)\n"
    )
    return fake


def _seen_env():
    return json.load(open(paths.reauth_tmp_dir() / "seen-env.json"))


def test_capture_login_injects_session_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(runtime / "bus"))
    try:
        ok = auth_login.capture_login("primary", claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))])
    finally:
        s.close()
    assert ok is True
    seen = _seen_env()
    assert seen["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'bus'}"
    assert seen["XDG_RUNTIME_DIR"] == str(runtime)


def test_capture_login_env_extra_beats_injected_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(runtime / "bus"))
    try:
        auth_login.capture_login(
            "primary",
            claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))],
            env_extra={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/explicit"},
        )
    finally:
        s.close()
    assert _seen_env()["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/explicit"


def test_capture_login_without_bus_leaves_env_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "no-bus-here"))
    auth_login.capture_login("primary", claude_bin=[sys.executable, str(_env_dumping_fake(tmp_path))])
    assert "DBUS_SESSION_BUS_ADDRESS" not in _seen_env()
```

Note: `seen-env.json` is written one level above the capture dir (into `reauth-tmp/`) because the capture dir is deleted in `capture_login`'s `finally`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_capture_login.py -v`
Expected: `test_capture_login_injects_session_bus` FAILS with `KeyError: 'DBUS_SESSION_BUS_ADDRESS'`; the other two pass already (they assert the absence/override cases, which hold today).

- [ ] **Step 3: Merge the bus env**

In `capture_login`, replace

```python
        env = {**os.environ, "CLAUDE_CONFIG_DIR": capture_dir, **(env_extra or {})}
```

with

```python
        # Service-launched (no session bus) → Chrome can't reach gnome-keyring and drops
        # every v11 cookie copied from the real profile. Inject the user's bus so the
        # copied Google session survives; explicit env_extra always wins.
        env = {
            **os.environ,
            **session_bus_env(),
            "CLAUDE_CONFIG_DIR": capture_dir,
            **(env_extra or {}),
        }
```

Also update the `env_extra` line in the docstring to read:
`env_extra: Additional environment variables to merge (e.g. BROWSER, DISPLAY). Applied last, so they override the injected session-bus variables.`

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_capture_login.py tests/test_session_bus_env.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/auth_login.py tests/test_capture_login.py
git commit -m "fix(reauth): give the login Chrome the user's session bus so copied Google cookies decrypt"
```

---

### Task 3: `prepare_reauth_profile()` copies the extension set, `Login Data`, and a minimal `Preferences`

**Files:**
- Modify: `src/ccswitch/auth_login.py` — add `REAUTH_EXTENSION_IDS`, `_copy_extensions()`, widen `prepare_reauth_profile` (currently lines 38–86)
- Modify: `tests/test_reauth_profile.py`

**Interfaces:**
- Produces:
  - `REAUTH_EXTENSION_IDS: tuple[str, ...] = ("aeblfdkhhhdcdjpifhhbdiojplfjncoa",)`
  - `prepare_reauth_profile(source_profile_dir: str, *, extension_ids: Sequence[str] | None = None) -> str` — `None` means `REAUTH_EXTENSION_IDS`; `()` disables extension copying.
- Consumed by Task 5 (runner passes `extension_ids=`).

- [ ] **Step 1: Write the failing tests**

Replace `_make_source` in `tests/test_reauth_profile.py` with a version that can also build the extension footprint, and add the new tests:

```python
import json

ONEP = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"


def _make_source(tmp_path, *, with_extensions=False):
    """Create a mock Chrome profile directory with a Cookies database.

    with_extensions=True adds the files a real Linux profile has for an installed
    extension (1Password id) plus a second registered-but-absent id, Login Data,
    Secure Preferences and a NativeMessagingHosts manifest that must NOT be copied.
    """
    src = tmp_path / "google-chrome" / "Default"
    src.mkdir(parents=True)
    (tmp_path / "google-chrome" / "Local State").write_text('{"os_crypt":{"encrypted_key":"x"}}')
    db = src / "Cookies"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE cookies(host_key TEXT, name TEXT)")
    c.executemany(
        "INSERT INTO cookies VALUES(?,?)",
        [
            (".google.com", "SID"),
            ("accounts.google.com", "X"),
            (".claude.ai", "sess"),
            (".claude.com", "sess2"),
            ("platform.claude.com", "p"),
            ("console.anthropic.com", "tok"),
        ],
    )
    c.commit()
    c.close()
    if with_extensions:
        ext = src / "Extensions" / ONEP / "8.12.34.34_0"
        ext.mkdir(parents=True)
        (ext / "manifest.json").write_text('{"name":"1Password","version":"8.12.34.34"}')
        les = src / "Local Extension Settings" / ONEP
        les.mkdir(parents=True)
        (les / "000003.ldb").write_bytes(b"ldb")
        state = src / "Extension State"
        state.mkdir()
        (state / "CURRENT").write_text("MANIFEST-000001")
        (src / "Login Data").write_bytes(b"sqlite-login-data")
        (src / "Secure Preferences").write_text('{"protection":{"super_mac":"x"}}')
        (src / "Preferences").write_text(json.dumps({
            "extensions": {"settings": {
                ONEP: {"location": 1, "from_webstore": True, "path": f"{ONEP}/8.12.34.34_0"},
                "notinstalledxxxxxxxxxxxxxxxxxxxx": {"location": 1},
            }},
            "signin": {"allowed": True},
            "account_info": [{"email": "me@example.com"}],
        }))
        nmh = tmp_path / "google-chrome" / "NativeMessagingHosts"
        nmh.mkdir()
        (nmh / "com.1password.1password.json").write_text("{}")
    return str(src)


def test_prepare_copies_registered_extension_and_login_data(tmp_path, monkeypatch):
    """Spec D2: copy Extensions/<id>, Local Extension Settings/<id>, Extension State,
    Login Data, and write a Preferences that registers ONLY the copied extension."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Extensions" / ONEP / "8.12.34.34_0" / "manifest.json").exists()
    assert (d / "Local Extension Settings" / ONEP / "000003.ldb").exists()
    assert (d / "Extension State" / "CURRENT").exists()
    assert (d / "Login Data").read_bytes() == b"sqlite-login-data"
    assert stat.S_IMODE(os.stat(d / "Login Data").st_mode) == 0o600
    prefs = json.loads((d / "Preferences").read_text())
    assert prefs == {"extensions": {"settings": {
        ONEP: {"location": 1, "from_webstore": True, "path": f"{ONEP}/8.12.34.34_0"},
    }}}
    # Must NOT travel: they either break Google (full Preferences) or are unwanted (D2/D3).
    assert not (d / "Secure Preferences").exists()
    assert not (Path(temp) / "NativeMessagingHosts").exists()
    assert stat.S_IMODE(os.stat(d / "Preferences").st_mode) == 0o600
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_without_extension_footprint_is_unchanged(tmp_path, monkeypatch):
    """Spec D4: a profile with no Extensions dir / Preferences still prepares fine and
    writes no Preferences at all (Chrome then starts as it does today)."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path)
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Cookies").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extensions").exists()
    assert not (d / "Login Data").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_extension_ids_empty_disables_copy(tmp_path, monkeypatch):
    """CCSWITCH_REAUTH_EXTENSIONS="" → extension_ids=() → nothing extension-related is
    copied, but Login Data still is (it is independent of the id list)."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    temp = auth_login.prepare_reauth_profile(src, extension_ids=())
    d = Path(temp) / "Default"
    assert not (d / "Extensions").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extension State").exists()
    assert (d / "Login Data").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_skips_id_registered_but_not_installed(tmp_path, monkeypatch):
    """An id in Preferences with no Extensions/<id> dir is skipped, never registered."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    temp = auth_login.prepare_reauth_profile(src, extension_ids=("notinstalledxxxxxxxxxxxxxxxxxxxx",))
    d = Path(temp) / "Default"
    assert not (d / "Extensions").exists()
    assert not (d / "Preferences").exists()
    auth_login.cleanup_reauth_profile(temp)


def test_prepare_unreadable_preferences_skips_extensions(tmp_path, monkeypatch):
    """Spec D4: a torn/invalid source Preferences must not fail the login."""
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path, with_extensions=True)
    (Path(src) / "Preferences").write_text("{not json")
    temp = auth_login.prepare_reauth_profile(src)
    d = Path(temp) / "Default"
    assert (d / "Cookies").exists()
    assert not (d / "Preferences").exists()
    assert not (d / "Extensions").exists()
    auth_login.cleanup_reauth_profile(temp)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_reauth_profile.py -v`
Expected: `test_prepare_copies_registered_extension_and_login_data` FAILS on the `Extensions` assert; `test_prepare_extension_ids_empty_disables_copy` and `test_prepare_skips_id_registered_but_not_installed` FAIL with `TypeError: ... unexpected keyword argument 'extension_ids'`; the other two pass.

- [ ] **Step 3: Implement**

In `src/ccswitch/auth_login.py` add `import json` to the imports, and add the constant directly under `_STRIP_SQL`:

```python
# Extensions carried into the temp login profile. 1Password by default: with its
# files + state copied, the user unlocks with the master password inside the reauth
# window instead of typing credentials by hand. Override: CCSWITCH_REAUTH_EXTENSIONS.
REAUTH_EXTENSION_IDS: tuple[str, ...] = ("aeblfdkhhhdcdjpifhhbdiojplfjncoa",)
```

Add this helper above `prepare_reauth_profile`:

```python
def _copy_extensions(src: Path, default: Path, extension_ids: Sequence[str]) -> None:
    """Copy the curated extension set and register it in a MINIMAL Preferences.

    Copies, per id that exists both on disk (Extensions/<id>) and in the source
    Preferences' extensions.settings: Extensions/<id> and Local Extension Settings/<id>.
    Once: Extension State/. Then writes Default/Preferences containing ONLY
    {"extensions": {"settings": {<id>: <entry>}}}.

    Why minimal: copying the full Preferences names the signed-in Google account whose
    refresh token lives in the (uncopied) Web Data, and Chrome's account reconciler then
    logs the Gaia cookies out — undoing the whole point of the cookie copy.
    Anything missing is skipped at INFO; this must never fail the login (spec D4).
    """
    if not extension_ids:
        return
    prefs_path = src / "Preferences"
    try:
        registered = json.loads(prefs_path.read_text()).get("extensions", {}).get("settings", {})
    except (OSError, ValueError) as e:
        log.info("reauth profile: source Preferences unreadable (%s); skipping extensions", e)
        return
    settings: dict[str, object] = {}
    for ext_id in extension_ids:
        ext_dir = src / "Extensions" / ext_id
        entry = registered.get(ext_id)
        if not ext_dir.is_dir() or entry is None:
            log.info("reauth profile: extension %s not installed in source; skipping", ext_id)
            continue
        shutil.copytree(ext_dir, default / "Extensions" / ext_id)
        les = src / "Local Extension Settings" / ext_id
        if les.is_dir():
            shutil.copytree(les, default / "Local Extension Settings" / ext_id)
        settings[ext_id] = entry
    if not settings:
        return
    state = src / "Extension State"
    if state.is_dir():
        shutil.copytree(state, default / "Extension State")
    dst_prefs = default / "Preferences"
    dst_prefs.write_text(json.dumps({"extensions": {"settings": settings}}))
    os.chmod(dst_prefs, 0o600)
```

Change the `prepare_reauth_profile` signature and docstring to:

```python
def prepare_reauth_profile(
    source_profile_dir: str,
    *,
    extension_ids: Sequence[str] | None = None,
) -> str:
    """Copy a Chrome profile's login state to a temp location and strip Claude cookies.

    Copies Cookies + Local State (always), Login Data (if present) and the curated
    extension set (see _copy_extensions). Google stays signed in, Claude is signed out,
    1Password is available.

    Args:
        source_profile_dir: Path to a Chrome profile directory (e.g. ~/.config/google-chrome/Default)
        extension_ids: Extension ids to carry over. None → REAUTH_EXTENSION_IDS; () → none.

    Returns:
        Path to the temp profile directory (under reauth-tmp)

    Raises:
        FileNotFoundError: If the source Cookies database is missing
    """
```

and insert, after the `_STRIP_SQL` execute/commit block and before `return temp`:

```python
    # Saved passwords (v11-encrypted like Cookies; same keyring, same trust class).
    login_data = src / "Login Data"
    if login_data.exists():
        dst_ld = default / "Login Data"
        shutil.copy(login_data, dst_ld)
        os.chmod(dst_ld, 0o600)
    else:
        log.info("reauth profile: no Login Data in source; skipping")

    _copy_extensions(
        src, default, REAUTH_EXTENSION_IDS if extension_ids is None else extension_ids
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_reauth_profile.py -v`
Expected: 9 passed (4 existing + 5 new)

- [ ] **Step 5: Lint and commit**

Run: `ruff check src/ccswitch/auth_login.py tests/test_reauth_profile.py`
Expected: no findings

```bash
git add src/ccswitch/auth_login.py tests/test_reauth_profile.py
git commit -m "feat(reauth): carry 1Password, Login Data and a minimal Preferences into the login profile"
```

---

### Task 4: `CCSWITCH_REAUTH_EXTENSIONS` setting

**Files:**
- Modify: `server/config.py` — pydantic `Settings` (after `chrome_profile`, ~line 73) and its property block (~line 149); dataclass fallback `Settings` (after `chrome_profile`, ~line 229) and its property block (~line 309)
- Modify: `server/tests/test_config_xvfb.py`

**Interfaces:**
- Produces: `Settings.reauth_extension_ids: str | None` (raw env, alias `CCSWITCH_REAUTH_EXTENSIONS`) and `Settings.reauth_extension_id_items -> list[str] | None` — `None` when the variable is unset (use the core default), `[]` when set to an empty/whitespace string (disable), otherwise the trimmed non-empty comma items.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_config_xvfb.py`:

```python
def test_reauth_extensions_unset_means_core_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_REAUTH_EXTENSIONS", raising=False)
    s = _fresh()
    assert s.reauth_extension_ids is None
    assert s.reauth_extension_id_items is None


def test_reauth_extensions_empty_disables(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAUTH_EXTENSIONS", "  ")
    s = _fresh()
    assert s.reauth_extension_id_items == []


def test_reauth_extensions_comma_list(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAUTH_EXTENSIONS", "aaaa, bbbb ,,cccc")
    s = _fresh()
    assert s.reauth_extension_id_items == ["aaaa", "bbbb", "cccc"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_SOCKET -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_config_xvfb.py -v`
Expected: 3 new tests FAIL with `AttributeError: 'Settings' object has no attribute 'reauth_extension_ids'`

- [ ] **Step 3: Add the setting to BOTH Settings classes**

Pydantic class, directly after the `chrome_profile` field:

```python
        # Extension ids carried into the temp login profile (comma list). Unset → the
        # core default (1Password, ccswitch.auth_login.REAUTH_EXTENSION_IDS); set but
        # empty → carry none. Spec: 2026-09-04-reauth-profile-fidelity-design.md D2.
        reauth_extension_ids: str | None = Field(default=None, alias="CCSWITCH_REAUTH_EXTENSIONS")
```

Pydantic class, next to `tailnet_allowlist_items`:

```python
        @property
        def reauth_extension_id_items(self) -> list[str] | None:
            """None = use the core default list; [] = disabled; else the configured ids."""
            if self.reauth_extension_ids is None:
                return None
            return [i.strip() for i in self.reauth_extension_ids.split(",") if i.strip()]
```

Dataclass fallback, directly after its `chrome_profile` field:

```python
        reauth_extension_ids: str | None = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_REAUTH_EXTENSIONS")
        )
```

Dataclass fallback, next to its `tailnet_allowlist_items`:

```python
        @property
        def reauth_extension_id_items(self) -> list[str] | None:
            if self.reauth_extension_ids is None:
                return None
            return [i.strip() for i in self.reauth_extension_ids.split(",") if i.strip()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: same command as Step 2
Expected: all passed (existing xvfb/reauth tests + 3 new)

- [ ] **Step 5: Commit**

```bash
git add server/config.py server/tests/test_config_xvfb.py
git commit -m "feat(config): CCSWITCH_REAUTH_EXTENSIONS overrides the extensions copied into the login profile"
```

---

### Task 5: Runner forwards the configured id list

**Files:**
- Modify: `server/reauth.py:171` (`temp = auth_login.prepare_reauth_profile(chrome_profile)`)
- Modify: `server/tests/test_reauth_manager.py` (add next to `test_runner_falls_back_to_host_display_without_x11`, ~line 124)

**Interfaces:**
- Consumes: `prepare_reauth_profile(source, *, extension_ids=...)` (Task 3), `Settings.reauth_extension_id_items` (Task 4).

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_runner_forwards_configured_extension_ids(monkeypatch):
    """CCSWITCH_REAUTH_EXTENSIONS reaches prepare_reauth_profile(extension_ids=...);
    unset → None so the core default (1Password) applies."""
    seen = []

    def fake_prepare(profile, *, extension_ids=None):
        seen.append(extension_ids)
        return "/tmp/reauth-tmp-z"

    monkeypatch.setattr("ccswitch.auth_login.prepare_reauth_profile", fake_prepare)
    monkeypatch.setattr("ccswitch.auth_login.capture_login", lambda name, env_extra=None: True)
    monkeypatch.setattr("ccswitch.auth_login.cleanup_reauth_profile", lambda t: None)
    monkeypatch.setattr("ccswitch.accounts.mark_reauthenticated", lambda n: None)

    monkeypatch.setenv("CCSWITCH_REAUTH_EXTENSIONS", "aaaa,bbbb")
    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert seen == [["aaaa", "bbbb"]]

    monkeypatch.delenv("CCSWITCH_REAUTH_EXTENSIONS")
    mgr = ReauthManager(x11=None, key_available=lambda: True, timeout_s=30)
    await mgr.start("primary", "me@example.com")
    await asyncio.sleep(0.05)
    assert seen[-1] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_SOCKET -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_reauth_manager.py::test_runner_forwards_configured_extension_ids -v`
Expected: FAIL — `assert seen == [["aaaa", "bbbb"]]` sees `[None]` because the runner never passes the kwarg.

- [ ] **Step 3: Forward the setting**

In `server/reauth.py` replace

```python
            temp = auth_login.prepare_reauth_profile(chrome_profile)
```

with

```python
            temp = auth_login.prepare_reauth_profile(
                chrome_profile, extension_ids=settings.reauth_extension_id_items
            )
```

and update the runner docstring line `prepare_reauth_profile(chrome_profile)` to `prepare_reauth_profile(chrome_profile, extension_ids=settings.reauth_extension_id_items)`.

- [ ] **Step 4: Run the reauth manager suite**

Run: same env as Step 2, `-m pytest server/tests/test_reauth_manager.py server/tests/test_reauth_no_token_leakage.py -v`
Expected: all passed — after widening the six existing one-positional stubs in `server/tests/test_reauth_manager.py`, which now receive the `extension_ids` kwarg and would `TypeError`. Change each `lambda p: "/tmp/reauth-tmp-<x>"` to `lambda p, **kw: "/tmp/reauth-tmp-<x>"` at lines 95 (`-x`), 115 (`-y`), 614 (`-mode`), 742 (`-p`), 764 (`-q`), 782 (`-r`). No other file stubs it (`grep -rn "prepare_reauth_profile" server/tests tests` confirms).

- [ ] **Step 5: Commit**

```bash
git add server/reauth.py server/tests/test_reauth_manager.py
git commit -m "feat(reauth): runner passes CCSWITCH_REAUTH_EXTENSIONS through to the login profile copy"
```

---

### Task 6: Document the knob and run the full gate

**Files:**
- Modify: `specs/002-session-x-display/quickstart.md` — the reauth note near line 58 (the `CCSWITCH_REAUTH_DISPLAY` display-range warning)

- [ ] **Step 1: Add the documentation**

Directly after the `> **Display range conflict:** …` blockquote in `specs/002-session-x-display/quickstart.md`, add:

```markdown
> **Login profile fidelity:** the reauth Chrome is a temp copy of `CCSWITCH_CHROME_PROFILE`
> with the Claude cookies stripped, so Google stays signed in and 1Password is available.
> Two things make that work from a systemd service: `capture_login` injects the user's
> `DBUS_SESSION_BUS_ADDRESS` (found at `$XDG_RUNTIME_DIR/bus`, default `/run/user/<uid>/bus`)
> so Chrome can reach gnome-keyring, and `CCSWITCH_REAUTH_EXTENSIONS` (comma-separated
> extension ids; default = 1Password `aeblfdkhhhdcdjpifhhbdiojplfjncoa`; set to an empty
> string to carry none) selects which extensions are copied. If Google shows a password
> prompt anyway, check that `/run/user/<uid>/bus` exists for the service user
> (`loginctl enable-linger <user>` keeps the user session bus alive without a desktop login).
```

- [ ] **Step 2: Run the full four-step gate**

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q
env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_SOCKET -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_DAEMON_ROLE CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -q 2>&1 | tee /tmp/reauth-fidelity-server-tests.log
```

Expected: ruff clean, mypy clean, both suites green. Run the server suite in the background or serialized (it takes ~20 min and concurrent runs get SIGKILLed; see memory "Server suite timeout herd").

- [ ] **Step 3: Commit and open the PR**

```bash
git add specs/002-session-x-display/quickstart.md
git commit -m "docs(reauth): document CCSWITCH_REAUTH_EXTENSIONS and the session-bus requirement"
git push -u origin fix/reauth-profile-fidelity
```

PR title: `fix(reauth): login Chrome keeps the Google session and carries 1Password`. Body: link the spec, paste the probe table from the spec's "Root cause" section, list the two behavioural changes, and note the D3 live-run assumption (1Password standalone unlock) as the one thing to confirm after deploy. Then `gh pr ready` if needed and `gh pr merge <#> --squash --auto`.

- [ ] **Step 4: Live verification after deploy (not a code step)**

On the Accounts page, add or reauth an account. Expected: the Google account chooser lists the user's accounts with no password prompt; the 1Password extension is present (puzzle-piece menu) and its unlock screen asks only for the master password. If it instead demands the desktop app, file a follow-up to make the `NativeMessagingHosts` manifest copy opt-in (spec D3).

---

## Self-review

- **Spec coverage:** D1 → Tasks 1–2. D2 → Tasks 3–5. D3 → Task 3 (asserted: no `NativeMessagingHosts`, no `Secure Preferences`) and Task 6 step 4. D4 → Task 3 tests for missing footprint / unreadable Preferences / not-installed id. Security posture → 0600 on `Preferences` and `Login Data`, everything under the existing 0700 temp root; no new files elsewhere. Verification section → Task 6.
- **Placeholders:** none; every step carries code or an exact command.
- **Type consistency:** `session_bus_env(environ, *, uid, run_root)` in Tasks 1–2; `prepare_reauth_profile(source, *, extension_ids: Sequence[str] | None)` in Tasks 3 and 5; `Settings.reauth_extension_id_items -> list[str] | None` in Tasks 4 and 5; `REAUTH_EXTENSION_IDS` referenced from Task 3 only, per the single-source constraint.
