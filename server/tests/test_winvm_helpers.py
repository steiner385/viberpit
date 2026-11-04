"""winrun / winsync host helpers (scripts/winvm/), tested without a VM.

`ssh` and `scp` are replaced by a PATH shim that records argv (and the decoded
PowerShell each call sends) and plays back canned responses per phase. Every
script the helpers send carries a `# winrun:<phase>` / `# winsync:<phase>`
marker on its first line; that is how the shim tells setup, poll and cleanup apart.
"""
from __future__ import annotations

import base64
import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "winvm"
WINRUN = SCRIPTS / "winrun.py"
WINSYNC = SCRIPTS / "winsync.py"

_SHIM = r'''#!{python}
import base64, json, os, re, shutil, sys, time
d = os.environ["SHIM_DIR"]
argv = sys.argv
tool = os.path.basename(argv[0])
script = ""
if tool == "ssh" and "-EncodedCommand" in argv:
    script = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
m = re.search(r"#\s*(win\w+:\w+)", script)
phase = m.group(1) if m else tool
if tool == "scp":
    n = len([f for f in os.listdir(d) if f.startswith("scp-")])
    shutil.copy(argv[-2], os.path.join(d, "scp-%d.tar" % n))
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({{"tool": tool, "argv": argv[1:], "phase": phase, "script": script}}) + "\n")
resp_file = os.path.join(d, "responses", phase.replace(":", "_") + ".json")
entry = {{}}
if os.path.exists(resp_file):
    entries = json.load(open(resp_file))
    counter = resp_file + ".n"
    i = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(i + 1))
    entry = entries[min(i, len(entries) - 1)]
time.sleep(entry.get("sleep", 0))
sys.stdout.write(entry.get("stdout", ""))
sys.stderr.write(entry.get("stderr", ""))
sys.exit(entry.get("rc", 0))
'''


class Shim:
    def __init__(self, root: Path):
        self.dir = root / "shim"
        (self.dir / "responses").mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        for tool in ("ssh", "scp"):
            p = self.bin / tool
            p.write_text(_SHIM.format(python=sys.executable))
            p.chmod(0o755)
        self.home = root / "home"
        self.home.mkdir()

    def respond(self, phase: str, *entries: dict) -> None:
        (self.dir / "responses" / (phase.replace(":", "_") + ".json")).write_text(json.dumps(entries))

    def calls(self) -> list[dict]:
        log = self.dir / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def env(self, **extra: str) -> dict:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "SHIM_DIR": str(self.dir),
            "LANG": "C.UTF-8",
        }
        env.update(extra)
        return env

    def run(self, script: Path, *args: str, cwd: Path | None = None, timeout: float = 60,
            **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), *args], env=self.env(**extra),
                              cwd=cwd, capture_output=True, timeout=timeout)


@pytest.fixture
def shim(tmp_path):
    return Shim(tmp_path)


def _poll(rc=None, out=b"", err=b"", state="Running", out_size=None, err_size=None) -> dict:
    return {"stdout": json.dumps({
        "state": state, "rc": rc,
        "out": base64.b64encode(out).decode(), "out_size": len(out) if out_size is None else out_size,
        "err": base64.b64encode(err).decode(), "err_size": len(err) if err_size is None else err_size,
    }) + "\n"}


def _import(path: Path):
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(f"_test_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(path.parent))


# --------------------------------------------------------------------------------------- winrun


def test_winrun_returns_remote_exit_code(shim):
    shim.respond("winrun:poll", _poll(), _poll(rc=3, out=b"hello\n", err=b"warn\n", state="Ready"))
    r = shim.run(WINRUN, "--", "python", "-c", "import sys; sys.exit(3)")
    assert r.returncode == 3, r.stderr
    assert r.stdout == b"hello\n"
    assert b"warn\n" in r.stderr
    phases = [c["phase"] for c in shim.calls()]
    assert phases[0] == "winrun:setup"
    assert phases[-1] == "winrun:cleanup"
    assert "winrun:poll" in phases
    for c in shim.calls():
        assert "ConnectTimeout=5" in c["argv"]
        assert "win11" in c["argv"]
    setup = shim.calls()[0]["script"]
    assert "-LogonType Interactive" in setup
    assert "Start-ScheduledTask" in setup
    cleanup = shim.calls()[-1]["script"]
    assert "Unregister-ScheduledTask" in cleanup
    assert "Remove-Item" in cleanup


def test_winrun_streams_output_across_polls_and_honours_host_env(shim):
    shim.respond("winrun:poll", _poll(out=b"one\n"), _poll(rc=0, out=b"two\n", state="Ready"))
    r = shim.run(WINRUN, "--", "cmd", VIBERPIT_WINDOWS_VM_SSH_HOST="otherwin")
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"one\ntwo\n"
    assert all("otherwin" in c["argv"] for c in shim.calls())
    polls = [c["script"] for c in shim.calls() if c["phase"] == "winrun:poll"]
    assert "$outOff = 4" in polls[1]  # the second poll resumes after the 4 bytes already relayed


def test_winrun_unreachable_vm_fails_fast(shim):
    shim.respond("winrun:setup", {"rc": 255, "stderr":
                                  "ssh: connect to host 192.168.122.76 port 22: Connection timed out\n"})
    t0 = time.monotonic()
    r = shim.run(WINRUN, "--", "python", "-V")
    assert time.monotonic() - t0 < 5
    assert r.returncode == 255
    err = r.stderr.decode()
    assert "cannot reach win11" in err
    assert len(err.strip().splitlines()) == 1
    calls = shim.calls()
    assert len(calls) == 1  # no polling, no cleanup round-trip against a dead VM
    assert "ConnectTimeout=5" in calls[0]["argv"] and "BatchMode=yes" in calls[0]["argv"]


def test_winrun_lock_serializes_and_times_out(shim):
    lock = shim.home / ".cache" / "viberpit" / "winvm.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        t0 = time.monotonic()
        r = shim.run(WINRUN, "--lock-wait", "1", "--", "python", "-V")
        waited = time.monotonic() - t0
    assert r.returncode == 75
    assert b"winrun: desktop busy" in r.stderr
    assert 1 <= waited < 10
    assert shim.calls() == []  # never touched the VM without the lock
    # Released: the next caller goes straight through.
    shim.respond("winrun:poll", _poll(rc=0, state="Ready"))
    assert shim.run(WINRUN, "--lock-wait", "1", "--", "python", "-V").returncode == 0


def test_winrun_never_echoes_secret_paths_in_output(shim):
    key = shim.home / ".ssh" / "win11_ed25519"
    key.parent.mkdir()
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    key.write_text(pem + "\n")
    secret = shim.home / ".config" / "viberpit" / "win11-vm.secret"
    secret.parent.mkdir(parents=True)
    secret.write_text("hunter2-password\n")
    shim.respond("winrun:setup", {"rc": 255, "stderr": (
        f'Load key "{key}": bad permissions\n{pem}\n'
        f"warning: {secret} is world readable\n"
        "Permission denied (publickey).\n")})
    r = shim.run(WINRUN, "--", "python", "-V")
    assert r.returncode != 0
    shown = (r.stdout + r.stderr).decode()
    for leak in (str(key), "win11_ed25519", str(secret), "win11-vm.secret", "PRIVATE KEY",
                 "b3BlbnNzaC1rZXktdjEAAAAA", "hunter2"):
        assert leak not in shown
    assert "Permission denied" in shown  # the useful part of the diagnostic survives
    for c in shim.calls():
        assert not any("win11_ed25519" in a or "win11-vm.secret" in a for a in c["argv"])


def test_winrun_timeout_stops_the_task_and_exits_124(shim):
    shim.respond("winrun:poll", _poll(out=b"partial\n"), _poll())
    r = shim.run(WINRUN, "--timeout", "1", "--", "python", "-c", "while True: pass")
    assert r.returncode == 124
    assert b"timed out" in r.stderr
    assert b"partial\n" in r.stdout
    cleanup = [c for c in shim.calls() if c["phase"] == "winrun:cleanup"]
    assert len(cleanup) == 1
    assert "$stop = $true" in cleanup[0]["script"]
    assert "Stop-ScheduledTask" in cleanup[0]["script"] and "taskkill" in cleanup[0]["script"]


def test_winrun_interrupt_still_unregisters_the_task(shim):
    shim.respond("winrun:poll", _poll())
    proc = subprocess.Popen([sys.executable, str(WINRUN), "--", "python", "-V"], env=shim.env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 20
    while not any(c["phase"] == "winrun:poll" for c in shim.calls()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    cleanup = [c for c in shim.calls() if c["phase"] == "winrun:cleanup"]
    assert len(cleanup) == 1 and "$stop = $true" in cleanup[0]["script"]


def test_winrun_task_that_never_reports_fails_instead_of_hanging(shim):
    shim.respond("winrun:poll", _poll(state="Ready"))
    r = shim.run(WINRUN, "--", "python", "-V", WINRUN_NO_RESULT_GRACE_S="1")
    assert r.returncode == 255
    assert b"without an exit code" in r.stderr


def test_winrun_setup_sweep_kills_orphaned_commands_and_bounds_the_task():
    """A run that died without cleanup (SIGKILL, lost link) must not keep driving the desktop."""
    winrun = _import(WINRUN)
    script = winrun.setup_script("abc123", winrun.build_spec(["app.exe"], ps=False, cwd=None), 600)
    sweep = script[:script.index("New-Item")]
    assert "taskkill.exe /PID" in sweep and "\\pid" in sweep and "/T /F" in sweep
    assert sweep.index("taskkill.exe") < sweep.index("Remove-Item")
    assert "[TimeSpan]::FromSeconds(900)" in script  # --timeout + 300 s margin as a VM-side backstop
    unbounded = winrun.setup_script("abc123", winrun.build_spec(["app.exe"], ps=False, cwd=None), None)
    assert "[TimeSpan]::Zero" in unbounded


def test_winrun_poll_with_null_sizes_does_not_crash(shim):
    shim.respond("winrun:poll", {"stdout": json.dumps({"state": "Running", "rc": None, "out": "",
                                                       "out_size": None, "err": "", "err_size": None}) + "\n"},
                 _poll(rc=0, state="Ready"))
    r = shim.run(WINRUN, "--", "app.exe")
    assert r.returncode == 0, r.stderr


def test_winrun_timeout_bounds_a_hung_poll(shim):
    shim.respond("winrun:poll", dict(_poll(), sleep=30))
    t0 = time.monotonic()
    r = shim.run(WINRUN, "--timeout", "1", "--", "app.exe")
    assert r.returncode == 124
    assert time.monotonic() - t0 < 15


def test_winrun_sighup_still_unregisters_the_task(shim):
    shim.respond("winrun:poll", _poll())
    proc = subprocess.Popen([sys.executable, str(WINRUN), "--", "python", "-V"], env=shim.env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 20
    while not any(c["phase"] == "winrun:poll" for c in shim.calls()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    proc.send_signal(signal.SIGHUP)
    proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGHUP
    assert any(c["phase"] == "winrun:cleanup" for c in shim.calls())


@pytest.mark.parametrize("remote_rc", [256, -1073741819])  # 256 would truncate to 0; an NTSTATUS crash
def test_winrun_exit_codes_outside_a_byte_never_read_as_success(shim, remote_rc):
    shim.respond("winrun:poll", _poll(rc=remote_rc, state="Ready"))
    r = shim.run(WINRUN, "--", "app.exe")
    assert r.returncode == 1
    assert str(remote_rc).encode() in r.stderr


def test_winrun_refuses_a_command_too_long_for_one_ssh_call(shim):
    r = shim.run(WINRUN, "--", "python", "-c", "x" * 20000)
    assert r.returncode == 2
    assert b"too long" in r.stderr
    assert shim.calls() == []


def test_winrun_requires_a_command(shim):
    r = shim.run(WINRUN)
    assert r.returncode == 2
    assert shim.calls() == []


def test_winrun_command_survives_arbitrary_arguments():
    winrun = _import(WINRUN)
    argv = ["python", "-c", "print('a \"b\" c')", "with space", "", "back\\slash\\", "$(rm -rf /)", "'; x"]
    spec = winrun.build_spec(argv, ps=False, cwd=r"C:\work\my repo")
    assert spec == {"file": "python", "args": subprocess.list2cmdline(argv[1:]),
                    "cwd": r"C:\work\my repo"}
    script = winrun.setup_script("abc123", spec, None)
    for a in argv[2:]:
        if a:
            assert a not in script  # user text only ever travels base64-encoded
    # The wrapper the task runs decodes back to exactly the spec.
    run_ps1 = winrun.run_script("abc123", spec)
    b64 = run_ps1.split("FromBase64String('")[1].split("'")[0]
    assert json.loads(base64.b64decode(b64)) == spec


def test_winrun_ps_mode_runs_the_script_through_encoded_powershell():
    winrun = _import(WINRUN)
    spec = winrun.build_spec(["Get-Process", "|", "Select", "-First", "1"], ps=True, cwd=None)
    assert spec["file"] == "powershell.exe"
    encoded = spec["args"].split("-EncodedCommand ")[1]
    assert "Get-Process | Select -First 1" in base64.b64decode(encoded).decode("utf-16-le")


# -------------------------------------------------------------------------------------- winsync


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@x"})


def test_winsync_sends_only_tracked_and_untracked_unignored_files(shim, tmp_path):
    repo = tmp_path / "my repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("*.log\nbuild/\n")
    (repo / "src" / "app.py").write_text("print('hi')\n")
    (repo / "gone.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "gone.txt").unlink()                       # tracked but deleted: not sent
    (repo / "new file with spaces.txt").write_text("u\n")  # untracked, not ignored: sent
    (repo / "debug.log").write_text("ignored\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("ignored\n")

    r = shim.run(WINSYNC, cwd=repo / "src")
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode().strip().splitlines()[-1] == r"C:\work\my repo"

    with tarfile.open(shim.dir / "scp-0.tar") as tar:
        names = set(tar.getnames())
        head = tar.extractfile(".git/HEAD").read().decode()
        ref = tar.extractfile(".git/refs/heads/main").read().decode().strip()
        manifest = json.loads(tar.extractfile(".winsync-manifest.json").read())
    assert names == {".gitignore", "src/app.py", "new file with spaces.txt", ".git/HEAD",
                     ".git/refs/heads/main", ".winsync-manifest.json"}
    assert head == "ref: refs/heads/main\n"
    assert len(ref) == 40
    assert set(manifest["files"]) == names - {".winsync-manifest.json"}

    scp = [c for c in shim.calls() if c["tool"] == "scp"][0]
    assert "ConnectTimeout=5" in scp["argv"]
    assert scp["argv"][-1].startswith("win11:")
    extract = [c for c in shim.calls() if c["phase"] == "winsync:extract"][0]
    assert "tar.exe" in extract["script"]
    assert "my repo" not in extract["script"]  # the destination travels base64-encoded


def test_winsync_dest_override_and_unreachable_vm(shim, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    r = shim.run(WINSYNC, "--dest", r"D:\elsewhere", str(repo))
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode().strip() == r"D:\elsewhere"
    assert shim.run(WINSYNC, "--dest", "relative\\path", str(repo)).returncode == 2
    for root in ("C:\\", "C:\\\\", "D:\\."):  # a drive root would mirror-delete across the drive
        assert shim.run(WINSYNC, "--dest", root, str(repo)).returncode == 2

    shim.respond("scp", {"rc": 255, "stderr": "ssh: connect to host win11 port 22: No route to host\n"})
    r = shim.run(WINSYNC, str(repo))
    assert r.returncode == 255
    assert b"cannot reach win11" in r.stderr
