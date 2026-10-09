"""NativeDisplay: the operator's live desktop as a session target (spec §4.3)."""
import os

import pytest

from server.sessions.desktop_session import DesktopSession, DesktopUnavailable
from server.sessions.native_display import NativeDisplay, build_native_display


class FakeProc:
    def __init__(self):
        self.pid = 4242; self.returncode = None; self.terminated = False
    def terminate(self): self.terminated = True; self.returncode = 0
    def kill(self): self.returncode = -9
    async def wait(self): return self.returncode if self.returncode is not None else 0


WAYLAND = DesktopSession(kind="wayland", display=":0", xauthority="/run/user/1000/.mutter-Xwaylandauth.X",
                         wayland_display="wayland-0", xdg_runtime_dir="/run/user/1000")
X11 = DesktopSession(kind="x11", display=":0", xauthority=None, wayland_display=None, xdg_runtime_dir=None)


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class Resolver:
    def __init__(self, result): self.result = result; self.calls = 0
    def __call__(self):
        self.calls += 1
        return self.result


def _mk(tmp_path, result=WAYLAND, display=None, **kw):
    spawned = []
    async def fake_spawn(*argv, **kwargs):
        spawned.append((argv, kwargs)); return FakeProc()
    resolve = Resolver(result)
    clock = Clock()
    nd = NativeDisplay(display, run_dir=str(tmp_path), chromium_cmd="/usr/bin/chromium",
                       no_sandbox=False, max_browsers=2, spawn=fake_spawn,
                       resolve=resolve, clock=clock, **kw)
    nd.resolver, nd.clock = resolve, clock   # test handles
    return nd, spawned


def test_available_when_session_resolved(tmp_path):
    nd, _ = _mk(tmp_path)
    assert nd.available() is True
    assert nd.unavailable_reason() is None


@pytest.mark.parametrize("reason", ["no_graphical_session", "cookie_missing", "connect_refused", "invalid_display"])
def test_unavailable_reports_resolver_reason(tmp_path, reason):
    nd, _ = _mk(tmp_path, result=DesktopUnavailable(reason, "d"))
    assert nd.available() is False
    assert nd.unavailable_reason() == reason


def test_blocked_range_makes_it_unavailable(tmp_path):
    session = DesktopSession(kind="x11", display=":101", xauthority=None, wayland_display=None, xdg_runtime_dir=None)
    nd, _ = _mk(tmp_path, result=session, blocked_range=range(100, 112))
    assert nd.available() is False
    assert nd.unavailable_reason() == "display_in_xvfb_pool"


def test_resolution_is_cached_within_ttl_and_refreshed_after(tmp_path):
    nd, _ = _mk(tmp_path)
    nd.available(); nd.env(); nd.metrics()
    assert nd.resolver.calls == 1
    nd.clock.now = 4.9
    nd.available()
    assert nd.resolver.calls == 1
    nd.clock.now = 5.0
    nd.resolver.result = DesktopUnavailable("no_graphical_session")
    assert nd.available() is False
    assert nd.resolver.calls == 2


def test_display_property(tmp_path):
    assert _mk(tmp_path, display=":3")[0].display == ":3"
    assert _mk(tmp_path)[0].display == ":0"
    assert _mk(tmp_path, result=DesktopUnavailable("no_graphical_session"))[0].display == ":0"
    other = DesktopSession(kind="x11", display=":2", xauthority=None, wayland_display=None, xdg_runtime_dir=None)
    assert _mk(tmp_path, result=other)[0].display == ":2"


def test_env_wayland_auto_exports_wayland_and_x_cookie(tmp_path):
    env = _mk(tmp_path)[0].env()
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == WAYLAND.xauthority
    assert env["XDG_SESSION_TYPE"] == "wayland"


def test_env_forced_x11_has_no_wayland(tmp_path):
    env = _mk(tmp_path, browser_platform="x11")[0].env()
    assert "WAYLAND_DISPLAY" not in env
    assert env["XAUTHORITY"] == WAYLAND.xauthority
    assert env["XDG_SESSION_TYPE"] == "x11"


def test_env_forced_wayland_on_x11_kind_has_no_wayland_display(tmp_path):
    env = _mk(tmp_path, result=X11, browser_platform="wayland")[0].env()
    assert "WAYLAND_DISPLAY" not in env     # nothing to export; DesktopSession only exports it for kind wayland


def test_env_empty_when_unavailable(tmp_path):
    assert _mk(tmp_path, result=DesktopUnavailable("connect_refused"))[0].env() == {}


@pytest.mark.asyncio
async def test_launch_browser_wayland_native(tmp_path):
    nd, spawned = _mk(tmp_path)
    assert await nd.launch_browser("s1", "https://example.com") is None
    argv, kwargs = spawned[0]
    assert argv[0] == "/usr/bin/chromium"
    assert "--ozone-platform=wayland" in argv
    assert kwargs["env"]["DISPLAY"] == ":0"
    assert kwargs["env"]["WAYLAND_DISPLAY"] == "wayland-0"
    assert any(a.startswith("--user-data-dir=") and "viberpit-native-s1-" in a for a in argv)
    assert argv[-1] == "https://example.com"
    assert nd.metrics()["browsers"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result,platform", [(X11, "auto"), (WAYLAND, "x11")])
async def test_launch_browser_x11_scrubs_stale_inherited_env(tmp_path, monkeypatch, result, platform):
    monkeypatch.setenv("XAUTHORITY", "/stale/cookie")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-stale")
    monkeypatch.setenv("WAYLAND_SOCKET", "9")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    nd, spawned = _mk(tmp_path, result=result, browser_platform=platform)
    assert await nd.launch_browser("s1", None) is None
    argv, kwargs = spawned[0]
    assert "--ozone-platform=x11" in argv
    env = kwargs["env"]
    assert "WAYLAND_DISPLAY" not in env and "WAYLAND_SOCKET" not in env
    assert env["XDG_SESSION_TYPE"] == "x11"
    assert env.get("XAUTHORITY") != "/stale/cookie"
    assert env.get("XAUTHORITY") == result.xauthority


@pytest.mark.asyncio
async def test_launch_browser_refuses_when_unavailable(tmp_path):
    nd, spawned = _mk(tmp_path, result=DesktopUnavailable("no_graphical_session"))
    assert await nd.launch_browser("s1", None) == "native_unavailable"
    assert spawned == []


@pytest.mark.asyncio
async def test_launch_browser_honours_cap(tmp_path):
    nd, _ = _mk(tmp_path)
    assert await nd.launch_browser("s1", None) is None
    assert await nd.launch_browser("s1", None) is None
    assert await nd.launch_browser("s1", None) == "cap"


@pytest.mark.asyncio
async def test_launch_browser_missing_binary_is_launch_failed(tmp_path):
    nd, _ = _mk(tmp_path)
    async def boom(*a, **k): raise FileNotFoundError("chromium")
    nd._spawn = boom
    assert await nd.launch_browser("s1", None) == "launch_failed"


@pytest.mark.asyncio
async def test_launch_browser_dies_in_liveness_window_is_launch_failed(tmp_path):
    nd, _ = _mk(tmp_path, browser_liveness_s=0.01)
    async def dead(*a, **k):
        p = FakeProc(); p.returncode = 1; return p
    nd._spawn = dead
    assert await nd.launch_browser("s1", None) == "launch_failed"
    assert nd.metrics()["browsers"] == 0


def test_metrics_shape(tmp_path):
    nd, _ = _mk(tmp_path)
    assert nd.metrics() == {"display": ":0", "available": True, "kind": "wayland", "reason": None, "browsers": 0}
    nd2, _ = _mk(tmp_path, result=DesktopUnavailable("cookie_missing"))
    m = nd2.metrics()
    assert (m["available"], m["kind"], m["reason"]) == (False, None, "cookie_missing")


@pytest.mark.asyncio
async def test_close_terminates_only_that_sessions_browsers_and_profiles(tmp_path):
    nd, spawned = _mk(tmp_path)
    await nd.launch_browser("s1", None); await nd.launch_browser("s2", None)
    profile_s1 = next(a for a in spawned[0][0] if a.startswith("--user-data-dir=")).split("=", 1)[1]
    assert os.path.isdir(profile_s1)
    await nd.close("s1")
    assert not os.path.isdir(profile_s1)
    assert nd.metrics()["browsers"] == 1
    await nd.close("s1")                    # idempotent


@pytest.mark.asyncio
async def test_stop_all(tmp_path):
    nd, _ = _mk(tmp_path)
    await nd.launch_browser("s1", None); await nd.launch_browser("s2", None)
    await nd.stop_all()
    assert nd.metrics()["browsers"] == 0


class _S:
    def __init__(self, **kw):
        self.native_display_enabled = True; self.native_display = ""; self.native_browser_platform = "auto"
        self.enable_xvfb = False; self.display_start = 100; self.display_count = 12
        self.max_browsers = 6; self.chromium_no_sandbox = False; self.browser_liveness_s = 0.0
        self.__dict__.update(kw)


def test_build_returns_none_when_disabled():
    assert build_native_display(_S(native_display_enabled=False)) is None


def test_build_blocks_pool_range_when_xvfb_on(caplog):
    nd = build_native_display(_S(enable_xvfb=True, native_display=":105"))
    assert nd is not None
    assert nd.available() is False
    assert nd.unavailable_reason() == "display_in_xvfb_pool"
    assert "inside the Xvfb pool" in caplog.text


def test_build_rejects_malformed_display():
    assert build_native_display(_S(native_display="localhost:0.0")) is None


def _capture_resolver(monkeypatch):
    seen = []
    def fake(*, uid, display=None, **kw):
        seen.append(display); return DesktopUnavailable("no_graphical_session")
    monkeypatch.setattr("server.sessions.native_display.resolve_desktop_session", fake)
    return seen


def test_build_empty_display_autodiscovers(monkeypatch):
    seen = _capture_resolver(monkeypatch)
    nd = build_native_display(_S(native_display=""))
    assert nd is not None
    nd.available()
    assert seen == [None]


def test_build_explicit_display_is_passed_through(monkeypatch):
    seen = _capture_resolver(monkeypatch)
    nd = build_native_display(_S(native_display=":0"))
    nd.available()
    assert seen == [":0"]


def test_build_unknown_platform_falls_back_to_auto(caplog):
    nd = build_native_display(_S(native_browser_platform="bogus"))
    assert nd is not None
    assert nd._browser_platform == "auto"
    assert "native_browser_platform" in caplog.text.lower() or "NATIVE_BROWSER_PLATFORM" in caplog.text


def test_build_passes_valid_platform():
    assert build_native_display(_S(native_browser_platform="x11"))._browser_platform == "x11"
