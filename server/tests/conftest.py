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


# --------------------------------------------------------------------------- #
# Durable in-memory test DBs — structural fix for the recurring test_ws.py     #
# "no such table: sessions" flake (red CI on PRs #188/#192/#197/#200).         #
#                                                                              #
# ~20 test files point CCSWITCH_DB_URL at `sqlite+aiosqlite:///:memory:`.      #
# SQLAlchemy maps a `:memory:` SQLite URL to a StaticPool, so the ENTIRE DB    #
# lives inside one connection: when that connection closes — engine dispose /  #
# GC on app-lifespan shutdown, or replacement by the next test's init_db() —   #
# the schema is destroyed. A background task that writes AFTER teardown (an    #
# orchestrator status writer persisting `status='ended'`, which is not always  #
# cancelled before the engine goes away) then forces StaticPool to reopen a    #
# FRESH, EMPTY in-memory DB and dies with "no such table: sessions". Whether   #
# the late write lands before or after teardown is a race, so it flaked        #
# intermittently — only in test_ws.py, the one suite that drives the full app  #
# (WebSocket bridge + orchestrator background loops) hard enough to hit it.    #
#                                                                              #
# `server.db._make_engine` is the SOLE engine factory, so wrapping it here     #
# redirects every in-memory URL to a unique temp FILE, pinned to StaticPool so #
# it keeps the SAME single-connection semantics a `:memory:` DB has (one       #
# shared connection — uncommitted writes visible to later reads, no QueuePool  #
# cross-connection visibility surprises) while being durable across connection #
# close. Each init_db still gets its own empty DB (per-init isolation), but a   #
# late write reopens the same file with its tables intact and is harmless      #
# instead of crashing. Production is unaffected (real file DB, never this shim).#
# --------------------------------------------------------------------------- #
def _cleanup_sqlite_file(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _install_durable_inmemory_db():
    import server.db as _db
    from sqlalchemy.pool import StaticPool

    _orig_make_engine = _db._make_engine
    if getattr(_orig_make_engine, "_durable_inmemory_shim", False):
        return  # already wrapped (conftest re-imported)

    def _durable_make_engine(db_url: str, *, poolclass=None):
        if db_url.startswith("sqlite") and ":memory:" in db_url:
            fd, path = tempfile.mkstemp(prefix="ccswitch-test-db-", suffix=".sqlite")
            os.close(fd)
            atexit.register(_cleanup_sqlite_file, path)
            db_url = f"sqlite+aiosqlite:///{path}"
            poolclass = StaticPool  # keep the one-connection semantics of :memory:
        return _orig_make_engine(db_url, poolclass=poolclass)

    _durable_make_engine._durable_inmemory_shim = True
    _db._make_engine = _durable_make_engine


_install_durable_inmemory_db()


# --------------------------------------------------------------------------- #
# mTLS cert fixture for testing mutual TLS scenarios                         #
# --------------------------------------------------------------------------- #
import dataclasses

import pytest

from server.tests.tls_certs import make_ca, make_leaf, write_pem_cert, write_pem_key


@dataclasses.dataclass(frozen=True)
class MTLSCerts:
    ca: str
    server_cert: str
    server_key: str
    client_cert: str
    client_key: str
    rogue_ca: str
    rogue_client_cert: str
    rogue_client_key: str


@pytest.fixture
def mtls_certs(tmp_path) -> MTLSCerts:
    d = tmp_path / "tls"
    d.mkdir()
    ca_key, ca_cert = make_ca()
    write_pem_cert(d / "ca.pem", ca_cert)
    s_key, s_cert = make_leaf(ca_key, ca_cert, "sessiond",
                              san_dns=["localhost"], san_ip=["127.0.0.1"])
    write_pem_cert(d / "server.pem", s_cert)
    write_pem_key(d / "server.key", s_key)
    c_key, c_cert = make_leaf(ca_key, ca_cert, "edge")
    write_pem_cert(d / "client.pem", c_cert)
    write_pem_key(d / "client.key", c_key)
    rca_key, rca_cert = make_ca()
    write_pem_cert(d / "rogue_ca.pem", rca_cert)
    rc_key, rc_cert = make_leaf(rca_key, rca_cert, "rogue")
    write_pem_cert(d / "rogue_client.pem", rc_cert)
    write_pem_key(d / "rogue_client.key", rc_key)
    return MTLSCerts(
        ca=str(d / "ca.pem"),
        server_cert=str(d / "server.pem"), server_key=str(d / "server.key"),
        client_cert=str(d / "client.pem"), client_key=str(d / "client.key"),
        rogue_ca=str(d / "rogue_ca.pem"),
        rogue_client_cert=str(d / "rogue_client.pem"),
        rogue_client_key=str(d / "rogue_client.key"),
    )
