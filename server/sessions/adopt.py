"""Discovery + classification of live `claude` processes for adoption.

Linux /proc only (the deploy target). A "session" candidate is an interactive
claude process NOT owned by ccswitch (it does not speak the SDK stream-json
protocol) and not an internal helper (pty-host / spare / daemon / auth login).

The discovery functions are pure-ish: process enumeration and the account-dir
resolver are injectable so the logic is unit-testable without a real /proc.
"""
from __future__ import annotations

import glob
import json
import os
import signal as _signal
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone

from ccswitch import paths as _paths

_HELPER_FLAGS = ("--bg-pty-host", "--bg-spare")


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    cmdline: list[str]
    environ: dict[str, str]
    cwd: str | None


@dataclass(frozen=True)
class AdoptionCandidate:
    pid: int | None
    account: str | None          # resolved ccswitch account, or None if unmapped
    cwd: str
    claude_session_id: str
    transcript_path: str
    name: str
    mapped: bool
    tracked: bool
    skip_reason: str | None      # "unmapped" | "tracked" | None
    # UX MUST-FIX #2 — informed-consent risk signal. The danger of force-takeover
    # is unsaved/in-flight work; the user can't judge that without knowing how
    # recently the session did anything. Derived from the transcript .jsonl mtime.
    last_activity: datetime
    pid_alive: bool              # process present at discovery (re-checked at adopt)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(cmdline: list[str]) -> str:
    """Bucket a process by argv: 'sdk' | 'helper' | 'session' | 'other'.

    - 'sdk':     ccswitch's own SDK driver (speaks --input-format stream-json) → already tracked
    - 'helper':  internal claude helper (pty-host / spare / daemon run / auth login)
    - 'session': an interactive claude process eligible for adoption
    - 'other':   not a claude process
    """
    if not cmdline:
        return "other"
    is_claude = any("claude" in part for part in cmdline)
    if not is_claude:
        return "other"
    # ccswitch's own SDK driver speaks stream-json on stdin — already tracked.
    if "--input-format" in cmdline and "stream-json" in cmdline:
        return "sdk"
    if any(flag in cmdline for flag in _HELPER_FLAGS):
        return "helper"
    if "daemon" in cmdline and "run" in cmdline:
        return "helper"
    if "auth" in cmdline and "login" in cmdline:
        return "helper"
    return "session"


# ---------------------------------------------------------------------------
# /proc enumeration (not unit-tested directly — injected in tests)
# ---------------------------------------------------------------------------


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
                        key, value = entry.split("=", 1)
                        environ[key] = value
        except (PermissionError, FileNotFoundError):
            pass
        try:
            cwd = os.readlink(f"{base}/cwd")
        except (PermissionError, FileNotFoundError, OSError):
            cwd = None
        return ProcInfo(pid=pid, cmdline=cmdline, environ=environ, cwd=cwd)
    except (FileNotFoundError, ProcessLookupError):
        return None


def _proc_lister() -> list[ProcInfo]:
    out: list[ProcInfo] = []
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except FileNotFoundError:
        return out
    for pid in pids:
        info = _read_proc(pid)
        if info is not None:
            out.append(info)
    return out


# ---------------------------------------------------------------------------
# Account mapping + transcript resolution
# ---------------------------------------------------------------------------


def _read_transcript_title(path: str) -> str | None:
    """Best-effort read of a human-friendly title embedded in the transcript.

    Claude writes ``summary`` / ``title`` / ``customTitle`` entries into the jsonl;
    we return the last such value. Self-contained (no dependency on the title
    module) so discovery stays robust to auto-naming changes. Returns None on any
    parse error — the caller falls back to the cwd basename.
    """
    title: str | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key in ("summary", "customTitle", "title"):
                    val = obj.get(key) if isinstance(obj, dict) else None
                    if isinstance(val, str) and val.strip():
                        title = val.strip()
    except OSError:
        return None
    return title


def _map_account(config_dir: str, known_accounts: list[str], account_cfg) -> str | None:
    """Reverse-map a CLAUDE_CONFIG_DIR to a managed account by realpath equality."""
    target = os.path.realpath(config_dir)
    for name in known_accounts:
        if os.path.realpath(str(account_cfg(name))) == target:
            return name
    return None


def _resolve_transcript(config_dir: str, cwd: str) -> tuple[str, str, datetime] | None:
    """Return (claude_session_id, transcript_path, mtime) for the most-recently
    modified transcript whose project dir matches cwd, else None.

    Claude names a project dir by replacing '/' and '.' with '-' and keeping the
    leading slash (→ leading dash). We restrict the glob to that project dir, then
    pick the freshest .jsonl as the conversation the live process is driving.
    """
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


def discover_candidates(
    known_accounts: list[str],
    tracked_sids: set[str],
    proc_lister=_proc_lister,
    account_cfg=_paths.account_cfg_dir,
) -> list[AdoptionCandidate]:
    """Enumerate live claude sessions and flag each for adoptability.

    Returns ALL session candidates (mapped, unmapped, already-tracked), each with
    a skip_reason where applicable — nothing is silently dropped. Adoptable =
    ``mapped and not tracked``. Candidates whose transcript can't be resolved are
    excluded (never adopt a conversation we can't locate).
    """
    out: list[AdoptionCandidate] = []
    home_default = os.path.expanduser("~/.claude")
    for proc in proc_lister():
        if classify(proc.cmdline) != "session":
            continue
        cwd = proc.cwd
        if not cwd:
            continue
        config_dir = proc.environ.get("CLAUDE_CONFIG_DIR", home_default)
        resolved = _resolve_transcript(config_dir, cwd)
        if resolved is None:
            continue
        sid, tpath, mtime = resolved
        account = _map_account(config_dir, known_accounts, account_cfg)
        mapped = account is not None
        tracked = sid in tracked_sids
        if mapped and not tracked:
            skip_reason = None
        elif tracked:
            skip_reason = "tracked"
        else:
            skip_reason = "unmapped"
        title = _read_transcript_title(tpath) or os.path.basename(cwd.rstrip("/")) or "session"
        out.append(
            AdoptionCandidate(
                pid=proc.pid,
                account=account,
                cwd=cwd,
                claude_session_id=sid,
                transcript_path=tpath,
                name=title,
                mapped=mapped,
                tracked=tracked,
                skip_reason=skip_reason,
                last_activity=mtime,
                pid_alive=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Process termination (force-takeover)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_tree(pid: int, killer=os.kill, alive=_pid_alive, grace: float = 1.5) -> bool:
    """SIGTERM, wait a grace period for a clean flush, then SIGKILL if still alive.

    Returns True if any signal was sent. A PID that is already gone is a no-op
    success (returns False — nothing to terminate).
    """
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
