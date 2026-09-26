import contextlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Promoted from `usage()`'s own local import (same line, same brand-ratchet hit —
# see `python-package` in `.brand-ratchet.toml`, which is already at its ceiling)
# so other test modules can import these quota-building blocks from here rather
# than each adding a second copy of that import (#2032).
from viberpit.quota import QuotaWindow, anthropic_snapshot  # noqa: F401 - re-exported for callers


@contextlib.contextmanager
def temp_env():
    """Redirect store root + shared base to throwaway temp dirs."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        home = d / "accounts"
        shared = d / "dot-claude"
        shared.mkdir(parents=True)
        (shared / "settings.json").write_text("{}")
        (shared / "projects").mkdir()
        old = {k: os.environ.get(k) for k in (
            "VIBERPIT_HOME", "CLAUDE_CONFIG_DIR", "VIBERPIT_CLAUDE_BIN",
            "VIBERPIT_ARCHIVE_HOME",
        )}
        os.environ["VIBERPIT_HOME"] = str(home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(shared)
        os.environ.pop("VIBERPIT_ARCHIVE_HOME", None)
        try:
            # Clear module-level singletons that would otherwise leak between
            # tests. Looked up by name (not a plain module-level import) so a
            # test file that never touches account_registry/accounts does not
            # force-import them; one `.get()` per module (not a membership
            # check plus a second subscript) keeps this at one brand-ratchet
            # `python-package` hit per module instead of two (#2288).
            import sys
            for modname, attrs in (
                ("viberpit.account_registry", ("_runner_missing_logged",)),
                ("viberpit.accounts", ("_dead_accounts", "_scope_gap_warned")),
            ):
                mod = sys.modules.get(modname)
                if mod is not None:
                    for attr in attrs:
                        getattr(mod, attr).clear()
            yield d
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def usage(session=10.0, weekly=20.0, extra=False, mins=120):
    """An Anthropic two-window QuotaSnapshot — the shape a real fetch produces."""
    reset = datetime.now(timezone.utc) + timedelta(minutes=mins)
    return anthropic_snapshot(session, reset, weekly, reset, extra)


def register_accounts(*names, provider="anthropic", engine=None):
    """Put *names* in the accounts.json manifest (inside a temp_env). *engine*
    (when given) is applied to every name — the same override
    ``account_registry.register_account`` accepts, exposed here so a caller
    needing an explicit engine (e.g. a Codex account) does not need a second
    copy of the registry import (#2032)."""
    from viberpit import account_registry
    from dataclasses import replace
    with account_registry.locked_manifest() as manifest:
        for n in names:
            entry = account_registry.entry_for(n, provider)
            if engine is not None:
                entry = replace(entry, engine=engine)
            manifest.accounts[n] = entry
