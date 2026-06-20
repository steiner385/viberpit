# Account Re-authenticator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-click in-app "Re-authenticate" button that re-logs a `needs-reauth` account by driving `claude auth login` in a fresh Chrome on the host whose profile has the user's Google cookies but NOT Claude's (so login is one-click and picks the right account), capturing and storing the new credentials.

**Architecture:** Reuse Claude's real OAuth — intercept its `BROWSER` env to launch a temp Chrome profile that is a copy of the user's real cookie store with the `claude.ai`/`anthropic` cookie rows deleted. A `ReauthManager` runs one reauth at a time with crash-safe cleanup of the cookie copy; REST endpoints + a polled UI button drive it.

**Tech Stack:** Python 3.12, FastAPI, sqlite3 (stdlib), `claude` CLI; React + TanStack Query; Google Chrome 149 on the host (`DISPLAY=:0`).

**Spec:** `docs/superpowers/specs/2026-06-20-account-reauthenticator-design.md` (read it, including the **Security hardening** section — those requirements are binding).

## Global Constraints (verbatim from spec)

- **All temp dirs (cookie-copy profile AND the reauth capture `CLAUDE_CONFIG_DIR`) live under `~/.config/ccswitch/reauth-tmp/`, mode `0700`, NEVER `/tmp`.** Create via `tempfile.mkdtemp(prefix="reauth-", dir=<reauth-tmp>)`; copied secret files `chmod 0600`.
- **Cookie strip query (verbatim):** `DELETE FROM cookies WHERE host_key LIKE '%claude.ai%' OR host_key LIKE '%anthropic%'`.
- **Cleanup guard:** only remove a path if `Path(p).resolve(strict=True).is_relative_to(<reauth-tmp>.resolve())`; accept only the path stored in `ReauthState`.
- **`validate_account_name(name)`:** regex `^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$`, reject `name in {".",".."}`, assert `paths.slot_path(name).resolve().is_relative_to(paths.store_dir().resolve())`; 404 on failure. Applied to ALL `/api/accounts/{name}/*` routes.
- **`DISPLAY`** from `Settings.reauth_display` (default `":0"`), read at startup — **no `display` request param**. **`chrome_profile`** from `Settings.chrome_profile` (default `~/.config/google-chrome/Default`). `claude_bin`/`CCSWITCH_CLAUDE_BIN` from env/config only; `subprocess` always `shell=False` with an argv list.
- **Launcher** shipped read-only in the package dir (`scripts/ccswitch-reauth-browser`); runner `os.path.realpath`s the `BROWSER=` path and refuses if it leaves the install dir; only `$1` (URL from claude) + `$CCSWITCH_REAUTH_PROFILE` (from runner) reach Chrome as separate argv; launcher validates `$CCSWITCH_REAUTH_PROFILE` is under `reauth-tmp`.
- **`ReauthManager.start` returns 503 if `crypto.key_available()` is False** (no plaintext token storage). `capture_login` MUST NOT log/audit the captured blob; audit rows carry `account` + `identity` ONLY (no `subscriptionType`).
- **One reauth at a time** → 409 (with holding identity + start time). States: `idle | running | cancelling | success | error | timeout`. **5-min timeout.** Cleanup in EVERY terminal path + a startup sweep of `reauth-tmp/*` older than 10 min + `SIGTERM`/`SIGINT` cleanup + the launcher writes its Chrome PID to `<temp>/launcher.pid` so timeout/cancel kills the real window.
- **Test commands:** `cd /mnt/ssk-ssd/tony/GitHub/cc-account-switcher && .venv/bin/python -m pytest server/tests/ -q` (server) and `.venv/bin/python -m unittest discover -s tests` (core); `cd web && npm run test && npm run typecheck && npm run build`. All stay green. No real `claude`, no real Chrome, no network in CI.
- Work on branch `feature/account-reauth` (off `master`). Conventional commits; commit per task.

---

## File structure

**Backend (new):**
- `scripts/ccswitch-reauth-browser` — the `BROWSER` launcher (executable shell).
- `src/ccswitch/auth_login.py` — `validate_account_name` is NOT here; this holds `reauth_tmp_root()`, `prepare_reauth_profile`, `cleanup_reauth_profile`, `capture_login`.
- `src/ccswitch/account_names.py` — `validate_account_name` (shared by core + API).
- `server/reauth.py` — `ReauthManager` + `ReauthState`.

**Backend (modified):**
- `src/ccswitch/accounts.py` — `add_account` calls `auth_login.capture_login`.
- `src/ccswitch/paths.py` — add `reauth_tmp_dir()`.
- `server/config.py` — add `reauth_display` (`":0"`), `chrome_profile` (`~/.config/google-chrome/Default`).
- `server/api/accounts.py` — replace `relogin` placeholder with `reauth` start/status/cancel; apply `validate_account_name` to all `{name}` routes.
- `server/app.py` — construct `ReauthManager` on `app.state`; startup sweep + SIGTERM cleanup hook.

**Frontend (modified):** `web/src/api/hooks.ts` (reauth hooks), `web/src/api/types.ts` (`ReauthState`), `web/src/components/Dashboard.tsx` (Re-authenticate button + status + cancel in the account row).

---

## Task 0: Feasibility gate (MANUAL, on the host — binary)

**Not TDD.** Confirms the cookie-copy approach works before building on it. Run on the host with `DISPLAY=:0`.

- [ ] **Step 1:** Create a throwaway temp profile and copy the cookie store + Local State:
```bash
T=$(mktemp -d ~/.config/ccswitch/reauth-tmp/gate-XXXX 2>/dev/null || mktemp -d)
mkdir -p "$T/Default"
cp ~/.config/google-chrome/Default/Cookies "$T/Default/Cookies"
cp ~/.config/google-chrome/"Local State" "$T/Local State"
sqlite3 "$T/Default/Cookies" "DELETE FROM cookies WHERE host_key LIKE '%claude.ai%' OR host_key LIKE '%anthropic%';"
```
- [ ] **Step 2:** Launch Chrome on it at claude.ai (and Google) and observe:
```bash
DISPLAY=:0 google-chrome --user-data-dir="$T" --no-first-run --no-default-browser-check --new-window "https://claude.ai" &
```
- [ ] **Step 3:** **Verdict.** Confirm (a) Google shows the user's accounts WITHOUT a password prompt (e.g. open accounts.google.com or the "Continue with Google" chooser shows accounts), and (b) claude.ai shows logged-OUT / the login screen. Record PASS/FAIL in `docs/superpowers/spikes/2026-06-20-reauth-cookie-copy.md`. Then `rm -rf "$T"`.

**Gate:** PASS → proceed with the cookie-copy plan as written. FAIL (Google not logged in) → STOP and escalate; switch to the dedicated-profile fallback (re-amend the spec's hardening for that posture first).

- [ ] **Step 4: Commit** the spike verdict.
```bash
git add docs/superpowers/spikes/2026-06-20-reauth-cookie-copy.md
git commit -m "spike(reauth): cookie-copy keeps Google logged in + Claude logged out — <PASS/FAIL>"
```

---

## Task 1: `validate_account_name` (shared, security-critical)

**Files:** Create `src/ccswitch/account_names.py`; Test `tests/test_account_names.py`.

**Interfaces:**
- Produces: `validate_account_name(name: str) -> bool` (True if safe). `InvalidAccountName(Exception)`. `assert_valid_account_name(name)` raises `InvalidAccountName` on failure.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_account_names.py
from ccswitch.account_names import validate_account_name, assert_valid_account_name, InvalidAccountName
import pytest

@pytest.mark.parametrize("name", ["kindash", "primary", "a", "A1._-x", "x"*64])
def test_valid_names(name):
    assert validate_account_name(name) is True

@pytest.mark.parametrize("name", ["", ".", "..", "...", "../x", "a/b", "a\\b", ".hidden",
                                   "name with space", "x"*65, "-leading-dot-not", "a;b", "a$b"])
def test_invalid_names(name):
    assert validate_account_name(name) is False

def test_assert_raises():
    with pytest.raises(InvalidAccountName):
        assert_valid_account_name("../evil")
    assert_valid_account_name("kindash")  # no raise
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `src/ccswitch/account_names.py`**
```python
from __future__ import annotations
import re
from . import paths

_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")

class InvalidAccountName(Exception):
    pass

def validate_account_name(name: str) -> bool:
    if not isinstance(name, str) or name in (".", ".."):
        return False
    if not _RE.match(name):
        return False
    try:
        slot = paths.slot_path(name).resolve()
        return slot.is_relative_to(paths.store_dir().resolve())
    except (OSError, ValueError):
        return False

def assert_valid_account_name(name: str) -> None:
    if not validate_account_name(name):
        raise InvalidAccountName(name)
```
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(accounts): central validate_account_name guard`.

---

## Task 2: `reauth_tmp_dir` + `prepare_reauth_profile` + `cleanup_reauth_profile`

**Files:** Modify `src/ccswitch/paths.py`; Create `src/ccswitch/auth_login.py`; Test `tests/test_reauth_profile.py`.

**Interfaces:**
- Produces (paths.py): `reauth_tmp_dir() -> Path` = `root()/"reauth-tmp"`.
- Produces (auth_login.py): `prepare_reauth_profile(source_profile_dir: str) -> str` (returns the temp profile dir; copies `<source>/Cookies` → `<temp>/Default/Cookies` 0600, `<source-parent>/Local State` → `<temp>/Local State` 0600, strips Claude rows; raises `FileNotFoundError` if the source Cookies DB is missing). `cleanup_reauth_profile(path: str) -> None` (guarded rmtree). `_under_reauth_tmp(path) -> bool`.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_reauth_profile.py
import os, sqlite3, stat
from pathlib import Path
import pytest
from ccswitch import auth_login, paths

def _make_source(tmp_path):
    src = tmp_path / "google-chrome" / "Default"; src.mkdir(parents=True)
    (tmp_path / "google-chrome" / "Local State").write_text('{"os_crypt":{"encrypted_key":"x"}}')
    db = src / "Cookies"
    c = sqlite3.connect(db); c.execute("CREATE TABLE cookies(host_key TEXT, name TEXT)")
    c.executemany("INSERT INTO cookies VALUES(?,?)",
                  [(".google.com","SID"),("accounts.google.com","X"),
                   (".claude.ai","sess"),("console.anthropic.com","tok")])
    c.commit(); c.close()
    return str(src)

def test_prepare_strips_claude_keeps_google(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    src = _make_source(tmp_path)
    temp = auth_login.prepare_reauth_profile(src)
    assert auth_login._under_reauth_tmp(temp)
    # 0700 dir
    assert stat.S_IMODE(os.stat(temp).st_mode) == 0o700
    ck = Path(temp) / "Default" / "Cookies"
    rows = sqlite3.connect(ck).execute("SELECT host_key FROM cookies").fetchall()
    hosts = {r[0] for r in rows}
    assert ".google.com" in hosts and "accounts.google.com" in hosts
    assert not any("claude" in h or "anthropic" in h for h in hosts)
    assert (Path(temp) / "Local State").exists()
    assert stat.S_IMODE(os.stat(ck).st_mode) == 0o600
    auth_login.cleanup_reauth_profile(temp)
    assert not os.path.exists(temp)

def test_prepare_missing_source_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    with pytest.raises(FileNotFoundError):
        auth_login.prepare_reauth_profile(str(tmp_path / "nope"))

def test_cleanup_refuses_outside_reauth_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    victim = tmp_path / "victim"; victim.mkdir()
    auth_login.cleanup_reauth_profile(str(victim))   # must be a no-op, NOT delete
    assert victim.exists()
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Add to `paths.py`:
```python
def reauth_tmp_dir() -> Path:
    return root() / "reauth-tmp"
```
Create `src/ccswitch/auth_login.py` (this task's portion):
```python
from __future__ import annotations
import os, sqlite3, shutil, tempfile
from pathlib import Path
from . import paths

_STRIP_SQL = "DELETE FROM cookies WHERE host_key LIKE '%claude.ai%' OR host_key LIKE '%anthropic%'"

def _reauth_root() -> Path:
    r = paths.reauth_tmp_dir()
    r.mkdir(parents=True, exist_ok=True)
    os.chmod(r, 0o700)
    return r

def _under_reauth_tmp(path: str) -> bool:
    try:
        return Path(path).resolve(strict=True).is_relative_to(_reauth_root().resolve())
    except (OSError, ValueError):
        return False

def prepare_reauth_profile(source_profile_dir: str) -> str:
    src = Path(source_profile_dir)
    src_cookies = src / "Cookies"
    if not src_cookies.exists():
        raise FileNotFoundError(f"chrome profile Cookies not found: {src_cookies}")
    temp = tempfile.mkdtemp(prefix="reauth-", dir=str(_reauth_root()))
    os.chmod(temp, 0o700)
    default = Path(temp) / "Default"; default.mkdir(); os.chmod(default, 0o700)
    dst_cookies = default / "Cookies"
    shutil.copy(src_cookies, dst_cookies); os.chmod(dst_cookies, 0o600)
    local_state = src.parent / "Local State"
    if local_state.exists():
        dst_ls = Path(temp) / "Local State"
        shutil.copy(local_state, dst_ls); os.chmod(dst_ls, 0o600)
    conn = sqlite3.connect(str(dst_cookies))
    try:
        conn.execute(_STRIP_SQL); conn.commit()
    finally:
        conn.close()
    return temp

def cleanup_reauth_profile(path: str) -> None:
    if _under_reauth_tmp(path):
        shutil.rmtree(path, ignore_errors=True)
```
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(reauth): prepare/cleanup temp cookie-copy profile (strip Claude, keep Google)`.

---

## Task 3: `BROWSER` launcher script

**Files:** Create `scripts/ccswitch-reauth-browser` (chmod +x); Test `tests/test_reauth_launcher.py`.

**Interfaces:** the script reads `$1` (url) + `$CCSWITCH_REAUTH_PROFILE`; resolves a chrome binary; validates the profile path prefix; `exec`s chrome. For the test, set `$CCSWITCH_REAUTH_BROWSER_BIN` to a fake "browser" that just echoes its args so we can assert without a real Chrome.

- [ ] **Step 1: Write the failing test**
```python
# tests/test_reauth_launcher.py
import os, subprocess, stat
from pathlib import Path
SCRIPT = Path(__file__).parent.parent / "scripts" / "ccswitch-reauth-browser"

def test_launcher_execs_browser_with_profile_and_url(tmp_path):
    fake = tmp_path / "fakebrowser"
    fake.write_text("#!/usr/bin/env bash\necho \"ARGS:$@\"\n"); fake.chmod(0o755)
    prof = tmp_path / ".config" / "ccswitch" / "reauth-tmp" / "reauth-x"; prof.mkdir(parents=True)
    env = dict(os.environ, CCSWITCH_REAUTH_PROFILE=str(prof),
               CCSWITCH_REAUTH_BROWSER_BIN=str(fake), HOME=str(tmp_path))
    out = subprocess.run([str(SCRIPT), "https://claude.ai/x"], env=env, capture_output=True, text=True)
    assert out.returncode == 0
    assert "--user-data-dir=" + str(prof) in out.stdout
    assert "https://claude.ai/x" in out.stdout

def test_launcher_rejects_profile_outside_reauth_tmp(tmp_path):
    fake = tmp_path / "fakebrowser"; fake.write_text("#!/usr/bin/env bash\necho ran\n"); fake.chmod(0o755)
    bad = tmp_path / "evil"; bad.mkdir()
    env = dict(os.environ, CCSWITCH_REAUTH_PROFILE=str(bad),
               CCSWITCH_REAUTH_BROWSER_BIN=str(fake), HOME=str(tmp_path))
    out = subprocess.run([str(SCRIPT), "https://x"], env=env, capture_output=True, text=True)
    assert out.returncode != 0
    assert "ran" not in out.stdout

def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)
```
- [ ] **Step 2: Run → FAIL** (script missing).
- [ ] **Step 3: Implement `scripts/ccswitch-reauth-browser`** (`chmod 755`):
```sh
#!/usr/bin/env bash
set -euo pipefail
url="${1:?url required}"
prof="${CCSWITCH_REAUTH_PROFILE:?CCSWITCH_REAUTH_PROFILE required}"
# the profile MUST be under the ccswitch reauth-tmp dir
case "$prof" in
  "$HOME/.config/ccswitch/reauth-tmp/"*) : ;;
  *) echo "ccswitch-reauth-browser: refusing profile outside reauth-tmp: $prof" >&2; exit 2 ;;
esac
bin="${CCSWITCH_REAUTH_BROWSER_BIN:-}"
if [ -z "$bin" ]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then bin="$c"; break; fi
  done
fi
[ -n "$bin" ] || { echo "ccswitch-reauth-browser: no supported browser found" >&2; exit 3; }
echo $$ > "$prof/launcher.pid" 2>/dev/null || true
exec "$bin" --user-data-dir="$prof" --no-first-run --no-default-browser-check --new-window "$url"
```
(Note: the test's fake echoes args, so `--user-data-dir=<prof>` and the url appear in stdout. The real `exec` replaces the process so `launcher.pid` becomes the Chrome PID.)
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(reauth): BROWSER launcher (fresh chrome on the prepared profile, path-guarded)`.

---

## Task 4: `capture_login` + `add_account` refactor + config

**Files:** Modify `src/ccswitch/auth_login.py` (add `capture_login`), `src/ccswitch/accounts.py` (call it), `server/config.py` (settings); Create `server/tests/fakes/fake_claude_login.py`; Test `tests/test_capture_login.py`.

**Interfaces:**
- Produces: `auth_login.capture_login(name: str, *, env_extra: dict | None = None, claude_bin: list[str] | None = None) -> bool` — runs `claude auth login --claudeai` with `CLAUDE_CONFIG_DIR` set to a capture dir created under `reauth_tmp_dir()` (NOT /tmp), merged env `{**os.environ, **(env_extra or {})}`, `shell=False`; on a captured `.credentials.json` stores via the existing slot/mcp/claude.json logic + `configdir.compose(name)`; returns True/False; NEVER logs the blob. Cleans its own capture dir.

- [ ] **Step 1: Write a fake claude** `server/tests/fakes/fake_claude_login.py` (a python script run via `CCSWITCH_CLAUDE_BIN`): writes a dummy `.credentials.json` into `$CLAUDE_CONFIG_DIR` and exits 0.
```python
#!/usr/bin/env python3
import os, json, sys
cfg = os.environ["CLAUDE_CONFIG_DIR"]
os.makedirs(cfg, exist_ok=True)
json.dump({"claudeAiOauth": {"accessToken": "fake-at", "refreshToken": "fake-rt",
          "expiresAt": 9999999999000, "subscriptionType": "max"}},
          open(os.path.join(cfg, ".credentials.json"), "w"))
sys.exit(0)
```
- [ ] **Step 2: Write the failing test**
```python
# tests/test_capture_login.py
import os, sys
from pathlib import Path
from ccswitch import auth_login, paths

FAKE = str(Path(__file__).parent.parent / "server" / "tests" / "fakes" / "fake_claude_login.py")

def test_capture_login_stores_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "dotclaude"))
    ok = auth_login.capture_login("primary", claude_bin=[sys.executable, FAKE])
    assert ok is True
    assert paths.slot_path("primary").exists()
    # capture dir was under reauth-tmp and is cleaned up
    leftovers = list(paths.reauth_tmp_dir().glob("*")) if paths.reauth_tmp_dir().exists() else []
    assert not any("capture" in p.name for p in leftovers)
```
- [ ] **Step 3: Run → FAIL.**
- [ ] **Step 4: Implement** `capture_login` in `auth_login.py` (extract the store body from `accounts.add_account`; reuse `creds.split_credentials`/`write_credentials`/`read_json`/`write_json_secure` and `configdir.compose`). Capture dir: `tempfile.mkdtemp(prefix="capture-", dir=str(_reauth_root()))`, `shell=False`, `try/finally: cleanup_reauth_profile(capture_dir)`. Refactor `accounts.add_account` to call `capture_login(name, env_extra=..., email=...)` (keep its current behavior for the CLI path; only the capture/store body moves). Add to `server/config.py` Settings: `reauth_display: str = ":0"` (`CCSWITCH_REAUTH_DISPLAY`), `chrome_profile: str = "~/.config/google-chrome/Default"` (`CCSWITCH_CHROME_PROFILE`).
- [ ] **Step 5: Run → PASS;** then full core+server suite green.
- [ ] **Step 6: Commit** `feat(reauth): capture_login (capture dir off /tmp) + add_account refactor + settings`.

---

## Task 5: `ReauthManager` state machine

**Files:** Create `server/reauth.py`; Test `server/tests/test_reauth_manager.py`.

**Interfaces:**
- Produces: `ReauthState` dataclass `{state: str, message: str|None, identity: str|None, started_at: float|None}` with `.to_dict()` (camelCase: `state`, `message`, `identity`, `startedAt`). `ReauthManager(runner=...)` where `runner(name) -> bool` is injectable (default = the real prepare+capture pipeline). Methods: `async start(name, identity) -> ReauthState` (raises `ReauthBusy` if another is running; raises `ReauthNoKey` if `crypto.key_available()` is False), `status(name) -> ReauthState`, `async cancel(name) -> ReauthState`, `sweep_stale()` (startup), `async shutdown()`. States `idle|running|cancelling|success|error|timeout`. 5-min timeout (injectable for tests).

- [ ] **Step 1: Write the failing test** (inject a fake runner + a key-available stub; use a short timeout)
```python
# server/tests/test_reauth_manager.py
import asyncio, time
from server.reauth import ReauthManager, ReauthBusy, ReauthNoKey

def test_success_flow():
    async def runner(name): await asyncio.sleep(0.01); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)
    async def run():
        st = await m.start("primary", "me"); assert st.state == "running"
        for _ in range(50):
            if m.status("primary").state in ("success","error","timeout"): break
            await asyncio.sleep(0.01)
        return m.status("primary").state
    assert asyncio.run(run()) == "success"

def test_busy_raises():
    async def runner(name): await asyncio.sleep(0.2); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=5)
    async def run():
        await m.start("primary", "me")
        try:
            await m.start("secondary", "me"); return "nobusy"
        except ReauthBusy: return "busy"
    assert asyncio.run(run()) == "busy"

def test_no_key_raises():
    async def runner(name): return True
    m = ReauthManager(runner=runner, key_available=lambda: False, timeout_s=5)
    import pytest
    with pytest.raises(ReauthNoKey):
        asyncio.run(m.start("primary", "me"))

def test_timeout_marks_timeout():
    async def runner(name): await asyncio.sleep(5); return True
    m = ReauthManager(runner=runner, key_available=lambda: True, timeout_s=0.05)
    async def run():
        await m.start("primary", "me")
        for _ in range(100):
            if m.status("primary").state in ("timeout","error","success"): break
            await asyncio.sleep(0.01)
        return m.status("primary").state
    assert asyncio.run(run()) == "timeout"
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement `server/reauth.py`** — an `asyncio.Lock`/single-task model: `start` checks `key_available()` (raise `ReauthNoKey`), checks no active task (raise `ReauthBusy`), records `ReauthState(state="running", identity, started_at=loop.time())`, launches `asyncio.create_task(self._run(name))` which `await asyncio.wait_for(runner(name), timeout_s)` and sets `success`/`error`/`timeout` accordingly (on `TimeoutError` → `timeout`; exception → `error`). `cancel` sets `cancelling`, cancels the task, kills any tracked launcher PID, marks `error`/`cancelled`→`idle`. The default production `runner` = prepare_reauth_profile(chrome_profile) → build env_extra (BROWSER=realpath(launcher), DISPLAY=reauth_display, CCSWITCH_REAUTH_PROFILE=temp) → `await asyncio.to_thread(auth_login.capture_login, name, env_extra=...)` → finally cleanup. `sweep_stale()` removes `reauth_tmp_dir()/*` older than 600s (use `os.stat().st_mtime`).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(reauth): ReauthManager state machine (one-at-a-time, timeout, no-key gate)`.

---

## Task 6: API endpoints + wiring + name-guard on all account routes

**Files:** Modify `server/api/accounts.py`, `server/app.py`; Test `server/tests/test_reauth_api.py`.

**Interfaces:** `POST /api/accounts/{name}/reauth` → `app.state.reauth.start` (202 + state; 409 `ReauthBusy`; 503 `ReauthNoKey`; 404 bad/unknown name). `GET /api/accounts/{name}/reauth` → state. `POST /api/accounts/{name}/reauth/cancel`. All use `assert_valid_account_name`. `app.py`: `app.state.reauth = ReauthManager()`; in lifespan startup call `reauth.sweep_stale()`; register a `SIGTERM`/`SIGINT` handler (or lifespan shutdown) that runs `reauth.shutdown()`. Remove the placeholder `relogin` route (or alias it to reauth). Audit: `account.reauth.start|success|cancel` with `{"account": name}` only.

- [ ] Steps: failing tests (inject a fake `ReauthManager` on `app.state` via the test fixture — mirror `server/tests/test_repos.py`/`test_settings.py` TestClient+identity pattern): `POST → 202` + state running; `GET → state`; `POST cancel`; **409** when busy; **503** when no key; **404** for `name="../x"` and unknown account; assert the audit row for a reauth carries no token/subscription fields → implement → green (full server suite) → commit `feat(api): reauth start/status/cancel + name-guard on all account routes`.

---

## Task 7: No-token-leakage test + startup sweep verification

**Files:** Create `server/tests/test_reauth_no_token_leakage.py`.

- [ ] Drive `capture_login` (fake claude writing `accessToken:"fake-at"`/`refreshToken:"fake-rt"`) through the manager/runner with a caplog fixture + an in-memory audit DB; assert neither `"fake-at"` nor `"fake-rt"` appears in any log record or any `audit_events.detail`. Assert `sweep_stale()` removes an aged dummy dir under `reauth_tmp_dir()` and leaves a fresh one. Commit `test(reauth): assert captured tokens never logged/audited + stale sweep`.

---

## Task 8: Frontend — Re-authenticate button + status polling

**Files:** Modify `web/src/api/types.ts` (`ReauthState`), `web/src/api/hooks.ts`, `web/src/components/Dashboard.tsx`; Tests under `web/src/components/__tests__/`.

**Interfaces:** `ReauthState { state: 'idle'|'running'|'cancelling'|'success'|'error'|'timeout'; message?: string|null; startedAt?: number|null }`. Hooks (mirror `useRefreshAccount`/`useReauthAccount` patterns, paths RELATIVE — `/accounts/${name}/reauth`, NOT `/api/...`): `useReauthAccount()` (POST), `useReauthStatus(name, enabled)` (GET, `refetchInterval: enabled ? 1500 : false`), `useCancelReauth()`.

- [ ] **Step 1: Write failing tests** — an `AccountRow` test: clicking "Re-authenticate" calls the POST hook and shows *"Opening a fresh Chrome on the host desktop :0 — sign in there…"*; while `running` a **Cancel** button shows and clicking it calls cancel + optimistically returns to idle; `success` invalidates accounts; `error`/`timeout` show `message`. (Mock the hooks like the existing `AccountsNickname.test.tsx`.)
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the hooks + add to the `AccountRow` (in `Dashboard.tsx`) a "Re-authenticate" button (emphasized when `account.status === 'needs-reauth'`/`'needs-relogin'`); on click start + poll via `useReauthStatus(name, enabled=started)`; render the status line (include the configured display from the status/message), a Cancel (optimistic), and on `success` `qc.invalidateQueries(QK.accounts)`. Copy MUST contain "on the host machine"/"host desktop".
- [ ] **Step 4: Run → PASS** (`npm run test && npm run typecheck && npm run build`).
- [ ] **Step 5: Commit** `feat(web): account re-authenticate button + status polling + optimistic cancel`.

---

## Task 9: Manual live smoke + finish

- [ ] **Step 1:** Full suites green (server, core, web test/typecheck/build).
- [ ] **Step 2: Manual smoke (host, real account that is `needs-reauth`):** click Re-authenticate → a fresh Chrome opens on the host with Google logged in + Claude logged out → sign in → confirm the account flips to `ok`, a real session can be created, and `~/.config/ccswitch/reauth-tmp/` is empty afterward (cleanup). Record in `docs/superpowers/SMOKE-reauth.md`.
- [ ] **Step 3: Commit** `docs: reauth live smoke checklist`.

---

## Self-review

**Spec coverage:** cookie-copy mechanism (Task 0 gate, Task 2) ✓; BROWSER intercept + launcher hardening (Task 3) ✓; capture off /tmp + add_account refactor (Task 4) ✓; ReauthManager one-at-a-time/timeout/cancelling/no-key-503 (Task 5) ✓; endpoints + central name-guard on ALL account routes (Task 6) ✓; never log/audit token + startup sweep (Tasks 5/7) ✓; button + polling + optimistic cancel + host-screen copy + DISPLAY surfaced (Task 8) ✓; SIGTERM cleanup + sweep wired (Task 6) ✓; dedicated-profile fallback gated (Task 0) ✓. The launcher-PID-kill on timeout is implemented in Task 3 (writes pid) + Task 5 (kills it).

**Placeholder scan:** Task 6 uses a prose "Steps:" rollup (exact files/interfaces/assertions named; mirrors an existing test fixture) — acceptable for endpoint wiring; the novel/security-critical logic (validate, prepare/cleanup, launcher, capture_login, manager) has complete code. No TBD/TODO.

**Type consistency:** `validate_account_name`/`assert_valid_account_name`, `prepare_reauth_profile`/`cleanup_reauth_profile`/`_under_reauth_tmp`, `capture_login(name, *, env_extra, claude_bin)`, `ReauthState`/`ReauthManager.start/status/cancel/sweep_stale`, and the wire `state` strings are consistent across backend tasks and mirrored by `web/src/api/types.ts` (Task 8). Settings `reauth_display`/`chrome_profile` consistent (Task 4 defines, Task 5 consumes).
