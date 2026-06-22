"""Pytest configuration and global guards for the server test suite.

Loaded by pytest before any test module in this package is collected, so the
``write_json_secure`` guard below is in place before tests bind the function via
their (function-local) ``from ccswitch.creds import write_json_secure`` imports.
"""
import atexit
import importlib
import os
import shutil
import tempfile
from pathlib import Path


# --------------------------------------------------------------------------- #
# Production isolation — redirect ALL ccswitch state to a per-session temp dir #
# BEFORE any test module is collected.                                         #
#                                                                              #
# `paths.root()` reads CCSWITCH_HOME and `server.config.Settings` reads        #
# CCSWITCH_DB_URL lazily, BOTH defaulting to the REAL ~/.claude-accounts when  #
# unset. Only ~20 of ~80 test files set CCSWITCH_HOME themselves, so the rest  #
# silently resolved to the live store: reading/writing the real ledger.json    #
# and ccswitch.db. That is how a reauth test stamped `needs-reauth` onto the   #
# real `primary` account and how phantom `alpha`/`dave` rows reached prod.     #
#                                                                              #
# Setting both env vars here — at import, before collection — makes production #
# UNREACHABLE by default: the value any test sees at session start is a temp   #
# dir, never the real store. Tests that need their own isolated home still     #
# override CCSWITCH_HOME via `patch.dict()` within their own scope (and even   #
# their restore falls back to this temp dir, not to ~/.claude-accounts).       #
#                                                                              #
# Each var is redirected independently iff it is unset or points at the real   #
# store, so a harness that already isolates one or both is left untouched.     #
# --------------------------------------------------------------------------- #
_REAL_HOME = (Path.home() / ".claude-accounts").resolve()


def _redirect_state_off_production():
    home = os.environ.get("CCSWITCH_HOME")
    db = os.environ.get("CCSWITCH_DB_URL")
    home_is_prod = home is None or Path(home).resolve() == _REAL_HOME
    db_is_prod = db is None or str(_REAL_HOME) in db
    if not home_is_prod and not db_is_prod:
        return  # caller already isolated both — respect it

    session_home = Path(tempfile.mkdtemp(prefix="ccswitch-tests-"))
    atexit.register(shutil.rmtree, session_home, ignore_errors=True)
    if home_is_prod:
        os.environ["CCSWITCH_HOME"] = str(session_home)
    if db_is_prod:
        os.environ["CCSWITCH_DB_URL"] = (
            f"sqlite+aiosqlite:///{session_home / 'ccswitch.db'}"
        )


_redirect_state_off_production()


# Enable asyncio mode for all tests in this package
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )


# --------------------------------------------------------------------------- #
# Real-store write guard — structural fix for the recurring phantom "alpha"    #
# account leak (PR #98, and the test-store-leak trap that kept reappearing).   #
#                                                                              #
# `paths.slot_path()` reads CCSWITCH_HOME lazily at call time, defaulting to   #
# the user's REAL ~/.claude-accounts. A test that seeds a credential via       #
# `write_json_secure(paths.slot_path(name), ...)` BEFORE entering its          #
# `with patch.dict(os.environ, {"CCSWITCH_HOME": tmp})` block writes a fake    #
# `*.cred.json` into the real store. Account discovery globs                   #
# `store/*.cred.json`, so the live usage poller then picks it up, fails auth,  #
# and stamps needs-reauth into the real ledger — a phantom account that        #
# reappears every test run.                                                    #
#                                                                              #
# `write_json_secure` is the SOLE store-write primitive in the codebase, so    #
# wrapping it here turns a silent production-state corruption into an          #
# immediate, loud test failure that points at the exact fix.                   #
# --------------------------------------------------------------------------- #
_REAL_STORE = (Path.home() / ".claude-accounts").resolve()


def _install_real_store_write_guard():
    import ccswitch.creds as _creds

    _orig = _creds.write_json_secure

    # Already guarded (e.g. conftest re-imported) — leave it alone.
    if getattr(_orig, "_is_real_store_guard", False):
        return

    def _guarded_write_json_secure(path, data):
        resolved = Path(path).resolve()
        if resolved == _REAL_STORE or _REAL_STORE in resolved.parents:
            raise RuntimeError(
                f"Test attempted to write {resolved} under the REAL account store "
                f"{_REAL_STORE}.\nCCSWITCH_HOME is unset or not pointed at a temp dir, "
                "so this write would leak a phantom account into ~/.claude-accounts "
                "(see PR #98 / the test-store-leak trap).\nMove the seeding INSIDE the "
                "`with patch.dict(os.environ, {'CCSWITCH_HOME': tmp}):` block (or use "
                "temp_env()) so paths.slot_path() resolves to the temp store."
            )
        return _orig(path, data)

    _guarded_write_json_secure._is_real_store_guard = True

    # Rebind every reference: the `creds` module attribute (picked up by tests'
    # function-local imports) plus the src modules that captured it at import
    # time via `from .creds import write_json_secure`.
    for _modname in (
        "ccswitch.creds",
        "ccswitch.configdir",
        "ccswitch.auth_login",
        "ccswitch.accounts",
    ):
        try:
            _m = importlib.import_module(_modname)
        except Exception:
            continue
        if getattr(_m, "write_json_secure", None) is _orig:
            _m.write_json_secure = _guarded_write_json_secure


_install_real_store_write_guard()
