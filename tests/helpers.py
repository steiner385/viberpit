import contextlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
            "CCSWITCH_HOME", "CLAUDE_CONFIG_DIR", "CCSWITCH_CLAUDE_BIN",
            "CCSWITCH_ARCHIVE_HOME",
        )}
        os.environ["CCSWITCH_HOME"] = str(home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(shared)
        os.environ.pop("CCSWITCH_ARCHIVE_HOME", None)
        try:
            # Clear module-level singletons that would otherwise leak between tests.
            import sys
            if "ccswitch.accounts" in sys.modules:
                sys.modules["ccswitch.accounts"]._dead_accounts.clear()
            yield d
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def usage(session=10.0, weekly=20.0, extra=False, mins=120):
    """An Anthropic two-window QuotaSnapshot — the shape a real fetch produces."""
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(minutes=mins)
    return anthropic_snapshot(session, reset, weekly, reset, extra)


def register_accounts(*names, provider="anthropic"):
    """Put *names* in the accounts.json manifest (inside a temp_env)."""
    from ccswitch import account_registry
    with account_registry.locked_manifest() as manifest:
        for n in names:
            manifest.accounts[n] = account_registry.entry_for(n, provider)
