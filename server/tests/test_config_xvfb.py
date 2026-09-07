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


def test_native_display_defaults(monkeypatch):
    monkeypatch.delenv("CCSWITCH_NATIVE_DISPLAY_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_NATIVE_DISPLAY", raising=False)
    s = _fresh()
    assert s.native_display_enabled is True
    assert s.native_display == ":0"


def test_native_display_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY_ENABLED", "false")
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY", ":1")
    s = _fresh()
    assert s.native_display_enabled is False
    assert s.native_display == ":1"


def test_native_display_fallback_dataclass_branch(monkeypatch):
    """The dataclass fallback must define the same fields (omitting it is a silent
    AttributeError on hosts without pydantic-settings)."""
    monkeypatch.setenv("CCSWITCH_NATIVE_DISPLAY", ":7")

    import server.config as cfg
    import builtins

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "pydantic_settings":
            raise ImportError(f"No module named '{name}'")
        return original_import(name, *args, **kwargs)

    try:
        monkeypatch.setattr(builtins, "__import__", mock_import)
        importlib.reload(cfg)
        # After reload with pydantic_settings blocked, Settings should be the dataclass
        inst = cfg.Settings()
        assert inst.native_display == ":7"
        assert inst.native_display_enabled is True
    finally:
        # Restore pydantic Settings for the rest of the test suite
        monkeypatch.setattr(builtins, "__import__", original_import)
        importlib.reload(cfg)
