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
