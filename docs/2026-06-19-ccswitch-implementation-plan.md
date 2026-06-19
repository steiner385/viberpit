# ccswitch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A zero-dependency Python tool that rotates between multiple Claude Max accounts automatically — selecting the account with the most usage headroom (read exactly from `/api/oauth/usage`) for each `claude` invocation, and switching mid-session when limits are hit — so work continues without manual intervention.

**Architecture:** Each account is its own `CLAUDE_CONFIG_DIR` (a directory of symlinks mirroring `~/.claude`, plus a real `.credentials.json` composed from the account's `claudeAiOauth` and a shared `mcpOAuth`). Selection is per-process via that env var, so concurrent sessions never contend. A flock-guarded `ledger.json` is a usage cache + soft-reservation store. Two thin clients consume a shared core library: `ccrun` (headless wrapper, retry-on-next-account) and `ccmax` (interactive launcher with a sidecar that polls usage and tails the session transcript, then kill+`--resume` on the next account).

**Tech Stack:** Python 3.11+, **standard library only** at runtime and in tests (`json`, `urllib`, `fcntl`, `subprocess`, `pathlib`, `argparse`, `datetime`, `zoneinfo`, `threading`, `unittest`, `unittest.mock`).

## Global Constraints

- **Zero third-party dependencies** — runtime and tests use only the Python stdlib. Tests use `unittest`, run via `python -m unittest discover -s tests`.
- **Never log token values.** Store dir `~/.claude-accounts` is mode `0700`; all credential files are mode `0600`; `ledger.json` holds no secrets.
- **Per-process account selection only** — never mutate the shared `~/.claude/.credentials.json` in place. An account == a `CLAUDE_CONFIG_DIR`.
- **All reset times displayed to the user are rendered in `America/New_York`.** Internally, all timestamps are timezone-aware UTC `datetime`s.
- **Exact endpoints (verified live against CLI v2.1.183):**
  - Usage: `GET https://api.anthropic.com/api/oauth/usage`, headers `Authorization: Bearer <accessToken>` and `anthropic-beta: oauth-2025-04-20`.
  - Refresh: `POST https://platform.claude.com/v1/oauth/token`, form body `grant_type=refresh_token&refresh_token=<rt>&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e`.
- **Test isolation via env overrides:** `CCSWITCH_HOME` overrides the store root; `CLAUDE_CONFIG_DIR` overrides the shared base dir; `CCSWITCH_CLAUDE_BIN` overrides the `claude` executable. Tests set all three to temp dirs.
- **Config defaults:** `soft_threshold=0.90`, `hard_ceiling=1.00`, `usage_cache_ttl_sec=60`, `poll_interval_sec=60`, `reservation_ttl_sec=120`, `expiry_skew_sec=300`, `timezone="America/New_York"`, `claude_bin="claude"`.

### Shared interface map (locked signatures — match these exactly across tasks)

```
paths.py     root() -> Path; store_dir() -> Path; cfg_root() -> Path; ledger_path() -> Path;
             config_path() -> Path; slot_path(name) -> Path; shared_mcp_path() -> Path;
             account_cfg_dir(name) -> Path; shared_base() -> Path
config.py    @dataclass Config(...); load_config() -> Config
creds.py     read_json(path) -> dict; write_json_secure(path, data) -> None;
             split_credentials(full: dict) -> tuple[dict, dict];      # (claudeAiOauth, mcpOAuth)
             compose_credentials(claude_oauth: dict, mcp_oauth: dict) -> dict;
             merge_keep_later(existing: dict | None, incoming: dict) -> dict
oauth.py     USAGE_URL, TOKEN_URL, CLIENT_ID, BETA_HEADER (constants);
             http_request(method, url, headers=None, data=None, timeout=15) -> tuple[int, bytes];
             @dataclass Usage(session_pct: float, session_resets_at: datetime,
                              weekly_pct: float, weekly_resets_at: datetime,
                              extra_usage_enabled: bool);
             parse_usage(payload: dict) -> Usage;
             fetch_usage(access_token: str) -> Usage;                  # raises AuthError on 401
             refresh_access_token(refresh_token: str, prior: dict) -> dict;  # new claudeAiOauth
             class AuthError(Exception); class UsageError(Exception)
ledger.py    locked_ledger() -> contextmanager yielding dict;
             record_usage(led, name, usage: Usage, now: datetime) -> None;
             set_status(led, name, status: str) -> None;
             reserve(led, name, until_ts: float) -> None;
             reservation_active(led, name, now_ts: float) -> bool;
             cached_usage(led, name, cfg, now: datetime) -> Usage | None
selector.py  @dataclass Selection(name: str);
             class AllAccountsBusy(Exception) with .earliest_reset: datetime | None;
             select_best(usages: dict[str, Usage | None], reserved: set[str],
                         cfg: Config, now: datetime, exclude: set[str] = frozenset()) -> str
configdir.py reconcile_links(name) -> None; compose(name) -> Path
detect.py    @dataclass ResetInfo(resets_at: datetime | None, scope: str | None);
             detect_limit_in_result(result: dict) -> ResetInfo | None;
             detect_limit_in_jsonl_line(line: dict) -> ResetInfo | None
accounts.py  ensure_valid_token(name) -> dict; usage_for(name) -> Usage | None;
             pick_account(cfg, exclude: set[str] = frozenset()) -> str;
             add_account(name) -> None; remove_account(name) -> None;
             list_accounts() -> list[dict]; sync_back(name) -> None
runner.py    main(argv: list[str] | None = None) -> int
launcher.py  main(argv: list[str] | None = None) -> int
```

---

## File structure

```
~/GitHub/cc-account-switcher/
  pyproject.toml                     # console scripts: ccswitch, ccmax, ccrun
  src/ccswitch/
    __init__.py
    paths.py        # filesystem locations (env-overridable)
    config.py       # Config dataclass + load_config
    creds.py        # credential read/write/split/merge (pure)
    oauth.py        # usage fetch + token refresh + parsing (http)
    ledger.py       # flock-guarded JSON cache + reservations
    selector.py     # select_best (pure ranking)
    configdir.py    # compose per-account config dir (symlinks + composed creds)
    detect.py       # parse limit signals from result JSON + transcript JSONL
    accounts.py     # registry + integration glue (add/remove/list/pick/sync_back)
    cli.py          # `ccswitch` argparse entry
    runner.py       # `ccrun` headless wrapper
    launcher.py     # `ccmax` interactive launcher + sidecar monitor
  tests/
    __init__.py
    helpers.py      # temp-env fixtures, fake Usage builders
    fake_claude.py  # stub `claude` executable for runner/launcher tests
    test_*.py       # one per module
```

---

## Task 1: Project scaffold — paths, config, package install

**Files:**
- Create: `pyproject.toml`, `src/ccswitch/__init__.py`, `src/ccswitch/paths.py`, `src/ccswitch/config.py`
- Create: `tests/__init__.py`, `tests/helpers.py`, `tests/test_paths.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `paths.root/store_dir/cfg_root/ledger_path/config_path/slot_path/shared_mcp_path/account_cfg_dir/shared_base`; `config.Config`, `config.load_config`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ccswitch"
version = "0.1.0"
description = "Automatic Claude Max account rotation"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
ccswitch = "ccswitch.cli:main"
ccrun = "ccswitch.runner:main"
ccmax = "ccswitch.launcher:main"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 2: Write `tests/helpers.py`** (shared temp-env context manager)

```python
import contextlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


@contextlib.contextmanager
def temp_env():
    """Redirect store root + shared base to throwaway temp dirs."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        home = d / "accounts"
        shared = d / "dot-claude"
        shared.mkdir(parents=True)
        (shared / "settings.json").write_text("{}")
        (shared / "projects").mkdir()
        old = {k: os.environ.get(k) for k in ("CCSWITCH_HOME", "CLAUDE_CONFIG_DIR", "CCSWITCH_CLAUDE_BIN")}
        os.environ["CCSWITCH_HOME"] = str(home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(shared)
        try:
            yield d
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def usage(session=10.0, weekly=20.0, extra=False, mins=120):
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(minutes=mins)
    return Usage(session, reset, weekly, reset, extra)
```

- [ ] **Step 3: Write the failing tests** `tests/test_paths.py` and `tests/test_config.py`

```python
# tests/test_paths.py
import os
import unittest
from pathlib import Path
from tests.helpers import temp_env


class TestPaths(unittest.TestCase):
    def test_root_honours_env(self):
        with temp_env():
            from ccswitch import paths
            self.assertEqual(paths.root(), Path(os.environ["CCSWITCH_HOME"]))
            self.assertEqual(paths.slot_path("alpha"), paths.store_dir() / "alpha.cred.json")
            self.assertEqual(paths.account_cfg_dir("alpha"), paths.cfg_root() / "alpha")
            self.assertEqual(paths.shared_base(), Path(os.environ["CLAUDE_CONFIG_DIR"]))
```

```python
# tests/test_config.py
import json
import unittest
from tests.helpers import temp_env


class TestConfig(unittest.TestCase):
    def test_defaults_when_no_file(self):
        with temp_env():
            from ccswitch.config import load_config
            cfg = load_config()
            self.assertEqual(cfg.soft_threshold, 0.90)
            self.assertEqual(cfg.hard_ceiling, 1.00)
            self.assertEqual(cfg.timezone, "America/New_York")

    def test_file_overrides_defaults(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.config import load_config
            paths.root().mkdir(parents=True, exist_ok=True)
            paths.config_path().write_text(json.dumps({"soft_threshold": 0.75, "claude_bin": "/usr/bin/claude"}))
            cfg = load_config()
            self.assertEqual(cfg.soft_threshold, 0.75)
            self.assertEqual(cfg.claude_bin, "/usr/bin/claude")
            self.assertEqual(cfg.hard_ceiling, 1.00)  # untouched default
```

- [ ] **Step 4: Run tests, verify they fail**

Run: `python -m unittest discover -s tests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.paths'` (and `ccswitch.config`).

- [ ] **Step 5: Implement `src/ccswitch/paths.py`**

```python
"""Filesystem locations. All env-overridable for testing/isolation."""
import os
from pathlib import Path


def root() -> Path:
    return Path(os.environ.get("CCSWITCH_HOME", str(Path.home() / ".claude-accounts")))


def shared_base() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))


def store_dir() -> Path:
    return root() / "store"


def cfg_root() -> Path:
    return root() / "cfg"


def ledger_path() -> Path:
    return root() / "ledger.json"


def config_path() -> Path:
    return root() / "config.json"


def slot_path(name: str) -> Path:
    return store_dir() / f"{name}.cred.json"


def shared_mcp_path() -> Path:
    return store_dir() / "mcp.shared.json"


def account_cfg_dir(name: str) -> Path:
    return cfg_root() / name
```

- [ ] **Step 6: Implement `src/ccswitch/config.py`**

```python
"""Tool configuration with sane defaults, overridable via config.json."""
import json
from dataclasses import dataclass, fields

from . import paths


@dataclass
class Config:
    soft_threshold: float = 0.90
    hard_ceiling: float = 1.00
    usage_cache_ttl_sec: int = 60
    poll_interval_sec: int = 60
    reservation_ttl_sec: int = 120
    expiry_skew_sec: int = 300
    timezone: str = "America/New_York"
    claude_bin: str = "claude"


def load_config() -> Config:
    cfg = Config()
    path = paths.config_path()
    if path.exists():
        data = json.loads(path.read_text())
        valid = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in valid:
                setattr(cfg, k, v)
    return cfg
```

- [ ] **Step 7: Create empty `src/ccswitch/__init__.py` and `tests/__init__.py`, run tests**

Run: `python -m unittest discover -s tests -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/ccswitch/__init__.py src/ccswitch/paths.py src/ccswitch/config.py tests/
git commit -m "feat: project scaffold — paths + config"
```

---

## Task 2: Credential read/write/split/merge (pure)

**Files:**
- Create: `src/ccswitch/creds.py`, `tests/test_creds.py`

**Interfaces:**
- Produces: `read_json`, `write_json_secure` (mode 0600), `split_credentials`, `compose_credentials`, `merge_keep_later`.

- [ ] **Step 1: Write failing tests** `tests/test_creds.py`

```python
import json
import os
import stat
import unittest
from tests.helpers import temp_env


class TestCreds(unittest.TestCase):
    def test_split_and_compose_roundtrip(self):
        from ccswitch.creds import split_credentials, compose_credentials
        full = {"mcpOAuth": {"srv": {"x": 1}}, "claudeAiOauth": {"accessToken": "a", "expiresAt": 5}}
        claude_oauth, mcp = split_credentials(full)
        self.assertEqual(claude_oauth["accessToken"], "a")
        self.assertEqual(mcp, {"srv": {"x": 1}})
        composed = compose_credentials(claude_oauth, mcp)
        self.assertEqual(composed["claudeAiOauth"]["accessToken"], "a")
        self.assertEqual(composed["mcpOAuth"], {"srv": {"x": 1}})

    def test_split_missing_keys_defaults_empty(self):
        from ccswitch.creds import split_credentials
        claude_oauth, mcp = split_credentials({})
        self.assertEqual(claude_oauth, {})
        self.assertEqual(mcp, {})

    def test_merge_keep_later_prefers_later_expiry(self):
        from ccswitch.creds import merge_keep_later
        old = {"accessToken": "old", "expiresAt": 100}
        new = {"accessToken": "new", "expiresAt": 200}
        self.assertEqual(merge_keep_later(old, new)["accessToken"], "new")
        self.assertEqual(merge_keep_later(new, old)["accessToken"], "new")
        self.assertEqual(merge_keep_later(None, new)["accessToken"], "new")

    def test_write_json_secure_is_0600(self):
        with temp_env():
            from ccswitch import paths
            from ccswitch.creds import write_json_secure, read_json
            paths.store_dir().mkdir(parents=True)
            p = paths.slot_path("alpha")
            write_json_secure(p, {"k": "v"})
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(read_json(p), {"k": "v"})
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_creds -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.creds'`.

- [ ] **Step 3: Implement `src/ccswitch/creds.py`**

```python
"""Credential file I/O and the claudeAiOauth/mcpOAuth split."""
import json
import os
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def write_json_secure(path: Path, data: dict) -> None:
    """Write JSON with mode 0600 (parent dir forced to 0700)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def split_credentials(full: dict) -> tuple[dict, dict]:
    """Return (claudeAiOauth, mcpOAuth) from a full .credentials.json dict."""
    return dict(full.get("claudeAiOauth", {})), dict(full.get("mcpOAuth", {}))


def compose_credentials(claude_oauth: dict, mcp_oauth: dict) -> dict:
    """Build a .credentials.json dict from the two parts."""
    out: dict = {}
    if mcp_oauth:
        out["mcpOAuth"] = mcp_oauth
    if claude_oauth:
        out["claudeAiOauth"] = claude_oauth
    return out


def merge_keep_later(existing: dict | None, incoming: dict) -> dict:
    """Keep whichever claudeAiOauth blob has the later expiresAt (refresh is monotonic)."""
    if not existing:
        return incoming
    if existing.get("expiresAt", 0) >= incoming.get("expiresAt", 0):
        return existing
    return incoming
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_creds -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/creds.py tests/test_creds.py
git commit -m "feat: credential split/merge + secure JSON I/O"
```

---

## Task 3: OAuth — usage fetch, parse, token refresh

**Files:**
- Create: `src/ccswitch/oauth.py`, `tests/test_oauth.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Usage`, `parse_usage`, `fetch_usage`, `refresh_access_token`, `http_request`, `AuthError`, `UsageError`, constants `USAGE_URL/TOKEN_URL/CLIENT_ID/BETA_HEADER`.

- [ ] **Step 1: Write failing tests** `tests/test_oauth.py` (monkeypatch `http_request`)

```python
import json
import unittest
from datetime import datetime, timezone
from unittest import mock


USAGE_PAYLOAD = {
    "five_hour": {"utilization": 11.0, "resets_at": "2026-06-19T18:00:00.6+00:00"},
    "seven_day": {"utilization": 69.0, "resets_at": "2026-06-22T20:00:00.6+00:00"},
    "extra_usage": {"is_enabled": False},
    "limits": [
        {"kind": "session", "group": "session", "percent": 11, "resets_at": "2026-06-19T18:00:00.6+00:00", "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 69, "resets_at": "2026-06-22T20:00:00.6+00:00", "is_active": True},
    ],
}


class TestOAuth(unittest.TestCase):
    def test_parse_usage_prefers_limits_array(self):
        from ccswitch.oauth import parse_usage
        u = parse_usage(USAGE_PAYLOAD)
        self.assertEqual(u.session_pct, 11.0)
        self.assertEqual(u.weekly_pct, 69.0)
        self.assertFalse(u.extra_usage_enabled)
        self.assertEqual(u.session_resets_at.tzinfo, timezone.utc)

    def test_parse_usage_falls_back_to_objects(self):
        from ccswitch.oauth import parse_usage
        payload = {k: v for k, v in USAGE_PAYLOAD.items() if k != "limits"}
        u = parse_usage(payload)
        self.assertEqual(u.session_pct, 11.0)
        self.assertEqual(u.weekly_pct, 69.0)

    def test_fetch_usage_ok(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(200, json.dumps(USAGE_PAYLOAD).encode())):
            u = oauth.fetch_usage("tok")
            self.assertEqual(u.session_pct, 11.0)

    def test_fetch_usage_401_raises_autherror(self):
        from ccswitch import oauth
        with mock.patch.object(oauth, "http_request", return_value=(401, b"{}")):
            with self.assertRaises(oauth.AuthError):
                oauth.fetch_usage("tok")

    def test_refresh_maps_response_to_claudeaioauth(self):
        from ccswitch import oauth
        resp = {"access_token": "newA", "refresh_token": "newR", "expires_in": 3600, "scope": "user:inference user:profile"}
        with mock.patch.object(oauth, "http_request", return_value=(200, json.dumps(resp).encode())):
            prior = {"subscriptionType": "max", "rateLimitTier": "default_claude_max"}
            blob = oauth.refresh_access_token("oldR", prior)
            self.assertEqual(blob["accessToken"], "newA")
            self.assertEqual(blob["refreshToken"], "newR")
            self.assertEqual(blob["subscriptionType"], "max")  # carried from prior
            self.assertGreater(blob["expiresAt"], 0)
            self.assertEqual(blob["scopes"], ["user:inference", "user:profile"])
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_oauth -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.oauth'`.

- [ ] **Step 3: Implement `src/ccswitch/oauth.py`**

```python
"""Anthropic OAuth: exact usage + independent token refresh (stdlib urllib)."""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
BETA_HEADER = "oauth-2025-04-20"


class AuthError(Exception):
    """Token rejected (401) — needs refresh or re-auth."""


class UsageError(Exception):
    """Usage endpoint returned an unexpected status."""


@dataclass
class Usage:
    session_pct: float
    session_resets_at: datetime
    weekly_pct: float
    weekly_resets_at: datetime
    extra_usage_enabled: bool


def http_request(method: str, url: str, headers: dict | None = None,
                 data: bytes | None = None, timeout: int = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_usage(payload: dict) -> Usage:
    session_pct = weekly_pct = None
    session_reset = weekly_reset = None
    for entry in payload.get("limits", []) or []:
        group = entry.get("group")
        if group == "session" and session_pct is None:
            session_pct = float(entry.get("percent", 0))
            session_reset = _parse_iso(entry["resets_at"])
        elif group == "weekly" and weekly_pct is None:
            weekly_pct = float(entry.get("percent", 0))
            weekly_reset = _parse_iso(entry["resets_at"])
    if session_pct is None:
        fh = payload.get("five_hour", {})
        session_pct = float(fh.get("utilization", 0))
        session_reset = _parse_iso(fh["resets_at"])
    if weekly_pct is None:
        sd = payload.get("seven_day", {})
        weekly_pct = float(sd.get("utilization", 0))
        weekly_reset = _parse_iso(sd["resets_at"])
    extra = bool((payload.get("extra_usage") or {}).get("is_enabled", False))
    return Usage(session_pct, session_reset, weekly_pct, weekly_reset, extra)


def fetch_usage(access_token: str) -> Usage:
    status, body = http_request(
        "GET", USAGE_URL,
        headers={"Authorization": f"Bearer {access_token}", "anthropic-beta": BETA_HEADER},
    )
    if status == 401:
        raise AuthError("usage endpoint returned 401")
    if status != 200:
        raise UsageError(f"usage endpoint returned {status}")
    return parse_usage(json.loads(body))


def refresh_access_token(refresh_token: str, prior: dict) -> dict:
    form = f"grant_type=refresh_token&refresh_token={refresh_token}&client_id={CLIENT_ID}"
    status, body = http_request(
        "POST", TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=form.encode("utf-8"),
    )
    if status != 200:
        raise AuthError(f"token refresh failed with {status}")
    resp = json.loads(body)
    now_ms = int(time.time() * 1000)
    blob = dict(prior)  # carry subscriptionType / rateLimitTier
    blob["accessToken"] = resp["access_token"]
    blob["refreshToken"] = resp.get("refresh_token", refresh_token)
    blob["expiresAt"] = now_ms + int(resp.get("expires_in", 0)) * 1000
    if "scope" in resp:
        blob["scopes"] = resp["scope"].split()
    return blob
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_oauth -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/oauth.py tests/test_oauth.py
git commit -m "feat: oauth usage fetch + parse + token refresh"
```

---

## Task 4: Ledger — flock-guarded cache + reservations

**Files:**
- Create: `src/ccswitch/ledger.py`, `tests/test_ledger.py`

**Interfaces:**
- Consumes: `oauth.Usage`, `config.Config`, `paths.ledger_path`.
- Produces: `locked_ledger`, `record_usage`, `set_status`, `reserve`, `reservation_active`, `cached_usage`.

- [ ] **Step 1: Write failing tests** `tests/test_ledger.py`

```python
import time
import unittest
from datetime import datetime, timezone
from tests.helpers import temp_env, usage


class TestLedger(unittest.TestCase):
    def test_record_and_read_back_usage(self):
        with temp_env():
            from ccswitch import ledger
            from ccswitch.config import load_config
            cfg = load_config()
            now = datetime.now(timezone.utc)
            with ledger.locked_ledger() as led:
                ledger.record_usage(led, "alpha", usage(session=42.0), now)
            with ledger.locked_ledger() as led:
                got = ledger.cached_usage(led, "alpha", cfg, now)
            self.assertEqual(got.session_pct, 42.0)

    def test_cache_expires_after_ttl(self):
        with temp_env():
            from ccswitch import ledger
            from ccswitch.config import load_config
            cfg = load_config()
            now = datetime.now(timezone.utc)
            with ledger.locked_ledger() as led:
                ledger.record_usage(led, "alpha", usage(), now)
            from datetime import timedelta
            later = now + timedelta(seconds=cfg.usage_cache_ttl_sec + 1)
            with ledger.locked_ledger() as led:
                self.assertIsNone(ledger.cached_usage(led, "alpha", cfg, later))

    def test_reservation_lifecycle(self):
        with temp_env():
            from ccswitch import ledger
            now = time.time()
            with ledger.locked_ledger() as led:
                ledger.reserve(led, "alpha", now + 100)
                self.assertTrue(ledger.reservation_active(led, "alpha", now))
                self.assertFalse(ledger.reservation_active(led, "alpha", now + 200))
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_ledger -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.ledger'`.

- [ ] **Step 3: Implement `src/ccswitch/ledger.py`**

```python
"""Flock-guarded JSON state: a usage cache + soft reservations. No secrets."""
import contextlib
import fcntl
import json
from datetime import datetime, timezone

from . import paths
from .oauth import Usage


@contextlib.contextmanager
def locked_ledger():
    """Open ledger.json under an exclusive flock; persist the dict on exit."""
    path = paths.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        data = json.loads(raw) if raw else {"accounts": {}}
        data.setdefault("accounts", {})
        yield data
        f.seek(0)
        f.truncate()
        f.write(json.dumps(data, indent=2))
        f.flush()
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _acct(led: dict, name: str) -> dict:
    return led["accounts"].setdefault(name, {})


def record_usage(led: dict, name: str, u: Usage, now: datetime) -> None:
    _acct(led, name)["usage"] = {
        "session_pct": u.session_pct,
        "session_resets_at": u.session_resets_at.isoformat(),
        "weekly_pct": u.weekly_pct,
        "weekly_resets_at": u.weekly_resets_at.isoformat(),
        "extra_usage_enabled": u.extra_usage_enabled,
        "fetched_at": now.isoformat(),
    }


def cached_usage(led: dict, name: str, cfg, now: datetime) -> Usage | None:
    blob = _acct(led, name).get("usage")
    if not blob:
        return None
    fetched = datetime.fromisoformat(blob["fetched_at"])
    if (now - fetched).total_seconds() > cfg.usage_cache_ttl_sec:
        return None
    return Usage(
        blob["session_pct"], datetime.fromisoformat(blob["session_resets_at"]),
        blob["weekly_pct"], datetime.fromisoformat(blob["weekly_resets_at"]),
        blob["extra_usage_enabled"],
    )


def set_status(led: dict, name: str, status: str) -> None:
    _acct(led, name)["status"] = status


def reserve(led: dict, name: str, until_ts: float) -> None:
    _acct(led, name)["reservationUntil"] = until_ts


def reservation_active(led: dict, name: str, now_ts: float) -> bool:
    return _acct(led, name).get("reservationUntil", 0) > now_ts
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_ledger -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/ledger.py tests/test_ledger.py
git commit -m "feat: flock-guarded ledger cache + reservations"
```

---

## Task 5: Selector — pure ranking with ceiling exclusion

**Files:**
- Create: `src/ccswitch/selector.py`, `tests/test_selector.py`

**Interfaces:**
- Consumes: `oauth.Usage`, `config.Config`.
- Produces: `Selection`, `AllAccountsBusy` (with `.earliest_reset`), `select_best`.

- [ ] **Step 1: Write failing tests** `tests/test_selector.py`

```python
import unittest
from datetime import datetime, timezone
from tests.helpers import temp_env, usage


class TestSelector(unittest.TestCase):
    def setUp(self):
        from ccswitch.config import load_config
        self.cfg = load_config.__wrapped__() if hasattr(load_config, "__wrapped__") else load_config()
        self.now = datetime.now(timezone.utc)

    def test_picks_lowest_session_utilization(self):
        from ccswitch.selector import select_best
        usages = {"alpha": usage(session=80.0), "beta": usage(session=10.0), "gamma": usage(session=40.0)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "beta")

    def test_weekly_breaks_ties(self):
        from ccswitch.selector import select_best
        usages = {"alpha": usage(session=10.0, weekly=90.0), "beta": usage(session=10.0, weekly=20.0)}
        self.assertEqual(select_best(usages, set(), self.cfg, self.now), "beta")

    def test_excludes_at_ceiling_and_reserved_and_reauth(self):
        from ccswitch.selector import select_best
        usages = {
            "alpha": usage(session=100.0),       # at session ceiling
            "beta": usage(session=10.0, weekly=100.0),  # at weekly ceiling
            "gamma": None,                       # needs-reauth
            "delta": usage(session=5.0),         # reserved by another
            "echo": usage(session=50.0),         # the only valid pick
        }
        self.assertEqual(select_best(usages, {"delta"}, self.cfg, self.now), "echo")

    def test_all_busy_raises_with_earliest_reset(self):
        from ccswitch.selector import select_best, AllAccountsBusy
        usages = {"alpha": usage(session=100.0, mins=60), "beta": usage(session=100.0, mins=30)}
        with self.assertRaises(AllAccountsBusy) as ctx:
            select_best(usages, set(), self.cfg, self.now)
        self.assertIsNotNone(ctx.exception.earliest_reset)
```

Replace the brittle `setUp` line with a clean one in your copy:
```python
    def setUp(self):
        with temp_env():
            from ccswitch.config import load_config
            self.cfg = load_config()
        self.now = datetime.now(timezone.utc)
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_selector -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.selector'`.

- [ ] **Step 3: Implement `src/ccswitch/selector.py`**

```python
"""Pure account selection: exclude accounts at/above ceiling, rank by headroom."""
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .oauth import Usage


@dataclass
class Selection:
    name: str


class AllAccountsBusy(Exception):
    def __init__(self, earliest_reset: datetime | None):
        super().__init__("all accounts at/above usage ceiling")
        self.earliest_reset = earliest_reset


def select_best(usages: dict[str, Usage | None], reserved: set[str],
                cfg: Config, now: datetime, exclude: set[str] = frozenset()) -> str:
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, str]] = []
    busy_resets: list[datetime] = []
    for name, u in usages.items():
        if name in exclude or name in reserved or u is None:
            continue
        at_ceiling = u.session_pct >= ceiling or u.weekly_pct >= ceiling
        if at_ceiling:
            busy_resets.append(min(u.session_resets_at, u.weekly_resets_at))
            continue
        candidates.append((u.session_pct, u.weekly_pct, name))
    if not candidates:
        raise AllAccountsBusy(min(busy_resets) if busy_resets else None)
    candidates.sort()  # lowest session_pct, then weekly_pct, then name
    return candidates[0][2]
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_selector -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/selector.py tests/test_selector.py
git commit -m "feat: pure select_best ranking"
```

---

## Task 6: Config-dir composition (symlink mirror + composed creds)

**Files:**
- Create: `src/ccswitch/configdir.py`, `tests/test_configdir.py`

**Interfaces:**
- Consumes: `paths.*`, `creds.read_json/write_json_secure/compose_credentials`.
- Produces: `reconcile_links(name)`, `compose(name) -> Path`.

- [ ] **Step 1: Write failing tests** `tests/test_configdir.py`

```python
import json
import unittest
from tests.helpers import temp_env


class TestConfigDir(unittest.TestCase):
    def _seed_store(self):
        from ccswitch import paths
        from ccswitch.creds import write_json_secure
        write_json_secure(paths.slot_path("alpha"), {"accessToken": "A", "expiresAt": 9})
        write_json_secure(paths.shared_mcp_path(), {"srv": {"k": 1}})

    def test_compose_creates_symlinks_and_credentials(self):
        with temp_env():
            from ccswitch import paths, configdir
            self._seed_store()
            cfg_dir = configdir.compose("alpha")
            # settings.json + projects mirrored from shared base as symlinks
            self.assertTrue((cfg_dir / "settings.json").is_symlink())
            self.assertTrue((cfg_dir / "projects").is_symlink())
            self.assertEqual((cfg_dir / "projects").resolve(), (paths.shared_base() / "projects").resolve())
            # .credentials.json is a real composed file
            self.assertFalse((cfg_dir / ".credentials.json").is_symlink())
            data = json.loads((cfg_dir / ".credentials.json").read_text())
            self.assertEqual(data["claudeAiOauth"]["accessToken"], "A")
            self.assertEqual(data["mcpOAuth"], {"srv": {"k": 1}})

    def test_reconcile_is_idempotent(self):
        with temp_env():
            from ccswitch import configdir
            self._seed_store()
            configdir.compose("alpha")
            configdir.reconcile_links("alpha")  # second run must not raise
            configdir.reconcile_links("alpha")
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_configdir -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.configdir'`.

- [ ] **Step 3: Implement `src/ccswitch/configdir.py`**

```python
"""Compose a per-account CLAUDE_CONFIG_DIR: symlink mirror of ~/.claude + real creds."""
import os
from pathlib import Path

from . import paths
from .creds import compose_credentials, read_json, write_json_secure

CREDENTIALS_NAME = ".credentials.json"


def reconcile_links(name: str) -> None:
    """Idempotently symlink every shared-base entry (except .credentials.json) into cfg/<name>."""
    cfg_dir = paths.account_cfg_dir(name)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg_dir, 0o700)
    base = paths.shared_base()
    for entry in base.iterdir():
        if entry.name == CREDENTIALS_NAME:
            continue
        link = cfg_dir / entry.name
        target = entry
        if link.is_symlink():
            if Path(os.readlink(link)) == target:
                continue
            link.unlink()
        elif link.exists():
            # a real file/dir shadowing the shared entry — remove so the symlink wins
            if link.is_dir() and not link.is_symlink():
                import shutil
                shutil.rmtree(link)
            else:
                link.unlink()
        link.symlink_to(target)


def compose(name: str) -> Path:
    """Reconcile links and write the composed .credentials.json. Returns the config dir."""
    reconcile_links(name)
    cfg_dir = paths.account_cfg_dir(name)
    claude_oauth = read_json(paths.slot_path(name))
    mcp = read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
    write_json_secure(cfg_dir / CREDENTIALS_NAME, compose_credentials(claude_oauth, mcp))
    return cfg_dir
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_configdir -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/configdir.py tests/test_configdir.py
git commit -m "feat: per-account config-dir composition"
```

---

## Task 7: Limit detection (result JSON + transcript JSONL)

**Files:**
- Create: `src/ccswitch/detect.py`, `tests/test_detect.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ResetInfo`, `detect_limit_in_result`, `detect_limit_in_jsonl_line`.

> **Calibration note:** the limit-signal shapes below encode the verified markers (HTTP 429, the literal `resets_at`, and the phrase "usage limit"/"rate limit"). They are intentionally tolerant — matching on a serialized scan plus known fields — so they survive minor envelope drift. During Task 11 manual smoke, capture one real limited response and confirm a fixture matches; adjust the fixtures (not the tolerant matcher) if the wording differs.

- [ ] **Step 1: Write failing tests** `tests/test_detect.py`

```python
import unittest
from datetime import timezone


class TestDetect(unittest.TestCase):
    def test_result_success_returns_none(self):
        from ccswitch.detect import detect_limit_in_result
        self.assertIsNone(detect_limit_in_result({"type": "result", "is_error": False, "result": "hi"}))

    def test_result_usage_limit_detected_with_reset(self):
        from ccswitch.detect import detect_limit_in_result
        r = detect_limit_in_result({
            "type": "result", "is_error": True,
            "error": {"message": "Claude usage limit reached", "resets_at": "2026-06-19T18:00:00+00:00"},
        })
        self.assertIsNotNone(r)
        self.assertEqual(r.resets_at.tzinfo, timezone.utc)

    def test_result_429_without_reset(self):
        from ccswitch.detect import detect_limit_in_result
        r = detect_limit_in_result({"type": "result", "is_error": True, "error": {"status": 429}})
        self.assertIsNotNone(r)
        self.assertIsNone(r.resets_at)

    def test_jsonl_rate_limit_line(self):
        from ccswitch.detect import detect_limit_in_jsonl_line
        r = detect_limit_in_jsonl_line({"type": "system", "subtype": "rate_limit", "resets_at": "2026-06-19T18:00:00+00:00"})
        self.assertIsNotNone(r)

    def test_jsonl_normal_line_none(self):
        from ccswitch.detect import detect_limit_in_jsonl_line
        self.assertIsNone(detect_limit_in_jsonl_line({"type": "assistant", "message": {"content": "ok"}}))
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_detect -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.detect'`.

- [ ] **Step 3: Implement `src/ccswitch/detect.py`**

```python
"""Detect a usage/rate limit from headless result JSON or a transcript JSONL line."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

_LIMIT_RE = re.compile(r"usage limit|rate[ _]?limit|\b429\b", re.IGNORECASE)
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


@dataclass
class ResetInfo:
    resets_at: datetime | None
    scope: str | None


def _parse_reset(blob: str) -> datetime | None:
    m = _ISO_RE.search(blob)
    if not m:
        return None
    s = m.group(0)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def _limit_from(obj: dict) -> ResetInfo | None:
    blob = json.dumps(obj)
    status = None
    err = obj.get("error")
    if isinstance(err, dict):
        status = err.get("status")
    hit = _LIMIT_RE.search(blob) is not None or status == 429
    if not hit:
        return None
    return ResetInfo(resets_at=_parse_reset(blob), scope=None)


def detect_limit_in_result(result: dict) -> ResetInfo | None:
    if not result.get("is_error"):
        return None
    return _limit_from(result)


def detect_limit_in_jsonl_line(line: dict) -> ResetInfo | None:
    if line.get("subtype") == "rate_limit" or line.get("type") == "rate_limit":
        return ResetInfo(resets_at=_parse_reset(json.dumps(line)), scope=None)
    # tolerant fallback: an explicit error line carrying a limit phrase
    if line.get("is_error") or line.get("type") == "system":
        return _limit_from(line)
    return None
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_detect -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/detect.py tests/test_detect.py
git commit -m "feat: limit detection from result JSON + transcript JSONL"
```

---

## Task 8: Accounts integration glue + token validity

**Files:**
- Create: `src/ccswitch/accounts.py`, `tests/test_accounts.py`

**Interfaces:**
- Consumes: `oauth`, `creds`, `ledger`, `selector`, `configdir`, `paths`, `config`.
- Produces: `ensure_valid_token(name) -> dict`, `usage_for(name) -> Usage | None`, `pick_account(cfg, exclude) -> str`, `sync_back(name)`, `remove_account(name)`, `list_accounts() -> list[dict]`. (`add_account` is in Task 10 because it orchestrates `claude /login`.)

- [ ] **Step 1: Write failing tests** `tests/test_accounts.py`

```python
import json
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from tests.helpers import temp_env, usage


def _seed_account(name, expires_in_ms=10_000_000):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-A", "refreshToken": f"{name}-R",
        "expiresAt": now_ms + expires_in_ms, "subscriptionType": "max",
    })
    write_json_secure(paths.shared_mcp_path(), {})


class TestAccounts(unittest.TestCase):
    def test_ensure_valid_token_refreshes_when_expired(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha", expires_in_ms=-1)  # already expired
            new_blob = {"accessToken": "fresh", "refreshToken": "r2", "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"}
            with mock.patch.object(oauth, "refresh_access_token", return_value=new_blob) as rt:
                blob = accounts.ensure_valid_token("alpha")
                rt.assert_called_once()
                self.assertEqual(blob["accessToken"], "fresh")
            # persisted to slot
            from ccswitch import paths
            self.assertEqual(json.loads(paths.slot_path("alpha").read_text())["accessToken"], "fresh")

    def test_usage_for_uses_cache_then_fetches(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(oauth, "fetch_usage", return_value=usage(session=33.0)) as fu:
                u1 = accounts.usage_for("alpha")
                u2 = accounts.usage_for("alpha")  # second call served from cache
                self.assertEqual(u1.session_pct, 33.0)
                self.assertEqual(fu.call_count, 1)

    def test_usage_for_autherror_marks_needs_reauth(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha")
            with mock.patch.object(oauth, "fetch_usage", side_effect=oauth.AuthError("401")), \
                 mock.patch.object(oauth, "refresh_access_token", side_effect=oauth.AuthError("dead")):
                self.assertIsNone(accounts.usage_for("alpha"))
                self.assertEqual(accounts.list_accounts()[0]["status"], "needs-reauth")

    def test_pick_account_reserves_winner(self):
        with temp_env():
            from ccswitch import accounts, oauth
            _seed_account("alpha"); _seed_account("beta")
            def fake_usage(name):
                return usage(session=80.0) if name == "alpha" else usage(session=5.0)
            with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                self.assertEqual(accounts.pick_account(accounts.load_config()), "beta")
                # beta now reserved → excluded on the next pick, alpha wins
                self.assertEqual(accounts.pick_account(accounts.load_config()), "alpha")

    def test_sync_back_splits_live_creds(self):
        with temp_env():
            from ccswitch import accounts, paths, configdir
            _seed_account("alpha")
            cfg_dir = configdir.compose("alpha")
            # simulate claude refreshing the token + an MCP login in the live file
            live = json.loads((cfg_dir / ".credentials.json").read_text())
            live["claudeAiOauth"]["accessToken"] = "refreshed"
            live["claudeAiOauth"]["expiresAt"] = 9_999_999_999_999
            live["mcpOAuth"] = {"sentry": {"t": 1}}
            (cfg_dir / ".credentials.json").write_text(json.dumps(live))
            accounts.sync_back("alpha")
            self.assertEqual(json.loads(paths.slot_path("alpha").read_text())["accessToken"], "refreshed")
            self.assertEqual(json.loads(paths.shared_mcp_path().read_text()), {"sentry": {"t": 1}})
```

- [ ] **Step 2: Run, verify fail**

Run: `python -m unittest tests.test_accounts -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.accounts'`.

- [ ] **Step 3: Implement `src/ccswitch/accounts.py`**

```python
"""Integration glue: token validity, usage caching, selection, sync-back, registry."""
import time
from datetime import datetime, timezone

from . import configdir, ledger, oauth, paths
from .config import Config, load_config
from .creds import compose_credentials, merge_keep_later, read_json, split_credentials, write_json_secure
from .selector import AllAccountsBusy, select_best


def _account_names() -> list[str]:
    d = paths.store_dir()
    if not d.exists():
        return []
    return sorted(p.name[: -len(".cred.json")] for p in d.glob("*.cred.json"))


def ensure_valid_token(name: str) -> dict:
    """Return a non-expired claudeAiOauth blob, refreshing + persisting if needed."""
    blob = read_json(paths.slot_path(name))
    cfg = load_config()
    now_ms = int(time.time() * 1000)
    if blob.get("expiresAt", 0) - now_ms > cfg.expiry_skew_sec * 1000:
        return blob
    new_blob = oauth.refresh_access_token(blob["refreshToken"], blob)
    write_json_secure(paths.slot_path(name), new_blob)
    return new_blob


def usage_for(name: str) -> oauth.Usage | None:
    """Cached-or-fetched usage. Returns None and marks needs-reauth if auth fails."""
    cfg = load_config()
    now = datetime.now(timezone.utc)
    with ledger.locked_ledger() as led:
        cached = ledger.cached_usage(led, name, cfg, now)
        if cached is not None:
            return cached
    try:
        blob = ensure_valid_token(name)
        u = oauth.fetch_usage(blob["accessToken"])
    except oauth.AuthError:
        try:
            blob = oauth.refresh_access_token(read_json(paths.slot_path(name))["refreshToken"], read_json(paths.slot_path(name)))
            write_json_secure(paths.slot_path(name), blob)
            u = oauth.fetch_usage(blob["accessToken"])
        except oauth.AuthError:
            with ledger.locked_ledger() as led:
                ledger.set_status(led, name, "needs-reauth")
            return None
    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, u, now)
        ledger.set_status(led, name, "ok")
    return u


def pick_account(cfg: Config, exclude: set[str] = frozenset()) -> str:
    names = _account_names()
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        name = select_best(usages, reserved, cfg, now, exclude)
        ledger.reserve(led, name, now_ts + cfg.reservation_ttl_sec)
    return name


def sync_back(name: str) -> None:
    """Split the live config-dir creds back into the per-account slot + shared mcp store."""
    live_path = paths.account_cfg_dir(name) / ".credentials.json"
    if not live_path.exists():
        return
    claude_oauth, mcp = split_credentials(read_json(live_path))
    existing = read_json(paths.slot_path(name)) if paths.slot_path(name).exists() else None
    write_json_secure(paths.slot_path(name), merge_keep_later(existing, claude_oauth))
    if mcp:
        write_json_secure(paths.shared_mcp_path(), mcp)


def remove_account(name: str) -> None:
    import shutil
    paths.slot_path(name).unlink(missing_ok=True)
    cfg_dir = paths.account_cfg_dir(name)
    if cfg_dir.exists():
        shutil.rmtree(cfg_dir)


def list_accounts() -> list[dict]:
    out = []
    with ledger.locked_ledger() as led:
        for name in _account_names():
            acct = led["accounts"].get(name, {})
            out.append({"name": name, "status": acct.get("status", "unknown"), "usage": acct.get("usage")})
    return out
```

- [ ] **Step 4: Run, verify pass**

Run: `python -m unittest tests.test_accounts -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/accounts.py tests/test_accounts.py
git commit -m "feat: accounts integration glue (token validity, usage cache, pick, sync-back)"
```

---

## Task 9: `ccrun` headless wrapper

**Files:**
- Create: `src/ccswitch/runner.py`, `tests/fake_claude.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: `accounts.pick_account/sync_back`, `configdir.compose`, `detect.detect_limit_in_result`, `config.load_config`.
- Produces: `runner.main(argv) -> int`. Internal seam: `run_claude(cmd: list[str], env: dict) -> tuple[int, str]` (monkeypatched in tests).

- [ ] **Step 1: Write `tests/fake_claude.py`** (a stub `claude` executable driven by env)

```python
#!/usr/bin/env python3
"""Fake `claude`. CCSWITCH_FAKE_MODE drives behavior; writes JSON to stdout.

Modes (per CLAUDE_CONFIG_DIR basename == account name):
  CCSWITCH_FAKE_LIMITED=alpha,beta  -> those accounts emit a 429 result, others succeed.
"""
import json
import os
import sys


def main():
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    account = os.path.basename(cfg_dir.rstrip("/"))
    limited = set(filter(None, os.environ.get("CCSWITCH_FAKE_LIMITED", "").split(",")))
    if account in limited:
        print(json.dumps({"type": "result", "is_error": True,
                          "error": {"message": "Claude usage limit reached", "resets_at": "2026-06-19T18:00:00+00:00"}}))
        return 1
    print(json.dumps({"type": "result", "is_error": False, "result": f"ok from {account}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write failing tests** `tests/test_runner.py`

```python
import json
import os
import sys
import time
import unittest
from unittest import mock
from tests.helpers import temp_env


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                               "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class TestRunner(unittest.TestCase):
    def test_runs_on_selected_account(self):
        with temp_env():
            from ccswitch import runner, accounts, oauth
            from tests.helpers import usage
            _seed("alpha"); _seed("beta")
            os.environ["CCSWITCH_CLAUDE_BIN"] = f"{sys.executable} {os.path.abspath('tests/fake_claude.py')}"
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0 if n == "beta" else 90.0)):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 0)

    def test_retries_next_account_on_limit(self):
        with temp_env():
            from ccswitch import runner, accounts
            from tests.helpers import usage
            _seed("alpha"); _seed("beta")
            os.environ["CCSWITCH_CLAUDE_BIN"] = f"{sys.executable} {os.path.abspath('tests/fake_claude.py')}"
            os.environ["CCSWITCH_FAKE_LIMITED"] = "beta"  # beta (lowest util) is limited → retry alpha
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0 if n == "beta" else 50.0)):
                rc = runner.main(["--", "-p", "hi"])
            self.assertEqual(rc, 0)  # succeeded after retrying alpha
            os.environ.pop("CCSWITCH_FAKE_LIMITED", None)
```

- [ ] **Step 3: Run, verify fail**

Run: `python -m unittest tests.test_runner -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.runner'`.

- [ ] **Step 4: Implement `src/ccswitch/runner.py`**

```python
"""ccrun: headless wrapper. Selects the best account, runs `claude -p`, retries on limit."""
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime

from . import accounts
from .config import load_config
from .detect import detect_limit_in_result
from .selector import AllAccountsBusy


def _claude_cmd(cfg) -> list[str]:
    raw = os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin)
    return shlex.split(raw)


def run_claude(cmd: list[str], env: dict) -> tuple[int, str]:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def _last_result(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            return obj
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    wait = False
    if argv and argv[0] == "--wait":
        wait = True
        argv = argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    cfg = load_config()
    from .configdir import compose
    tried: set[str] = set()
    n_accounts = len(accounts.list_accounts())
    while True:
        try:
            name = accounts.pick_account(cfg, exclude=tried)
        except AllAccountsBusy as busy:
            if wait and busy.earliest_reset is not None:
                delay = max(1.0, (busy.earliest_reset - datetime.now(busy.earliest_reset.tzinfo)).total_seconds())
                time.sleep(delay)
                tried.clear()
                continue
            when = busy.earliest_reset.astimezone() if busy.earliest_reset else "unknown"
            print(f"ccrun: all accounts at usage ceiling (earliest reset: {when})", file=sys.stderr)
            return 2
        cfg_dir = compose(name)
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg_dir))
        rc, stdout = run_claude(_claude_cmd(cfg) + argv, env)
        sys.stdout.write(stdout)
        result = _last_result(stdout)
        limit = detect_limit_in_result(result) if result else None
        if limit is None:
            accounts.sync_back(name)
            return rc
        tried.add(name)
        print(f"ccrun: {name} hit its usage limit — rotating to another account", file=sys.stderr)
        if len(tried) >= max(1, n_accounts):
            if wait:
                tried.clear()
                time.sleep(5)
                continue
            print("ccrun: all accounts exhausted", file=sys.stderr)
            return 2
```

- [ ] **Step 5: Run, verify pass**

Run: `python -m unittest tests.test_runner -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add src/ccswitch/runner.py tests/fake_claude.py tests/test_runner.py
git commit -m "feat: ccrun headless wrapper with retry-on-limit"
```

---

## Task 10: `ccmax` launcher + sidecar monitor, and `ccswitch` CLI (add/list/remove/link)

**Files:**
- Create: `src/ccswitch/launcher.py`, `src/ccswitch/cli.py`, `tests/test_launcher.py`, `tests/test_cli.py`
- Modify: `src/ccswitch/accounts.py` (add `add_account`)

**Interfaces:**
- Consumes: `accounts.*`, `configdir.compose`, `detect.detect_limit_in_jsonl_line`, `config.load_config`.
- Produces: `launcher.main(argv) -> int`, `cli.main(argv) -> int`, `accounts.add_account(name)`. Internal seams (monkeypatched in tests): `launcher.spawn_claude(cmd, env) -> Popen-like`, `launcher.scan_for_limit(cfg_dir) -> ResetInfo | None`.

- [ ] **Step 1: Write failing test** `tests/test_launcher.py` (decision logic + one orchestration pass)

```python
import os
import sys
import time
import unittest
from unittest import mock
from tests.helpers import temp_env, usage


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                               "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class FakeProc:
    """Stand-in for Popen: exits cleanly after `turns` poll cycles."""
    def __init__(self, turns=1):
        self._turns = turns
        self.terminated = False
    def poll(self):
        self._turns -= 1
        return 0 if self._turns < 0 else None
    def wait(self, timeout=None):
        return 0
    def terminate(self):
        self.terminated = True


class TestLauncher(unittest.TestCase):
    def test_switch_when_limit_detected(self):
        with temp_env():
            from ccswitch import launcher, accounts
            _seed("alpha"); _seed("beta")
            # First spawn (alpha) trips the limit on first scan; second spawn (beta) exits clean.
            spawns = [FakeProc(turns=99), FakeProc(turns=0)]
            scan_results = [object(), None]  # truthy ResetInfo then None
            from ccswitch.detect import ResetInfo
            scan_results = [ResetInfo(resets_at=None, scope=None), None]
            with mock.patch.object(accounts, "usage_for", side_effect=lambda n: usage(session=5.0 if n == "alpha" else 50.0)), \
                 mock.patch.object(launcher, "spawn_claude", side_effect=lambda cmd, env: spawns.pop(0)), \
                 mock.patch.object(launcher, "scan_for_limit", side_effect=lambda d: scan_results.pop(0)), \
                 mock.patch.object(launcher, "current_session_id", return_value="sess-1"), \
                 mock.patch.object(accounts, "sync_back"):
                rc = launcher.main(["--poll-interval", "0"])
            self.assertEqual(rc, 0)
            self.assertTrue(spawns == [] )  # both procs consumed → a switch happened
```

- [ ] **Step 2: Write failing test** `tests/test_cli.py`

```python
import unittest
from unittest import mock
from tests.helpers import temp_env


class TestCli(unittest.TestCase):
    def test_list_runs(self):
        with temp_env():
            from ccswitch import cli
            self.assertEqual(cli.main(["list"]), 0)

    def test_add_invokes_capture(self):
        with temp_env():
            from ccswitch import cli, accounts
            with mock.patch.object(accounts, "add_account") as add:
                self.assertEqual(cli.main(["add", "alpha"]), 0)
                add.assert_called_once_with("alpha")
```

- [ ] **Step 3: Run, verify fail**

Run: `python -m unittest tests.test_launcher tests.test_cli -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.launcher'` (and `ccswitch.cli`).

- [ ] **Step 4: Add `add_account` to `src/ccswitch/accounts.py`**

```python
def add_account(name: str) -> None:
    """Capture-on-login: run `claude /login` against a temp capture dir, then store the creds."""
    import os
    import shlex
    import subprocess
    import tempfile
    from .config import load_config
    cfg = load_config()
    claude_cmd = shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin))
    with tempfile.TemporaryDirectory() as capture:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=capture)
        print(f"Log in to the '{name}' account in the browser window that opens…")
        subprocess.run(claude_cmd + ["/login"], env=env)
        cred_path = os.path.join(capture, ".credentials.json")
        if not os.path.exists(cred_path):
            raise RuntimeError("login did not produce credentials; aborting")
        claude_oauth, mcp = split_credentials(read_json(cred_path))
        if claude_oauth.get("subscriptionType") != "max":
            print(f"WARNING: '{name}' is not a Max subscription (got {claude_oauth.get('subscriptionType')!r})")
        write_json_secure(paths.slot_path(name), claude_oauth)
        if mcp:
            existing = read_json(paths.shared_mcp_path()) if paths.shared_mcp_path().exists() else {}
            existing.update(mcp)
            write_json_secure(paths.shared_mcp_path(), existing)
    configdir.compose(name)
```

- [ ] **Step 5: Implement `src/ccswitch/launcher.py`**

```python
"""ccmax: interactive launcher + sidecar monitor (usage poll + transcript tail)."""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import accounts
from .config import load_config
from .detect import detect_limit_in_jsonl_line


def _claude_cmd(cfg) -> list[str]:
    return shlex.split(os.environ.get("CCSWITCH_CLAUDE_BIN", cfg.claude_bin))


def spawn_claude(cmd: list[str], env: dict):
    return subprocess.Popen(cmd, env=env)


def current_session_id(cfg_dir: str) -> str | None:
    """Newest *.jsonl basename (without extension) under the config dir's projects/ tree."""
    base = os.path.join(cfg_dir, "projects")
    newest, newest_mtime = None, -1.0
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(".jsonl"):
                p = os.path.join(dirpath, fn)
                m = os.path.getmtime(p)
                if m > newest_mtime:
                    newest, newest_mtime = fn[:-len(".jsonl")], m
    return newest


def scan_for_limit(cfg_dir: str):
    """Scan the newest transcript for a rate-limit event. Returns ResetInfo | None."""
    sid = current_session_id(cfg_dir)
    if not sid:
        return None
    base = os.path.join(cfg_dir, "projects")
    for dirpath, _dirs, files in os.walk(base):
        if f"{sid}.jsonl" in files:
            with open(os.path.join(dirpath, f"{sid}.jsonl"), encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    info = detect_limit_in_jsonl_line(line)
                    if info is not None:
                        return info
    return None


def _poll_usage_exceeds_soft(name: str, cfg) -> bool:
    u = accounts.usage_for(name)
    return u is not None and u.session_pct >= cfg.soft_threshold * 100.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccmax", add_help=False)
    parser.add_argument("--confirm-switch", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=None)
    known, claude_args = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    poll = cfg.poll_interval_sec if known.poll_interval is None else known.poll_interval

    from .configdir import compose
    name = accounts.pick_account(cfg)
    resume_id: str | None = None
    while True:
        cfg_dir = str(compose(name))
        env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg_dir)
        cmd = _claude_cmd(cfg) + (["--resume", resume_id] if resume_id else []) + claude_args
        proc = spawn_claude(cmd, env)
        switch = False
        while proc.poll() is None:
            if scan_for_limit(cfg_dir) is not None or _poll_usage_exceeds_soft(name, cfg):
                switch = True
                break
            if poll:
                time.sleep(poll)
        if not switch:
            proc.wait()
            accounts.sync_back(name)
            return 0
        resume_id = current_session_id(cfg_dir) or resume_id
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        accounts.sync_back(name)
        try:
            nxt = accounts.pick_account(cfg, exclude={name})
        except Exception as e:
            print(f"ccmax: cannot switch ({e})", file=sys.stderr)
            return 2
        if known.confirm_switch:
            print(f"ccmax: {name} hit its limit. Switch to {nxt} and resume? [Y/n] ", end="")
            if input().strip().lower() in ("n", "no"):
                return 0
        print(f"ccmax: {name} hit its limit — switching to {nxt} and resuming…", file=sys.stderr)
        name = nxt
```

- [ ] **Step 6: Implement `src/ccswitch/cli.py`**

```python
"""ccswitch: account store management (add / remove / list / link)."""
import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import accounts, configdir
from .config import load_config


def _fmt_reset(iso: str | None, tz: str) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%a %-I:%M %p %Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccswitch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_add = sub.add_parser("add"); p_add.add_argument("name")
    p_rm = sub.add_parser("remove"); p_rm.add_argument("name")
    p_link = sub.add_parser("link"); p_link.add_argument("name", nargs="?")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    cfg = load_config()

    if args.cmd == "list":
        for a in accounts.list_accounts():
            u = a.get("usage") or {}
            print(f"{a['name']:<12} {a['status']:<12} "
                  f"session={u.get('session_pct', '?')}% (resets {_fmt_reset(u.get('session_resets_at'), cfg.timezone)})  "
                  f"weekly={u.get('weekly_pct', '?')}% (resets {_fmt_reset(u.get('weekly_resets_at'), cfg.timezone)})")
        return 0
    if args.cmd == "add":
        accounts.add_account(args.name); return 0
    if args.cmd == "remove":
        accounts.remove_account(args.name); return 0
    if args.cmd == "link":
        names = [args.name] if args.name else [a["name"] for a in accounts.list_accounts()]
        for n in names:
            configdir.reconcile_links(n)
        return 0
    return 1
```

- [ ] **Step 7: Run, verify pass**

Run: `python -m unittest tests.test_launcher tests.test_cli -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Commit**

```bash
git add src/ccswitch/launcher.py src/ccswitch/cli.py src/ccswitch/accounts.py tests/test_launcher.py tests/test_cli.py
git commit -m "feat: ccmax launcher + sidecar monitor; ccswitch CLI; add_account capture"
```

---

## Task 11: Full suite, concurrency test, install, manual smoke

**Files:**
- Create: `tests/test_concurrency.py`, `docs/SMOKE.md`

**Interfaces:**
- Consumes: everything.
- Produces: install verification + manual smoke checklist.

- [ ] **Step 1: Write concurrency test** `tests/test_concurrency.py`

```python
import threading
import time
import unittest
from unittest import mock
from tests.helpers import temp_env, usage


def _seed(name):
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {"accessToken": f"{name}A", "refreshToken": f"{name}R",
                                              "expiresAt": int(time.time()*1000)+9_000_000, "subscriptionType": "max"})
    write_json_secure(paths.shared_mcp_path(), {})


class TestConcurrency(unittest.TestCase):
    def test_parallel_picks_do_not_double_assign_when_one_clear_winner(self):
        with temp_env():
            from ccswitch import accounts
            _seed("alpha"); _seed("beta"); _seed("gamma")
            # beta has by far the most headroom; with reservations, only ONE picker should get beta.
            def fake_usage(n):
                return {"alpha": usage(session=70.0), "beta": usage(session=5.0), "gamma": usage(session=72.0)}[n]
            picks = []
            lock = threading.Lock()
            def worker():
                with mock.patch.object(accounts, "usage_for", side_effect=fake_usage):
                    name = accounts.pick_account(accounts.load_config())
                with lock:
                    picks.append(name)
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(len(picks), 3)
            self.assertEqual(picks.count("beta"), 1)  # reservation prevents double-assign
```

- [ ] **Step 2: Run the full suite**

Run: `python -m unittest discover -s tests -v`
Expected: PASS (all tests across all modules).

- [ ] **Step 3: Editable install + console-script check**

Run:
```bash
pip install -e .
ccswitch --help && ccrun --help 2>&1 | head -1; ccmax --help 2>&1 | head -1
```
Expected: `ccswitch` prints subcommands; `ccrun`/`ccmax` run without import error.

- [ ] **Step 4: Write `docs/SMOKE.md` (manual real-account verification)**

```markdown
# Manual smoke test (uses one real Max account)

1. `ccswitch add alpha`  → log in to your first account when the browser opens.
   - Verify: `ls -la ~/.claude-accounts/store/alpha.cred.json` (mode 600) exists.
   - Verify: `ccswitch list` shows `alpha  ok  session=N% ... weekly=M% ...` with reset times in ET.
2. `ccrun -- -p "say hi in 3 words"`  → prints a normal Claude response.
   - Verify sync-back: note `expiresAt` in `store/alpha.cred.json` before/after; if Claude refreshed, it bumped.
3. Repeat `ccswitch add beta` / `add gamma` for the other accounts.
4. `ccswitch list` shows all three with live utilization.
5. Calibrate detection (Task 7 note): once any account nears its limit, run `ccrun -- -p "..."`
   against it and capture the limited result JSON; confirm `detect_limit_in_result` matches
   (add a fixture to `tests/test_detect.py` if the wording differs).
6. Interactive: `ccmax` launches on the most-headroom account; confirm the session works.
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_concurrency.py docs/SMOKE.md
git commit -m "test: concurrency reservation test + manual smoke checklist"
```

---

## Self-review (completed against the spec)

- **§1 Account store layout** → Tasks 2 (split/merge), 6 (config-dir composition + symlink mirror), 8 (slots/shared mcp). ✓
- **§2 Exact usage + selection** → Tasks 3 (fetch/parse), 4 (cache), 5 (select_best ranking + ceiling), 8 (usage_for cache+refresh, pick_account reserve). ✓
- **§ Independent token refresh** → Task 3 (`refresh_access_token`), Task 8 (`ensure_valid_token`, 401→refresh→needs-reauth). ✓
- **§3 Interactive ccmax (poll + JSONL tail, kill+resume, --confirm-switch)** → Task 10. ✓
- **§4 Headless ccrun (retry, --wait)** → Task 9. ✓
- **§5 Capture-on-login + list/remove/link** → Tasks 8 (remove/list), 10 (add_account, cli). ✓
- **§6 Error/edge (all-cooling earliest reset, needs-reauth, keep-later-expiresAt, no secrets, ET display)** → Tasks 2, 5, 8, 9, cli `_fmt_reset`. ✓
- **§7 Testing (unit, mock endpoints, fake-claude stub, concurrency, smoke)** → every task's tests + Tasks 9/10 fakes + Task 11. ✓
- **§ Compatibility (per-process CLAUDE_CONFIG_DIR)** → enforced structurally: every launch composes a per-account dir and sets the env var (Tasks 9, 10). ✓
- **Placeholder scan:** no TBD/TODO; all code blocks complete. The one calibration note (Task 7) is a deliberate, tested-with-fixtures function plus a smoke-time confirmation, not a placeholder.
- **Type consistency:** signatures match the locked interface map; `Usage`, `ResetInfo`, `Config`, `select_best`, `pick_account`, `sync_back`, `compose` names are used identically across tasks.
