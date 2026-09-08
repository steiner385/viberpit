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
