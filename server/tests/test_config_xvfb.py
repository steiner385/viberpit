import importlib
import server.config as config

def _fresh():
    importlib.reload(config)
    return config.get_settings()

def test_xvfb_defaults(monkeypatch):
    for k in ("CCSWITCH_ENABLE_XVFB","CCSWITCH_DISPLAY_START","CCSWITCH_DISPLAY_COUNT",
              "CCSWITCH_MAX_BROWSERS","CCSWITCH_VNC_PORT_BASE","CCSWITCH_XVFB_RESOLUTION",
              "CCSWITCH_VNC_IDLE_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)
    s = _fresh()
    assert s.enable_xvfb is False
    assert s.display_start == 100
    assert s.display_count == 12
    assert s.max_browsers == 6
    assert s.vnc_port_base == 6100
    assert s.xvfb_resolution == "1280x800x24"
    assert s.vnc_idle_timeout_s == 1800.0

def test_xvfb_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_ENABLE_XVFB", "true")
    monkeypatch.setenv("CCSWITCH_DISPLAY_COUNT", "4")
    s = _fresh()
    assert s.enable_xvfb is True
    assert s.display_count == 4
