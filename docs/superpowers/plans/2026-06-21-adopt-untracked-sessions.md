# Adopt Untracked Live Claude Sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let ccswitch discover live `claude` sessions running outside its control, and force-take-over a chosen one — terminate the original process and resume its conversation under a ccswitch driver.

**Architecture:** A new discovery module (`server/sessions/adopt.py`) scans `/proc`, classifies `claude` processes, maps each to a managed account, and resolves its transcript. `SessionOrchestrator.adopt_session` terminates the original process tree and resumes the conversation in the original cwd (no worktree). Two REST endpoints surface candidates and perform adoption. A dashboard panel drives risk-scaled consent.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (SQLite), pytest. Frontend React+Vite+TS, vitest.

**Spec:** `docs/superpowers/specs/2026-06-21-adopt-untracked-sessions-design.md`

## Global Constraints

- **Account = `CLAUDE_CONFIG_DIR`** (Constitution II). Map a config dir to an account by comparing realpaths against `ccswitch.paths.account_cfg_dir(name)`.
- **Skip-unmapped:** only `mapped and not tracked` candidates are adoptable; others are returned flagged, never dropped.
- **Resume in original cwd, no worktree** for adopted sessions; flag `adopted_in_place=True`.
- **New DB columns MUST be nullable** (dev SQLite `init_db` backfills nullable columns; NOT NULL needs a real migration).
- **Auth:** every endpoint uses `Depends(current_identity)`. Terminations + adoptions write `AuditEvent`s via `record_audit(db, type, identity, detail)`.
- **Test env:** run with the main checkout's venv; API auth tests pin identity header `{"Tailscale-User-Login": "testuser@example.com"}` or run under `CCSWITCH_DEV=1` with empty allowlist.
- **`skip_reason` codes:** `"unmapped"`, `"tracked"`, or `None`. UI maps to plain copy.
- **Linux `/proc` only** — the deploy target.

---

### Task 1: Process discovery + classification primitive

**Files:**
- Create: `server/sessions/adopt.py`
- Test: `server/tests/test_adopt_classify.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ProcInfo(pid:int, cmdline:list[str], environ:dict[str,str], cwd:str|None)`
  - `classify(cmdline: list[str]) -> str` returning one of `"sdk"` (ccswitch-owned), `"helper"`, `"session"` (adoptable TUI), `"other"`.
  - `_proc_lister() -> list[ProcInfo]` (reads `/proc`; not unit-tested directly).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_adopt_classify.py
from server.sessions.adopt import classify

def test_classify_sdk_owned_driver():
    cmd = ["/x/claude_agent_sdk/_bundled/claude", "--output-format", "stream-json",
           "--resume", "abc", "--input-format", "stream-json"]
    assert classify(cmd) == "sdk"

def test_classify_helper_processes():
    assert classify(["/x/claude/versions/2.1", "--bg-pty-host", "/tmp/x.sock"]) == "helper"
    assert classify(["/x/claude/versions/2.1", "--bg-spare", "/tmp/x.sock"]) == "helper"
    assert classify(["/x/claude/versions/2.1", "daemon", "run", "--origin", "transient"]) == "helper"
    assert classify(["claude", "auth", "login", "--claudeai"]) == "helper"

def test_classify_interactive_session_is_adoptable():
    assert classify(["claude", "agents"]) == "session"
    assert classify(["claude"]) == "session"

def test_classify_non_claude_is_other():
    assert classify(["node", "/x/storybook/bin.js"]) == "other"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_classify.py -v`
Expected: FAIL — `ModuleNotFoundError`/`ImportError: classify`.

- [ ] **Step 3: Write minimal implementation**

```python
# server/sessions/adopt.py
"""Discovery + classification of live `claude` processes for adoption.

Linux /proc only (deploy target). A "session" candidate is an interactive
claude process NOT owned by ccswitch (no stream-json) and not a helper.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_HELPER_FLAGS = ("--bg-pty-host", "--bg-spare")


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    cmdline: list[str]
    environ: dict[str, str]
    cwd: str | None


def classify(cmdline: list[str]) -> str:
    """Bucket a process by argv: 'sdk' | 'helper' | 'session' | 'other'."""
    if not cmdline:
        return "other"
    joined = " ".join(cmdline)
    is_claude = any("claude" in part for part in cmdline)
    if not is_claude:
        return "other"
    # ccswitch's own SDK driver speaks stream-json on stdin — already tracked.
    if "--input-format" in cmdline and "stream-json" in cmdline:
        return "sdk"
    if any(f in cmdline for f in _HELPER_FLAGS):
        return "helper"
    if "daemon" in cmdline and "run" in cmdline:
        return "helper"
    if "auth" in cmdline and "login" in cmdline:
        return "helper"
    return "session"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_classify.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Add the real `/proc` lister (not unit-tested) and commit**

Append to `server/sessions/adopt.py`:

```python
def _read_proc(pid: int) -> ProcInfo | None:
    base = f"/proc/{pid}"
    try:
        with open(f"{base}/cmdline", "rb") as fh:
            raw = fh.read()
        cmdline = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
        environ: dict[str, str] = {}
        try:
            with open(f"{base}/environ", "rb") as fh:
                for entry in fh.read().decode("utf-8", "replace").split("\0"):
                    if "=" in entry:
                        k, v = entry.split("=", 1)
                        environ[k] = v
        except (PermissionError, FileNotFoundError):
            pass
        try:
            cwd = os.readlink(f"{base}/cwd")
        except (PermissionError, FileNotFoundError):
            cwd = None
        return ProcInfo(pid=pid, cmdline=cmdline, environ=environ, cwd=cwd)
    except (FileNotFoundError, ProcessLookupError):
        return None


def _proc_lister() -> list[ProcInfo]:
    out: list[ProcInfo] = []
    try:
        pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except FileNotFoundError:
        return out
    for pid in pids:
        info = _read_proc(pid)
        if info is not None:
            out.append(info)
    return out
```

```bash
git add server/sessions/adopt.py server/tests/test_adopt_classify.py
git commit -m "feat(adopt): classify live claude processes (sdk/helper/session)"
```

---

### Task 2: Account mapping, transcript resolution, `discover_candidates`

**Files:**
- Modify: `server/sessions/adopt.py`
- Test: `server/tests/test_adopt_discover.py`

**Interfaces:**
- Consumes: `ProcInfo`, `classify`, `_proc_lister` (Task 1).
- Produces:
  - `@dataclass(frozen=True) AdoptionCandidate(pid:int|None, account:str|None, cwd:str, claude_session_id:str, transcript_path:str, name:str, mapped:bool, tracked:bool, skip_reason:str|None, last_activity:datetime, pid_alive:bool)`
  - `discover_candidates(known_accounts:list[str], tracked_sids:set[str], proc_lister=_proc_lister, account_cfg=ccswitch.paths.account_cfg_dir) -> list[AdoptionCandidate]`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_adopt_discover.py
import json, os
from datetime import datetime
from pathlib import Path
from server.sessions.adopt import discover_candidates, ProcInfo

def _make_transcript(cfg: Path, project: str, sid: str, title="My Work"):
    pdir = cfg / "projects" / project
    pdir.mkdir(parents=True, exist_ok=True)
    f = pdir / f"{sid}.jsonl"
    f.write_text(json.dumps({"type": "summary", "summary": title}) + "\n")
    return f

def test_discover_maps_account_and_resolves_transcript(tmp_path):
    cfg = tmp_path / "cfg" / "kindash"
    cwd = "/home/u/repo"
    sid = "11111111-1111-1111-1111-111111111111"
    tf = _make_transcript(cfg, "-home-u-repo", sid)
    procs = [ProcInfo(pid=42, cmdline=["claude", "agents"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd=cwd)]
    cands = discover_candidates(
        known_accounts=["kindash"], tracked_sids=set(),
        proc_lister=lambda: procs,
        account_cfg=lambda name: cfg if name == "kindash" else tmp_path / name,
    )
    assert len(cands) == 1
    c = cands[0]
    assert c.account == "kindash" and c.mapped and not c.tracked
    assert c.claude_session_id == sid and c.skip_reason is None
    assert c.pid == 42 and c.pid_alive
    assert isinstance(c.last_activity, datetime)

def test_discover_flags_unmapped_config_dir(tmp_path):
    cfg = tmp_path / "dot-claude"  # not a managed account dir
    sid = "22222222-2222-2222-2222-222222222222"
    _make_transcript(cfg, "-home-u-repo", sid)
    procs = [ProcInfo(pid=7, cmdline=["claude"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd="/home/u/repo")]
    cands = discover_candidates(
        known_accounts=["kindash"], tracked_sids=set(), proc_lister=lambda: procs,
        account_cfg=lambda name: tmp_path / "cfg" / name)
    assert len(cands) == 1
    assert not cands[0].mapped and cands[0].skip_reason == "unmapped"

def test_discover_flags_already_tracked(tmp_path):
    cfg = tmp_path / "cfg" / "kindash"
    sid = "33333333-3333-3333-3333-333333333333"
    _make_transcript(cfg, "-home-u-repo", sid)
    procs = [ProcInfo(pid=9, cmdline=["claude", "agents"],
                      environ={"CLAUDE_CONFIG_DIR": str(cfg)}, cwd="/home/u/repo")]
    cands = discover_candidates(
        known_accounts=["kindash"], tracked_sids={sid}, proc_lister=lambda: procs,
        account_cfg=lambda name: cfg if name == "kindash" else tmp_path / name)
    assert cands[0].tracked and cands[0].skip_reason == "tracked"

def test_discover_excludes_sdk_and_helper(tmp_path):
    procs = [
        ProcInfo(pid=1, cmdline=["claude", "--input-format", "stream-json"], environ={}, cwd="/x"),
        ProcInfo(pid=2, cmdline=["claude", "--bg-spare", "/s"], environ={}, cwd="/x"),
    ]
    cands = discover_candidates(["kindash"], set(), proc_lister=lambda: procs,
                                account_cfg=lambda n: tmp_path / n)
    assert cands == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_discover.py -v`
Expected: FAIL — `ImportError: discover_candidates`.

- [ ] **Step 3: Write minimal implementation**

Append to `server/sessions/adopt.py`:

```python
import glob
from datetime import datetime, timezone

from ccswitch import paths as _paths
from server.sessions.titles import read_ai_title_from_file


@dataclass(frozen=True)
class AdoptionCandidate:
    pid: int | None
    account: str | None
    cwd: str
    claude_session_id: str
    transcript_path: str
    name: str
    mapped: bool
    tracked: bool
    skip_reason: str | None
    last_activity: datetime
    pid_alive: bool


def _map_account(config_dir: str, known_accounts: list[str], account_cfg) -> str | None:
    target = os.path.realpath(config_dir)
    for name in known_accounts:
        if os.path.realpath(str(account_cfg(name))) == target:
            return name
    return None


def _resolve_transcript(config_dir: str, cwd: str) -> tuple[str, str, datetime] | None:
    """Return (claude_session_id, transcript_path, mtime) for the most-recently
    modified transcript whose project dir matches cwd, else None."""
    project = "-" + cwd.lstrip("/").replace("/", "-").replace(".", "-")
    pattern = os.path.join(config_dir, "projects", "*", "*.jsonl")
    best: tuple[float, str] | None = None
    for path in glob.glob(pattern):
        if os.path.basename(os.path.dirname(path)) != project:
            continue
        mtime = os.path.getmtime(path)
        if best is None or mtime > best[0]:
            best = (mtime, path)
    if best is None:
        return None
    sid = os.path.splitext(os.path.basename(best[1]))[0]
    return sid, best[1], datetime.fromtimestamp(best[0], tz=timezone.utc)


def discover_candidates(known_accounts, tracked_sids, proc_lister=_proc_lister,
                        account_cfg=_paths.account_cfg_dir) -> list[AdoptionCandidate]:
    out: list[AdoptionCandidate] = []
    home_default = os.path.expanduser("~/.claude")
    for p in proc_lister():
        if classify(p.cmdline) != "session":
            continue
        cwd = p.cwd
        if not cwd:
            continue
        config_dir = p.environ.get("CLAUDE_CONFIG_DIR", home_default)
        resolved = _resolve_transcript(config_dir, cwd)
        if resolved is None:
            continue  # unresolvable transcript → never adopt blindly
        sid, tpath, mtime = resolved
        account = _map_account(config_dir, known_accounts, account_cfg)
        mapped = account is not None
        tracked = sid in tracked_sids
        skip = None if (mapped and not tracked) else ("tracked" if tracked else "unmapped")
        title = read_ai_title_from_file(tpath) or os.path.basename(cwd.rstrip("/")) or "session"
        out.append(AdoptionCandidate(
            pid=p.pid, account=account, cwd=cwd, claude_session_id=sid,
            transcript_path=tpath, name=title, mapped=mapped, tracked=tracked,
            skip_reason=skip, last_activity=mtime, pid_alive=True))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_discover.py -v`
Expected: PASS (4 tests). If `read_ai_title_from_file` chokes on the minimal jsonl, the `or basename` fallback still yields a name.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/adopt.py server/tests/test_adopt_discover.py
git commit -m "feat(adopt): discover_candidates with account mapping + transcript resolution"
```

---

### Task 3: DB column + SessionRecord field for `adopted_in_place`

**Files:**
- Modify: `server/db/models.py` (Session), `server/sessions/types.py` (SessionRecord), `server/sessions/orchestrator.py` (rehydrate reads it), `server/api/sessions.py` (`_record_to_dict`)
- Test: `server/tests/test_adopt_model_flag.py`

**Interfaces:**
- Produces: `Session.adopted_in_place: bool | None` (nullable), `SessionRecord.adopted_in_place: bool = False`, wire key `adoptedInPlace`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_adopt_model_flag.py
from server.db.models import Session
from server.sessions.types import SessionRecord

def test_session_model_has_nullable_adopted_flag():
    col = Session.__table__.c.adopted_in_place
    assert col.nullable is True

def test_record_defaults_adopted_false():
    r = SessionRecord(id="i", name="n", account="a", claude_session_id="", cols=80, rows=24, cwd="/x")
    assert r.adopted_in_place is False
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_model_flag.py -v`
Expected: FAIL — `AttributeError`/`KeyError: adopted_in_place`.

- [ ] **Step 3: Implement**

In `server/db/models.py` Session, after `name_is_auto`:

```python
    # True when this session was adopted from an untracked live process and
    # resumed in the original cwd (documented worktree-isolation exception).
    adopted_in_place: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
```

In `server/sessions/types.py` SessionRecord, after `permission_mode`:

```python
    adopted_in_place: bool = False   # resumed in original cwd, no worktree
```

In `server/sessions/orchestrator.py` `rehydrate`, add to the `SessionRecord(...)` construction:

```python
                adopted_in_place=bool(getattr(row, "adopted_in_place", False)),
```

In `server/api/sessions.py` `_record_to_dict`, add:

```python
        "adoptedInPlace": getattr(record, "adopted_in_place", False),
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest server/tests/test_adopt_model_flag.py server/tests/test_db_schema_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/sessions/types.py server/sessions/orchestrator.py server/api/sessions.py server/tests/test_adopt_model_flag.py
git commit -m "feat(adopt): persist adopted_in_place flag (nullable) on sessions"
```

---

### Task 4: Orchestrator `adopt_session` — terminate + resume-in-place

**Files:**
- Modify: `server/sessions/orchestrator.py`
- Test: `server/tests/test_adopt_session.py`

**Interfaces:**
- Consumes: `discover_candidates` (Task 2), `adopted_in_place` (Task 3), `SessionDriver`, `compose`, `configdir.trust_project`, `record_audit`.
- Produces:
  - `terminate_process_tree(pid:int, killer=os.kill, alive=_pid_alive, grace:float=0.0) -> bool` (module-level in orchestrator or adopt.py; returns True if a kill was issued).
  - `SessionOrchestrator.adopt_session(claude_session_id:str, identity:str, killer=os.kill) -> tuple[SessionRecord, dict]` where dict = `{"resumed": bool, "terminatedPid": int|None}`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_adopt_session.py
import os, pytest
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.adopt import AdoptionCandidate
from server.tests.fakes.fake_factory import fake_sdk_factory
from datetime import datetime, timezone

@pytest.mark.asyncio
async def test_adopt_resumes_in_place_no_worktree(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"; cwd.mkdir()
    sid = "44444444-4444-4444-4444-444444444444"
    cand = AdoptionCandidate(pid=999, account="kindash", cwd=str(cwd),
        claude_session_id=sid, transcript_path="/x.jsonl", name="Adopted",
        mapped=True, tracked=False, skip_reason=None,
        last_activity=datetime.now(timezone.utc), pid_alive=True)
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), repo_root=str(tmp_path))
    monkeypatch.setattr(orch, "_resolve_candidate", lambda s: cand)
    monkeypatch.setattr("server.sessions.orchestrator.compose", lambda a: tmp_path / "cfg")
    monkeypatch.setattr("server.sessions.orchestrator.configdir.trust_project", lambda *a, **k: None)
    killed = []
    rec, outcome = await orch.adopt_session(sid, identity="t@e.com", killer=lambda p, s: killed.append((p, s)))
    assert rec.cwd == str(cwd)           # original cwd, not a worktree
    assert rec.adopted_in_place is True
    assert outcome["terminatedPid"] == 999 and outcome["resumed"] is True
    assert killed                         # a signal was sent
    assert rec.id not in orch._worktrees  # no worktree registered
```

- [ ] **Step 2: Run to verify fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_adopt_session.py -v`
Expected: FAIL — `AttributeError: adopt_session`.

- [ ] **Step 3: Implement**

Add to `server/sessions/adopt.py`:

```python
import signal as _signal
import time as _time


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_tree(pid, killer=os.kill, alive=_pid_alive, grace: float = 1.5) -> bool:
    """SIGTERM then (after grace) SIGKILL. Returns True if any signal was sent.
    A vanished PID is a no-op success (returns False)."""
    if not alive(pid):
        return False
    killer(pid, _signal.SIGTERM)
    deadline = _time.monotonic() + grace
    while _time.monotonic() < deadline:
        if not alive(pid):
            return True
        _time.sleep(0.05)
    if alive(pid):
        killer(pid, _signal.SIGKILL)
    return True
```

Add to `SessionOrchestrator` (`server/sessions/orchestrator.py`), importing at top
`from server.sessions import adopt as _adopt` and `from server.audit import record_audit`:

```python
    def _resolve_candidate(self, claude_session_id: str):
        from ccswitch import accounts as _acc
        known = [a["name"] for a in _acc.list_accounts()]
        tracked = {r.claude_session_id for r in self._records.values() if r.claude_session_id}
        for c in _adopt.discover_candidates(known, tracked):
            if c.claude_session_id == claude_session_id:
                return c
        return None

    async def adopt_session(self, claude_session_id: str, identity: str, killer=os.kill):
        cand = self._resolve_candidate(claude_session_id)
        if cand is None or not cand.mapped or cand.tracked:
            raise KeyError(claude_session_id)

        terminated_pid = None
        if cand.pid is not None:
            sent = await asyncio.to_thread(_adopt.terminate_process_tree, cand.pid, killer)
            if sent:
                terminated_pid = cand.pid
                async with db_session() as db:
                    await record_audit(db, "process.terminate", identity,
                                       {"pid": cand.pid, "claudeSessionId": claude_session_id})

        session_id = str(uuid.uuid4())
        cfg_dir = compose(cand.account)
        Path(cand.cwd).mkdir(parents=True, exist_ok=True)
        configdir.trust_project(cand.account, cand.cwd)

        def _build(resume_arg):
            return SessionDriver(
                session_id, cand.account, cand.cwd, str(cfg_dir),
                mode="interactive", sdk_factory=self._sdk_factory, registry=self._registry,
                identity=identity, resume=resume_arg,
                on_session_id=self._persist_claude_session_id,
                on_sync_back=functools.partial(ccswitch_accounts.sync_back, cand.account),
                on_status=self._persist_status)

        resumed = True
        driver = _build(claude_session_id)
        try:
            await driver.start()
        except Exception:
            log.warning("adopt resume failed sid=%s; starting fresh", claude_session_id, exc_info=True)
            try:
                await driver.aclose()
            except Exception:
                pass
            driver = _build(None)
            await driver.start()
            resumed = False
        self._drivers[session_id] = driver

        async with db_session() as db:
            db.add(DbSession(id=session_id, name=cand.name, account_name=cand.account,
                             claude_session_id=claude_session_id, status="idle", pid=None,
                             cwd=cand.cwd, repo=None, permission_mode="interactive",
                             name_is_auto=True, adopted_in_place=True))
            await record_audit(db, "session.adopt", identity,
                               {"sessionId": session_id, "account": cand.account,
                                "claudeSessionId": claude_session_id, "resumed": resumed})

        rec = SessionRecord(id=session_id, name=cand.name, account=cand.account,
                            claude_session_id=claude_session_id, cols=80, rows=24,
                            cwd=cand.cwd, repo=None, status="idle", adopted_in_place=True)
        self._records[session_id] = rec
        log.info("session.adopt id=%s sid=%s resumed=%s killed=%s",
                 session_id, claude_session_id, resumed, terminated_pid)
        return rec, {"resumed": resumed, "terminatedPid": terminated_pid}
```

- [ ] **Step 4: Run to verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_adopt_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/adopt.py server/sessions/orchestrator.py server/tests/test_adopt_session.py
git commit -m "feat(adopt): orchestrator.adopt_session — terminate + resume-in-place"
```

---

### Task 5: REST endpoints — GET /adoptable, POST /adopt

**Files:**
- Modify: `server/api/sessions.py`
- Test: `server/tests/test_adopt_api.py`

**Interfaces:**
- Consumes: `orch.list_sessions`, `_adopt.discover_candidates`, `orch.adopt_session` (Task 4), `_record_to_dict` (Task 3).
- Produces:
  - `GET /api/sessions/adoptable` → `{"candidates": [ {pid, account, cwd, claudeSessionId, name, mapped, tracked, skipReason, lastActivity, pidAlive}, ... ]}`
  - `POST /api/sessions/adopt` body `{"claudeSessionId": str}` → `201 {"session": {...}, "resumed": bool, "terminatedPid": int|None}`; `409` when not adoptable.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_adopt_api.py
import os
from unittest.mock import patch
from fastapi.testclient import TestClient
from datetime import datetime, timezone
from server.sessions.adopt import AdoptionCandidate

H = {"Tailscale-User-Login": "testuser@example.com"}

def _client():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
                                 "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        app = create_app(orchestrator=orch)
        return TestClient(app, raise_server_exceptions=False), orch

def _cand(sid, **kw):
    base = dict(pid=5, account="kindash", cwd="/home/u/repo", claude_session_id=sid,
                transcript_path="/x.jsonl", name="Work", mapped=True, tracked=False,
                skip_reason=None, last_activity=datetime.now(timezone.utc), pid_alive=True)
    base.update(kw); return AdoptionCandidate(**base)

def test_adoptable_requires_identity():
    client, _ = _client()
    with patch("server.api.sessions._adopt.discover_candidates", return_value=[]):
        assert client.get("/api/sessions/adoptable").status_code == 401

def test_adoptable_returns_flagged_candidates():
    client, _ = _client()
    cands = [_cand("aaa"), _cand("bbb", mapped=False, account=None, skip_reason="unmapped")]
    with patch("server.api.sessions._adopt.discover_candidates", return_value=cands):
        r = client.get("/api/sessions/adoptable", headers=H)
    assert r.status_code == 200
    body = r.json()["candidates"]
    assert body[0]["claudeSessionId"] == "aaa" and body[0]["skipReason"] is None
    assert body[1]["skipReason"] == "unmapped" and "lastActivity" in body[1]

def test_adopt_happy_path(monkeypatch):
    client, orch = _client()
    from server.sessions.types import SessionRecord
    rec = SessionRecord(id="newid", name="Work", account="kindash", claude_session_id="aaa",
                        cols=80, rows=24, cwd="/home/u/repo", adopted_in_place=True)
    async def fake_adopt(sid, identity, **kw):
        return rec, {"resumed": True, "terminatedPid": 5}
    monkeypatch.setattr(orch, "adopt_session", fake_adopt)
    r = client.post("/api/sessions/adopt", json={"claudeSessionId": "aaa"}, headers=H)
    assert r.status_code == 201
    assert r.json()["resumed"] is True and r.json()["terminatedPid"] == 5
    assert r.json()["session"]["adoptedInPlace"] is True

def test_adopt_conflict_when_not_adoptable(monkeypatch):
    client, orch = _client()
    async def fake_adopt(sid, identity, **kw):
        raise KeyError(sid)
    monkeypatch.setattr(orch, "adopt_session", fake_adopt)
    r = client.post("/api/sessions/adopt", json={"claudeSessionId": "gone"}, headers=H)
    assert r.status_code == 409
```

- [ ] **Step 2: Run to verify fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_adopt_api.py -v`
Expected: FAIL — 404 (routes missing).

- [ ] **Step 3: Implement**

In `server/api/sessions.py` add imports near the top:

```python
from server.sessions import adopt as _adopt
from ccswitch import accounts as _accounts
```

Add a request model with the others:

```python
class AdoptRequest(BaseModel):
    claudeSessionId: str
```

Add a candidate serializer near `_record_to_dict`:

```python
def _candidate_to_dict(c) -> dict[str, Any]:
    return {
        "pid": c.pid, "account": c.account, "cwd": c.cwd,
        "claudeSessionId": c.claude_session_id, "name": c.name,
        "mapped": c.mapped, "tracked": c.tracked, "skipReason": c.skip_reason,
        "lastActivity": c.last_activity.isoformat() if c.last_activity else None,
        "pidAlive": c.pid_alive,
    }
```

Add the routes (place ABOVE the `/{session_id}` routes so `adoptable`/`adopt` aren't captured as ids):

```python
@router.get("/adoptable")
async def list_adoptable(
    request: Request,
    _identity: str = Depends(current_identity),
) -> dict:
    orch = _get_orchestrator(request)
    known = [a["name"] for a in _accounts.list_accounts()]
    tracked = {r.claude_session_id for r in orch.list_sessions() if r.claude_session_id}
    cands = await asyncio.to_thread(_adopt.discover_candidates, known, tracked)
    return {"candidates": [_candidate_to_dict(c) for c in cands]}


@router.post("/adopt", status_code=status.HTTP_201_CREATED)
async def adopt_session(
    body: AdoptRequest,
    request: Request,
    identity: str = Depends(current_identity),
) -> dict:
    orch = _get_orchestrator(request)
    try:
        rec, outcome = await orch.adopt_session(body.claudeSessionId, identity=identity)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="session no longer adoptable")
    return {"session": _record_to_dict(rec), **outcome}
```

- [ ] **Step 4: Run to verify pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_adopt_api.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run full server suite + lint, then commit**

Run:
```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q
.venv/bin/python -m ruff check server/sessions/adopt.py server/api/sessions.py server/sessions/orchestrator.py
```
Expected: all pass; ruff clean.

```bash
git add server/api/sessions.py server/tests/test_adopt_api.py
git commit -m "feat(adopt): REST endpoints GET /adoptable + POST /adopt"
```

---

### Task 6: Dashboard "Untracked sessions" panel

**Files:**
- Create: `web/src/features/adopt/AdoptPanel.tsx`, `web/src/features/adopt/api.ts`
- Test: `web/src/features/adopt/AdoptPanel.test.tsx`
- Modify: the dashboard page to render `<AdoptPanel />` (follow the existing sessions-list import site).

**Interfaces:**
- Consumes: `GET /api/sessions/adoptable`, `POST /api/sessions/adopt` (Task 5).
- Produces: a panel that lists adoptable candidates with risk-scaled consent and a collapsed "Not adoptable (N)" subsection.

- [ ] **Step 1: Write the failing test (vitest + Testing Library)**

```tsx
// web/src/features/adopt/AdoptPanel.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { AdoptPanel } from "./AdoptPanel";

const cand = (over = {}) => ({
  pid: 5, account: "kindash", cwd: "/home/u/repo", claudeSessionId: "aaa",
  name: "Work", mapped: true, tracked: false, skipReason: null,
  lastActivity: new Date().toISOString(), pidAlive: true, ...over,
});

beforeEach(() => { vi.restoreAllMocks(); });

it("renders adoptable candidate with name and account, hides UUID in title attr", async () => {
  vi.spyOn(global, "fetch").mockResolvedValue({
    ok: true, json: async () => ({ candidates: [cand()] }),
  } as Response);
  render(<AdoptPanel />);
  expect(await screen.findByText("Work")).toBeInTheDocument();
  expect(screen.getByText(/kindash/)).toBeInTheDocument();
  expect(screen.queryByText("aaa")).not.toBeInTheDocument(); // UUID demoted
});

it("requires confirm before POSTing adopt", async () => {
  const fetchMock = vi.spyOn(global, "fetch")
    .mockResolvedValueOnce({ ok: true, json: async () => ({ candidates: [cand()] }) } as Response)
    .mockResolvedValueOnce({ ok: true, status: 201,
      json: async () => ({ session: { id: "n" }, resumed: true, terminatedPid: 5 }) } as Response);
  render(<AdoptPanel />);
  fireEvent.click(await screen.findByRole("button", { name: /take over|manage/i }));
  fireEvent.click(await screen.findByRole("button", { name: /stop & take over/i }));
  await waitFor(() =>
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/adopt", expect.objectContaining({ method: "POST" })));
});

it("groups unmapped into Not adoptable with plain copy", async () => {
  vi.spyOn(global, "fetch").mockResolvedValue({ ok: true,
    json: async () => ({ candidates: [cand({ mapped: false, account: null, skipReason: "unmapped" })] }),
  } as Response);
  render(<AdoptPanel />);
  expect(await screen.findByText(/Not adoptable/i)).toBeInTheDocument();
  expect(screen.getByText(/Not a managed account/i)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify fail**

Run (in `web/`): `npx vitest run src/features/adopt/AdoptPanel.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `api.ts`**

```ts
// web/src/features/adopt/api.ts
export interface Candidate {
  pid: number | null; account: string | null; cwd: string;
  claudeSessionId: string; name: string; mapped: boolean; tracked: boolean;
  skipReason: "unmapped" | "tracked" | null; lastActivity: string; pidAlive: boolean;
}
export async function fetchAdoptable(): Promise<Candidate[]> {
  const r = await fetch("/api/sessions/adoptable");
  if (!r.ok) throw new Error(`adoptable ${r.status}`);
  return (await r.json()).candidates;
}
export async function adopt(claudeSessionId: string) {
  const r = await fetch("/api/sessions/adopt", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ claudeSessionId }),
  });
  if (!r.ok) throw new Error(`adopt ${r.status}`);
  return r.json();
}
export const SKIP_COPY: Record<string, string> = {
  unmapped: "Not a managed account — running under an unmanaged config dir.",
  tracked: "Already managed by ccswitch.",
};
```

- [ ] **Step 4: Implement `AdoptPanel.tsx`**

```tsx
// web/src/features/adopt/AdoptPanel.tsx
import { useEffect, useState } from "react";
import { fetchAdoptable, adopt, SKIP_COPY, type Candidate } from "./api";

function relTime(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export function AdoptPanel() {
  const [cands, setCands] = useState<Candidate[]>([]);
  const [confirming, setConfirming] = useState<Candidate | null>(null);
  const [ack, setAck] = useState(false);
  const refresh = () => fetchAdoptable().then(setCands).catch(() => setCands([]));
  useEffect(() => { refresh(); }, []);

  const adoptable = cands.filter((c) => c.mapped && !c.tracked);
  const skipped = cands.filter((c) => !(c.mapped && !c.tracked));
  if (cands.length === 0) return <p className="text-sm text-muted">All running sessions are managed.</p>;

  return (
    <section>
      <h2>Untracked sessions</h2>
      {adoptable.map((c) => {
        const active = c.pidAlive;
        return (
          <div key={c.claudeSessionId} className="adopt-row" title={c.claudeSessionId}>
            <span className="adopt-name">{c.name}</span>
            <span className="adopt-meta">{c.account} · {c.cwd} · {relTime(c.lastActivity)} · PID {c.pid}</span>
            <button onClick={() => { setAck(false); setConfirming(c); }}>
              {active ? "Stop & take over" : "Manage this session"}
            </button>
          </div>
        );
      })}

      {skipped.length > 0 && (
        <details>
          <summary>Not adoptable ({skipped.length})</summary>
          {skipped.map((c) => (
            <div key={c.claudeSessionId} className="adopt-row adopt-skipped">
              <span>{c.name}</span>
              <span className="adopt-skip">{c.skipReason ? SKIP_COPY[c.skipReason] : ""}</span>
            </div>
          ))}
        </details>
      )}

      {confirming && (
        <div role="dialog" aria-label="Take over this session?">
          <p>
            ccswitch will stop the <code>claude</code> process running in <b>{confirming.cwd}</b>
            {confirming.pid != null && <> (PID {confirming.pid})</>} under <b>{confirming.account}</b> and
            resume <b>"{confirming.name}"</b> here. Any unsaved work in that terminal will be lost.
            This can't be undone.
          </p>
          {confirming.pidAlive && (
            <label>
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              I understand this will stop PID {confirming.pid}.
            </label>
          )}
          <button onClick={() => setConfirming(null)}>Cancel</button>
          <button
            disabled={confirming.pidAlive && !ack}
            onClick={async () => {
              await adopt(confirming.claudeSessionId);
              setConfirming(null);
              refresh();
            }}
          >
            Stop & take over
          </button>
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 5: Run tests + typecheck + lint, wire into dashboard, commit**

Run (in `web/`):
```bash
npx vitest run src/features/adopt/AdoptPanel.test.tsx
npx tsc --noEmit
npx eslint src/features/adopt
```
Expected: tests pass, no type/lint errors. Then add `<AdoptPanel />` to the dashboard page beside the sessions list.

```bash
git add web/src/features/adopt web/src/<dashboard-page>
git commit -m "feat(adopt): dashboard Untracked sessions panel with risk-scaled consent"
```

---

## Self-Review

**Spec coverage:**
- Discovery + classification → Task 1; account mapping + transcript + `last_activity` + dedup → Task 2; `adopted_in_place` model/record → Task 3; terminate + resume-in-place + outcome → Task 4; API (`/adoptable`, `/adopt`) + audit → Tasks 4–5; dashboard panel + risk-scaled consent + skip copy + UUID demotion → Task 6. All spec sections map to a task.
- UX MUST-FIX #1 (named risk-scaled confirm) → Task 6; #2 (`last_activity`/`pid_alive`) → Tasks 2/5/6; #3 (honest outcome `resumed`/`terminatedPid`, SIGTERM grace) → Tasks 4/5/6. UX SHOULD #5 copy → Task 6 `SKIP_COPY`.

**Placeholder scan:** No TBD/TODO; every code step shows full code.

**Type consistency:** `AdoptionCandidate` field names identical across Tasks 2/4/5; wire keys (`claudeSessionId`, `skipReason`, `lastActivity`, `pidAlive`, `adoptedInPlace`) consistent between Task 5 serializers and Task 6 `Candidate` interface; `adopt_session` returns `(SessionRecord, {"resumed","terminatedPid"})` in Tasks 4 and 5.

**Known follow-ups (not blocking):** routes `/adoptable` and `/adopt` must be registered before `/{session_id}` (noted in Task 5); the "scanned Ns ago"/Refresh affordance and transcript-tail preview (UX SHOULD #4/#6 nice-to-haves) are left for a polish increment.
