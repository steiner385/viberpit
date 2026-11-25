"""DisplayBridge: the target daemon's supervised ssh+waypipe bridge per source host.

Every process is a fake: these tests never start ssh or waypipe. The argv they pin is
spec §8 "Verified invocation" verbatim.
"""
from __future__ import annotations

import asyncio
import logging
import struct

import pytest

from server.sessions.desktop_session import DesktopSession, DesktopUnavailable
from server.sessions.display_bridge import DisplayBridge, build_display_bridge

COOKIE = bytes.fromhex("0123456789abcdeffedcba9876543210")
COOKIE_HEX = COOKIE.hex()
ROTATED = bytes.fromhex("aaaabbbbccccddddeeeeffff00001111")
S_XRD = "/run/user/1000"


def _xauth_record(family: int, addr: bytes, num: bytes, name: bytes, data: bytes) -> bytes:
    out = struct.pack(">H", family)
    for f in (addr, num, name, data):
        out += struct.pack(">H", len(f)) + f
    return out


def _write_xauth(path, cookie: bytes) -> str:
    path.write_bytes(_xauth_record(0x0100, b"dobby", b"0", b"MIT-MAGIC-COOKIE-1", cookie))
    return str(path)


class FakeStdin:
    def __init__(self):
        self.closed = False
        self.written = b""

    def write(self, data):
        self.written += data

    def close(self):
        self.closed = True


class FakeProc:
    pid = None  # never a real pid: the bridge must not killpg a fake

    def __init__(self, argv, kw, *, rc=None, stdout=b""):
        self.argv = list(argv)
        self.kw = kw
        self.returncode = None
        self._done = asyncio.Event()
        self._stdout = stdout
        self._rc_on_communicate = rc
        self.stdin = FakeStdin() if kw.get("stdin") == asyncio.subprocess.PIPE else None
        self.stdin_data = b""
        self.terminated = False

    def exit(self, rc):
        if self.returncode is None:
            self.returncode = rc
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    async def communicate(self, input=None):
        self.stdin_data = input or b""
        self.exit(self._rc_on_communicate if self._rc_on_communicate is not None else 0)
        return self._stdout, b""

    def terminate(self):
        self.terminated = True
        self.exit(-15)

    def kill(self):
        self.terminated = True
        self.exit(-9)


def _kind(argv) -> str:
    if argv[0] == "waypipe":
        return "client"
    if "-R" in argv:
        return "bridge"
    if "ConnectTimeout=10" in argv:
        return "cleanup"
    return "prep"


class FakeSpawn:
    """Records every spawn. ``bridge_exits`` is a queue of return codes: each bridge ssh
    pops one and exits with it at once; an empty queue means the ssh stays up."""

    def __init__(self, *, prep_rc=0, cleanup_rc=0, bridge_exits=(), cleanup_hang=False):
        self.procs: list[FakeProc] = []
        self.cleanup_hang = cleanup_hang
        self.prep_rc = prep_rc
        self.cleanup_rc = cleanup_rc
        self.bridge_exits = list(bridge_exits)

    async def __call__(self, *argv, **kw):
        kind = _kind(argv)
        if kind == "prep":
            proc = FakeProc(argv, kw, rc=self.prep_rc, stdout=f"{S_XRD}\n".encode())
        else:
            proc = FakeProc(argv, kw)
            if kind == "cleanup" and not self.cleanup_hang:
                proc.exit(self.cleanup_rc)
            elif kind == "bridge" and self.bridge_exits:
                proc.exit(self.bridge_exits.pop(0))
        self.procs.append(proc)
        return proc

    def of(self, kind):
        return [p for p in self.procs if _kind(p.argv) == kind]


class FakeNative:
    def __init__(self, session):
        self.session = session
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1

    def current_session(self):
        return self.session if isinstance(self.session, DesktopSession) else None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, d):
        self.calls.append(d)
        await asyncio.sleep(0)


def _wayland(tmp_path, cookie=COOKIE, display=":0") -> DesktopSession:
    xrd = tmp_path / "xrd"
    xrd.mkdir(exist_ok=True)
    auth = _write_xauth(tmp_path / f"mutter-Xwaylandauth-{cookie.hex()[:4]}", cookie)
    return DesktopSession(kind="wayland", display=display, xauthority=auth,
                          wayland_display="wayland-0", xdg_runtime_dir=str(xrd))


def _bridge(native, spawn, clock=None, *, accept=("rocky",), x11=True, sleeps=None, **kw):
    return DisplayBridge(
        native=native, accept_from=frozenset(accept), ssh_dest=lambda s: None,
        x11_enabled=x11, x_start=150, grace_s=60.0, silence_s=90.0,
        spawn=spawn, clock=clock or Clock(), settle_s=0.01,
        sleep=sleeps or Sleeps(), wall=lambda: 1234.5, **kw)


async def _until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def _state(rows, source):
    return next(r for r in rows if r["source"] == source)


async def _up(bridge, source="rocky", dest="tony@rocky"):
    await bridge.ensure([source], {source: dest})
    await _until(lambda: bridge._known[source].state == "up")


@pytest.mark.asyncio
async def test_ensure_starts_one_ssh_per_source_with_verified_argv(tmp_path):
    desk = _wayland(tmp_path)
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(desk), spawn)
    rows = await bridge.ensure(["rocky"], {"rocky": "tony@rocky"})
    assert _state(rows, "rocky")["state"] == "starting"
    await _until(lambda: bridge._known["rocky"].state == "up")

    t_xrd = desk.xdg_runtime_dir
    (prep,) = spawn.of("prep")
    assert prep.argv[:4] == ["ssh", "-o", "BatchMode=yes", "tony@rocky"]
    assert prep.argv[4] == (
        "[ -d /tmp/.X11-unix ] || { echo 'viberpit-bridge: /tmp/.X11-unix missing' >&2; exit 3; }\n"
        "umask 077\n"
        'd="$XDG_RUNTIME_DIR/viberpit-bridge"\n'
        'mkdir -p "$d" && chmod 700 "$d"\n'
        '[ -d "$d" ] && [ -O "$d" ] && [ ! -L "$d" ] || '
        "{ echo 'viberpit-bridge: private dir not owned' >&2; exit 4; }\n"
        'rm -f "$d/waypipe.sock" /tmp/.X11-unix/X150\n'
        "if [ -e /tmp/.X11-unix/X150 ] || [ -L /tmp/.X11-unix/X150 ]; then "
        "echo 'viberpit-bridge: /tmp/.X11-unix/X150 held by another user' >&2; exit 4; fi\n"
        ': > "$d/xauth" && chmod 600 "$d/xauth"\n'
        'xauth -q -f "$d/xauth" source -\n'
        "printf '%s\\n' \"$XDG_RUNTIME_DIR\"")
    assert prep.stdin_data == f"add :150 MIT-MAGIC-COOKIE-1 {COOKIE_HEX}\n".encode()

    (client,) = spawn.of("client")
    assert client.argv == ["waypipe", "--socket", f"{t_xrd}/viberpit-bridge/rocky-waypipe.sock",
                           "client"]
    assert client.kw["env"]["WAYLAND_DISPLAY"] == "wayland-0"
    assert client.kw["env"]["XDG_RUNTIME_DIR"] == t_xrd

    (ssh,) = spawn.of("bridge")
    assert ssh.argv == [
        "ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "StreamLocalBindUnlink=yes", "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-R", f"{S_XRD}/viberpit-bridge/waypipe.sock:{t_xrd}/viberpit-bridge/rocky-waypipe.sock",
        "-R", "/tmp/.X11-unix/X150:/tmp/.X11-unix/X0",
        "tony@rocky",
        f"exec waypipe --socket {S_XRD}/viberpit-bridge/waypipe.sock "
        "--display wayland-viberpit-bridge --unlink-socket server -- cat"]
    # stdin is a pipe held open (finding 2): closing it is what stops the remote server.
    assert ssh.kw["stdin"] == asyncio.subprocess.PIPE
    assert ssh.stdin is not None and not ssh.stdin.closed
    assert all(p.kw.get("start_new_session") for p in spawn.procs)
    for p in spawn.procs:
        assert COOKIE_HEX not in " ".join(p.argv)

    rows = await bridge.ensure(["rocky"], {"rocky": "tony@rocky"})
    assert rows == [{"source": "rocky", "state": "up", "legs": ["wayland", "x11"], "since": 1234.5}]
    assert len(spawn.of("bridge")) == 1  # idempotent
    assert bridge.info() == {"accepts": ["rocky"], "active": [
        {"source": "rocky", "since": 1234.5, "legs": ["wayland", "x11"]}]}
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_not_accepted_source_never_spawns(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn)
    rows = await bridge.ensure(["Mallory"], {"mallory": "x@mallory"})
    assert rows == [{"source": "mallory", "state": "not_accepted", "legs": [], "since": None}]
    await asyncio.sleep(0.02)
    assert spawn.procs == []
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_sources_compare_case_insensitively(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn)
    await bridge.ensure(["ROCKY"], {"ROCKY": "rocky"})
    await _until(lambda: bridge._known["rocky"].state == "up")
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_unsafe_ssh_dest_is_refused(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn)
    rows = await bridge.ensure(["rocky"], {"rocky": "-oProxyCommand=evil"})
    assert _state(rows, "rocky")["state"] == "failed"
    await asyncio.sleep(0.02)
    assert spawn.procs == []
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_desktop_unavailable_never_spawns(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(DesktopUnavailable("no_graphical_session")), spawn)
    rows = await bridge.ensure(["rocky"], {"rocky": "rocky"})
    assert rows == [{"source": "rocky", "state": "desktop_unavailable", "legs": [], "since": None}]
    await asyncio.sleep(0.02)
    assert spawn.procs == []
    assert bridge.info()["active"] == []
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_desktop_logout_tears_down(tmp_path):
    native = FakeNative(_wayland(tmp_path))
    spawn = FakeSpawn()
    bridge = _bridge(native, spawn)
    await _up(bridge)
    native.session = DesktopUnavailable("no_graphical_session")
    rows = await bridge.tick()
    assert _state(rows, "rocky")["state"] == "desktop_unavailable"
    assert spawn.of("bridge")[0].terminated and spawn.of("client")[0].terminated
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_unlisted_source_torn_down_after_grace(tmp_path):
    clock = Clock()
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock)
    await _up(bridge)
    clock.t += 10
    rows = await bridge.ensure([], {})
    assert _state(rows, "rocky")["state"] == "stopping"
    clock.t += 59
    await bridge.ensure([], {})
    (ssh,) = spawn.of("bridge")
    assert not ssh.terminated
    assert spawn.of("cleanup") == []
    clock.t += 1
    rows = await bridge.ensure([], {})
    assert rows == []
    assert ssh.terminated and ssh.stdin.closed
    assert spawn.of("client")[0].terminated
    await _until(lambda: len(spawn.of("cleanup")) == 1)
    assert spawn.of("cleanup")[0].argv == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "tony@rocky",
        'rm -f /tmp/.X11-unix/X150 "$XDG_RUNTIME_DIR/viberpit-bridge/waypipe.sock" '
        '"$XDG_RUNTIME_DIR/viberpit-bridge/xauth"']
    assert bridge.info()["active"] == []
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_relisted_within_grace_keeps_process(tmp_path):
    clock = Clock()
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock)
    await _up(bridge)
    clock.t += 10
    await bridge.ensure([], {})
    clock.t += 30
    rows = await bridge.ensure(["rocky"], {"rocky": "tony@rocky"})
    assert _state(rows, "rocky")["state"] == "up"
    clock.t += 100
    await bridge.ensure(["rocky"], {"rocky": "tony@rocky"})
    assert len(spawn.of("bridge")) == 1
    assert not spawn.of("bridge")[0].terminated
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_silence_fails_closed_after_three_intervals(tmp_path):
    clock = Clock()
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock)
    await _up(bridge)
    clock.t += 89
    rows = await bridge.tick()
    assert _state(rows, "rocky")["state"] == "up"
    assert not spawn.of("bridge")[0].terminated
    clock.t += 1
    rows = await bridge.tick()
    assert rows == []
    assert spawn.of("bridge")[0].terminated
    await _until(lambda: len(spawn.of("cleanup")) == 1)
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_two_sources_are_independent(tmp_path):
    clock = Clock()
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock, accept=("rocky", "zeta"))
    await bridge.ensure(["rocky", "zeta"], {"rocky": "rocky", "zeta": "zeta"})
    await _until(lambda: all(b.state == "up" for b in bridge._known.values()))
    ssh = {p.argv[-2]: p for p in spawn.of("bridge")}
    assert set(ssh) == {"rocky", "zeta"}
    assert any("zeta-waypipe.sock" in a for a in ssh["zeta"].argv)
    ssh["zeta"].exit(255)  # zeta's crash restarts zeta only
    await _until(lambda: [p.argv[-2] for p in spawn.of("bridge")].count("zeta") == 2)
    assert [p.argv[-2] for p in spawn.of("bridge")].count("rocky") == 1
    assert not ssh["rocky"].terminated
    assert bridge._known["rocky"].state == "up"
    clock.t += 1
    await bridge.ensure(["rocky"], {"rocky": "rocky"})
    clock.t += 61
    rows = await bridge.ensure(["rocky"], {"rocky": "rocky"})
    assert [r["source"] for r in rows] == ["rocky"]
    assert not ssh["rocky"].terminated
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_crash_restarts_with_backoff_while_desired(tmp_path):
    sleeps = Sleeps()
    spawn = FakeSpawn(bridge_exits=[255, 255])
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, sleeps=sleeps,
                     backoff_initial_s=1.0, backoff_max_s=60.0)
    await bridge.ensure(["rocky"], {"rocky": "rocky"})
    await _until(lambda: bridge._known["rocky"].state == "up")
    assert len(spawn.of("bridge")) == 3
    assert len(spawn.of("prep")) == 3   # each restart re-runs prep (stale remote sockets)
    assert sleeps.calls == [1.0, 2.0]
    # Every crashed attempt's client was reaped, not leaked.
    assert [c.terminated for c in spawn.of("client")] == [True, True, False]
    await bridge.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("rc", [3, 4])
async def test_prep_failure_is_failed_and_retried(tmp_path, caplog, rc):
    caplog.set_level(logging.INFO, logger="server.sessions.display_bridge")
    sleeps = Sleeps()
    spawn = FakeSpawn(prep_rc=rc)
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, sleeps=sleeps)
    await bridge.ensure(["rocky"], {"rocky": "rocky"})
    await _until(lambda: len(sleeps.calls) >= 1)
    failed = [r for r in caplog.records if f"rc={rc}" in r.getMessage()]
    assert failed and all(r.levelno == logging.INFO for r in failed)
    assert "source=rocky leg=prep state=failed" in failed[0].getMessage()
    assert "/tmp" not in failed[0].getMessage()      # names the source, not paths
    assert spawn.of("bridge") == [] and spawn.of("client") == []
    await bridge.stop_all()


def test_prep_refuses_a_squatted_x_socket_or_foreign_private_dir():
    from server.sessions.display_bridge import PREP_RC_SQUATTED, _prep_script

    script = _prep_script(150, x11=True)
    lines = script.splitlines()
    rm = next(i for i, ln in enumerate(lines) if ln.startswith("rm -f"))
    check = next(i for i, ln in enumerate(lines) if "[ -e /tmp/.X11-unix/X150 ]" in ln)
    # After the rm (which silently fails on another user's file in the sticky dir), before
    # any cookie is written: a survivor means an impostor would get the cookie.
    assert rm < check < next(i for i, ln in enumerate(lines) if "xauth" in ln)
    assert f"exit {PREP_RC_SQUATTED}" in lines[check] and "[ -L /tmp/.X11-unix/X150 ]" in lines[check]
    dir_check = next(i for i, ln in enumerate(lines) if '[ -O "$d" ]' in ln)
    assert dir_check < rm and f"exit {PREP_RC_SQUATTED}" in lines[dir_check]
    # The Wayland sockets and the cookie file live only under the private dir / runtime dir.
    assert '"$d/waypipe.sock"' in script and '"$d/xauth"' in script
    # No X11 leg: an X squatter must not take the Wayland leg down.
    assert "[ -e /tmp/.X11-unix/X150 ]" not in _prep_script(150, x11=False)


@pytest.mark.asyncio
async def test_tick_loop_survives_a_raising_tick(tmp_path, caplog):
    bridge = _bridge(FakeNative(_wayland(tmp_path)), FakeSpawn(), tick_s=0.005)
    calls = []

    async def flaky_tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []

    bridge.tick = flaky_tick
    bridge.start()
    await _until(lambda: len(calls) >= 3)
    assert not bridge._tick_task.done()
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_late_failing_bridge_keeps_backing_off(tmp_path):
    clock = Clock()
    sleeps = Sleeps()
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock, sleeps=sleeps,
                     backoff_initial_s=1.0, backoff_max_s=60.0)
    await bridge.ensure(["rocky"], {"rocky": "rocky"})
    for n in range(1, 4):   # up past settle_s, then dies soon after: still a crash loop
        await _until(lambda: len(spawn.of("bridge")) == n and bridge._known["rocky"].state == "up")
        clock.t += 5
        spawn.of("bridge")[-1].exit(255)
    await _until(lambda: len(spawn.of("bridge")) == 4 and bridge._known["rocky"].state == "up")
    assert sleeps.calls == [1.0, 2.0, 4.0]
    clock.t += 60           # this one stayed up a full ceiling: backoff resets
    spawn.of("bridge")[-1].exit(255)
    await _until(lambda: len(spawn.of("bridge")) == 5)
    assert sleeps.calls[-1] == 1.0
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_ensure_after_stop_all_starts_nothing(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn)
    await bridge.stop_all()
    assert await bridge.ensure(["rocky"], {"rocky": "rocky"}) == []
    assert await bridge.tick() == []
    await asyncio.sleep(0.02)
    assert spawn.procs == [] and bridge._known == {}


@pytest.mark.asyncio
async def test_stop_all_retries_an_interrupted_cleanup(tmp_path):
    clock = Clock()
    spawn = FakeSpawn(cleanup_hang=True)
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock)
    await _up(bridge)
    clock.t += 200
    await bridge.tick()                          # silence: teardown, cleanup now in flight
    await _until(lambda: len(spawn.of("cleanup")) == 1)
    spawn.cleanup_hang = False
    await bridge.stop_all()
    cleanups = spawn.of("cleanup")
    assert len(cleanups) == 2 and cleanups[0].terminated
    assert cleanups[1].argv[-2] == "tony@rocky"
    assert bridge._cleanup_pending == set()


@pytest.mark.asyncio
async def test_changed_ssh_dest_cleans_the_old_dest(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn)
    await _up(bridge, dest="old@rocky")
    await bridge.ensure(["rocky"], {"rocky": "new@rocky"})
    await _until(lambda: len(spawn.of("bridge")) == 2 and bridge._known["rocky"].state == "up")
    await _until(lambda: len(spawn.of("cleanup")) == 1)
    assert spawn.of("cleanup")[0].argv[-2] == "old@rocky"
    assert spawn.of("bridge")[1].argv[-2] == "new@rocky"
    await bridge.stop_all()


def test_shutdown_cleanup_fits_the_daemon_shutdown_cap(tmp_path):
    from server.ipc.serve import _DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S

    bridge = DisplayBridge(native=FakeNative(None), accept_from=frozenset(),
                           ssh_dest=lambda s: None, x11_enabled=True, x_start=150,
                           grace_s=60.0, silence_s=90.0)
    terminate_wait_s = 5.0   # x11manager._terminate's SIGTERM grace, per process group
    assert bridge._shutdown_cleanup_timeout_s + terminate_wait_s < _DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S


@pytest.mark.asyncio
async def test_cookie_rotation_repushes_and_restarts_x11_leg(tmp_path):
    native = FakeNative(_wayland(tmp_path))
    spawn = FakeSpawn()
    bridge = _bridge(native, spawn)
    await _up(bridge)
    first = spawn.of("bridge")[0]
    native.session = _wayland(tmp_path, cookie=ROTATED)   # a new login: new cookie file
    await bridge.ensure(["rocky"], {"rocky": "tony@rocky"})
    await _until(lambda: len(spawn.of("bridge")) == 2 and bridge._known["rocky"].state == "up")
    assert first.terminated
    preps = spawn.of("prep")
    assert len(preps) == 2
    assert preps[1].stdin_data == f"add :150 MIT-MAGIC-COOKIE-1 {ROTATED.hex()}\n".encode()
    # Same desktop on the next tick: no further restart.
    await bridge.tick()
    await asyncio.sleep(0.02)
    assert len(spawn.of("bridge")) == 2
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_cookie_sent_via_stdin_never_argv_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    native = FakeNative(_wayland(tmp_path))
    spawn = FakeSpawn(bridge_exits=[255])
    clock = Clock()
    bridge = _bridge(native, spawn, clock)
    await _up(bridge)
    native.session = _wayland(tmp_path, cookie=ROTATED)
    await bridge.tick()
    await _until(lambda: len(spawn.of("bridge")) == 3 and bridge._known["rocky"].state == "up")
    clock.t += 200
    await bridge.tick()                       # silence ⇒ teardown + cleanup
    await _until(lambda: len(spawn.of("cleanup")) == 1)
    await bridge.stop_all()
    secrets = (COOKIE_HEX, ROTATED.hex())
    for p in spawn.procs:
        flat = " ".join(p.argv) + " " + " ".join(
            f"{k}={v}" for k, v in (p.kw.get("env") or {}).items())
        assert not any(s in flat for s in secrets), p.argv
    assert any(p.stdin_data for p in spawn.of("prep"))
    assert caplog.records, "the bridge should log its state changes"
    for rec in caplog.records:
        text = rec.getMessage() + repr(rec.args) + repr(rec.__dict__)
        assert not any(s in text for s in secrets)


@pytest.mark.asyncio
async def test_x11_leg_absent_when_disabled_or_target_is_x11_only_for_wayland_leg(tmp_path):
    # x11_enabled=False: no -R X11 forward, no cookie pushed.
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, x11=False)
    await _up(bridge, dest="rocky")
    (ssh,) = spawn.of("bridge")
    assert not any(a.startswith("/tmp/.X11-unix/") for a in ssh.argv)
    assert ssh.argv.count("-R") == 1
    assert spawn.of("prep")[0].stdin_data == b""
    assert bridge.info()["active"][0]["legs"] == ["wayland"]
    await bridge.stop_all()

    # An X11-only target: no waypipe leg at all; ssh holds only the X forward.
    auth = _write_xauth(tmp_path / "Xauthority", COOKIE)
    x11_desk = DesktopSession(kind="x11", display=":1", xauthority=auth,
                              wayland_display=None, xdg_runtime_dir=None)
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(x11_desk), spawn)
    await _up(bridge, dest="rocky")
    assert spawn.of("client") == []
    (ssh,) = spawn.of("bridge")
    assert ssh.argv[-3:] == ["/tmp/.X11-unix/X150:/tmp/.X11-unix/X1", "rocky", "exec cat"]
    assert ssh.argv.count("-R") == 1
    assert bridge.info()["active"][0]["legs"] == ["x11"]
    await bridge.stop_all()

    # X11-only target with the X11 leg disabled: nothing to bridge.
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(x11_desk), spawn, x11=False)
    rows = await bridge.ensure(["rocky"], {"rocky": "rocky"})
    assert _state(rows, "rocky")["state"] == "desktop_unavailable"
    await asyncio.sleep(0.02)
    assert spawn.procs == []
    await bridge.stop_all()


@pytest.mark.asyncio
async def test_stop_all_reaps_everything(tmp_path):
    spawn = FakeSpawn()
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, accept=("rocky", "zeta"))
    bridge.start()
    await bridge.ensure(["rocky", "zeta"], {"rocky": "rocky", "zeta": "zeta"})
    await _until(lambda: all(b.state == "up" for b in bridge._known.values()))
    tasks = [b.task for b in bridge._known.values()]
    await bridge.stop_all()
    assert all(t.done() for t in tasks)
    assert bridge._tick_task is None
    for p in spawn.of("bridge") + spawn.of("client"):
        assert p.terminated
    assert all(p.stdin.closed for p in spawn.of("bridge"))
    assert sorted(p.argv[-2] for p in spawn.of("cleanup")) == ["rocky", "zeta"]
    assert bridge.info()["active"] == []
    assert await bridge.ensure([], {}) == []


@pytest.mark.asyncio
async def test_failed_cleanup_retries_then_flags_pending(tmp_path):
    clock = Clock()
    sleeps = Sleeps()
    spawn = FakeSpawn(cleanup_rc=255)
    bridge = _bridge(FakeNative(_wayland(tmp_path)), spawn, clock, sleeps=sleeps)
    await _up(bridge)
    clock.t += 200
    await bridge.tick()
    await _until(lambda: ("rocky", "tony@rocky") in bridge._cleanup_pending)
    assert len(spawn.of("cleanup")) == 4      # one attempt + 3 retries
    assert sleeps.calls[-3:] == [5.0, 30.0, 120.0]
    spawn.cleanup_rc = 0
    await bridge.ensure([], {})               # the next ensure retries it
    await _until(lambda: ("rocky", "tony@rocky") not in bridge._cleanup_pending)
    assert len(spawn.of("cleanup")) == 5
    await bridge.stop_all()


def test_native_display_current_session_accessor():
    from server.sessions.native_display import NativeDisplay

    desk = DesktopSession(kind="x11", display=":0", xauthority=None,
                          wayland_display=None, xdg_runtime_dir=None)
    nd = NativeDisplay(chromium_cmd="chromium", no_sandbox=False, max_browsers=1,
                       resolve=lambda: desk)
    assert nd.current_session() is None           # nothing resolved yet
    asyncio.run(nd.refresh())
    assert nd.current_session() == desk
    pooled = NativeDisplay(chromium_cmd="chromium", no_sandbox=False, max_browsers=1,
                           resolve=lambda: desk, blocked_range=range(0, 5))
    asyncio.run(pooled.refresh())
    assert pooled.current_session() is None


class _Settings:
    display_bridge_enabled = True
    display_bridge_accept_from = "Rocky, zeta"
    display_bridge_x_start = 150
    display_bridge_x11 = True
    display_bridge_grace_s = 60.0
    enable_xvfb = True
    display_start = 100
    display_count = 12


def test_build_display_bridge(caplog):
    native = FakeNative(None)
    assert build_display_bridge(type("S", (_Settings,), {"display_bridge_enabled": False})(),
                                native) is None
    assert build_display_bridge(_Settings(), None) is None
    b = build_display_bridge(_Settings(), native)
    assert b is not None and b.info() == {"accepts": ["rocky", "zeta"], "active": []}
    assert b._x11_enabled is True and b._silence_s == 90.0
    caplog.set_level(logging.INFO)
    pooled = build_display_bridge(type("S", (_Settings,), {"display_bridge_x_start": 105})(), native)
    assert pooled is not None and pooled._x11_enabled is False
    off = build_display_bridge(type("S", (_Settings,), {"display_bridge_x11": False})(), native)
    assert off is not None and off._x11_enabled is False
    assert all(r.levelno < logging.WARNING for r in caplog.records)
