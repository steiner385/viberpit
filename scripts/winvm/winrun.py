#!/usr/bin/env python3
"""winrun: run a command inside the Windows VM's logged-on interactive desktop.

    winrun [--timeout S] [--cwd WINPATH] [--lock-wait S] [--ps] -- <command...>

A plain `ssh win11 <cmd>` runs in session 0, where there is no desktop (GetForegroundWindow is
0), so UI automation cannot work there. winrun instead registers a one-shot scheduled task as the
logged-on user (LogonType Interactive, RunLevel Highest), which runs in the desktop session. The
task's stdout/stderr go to C:\\winrun\\<id>\\out and err, and its exit code to rc. winrun polls
those over ssh, relays the output as it arrives, then unregisters the task and deletes the dir.

The command is a program plus arguments (quoted for Windows with the MS C runtime rules). With
--ps, the words are joined into one PowerShell script instead (pipes, cmdlets). Nothing the
caller passes is ever interpolated into PowerShell source: it travels base64-encoded.

Only one winrun drives the desktop at a time: an flock on ~/.cache/viberpit/winvm.lock, waited on
for at most --lock-wait seconds.

Exit status: the remote command's, or 75 desktop busy, 124 timed out, 130 interrupted,
143 / 129 on SIGTERM / SIGHUP, 255 VM unreachable or the desktop task failed. 2 for a usage error.
Environment: VIBERPIT_WINDOWS_VM_SSH_HOST (ssh host alias, default win11);
WINRUN_NO_RESULT_GRACE_S (seconds a task may sit not running without an exit code, default 20).
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import _winvm

TOOL = "winrun"
EXIT_BUSY = 75
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130
LOCK_PATH = Path("~/.cache/viberpit/winvm.lock")
TASK_PATH = "\\viberpit\\"
REMOTE_ROOT = "C:\\winrun"
POLL_INTERVAL_S = 0.5
TASK_LIMIT_MARGIN_S = 300      # the VM-side task time limit is --timeout plus this backstop margin
CHUNK_BYTES = 1 << 20          # max bytes of each stream relayed per poll
SSH_CALL_TIMEOUT_S = 120       # a single ssh round-trip that hangs past this counts as failed
MAX_POLL_FAILURES = 5          # consecutive failed polls before giving up on the VM


class _Terminated(BaseException):
    """SIGTERM or SIGHUP, turned into an exception so the cleanup in `finally` still runs."""

    def __init__(self, signum: int = signal.SIGTERM):
        super().__init__(signum)
        self.signum = signum


# ------------------------------------------------------------------------- PowerShell sources
# Only `rid` (hex we generate), integers and base64 are ever formatted into these. Each one's
# first line is a `# winrun:<phase>` marker (the tests' ssh shim keys on it).

_RUN_PS1 = r"""# winrun:task
$ErrorActionPreference = 'Stop'
$d = '{root}\{rid}'
$rc = 255
try {{
  $spec = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{spec}')) | ConvertFrom-Json
  $sp = @{{ FilePath = $spec.file; NoNewWindow = $true; PassThru = $true
           RedirectStandardOutput = "$d\out"; RedirectStandardError = "$d\err"
           WorkingDirectory = $env:USERPROFILE }}
  if ($spec.args) {{ $sp.ArgumentList = $spec.args }}
  if ($spec.script) {{
    # --ps: run from a file. -EncodedCommand with a redirected stderr makes Windows PowerShell
    # 5.1 serialize the error stream as CLIXML; -File keeps it plain text. BOM = read as UTF-8.
    $f = "$d\cmd.ps1"
    [IO.File]::WriteAllText($f, $spec.script, (New-Object Text.UTF8Encoding $true))
    $sp.ArgumentList = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$f`""
  }}
  if ($spec.cwd) {{ $sp.WorkingDirectory = $spec.cwd }}
  $p = Start-Process @sp
  $null = $p.Handle  # keeps ExitCode readable after exit
  Set-Content -LiteralPath "$d\pid" -Value $p.Id -Encoding ascii
  $p.WaitForExit()
  $rc = $p.ExitCode
}} catch {{
  [IO.File]::AppendAllText("$d\err", "winrun: " + ($_ | Out-String))
  $rc = 127
}}
Set-Content -LiteralPath "$d\rc.tmp" -Value $rc -Encoding ascii
Move-Item -LiteralPath "$d\rc.tmp" -Destination "$d\rc" -Force
"""

_SETUP_PS1 = r"""# winrun:setup
# The host lock means no other winrun is live: anything left under \viberpit\ is a leftover
# from a run whose cleanup never reached the VM (SIGKILL, lost link). Stopping its task only
# kills the wrapper powershell.exe, so the command it started is killed by its recorded pid.
$ErrorActionPreference = 'SilentlyContinue'
Get-ScheduledTask -TaskPath '{task_path}' | ForEach-Object {{
  Stop-ScheduledTask -InputObject $_
  Unregister-ScheduledTask -InputObject $_ -Confirm:$false
}}
Get-ChildItem -Path '{root}\*\pid' | ForEach-Object {{
  $opid = (Get-Content -LiteralPath $_.FullName -Raw).Trim()
  $op = if ($opid) {{ Get-Process -Id $opid }} else {{ $null }}
  # Only a process older than the pid file is the orphan; a newer one reuses a recycled pid.
  if ($op -and $op.StartTime -le $_.LastWriteTime) {{ taskkill.exe /PID $opid /T /F 2>&1 | Out-Null }}
}}
for ($i = 0; $i -lt 10 -and (Test-Path -Path '{root}\*'); $i++) {{
  Remove-Item -Path '{root}\*' -Recurse -Force
  if (Test-Path -Path '{root}\*') {{ Start-Sleep -Milliseconds 200 }}  # the killed tree may still hold out/err
}}
$ErrorActionPreference = 'Stop'
$d = '{root}\{rid}'
New-Item -ItemType Directory -Force -Path $d | Out-Null
[IO.File]::WriteAllText("$d\run.ps1", [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{run}')))
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $d\run.ps1"
# The ssh user is the auto-logged-on desktop user (viber), so the task lands in that desktop.
$pr = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ({time_limit}) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskPath '{task_path}' -TaskName 'winrun-{rid}' -Action $a -Principal $pr -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskPath '{task_path}' -TaskName 'winrun-{rid}'
"""

_POLL_PS1 = r"""# winrun:poll
$d = '{root}\{rid}'
$outOff = {out_off}
$errOff = {err_off}
$t = Get-ScheduledTask -TaskPath '{task_path}' -TaskName 'winrun-{rid}' -ErrorAction SilentlyContinue
$state = if ($t) {{ [string]$t.State }} else {{ 'Missing' }}
# rc is read BEFORE the streams, so once it exists the chunks below hold the final output.
$rc = $null
if (Test-Path -LiteralPath "$d\rc") {{ $rc = [int]((Get-Content -LiteralPath "$d\rc" -Raw).Trim()) }}
function Read-Chunk([string]$path, [long]$off) {{
  if (-not (Test-Path -LiteralPath $path)) {{ return @{{ size = 0; data = '' }} }}
  $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite, Delete')
  try {{
    $size = $fs.Length
    $n = [int][Math]::Min([long]{chunk}, [Math]::Max([long]0, $size - $off))
    $buf = New-Object byte[] $n
    $null = $fs.Seek($off, 'Begin')
    $got = 0
    while ($got -lt $n) {{ $r = $fs.Read($buf, $got, $n - $got); if ($r -le 0) {{ break }}; $got += $r }}
    return @{{ size = $size; data = [Convert]::ToBase64String($buf, 0, $got) }}
  }} finally {{ $fs.Close() }}
}}
$o = Read-Chunk "$d\out" $outOff
$e = Read-Chunk "$d\err" $errOff
[pscustomobject]@{{ state = $state; rc = $rc; out = $o.data; out_size = $o.size; err = $e.data; err_size = $e.size }} | ConvertTo-Json -Compress
"""

_CLEANUP_PS1 = r"""# winrun:cleanup
$ErrorActionPreference = 'SilentlyContinue'
$d = '{root}\{rid}'
$stop = {stop}
if ($stop) {{
  Stop-ScheduledTask -TaskPath '{task_path}' -TaskName 'winrun-{rid}'
  if (Test-Path -LiteralPath "$d\pid") {{
    $cpid = (Get-Content -LiteralPath "$d\pid" -Raw).Trim()
    if ($cpid) {{ taskkill.exe /PID $cpid /T /F 2>&1 | Out-Null }}
  }}
}}
Unregister-ScheduledTask -TaskPath '{task_path}' -TaskName 'winrun-{rid}' -Confirm:$false
for ($i = 0; $i -lt 10 -and (Test-Path -LiteralPath $d); $i++) {{
  Remove-Item -LiteralPath $d -Recurse -Force
  if (Test-Path -LiteralPath $d) {{ Start-Sleep -Milliseconds 200 }}  # the killed tree may still hold out/err
}}
"""


def _fmt(template: str, rid: str, **kw: object) -> str:
    return template.format(root=REMOTE_ROOT, task_path=TASK_PATH, rid=rid, **kw)


def build_spec(command: list[str], *, ps: bool, cwd: str | None) -> dict:
    """What the desktop task starts: ``file`` plus a ready-made Windows argument string."""
    if ps:
        # The task wrapper writes `script` to a .ps1 and runs it with -File (see _RUN_PS1): the
        # -EncodedCommand form made stderr CLIXML ("#< CLIXML <Objs ...>"), which the first real
        # env:windows session had to grep out. Quiet progress so no progress records reach it either.
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
                  "$ProgressPreference = 'SilentlyContinue'\n"
                  + " ".join(command) + "\n"
                  # bash-like status: the last statement's success, a native exit code if it set one
                  "$__ok = $?\nif (-not $__ok) { if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }\nexit 0\n")
        spec = {"file": "powershell.exe", "script": script}
    else:
        spec = {"file": command[0], "args": subprocess.list2cmdline(command[1:])}
    if cwd:
        spec["cwd"] = cwd
    return spec


def run_script(rid: str, spec: dict) -> str:
    return _fmt(_RUN_PS1, rid, spec=_winvm.b64_utf8(json.dumps(spec)))


def setup_script(rid: str, spec: dict, timeout_s: float | None) -> str:
    """``timeout_s`` None means no limit; otherwise the task is bounded VM-side as a backstop."""
    time_limit = ("[TimeSpan]::Zero" if timeout_s is None
                  else f"[TimeSpan]::FromSeconds({math.ceil(timeout_s) + TASK_LIMIT_MARGIN_S})")
    return _fmt(_SETUP_PS1, rid, run=_winvm.b64_utf8(run_script(rid, spec)), time_limit=time_limit)


# ------------------------------------------------------------------------------------- lock


def _acquire_lock(wait_s: float) -> int | None:
    """Take the host-wide desktop lock, or return None after ``wait_s`` seconds."""
    path = LOCK_PATH.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_s
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            if not announced:
                print(f"{TOOL}: waiting up to {wait_s:g}s for the desktop lock (held by {_holder(path)})",
                      file=sys.stderr)
                announced = True
            time.sleep(0.25)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, f"pid {os.getpid()}\n".encode())
        return fd


def _holder(path: Path) -> str:
    try:
        return path.read_text().strip() or "another winrun"
    except OSError:
        return "another winrun"


# ---------------------------------------------------------------------------------- the run


def _write(stream, b64: str) -> int:
    data = base64.b64decode(b64) if b64 else b""
    if data:
        stream.buffer.write(data)
        stream.flush()
    return len(data)


def _poll(rid: str, out_off: int, err_off: int, timeout_s: float) -> dict | None:
    try:
        proc = _winvm.run_ps(_fmt(_POLL_PS1, rid, out_off=out_off, err_off=err_off, chunk=CHUNK_BYTES),
                             timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _drive(rid: str, timeout_s: float | None) -> int:
    """Poll the task until it reports an exit code; relay its output. Returns the exit status."""
    grace = float(os.environ.get("WINRUN_NO_RESULT_GRACE_S") or 20)
    started = time.monotonic()
    out_off = err_off = failures = 0
    not_running_since: float | None = None
    while True:
        call_timeout: float = SSH_CALL_TIMEOUT_S
        if timeout_s is not None:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            call_timeout = min(call_timeout, remaining)  # a hung poll must not outlive --timeout
        status = _poll(rid, out_off, err_off, call_timeout)
        if status is None:
            failures += 1
            if failures >= MAX_POLL_FAILURES:
                return _winvm.die(TOOL, f"lost contact with {_winvm.ssh_host()} while the command ran",
                                  _winvm.EXIT_UNREACHABLE)
            time.sleep(POLL_INTERVAL_S)
            continue
        failures = 0
        out_off += _write(sys.stdout, status.get("out", ""))
        err_off += _write(sys.stderr, status.get("err", ""))
        # A failed Read-Chunk reports a null size: treat it as nothing more to fetch this poll.
        drained = out_off >= (status.get("out_size") or 0) and err_off >= (status.get("err_size") or 0)
        if status.get("rc") is not None:
            if drained:
                return int(status["rc"])
            continue  # more output than one poll carries: fetch the rest straight away
        if status.get("state") in ("Running", "Queued"):
            not_running_since = None
        else:
            not_running_since = not_running_since or time.monotonic()
            if time.monotonic() - not_running_since >= grace:
                return _winvm.die(TOOL, f"the desktop task ended without an exit code "
                                        f"(state {status.get('state')}); is the desktop user logged on?",
                                  _winvm.EXIT_UNREACHABLE)
        if drained:
            time.sleep(POLL_INTERVAL_S)


def _cleanup(rid: str, *, stop: bool) -> None:
    try:
        proc = _winvm.run_ps(_fmt(_CLEANUP_PS1, rid, stop="$true" if stop else "$false"),
                             timeout=SSH_CALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc = None
    if proc is None or proc.returncode != 0:
        # The next winrun's setup sweeps leftovers, so this is a warning, not a failure.
        print(f"{TOOL}: could not clean up task winrun-{rid}; the next winrun will", file=sys.stderr)


def _run(args: argparse.Namespace) -> int:
    rid = uuid.uuid4().hex[:12]
    spec = build_spec(args.command, ps=args.ps, cwd=args.cwd)
    cleanup, stop = True, True
    try:
        try:
            setup = _winvm.run_ps(setup_script(rid, spec, args.timeout or None), timeout=SSH_CALL_TIMEOUT_S)
        except _winvm.ScriptTooLong as exc:
            cleanup = False  # refused before connecting
            return _winvm.die(TOOL, f"command too long to send ({exc}); put it in a script, winsync it, "
                                    "and winrun the script", 2)
        except subprocess.TimeoutExpired:
            return _winvm.die(TOOL, f"{_winvm.ssh_host()} did not answer within {SSH_CALL_TIMEOUT_S}s",
                              _winvm.EXIT_UNREACHABLE)
        if setup.returncode == _winvm.EXIT_UNREACHABLE:
            cleanup = False  # never connected, so nothing was created: no second 5 s round-trip
            return _winvm.unreachable(TOOL, setup)
        if setup.returncode != 0:
            return _winvm.die(TOOL, f"could not start the desktop task ({_winvm.ssh_diagnostic(setup)})",
                              _winvm.EXIT_UNREACHABLE)
        try:
            rc = _drive(rid, args.timeout or None)
        except TimeoutError:
            return _winvm.die(TOOL, f"timed out after {args.timeout:g}s; stopped the command",
                              EXIT_TIMEOUT)
        stop = False
        if not 0 <= rc <= 255:
            # A shell sees only the low byte: 256 would read as success. NTSTATUS crash codes land here.
            return _winvm.die(TOOL, f"remote exit code {rc} (0x{rc & 0xFFFFFFFF:08X}) does not fit an "
                                    "exit status; exiting 1", 1)
        return rc
    except KeyboardInterrupt:
        return _winvm.die(TOOL, "interrupted; stopped the command", EXIT_INTERRUPTED)
    except _Terminated as exc:
        return _winvm.die(TOOL, f"terminated ({signal.Signals(exc.signum).name}); stopped the command", 128 + exc.signum)
    finally:
        if cleanup:
            _cleanup(rid, stop=stop)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=TOOL, formatter_class=argparse.RawDescriptionHelpFormatter,
        description=_winvm.help_description(__doc__),
        usage="%(prog)s [--timeout S] [--cwd WINPATH] [--lock-wait S] [--ps] -- <command...>")
    p.add_argument("--timeout", type=float, default=3600, metavar="S",
                   help="stop the command after S seconds and exit 124 (default 3600; 0 = no limit)")
    p.add_argument("--cwd", metavar="WINPATH", help="working directory in the VM (default: the user profile)")
    p.add_argument("--lock-wait", type=float, default=900, metavar="S",
                   help="wait at most S seconds for the desktop lock, then exit 75 (default 900)")
    p.add_argument("--ps", action="store_true", help="treat the command words as one PowerShell script")
    p.add_argument("command", nargs=argparse.REMAINDER, help="program and arguments, after --")
    args = p.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        p.error("no command given (usage: winrun [options] -- <command...>)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    def _on_term(signum, frame):
        raise _Terminated(signum)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGHUP, _on_term)  # a closed terminal or session must still clean up
    try:
        fd = _acquire_lock(args.lock_wait)
    except (KeyboardInterrupt, _Terminated):
        return _winvm.die(TOOL, "interrupted while waiting for the desktop lock", EXIT_INTERRUPTED)
    if fd is None:
        return _winvm.die(TOOL, f"desktop busy: another winrun has held the desktop lock for over "
                                f"{args.lock_wait:g}s; wait and retry", EXIT_BUSY)
    try:
        return _run(args)
    finally:
        os.close(fd)  # releases the flock


if __name__ == "__main__":
    sys.exit(main())
