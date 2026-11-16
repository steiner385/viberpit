"""An empty setting is an unset one (#3386).

Every agent CLI gets the daemon's namespace emptied (``env_compat.withheld_env``), and
the agent's shell imports this app whenever it runs the repo's own tests. Before
``env_ignore_empty``, an emptied bool (``VIBERPIT_ENABLE_XVFB=``) failed validation at
collection and an emptied ``VIBERPIT_DB_URL`` replaced the default with ``""``.
"""
from server.config import Settings


def test_empty_values_fall_back_to_the_defaults(monkeypatch):
    for name in ("VIBERPIT_ENABLE_XVFB", "VIBERPIT_DISPLAY_COUNT", "VIBERPIT_DB_URL",
                 "VIBERPIT_MASTER_KEY"):
        monkeypatch.setenv(name, "")
    s = Settings(_env_file=None)
    assert s.enable_xvfb is False
    assert s.display_count == 12
    assert s.db_url.startswith("sqlite+aiosqlite:///")
    assert s.master_key is None


def test_a_set_value_still_wins(monkeypatch):
    monkeypatch.setenv("VIBERPIT_ENABLE_XVFB", "true")
    assert Settings(_env_file=None).enable_xvfb is True


# ── fields whose empty value means something (#3386 follow-up) ──────────────
import pytest  # noqa: E402

from viberpit import brand  # noqa: E402

#: ``config.set_but_empty`` fields: "" is "carry none" / "stdout only" / "no gated tier".
SET_BUT_EMPTY = {"reauth_extension_ids", "reauth_cookie_hosts", "log_dir",
                 "model_approval_denylist"}


@pytest.mark.parametrize("name", sorted(SET_BUT_EMPTY))
def test_a_set_but_empty_field_keeps_its_meaning(monkeypatch, name):
    alias = Settings.model_fields[name].alias
    monkeypatch.delenv(alias, raising=False)
    unset = getattr(Settings(_env_file=None), name)
    monkeypatch.setenv(alias, "")
    assert getattr(Settings(_env_file=None), name) == "" != unset
    monkeypatch.setenv(alias, "x")
    assert getattr(Settings(_env_file=None), name) == "x"


def test_settings_build_from_a_session_env_with_every_name_blanked(monkeypatch):
    """Every alias, both spellings, as a session's shell sees it: each reads as its
    default except the four whose empty value means something."""
    new, old = brand.ENV_PREFIX, brand.LEGACY_ENV_PREFIX
    aliases = [f.alias for f in Settings.model_fields.values() if f.alias]
    names = [n for a in aliases for n in (a, f"{old}{a[len(new):]}")]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    defaults = Settings(_env_file=None)
    for name in names:
        monkeypatch.setenv(name, "")
    blanked = Settings(_env_file=None)
    for field in Settings.model_fields:
        expected = "" if field in SET_BUT_EMPTY else getattr(defaults, field)
        assert getattr(blanked, field) == expected, field


def test_the_suite_treats_a_withheld_name_as_unset(monkeypatch):
    """A session's shell sees withheld names as ""; the server conftest pops them before
    anything reads them (the edge's startup guard would refuse every lifespan)."""
    import os
    from server.tests.conftest import _no_withheld_namespace_names
    new, old = brand.ENV_PREFIX, brand.LEGACY_ENV_PREFIX
    monkeypatch.setenv(f"{new}MASTER_KEY", "")
    monkeypatch.setenv(f"{old}MASTER_KEY", "")
    monkeypatch.setenv(f"{new}DAEMON_ROLE", "worker")
    _no_withheld_namespace_names()
    assert f"{new}MASTER_KEY" not in os.environ and f"{old}MASTER_KEY" not in os.environ
    assert os.environ[f"{new}DAEMON_ROLE"] == "worker"
