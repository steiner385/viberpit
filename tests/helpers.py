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
            yield d
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def usage(session=10.0, weekly=20.0, extra=False, mins=120):
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(minutes=mins)
    return Usage(session, reset, weekly, reset, extra)
