# server/tests/test_app_x11_lifespan.py
from server.sessions.x11manager import build_x11_manager


class S:  # minimal settings stand-in
    enable_xvfb = False
    display_start = 100
    display_count = 4
    max_browsers = 2
    vnc_port_base = 6100
    xvfb_resolution = "800x600x24"
    vnc_idle_timeout_s = 1800.0


def test_disabled_returns_none():
    assert build_x11_manager(S()) is None


def test_enabled_but_missing_binaries_returns_none(monkeypatch):
    s = S()
    s.enable_xvfb = True
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert build_x11_manager(s) is None  # fail-loud preflight → disabled
