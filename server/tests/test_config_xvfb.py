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

def test_reauth_virtual_display_defaults_true(monkeypatch):
    monkeypatch.delenv("CCSWITCH_REAUTH_VIRTUAL_DISPLAY", raising=False)
    s = _fresh()
    assert s.reauth_virtual_display is True


def test_reauth_virtual_display_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAUTH_VIRTUAL_DISPLAY", "false")
    s = _fresh()
    assert s.reauth_virtual_display is False


def test_reauth_extensions_unset_means_core_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_REAUTH_EXTENSIONS", raising=False)
    s = _fresh()
    assert s.reauth_extension_ids is None
    assert s.reauth_extension_id_items is None


def test_reauth_extensions_empty_disables(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAUTH_EXTENSIONS", "  ")
    s = _fresh()
    assert s.reauth_extension_id_items == []


def test_reauth_extensions_comma_list(monkeypatch):
    monkeypatch.setenv("CCSWITCH_REAUTH_EXTENSIONS", "aaaa, bbbb ,,cccc")
    s = _fresh()
    assert s.reauth_extension_id_items == ["aaaa", "bbbb", "cccc"]
