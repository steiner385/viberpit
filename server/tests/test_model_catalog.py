"""Tests for server.sessions.model_catalog.

catalog() is now dynamic (probes the running claude binary), so tests that assert
specific alias content mock _cache to decouple from whichever binary version is
installed. Tests for the probe mechanism use a synthetic binary file.
"""
import json
import shutil

import pytest

from server.sessions import model_catalog as mc


# --------------------------------------------------------------------------- #
# Static fallback (ALIASES)                                                    #
# --------------------------------------------------------------------------- #

def test_static_aliases_include_core_tiers():
    # ALIASES is the floor: all tiers that have existed since v2.1.197.
    for alias in ("default", "sonnet", "opus", "haiku", "fable", "best", "opusplan"):
        assert alias in mc.ALIASES


def test_is_known_alias():
    assert mc.is_known_alias("opus") is True
    assert mc.is_known_alias("fable") is True
    assert mc.is_known_alias("opus[1m]") is True          # 1M-context beta variant
    assert mc.is_known_alias("best") is True
    assert mc.is_known_alias("claude-haiku-4-5") is False  # pinned ids are free-text, not aliases


# --------------------------------------------------------------------------- #
# Label derivation                                                              #
# --------------------------------------------------------------------------- #

def test_label_for_known_aliases():
    assert mc._label_for("opus") == "Opus"
    assert mc._label_for("sonnet[1m]") == "Sonnet (1M context)"
    assert mc._label_for("default") == "Default"


def test_label_for_unknown_alias_derives_title_case():
    assert mc._label_for("nova") == "Nova"


def test_label_for_unknown_1m_alias_appends_context():
    assert mc._label_for("nova[1m]") == "Nova (1M context)"


# --------------------------------------------------------------------------- #
# Binary probe (_discover_from_binary)                                         #
# --------------------------------------------------------------------------- #

def _make_binary(path, aliases):
    """Write a synthetic binary containing the alias array surrounded by junk bytes.

    Uses compact JSON (no spaces) to match the Bun-minified JS format in the
    real binary that the regex expects.
    """
    payload = json.dumps(aliases, separators=(",", ":")).encode()
    path.write_bytes(b"\x00junk\x00" + payload + b"\x00more junk\x00")


def test_discover_finds_alias_array_with_opusplan_anchor(tmp_path):
    binary = tmp_path / "claude"
    aliases = ["sonnet", "opus", "haiku", "best", "sonnet[1m]", "opus[1m]", "opusplan"]
    _make_binary(binary, aliases)
    result = mc._discover_from_binary(str(binary))
    assert result is not None
    assert result[0] == "default"           # prepended by probe
    assert "sonnet" in result
    assert "opusplan" in result
    assert "opus[1m]" in result


def test_discover_prepends_default_when_absent(tmp_path):
    binary = tmp_path / "claude"
    _make_binary(binary, ["sonnet", "opusplan"])
    result = mc._discover_from_binary(str(binary))
    assert result is not None
    assert result[0] == "default"


def test_discover_does_not_duplicate_default_when_present(tmp_path):
    binary = tmp_path / "claude"
    _make_binary(binary, ["default", "sonnet", "opusplan"])
    result = mc._discover_from_binary(str(binary))
    assert result is not None
    assert result.count("default") == 1


def test_discover_returns_none_for_missing_file():
    result = mc._discover_from_binary("/nonexistent/path/claude")
    assert result is None


def test_discover_returns_none_when_no_alias_array(tmp_path):
    binary = tmp_path / "claude"
    binary.write_bytes(b"no model aliases here, just random bytes 0x00 0xff")
    result = mc._discover_from_binary(str(binary))
    assert result is None


def test_discover_returns_none_when_array_lacks_opusplan(tmp_path):
    binary = tmp_path / "claude"
    # Array with aliases but no opusplan — not the picker array.
    _make_binary(binary, ["sonnet", "opus", "haiku"])
    result = mc._discover_from_binary(str(binary))
    assert result is None


def test_discover_handles_future_alias(tmp_path):
    binary = tmp_path / "claude"
    aliases = ["sonnet", "opus", "haiku", "nova", "nova[1m]", "opusplan"]
    _make_binary(binary, aliases)
    result = mc._discover_from_binary(str(binary))
    assert result is not None
    assert "nova" in result
    assert "nova[1m]" in result


# --------------------------------------------------------------------------- #
# catalog() — live probe + fallback                                            #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def reset_cache():
    """Isolate each test from module-level _cache state."""
    original = mc._cache
    yield
    mc._cache = original


def test_catalog_returns_id_label_dicts(monkeypatch):
    monkeypatch.setattr(mc, "_cache", mc.ALIASES[:])
    cat = mc.catalog()
    assert all(set(e) == {"id", "label"} for e in cat)
    assert all(isinstance(e["id"], str) and isinstance(e["label"], str) for e in cat)


def test_catalog_order_matches_cached_aliases(monkeypatch):
    aliases = ["default", "opus", "sonnet", "opusplan"]
    monkeypatch.setattr(mc, "_cache", aliases)
    assert [e["id"] for e in mc.catalog()] == aliases


def test_catalog_uses_discovered_list_when_binary_found(monkeypatch, tmp_path):
    binary = tmp_path / "claude"
    discovered = ["sonnet", "opus", "haiku", "opusplan"]
    _make_binary(binary, discovered)
    monkeypatch.setenv("CCSWITCH_CLAUDE_BIN", str(binary))
    monkeypatch.setattr(shutil, "which", lambda _: str(binary))
    monkeypatch.setattr(mc, "_cache", None)
    cat = mc.catalog()
    ids = [e["id"] for e in cat]
    assert "default" in ids   # prepended by probe
    assert "opusplan" in ids


def test_live_aliases_probe_uses_shared_resolver(monkeypatch, tmp_path):
    """The picker must probe the SAME binary the SDK session factory runs, so the
    two can never diverge (constitution VI). _live_aliases resolves via the shared
    resolve_claude_bin helper, not its own private lookup."""
    binary = tmp_path / "claude"
    _make_binary(binary, ["sonnet", "opus", "opusplan"])
    monkeypatch.setattr(mc, "resolve_claude_bin", lambda: str(binary))
    monkeypatch.setattr(mc, "_cache", None)
    ids = [e["id"] for e in mc.catalog()]
    assert "opusplan" in ids and "default" in ids


def test_catalog_falls_back_to_static_when_binary_not_found(monkeypatch):
    monkeypatch.setenv("CCSWITCH_CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(mc, "_cache", None)
    cat = mc.catalog()
    assert [e["id"] for e in cat] == mc.ALIASES


def test_catalog_labels_future_alias_automatically(monkeypatch):
    monkeypatch.setattr(mc, "_cache", ["default", "nova", "nova[1m]", "opusplan"])
    cat = mc.catalog()
    by_id = {e["id"]: e["label"] for e in cat}
    assert by_id["nova"] == "Nova"
    assert by_id["nova[1m]"] == "Nova (1M context)"


# --------------------------------------------------------------------------- #
# refresh()                                                                    #
# --------------------------------------------------------------------------- #

def test_refresh_clears_cache(monkeypatch):
    monkeypatch.setattr(mc, "_cache", ["opus"])
    mc.refresh()
    assert mc._cache is None


# --------------------------------------------------------------------------- #
# model_tier() — tier extraction helper                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model,expected", [
    (None, None),
    ("", None),
    ("fable", "fable"),
    ("Fable", "fable"),
    ("fable[1m]", "fable"),
    ("claude-fable-5", "fable"),
    ("opus", "opus"),
    ("opus[1m]", "opus"),
    ("opusplan", "opus"),
    ("claude-opus-4-8", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
    ("default", None),
    ("best", None),
    ("something-unknown", None),
])
def test_model_tier(model, expected):
    assert mc.model_tier(model) == expected
