"""Pytest configuration and global guards for the server test suite.

Loaded by pytest before any test module in this package is collected, so the
``write_json_secure`` guard below is in place before tests bind the function via
their (function-local) ``from ccswitch.creds import write_json_secure`` imports.
"""
import atexit
import functools
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


def _db_is_production(db: str | None) -> bool:
    """True if ``db`` (a CCSWITCH_DB_URL) could be the PRODUCTION database.

    The redirect below rewrites CCSWITCH_DB_URL to a throwaway SQLite file
    whenever this returns True, so it must err on the side of "yes, prod".

    - ``None`` — unset; ``server.config.Settings`` would default to the real
      store → production.
    - In-memory SQLite — ephemeral, never production.
    - File SQLite — production only if it IS the real ``~/.claude-accounts``
      store (a tmp-dir file is a test's own isolated DB).
    - Any networked URL (Postgres, …) — production UNLESS the *database name*
      contains "test". This mirrors the ``pg_url`` guard's naming convention
      and is the case the original ``str(_REAL_HOME) in db`` check missed once
      production moved from a SQLite file to Postgres: the prod URL contains no
      filesystem path, so it was silently classified non-prod and the whole
      suite ran against live Postgres.
    """
    if db is None:
        return True
    if db.startswith("sqlite"):
        if ":memory:" in db:
            return False
        return str(_REAL_HOME) in db
    from urllib.parse import urlsplit
    db_name = urlsplit(db).path.lstrip("/")
    return "test" not in db_name.lower()


def _archive_home_is_production(archive: str | None) -> bool:
    """True if ``archive`` (a CCSWITCH_ARCHIVE_HOME) is a production location.

    ``paths.archive_dir()`` reads this env var and, when unset, falls back to
    ``$CCSWITCH_HOME/archive``. A dev/deploy shell exports it at the real
    archive disk (e.g. ``/mnt/hdd-storage/ccswitch-archives``); inherited into
    a test process it overrides the per-test ``$CCSWITCH_HOME/archive`` and
    points archive/unarchive at the live archive tree. A test's OWN isolated
    archive lives under the system temp dir (``tmp_path``), so treat anything
    outside it as production. Unset is fine — it means "use the fallback".
    """
    if archive is None:
        return False
    return not str(Path(archive).resolve()).startswith(tempfile.gettempdir())


def _redirect_state_off_production():
    home = os.environ.get("CCSWITCH_HOME")
    db = os.environ.get("CCSWITCH_DB_URL")
    archive = os.environ.get("CCSWITCH_ARCHIVE_HOME")
    home_is_prod = home is None or Path(home).resolve() == _REAL_HOME
    db_is_prod = _db_is_production(db)
    archive_is_prod = _archive_home_is_production(archive)
    if not home_is_prod and not db_is_prod and not archive_is_prod:
        return  # caller already isolated everything — respect it

    session_home = Path(tempfile.mkdtemp(prefix="ccswitch-tests-"))
    atexit.register(shutil.rmtree, session_home, ignore_errors=True)
    if home_is_prod:
        os.environ["CCSWITCH_HOME"] = str(session_home)
    if db_is_prod:
        os.environ["CCSWITCH_DB_URL"] = (
            f"sqlite+aiosqlite:///{session_home / 'ccswitch.db'}"
        )
    if archive_is_prod:
        # Unset (not redirect): archive_dir() then falls back to
        # $CCSWITCH_HOME/archive, tracking each test's own (isolated)
        # CCSWITCH_HOME, which per-test patch.dict overrides freely.
        os.environ.pop("CCSWITCH_ARCHIVE_HOME", None)


_redirect_state_off_production()


# --------------------------------------------------------------------------- #
# Helper scratch config dirs must not land in the developer's real ~/.cache.  #
#                                                                             #
# account_auth.helper_env() mirrors an account config dir into                #
# scratch_config.scratch_root() (#1005). Left at its default that is          #
# ~/.cache/ccswitch/helper-cfg on the dev box, so any test that builds a      #
# helper env would litter it (and share torn-read markers across runs).       #
# Anchor it under the system temp dir; test_helper_scratch_config.py          #
# monkeypatches its own per-test value on top.                                #
# --------------------------------------------------------------------------- #
if not os.environ.get("CCSWITCH_HELPER_SCRATCH_ROOT"):
    _scratch_root = Path(tempfile.mkdtemp(prefix="ccswitch-tests-scratch-"))
    atexit.register(shutil.rmtree, _scratch_root, ignore_errors=True)
    os.environ["CCSWITCH_HELPER_SCRATCH_ROOT"] = str(_scratch_root)


# --------------------------------------------------------------------------- #
# Auth env must not leak from the dev machine into tests.                      #
#                                                                              #
# A developer box runs with CCSWITCH_TAILNET_ALLOWLIST set to the owner's own  #
# identity (and a non-loopback CCSWITCH_HOST). get_settings() reads both fresh  #
# (uncached), so verify_identity() rejects the tests' synthetic identities      #
# (e.g. wstest@example.com) — WS/HTTP handshakes close 1008/401 — and the boot  #
# gate refuses an empty allowlist on a non-loopback bind. The suite then passes #
# in CI (env unset) but fails locally. Establish the same permissive baseline   #
# ~25 test files already set individually: CCSWITCH_DEV=1 + empty allowlist =    #
# "any identity passes". Tests that exercise the allowlist / boot gate override  #
# this explicitly via monkeypatch / patch.dict(clear=True).                      #
os.environ["CCSWITCH_TAILNET_ALLOWLIST"] = ""
os.environ["CCSWITCH_DEV"] = "1"


# --------------------------------------------------------------------------- #
# TestClient peers must look like the deployed topology.                       #
#                                                                              #
# `tailscale serve` terminates the tailnet connection and proxies to the edge's #
# loopback bind, so in production the peer asserting Tailscale-User-Login is    #
# ALWAYS 127.0.0.1 — which is why only a trusted peer may assert that header    #
# (server/auth/identity.py). Starlette defaults TestClient to                   #
# client=("testclient", 50000): an address that cannot exist in production and  #
# is not entitled to assert an identity. Left alone, the ~88 suites that send   #
# the header would all authenticate over a path the real deployment never uses. #
#                                                                              #
# Patch the default at import (before collection) rather than via a fixture —   #
# several test modules build their TestClient at module scope, which a fixture  #
# would be too late to affect. A test that wants an untrusted peer passes       #
# client=(...) explicitly and this leaves it alone.                            #
# --------------------------------------------------------------------------- #
import starlette.testclient as _starlette_testclient  # noqa: E402

_orig_testclient_init = _starlette_testclient.TestClient.__init__


@functools.wraps(_orig_testclient_init)
def _testclient_init_loopback_peer(self, *args, **kwargs):
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    return _orig_testclient_init(self, *args, **kwargs)


_starlette_testclient.TestClient.__init__ = (  # type: ignore[method-assign]
    _testclient_init_loopback_peer
)


# --------------------------------------------------------------------------- #
# A live session daemon on the dev box must not leak into tests.              #
#                                                                             #
# create_app() (when a test doesn't inject an orchestrator) probes the        #
# DEFAULT_SOCKET_PATH. On a machine actually running ccswitch, that socket    #
# exists → create_app either flips to remote mode or hard-fails with          #
# "refusing in-process fallback" (the split-brain guard). CI has no daemon,   #
# so those tests pass there but fail locally. Point the default socket at a   #
# path that cannot exist, so the ambient probe reports "no daemon on this     #
# host" (the CI/in-process default).                                          #
#                                                                             #
# We patch the PATH, not _daemon_present/_daemon_compatible: the latter       #
# compose (_daemon_compatible calls _daemon_present internally), so stubbing  #
# them breaks tests that probe a real socket they serve themselves            #
# (test_remote_default). Redirecting only the default path leaves the real    #
# functions intact for any explicit endpoint a test provides. Tests that      #
# exercise remote mode set CCSWITCH_SESSIOND_SOCKET explicitly, bypassing     #
# the default-path branch entirely.                                           #
import pytest


# Test modules that exercise the daemon/IPC layer itself assert on the real
# DEFAULT_SOCKET_PATH (value, precedence, live-socket probing), so the redirect
# below must NOT apply to them. They manage their own daemon/socket state.
_DAEMON_LAYER_MODULE_PREFIXES = ("test_ipc_", "test_remote_", "test_daemon_")


@pytest.fixture(autouse=True)
def _no_ambient_session_daemon(request, monkeypatch):
    modname = request.node.module.__name__.rsplit(".", 1)[-1]
    if modname.startswith(_DAEMON_LAYER_MODULE_PREFIXES):
        return
    import server.ipc.paths as _paths
    monkeypatch.setattr(
        _paths, "DEFAULT_SOCKET_PATH", "/nonexistent/ccswitch-tests-no-daemon.sock",
        raising=False,
    )


@pytest.fixture(autouse=True)
def _reset_helper_call_breakers():
    # The background-helper backoff/circuit-breaker (server.sessions.helper_call)
    # keeps process-global per-account state. Reset it around every test so a
    # rate-limit/auth failure in one test can't leave a lane open and skip a
    # later test's helper call, which would surface as an order-dependent flake.
    from server.sessions import helper_call
    helper_call.reset_breakers()
    yield
    helper_call.reset_breakers()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Fresh per-test DB under tmp_path — kills cross-file order dependence (#728).

    Bare ``await init_db()`` re-resolves CCSWITCH_DB_URL on every call (settings
    are uncached), so tests that relied on it shared the per-session redirect DB
    above with every other bare caller on their worker — visible rows depended
    on which files ran before them. Pointing the env var at a private file makes
    all existing bare call sites land here, and the eager init gives tests that
    never call init_db themselves a schema to talk to. Opt in by name, or via a
    module-local autouse wrapper in files whose every test needs it.
    """
    import asyncio

    from server.db import init_db

    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("CCSWITCH_DB_URL", url)
    asyncio.run(init_db(url))
    return url


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
# redirects every in-memory URL to a unique temp FILE (durable across          #
# connection close). Each init_db still gets its own empty DB (per-init         #
# isolation); a late write reopens the same file with its tables intact and is  #
# harmless instead of crashing. Production is unaffected (real file DB, never   #
# this shim).                                                                   #
#                                                                              #
# Pool choice — NullPool, NOT StaticPool (issue #890). A StaticPool engine      #
# holds ONE shared DBAPI connection for every operation. Under `-n auto`        #
# contention, when any bounded_wait teardown phase (dispose-db,                 #
# `stop:primary-lease-release`, …) is abandoned mid-query, that single          #
# connection's aiosqlite worker thread is left wedged — and because the engine  #
# keeps a handle to it, `engine.dispose()` / asyncio loop-close must await its  #
# close, hanging TestClient.__exit__'s portal-thread join to the 300s pytest-   #
# timeout (#846/#881/#882). #882 file-backed the DB but kept StaticPool, so the #
# shared wedge-able connection survived and the hang merely migrated phases.    #
# NullPool retains NO connection: it opens a fresh one per operation and closes #
# it on return, so the engine never holds a reusable handle to a checked-out    #
# (possibly wedged) connection — teardown has nothing to await and the flake's  #
# substrate is gone regardless of which phase overruns its budget. The file     #
# backing keeps the schema durable, and WAL keeps COMMITTED writes visible      #
# across the now-separate connections (db_session/get_db always commit).        #
#                                                                               #
# NullPool has no pool to return an ABANDONED checked-out connection to, so it  #
# survives to GC after its loop is gone; the loop-safe worker installed by      #
# server/db/aiosqlite_guard.py is what keeps that harmless (issue #1134).       #
# --------------------------------------------------------------------------- #
def _cleanup_sqlite_file(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _install_durable_inmemory_db():
    import server.db as _db
    from sqlalchemy.pool import NullPool

    _orig_make_engine = _db._make_engine
    if getattr(_orig_make_engine, "_durable_inmemory_shim", False):
        return  # already wrapped (conftest re-imported)

    def _durable_make_engine(db_url: str, *, poolclass=None):
        if db_url.startswith("sqlite") and ":memory:" in db_url:
            fd, path = tempfile.mkstemp(prefix="ccswitch-test-db-", suffix=".sqlite")
            os.close(fd)
            atexit.register(_cleanup_sqlite_file, path)
            db_url = f"sqlite+aiosqlite:///{path}"
            # NullPool, not StaticPool: no single shared wedge-able connection,
            # so no abandoned teardown phase can hang loop-close (issue #890).
            poolclass = NullPool
        return _orig_make_engine(db_url, poolclass=poolclass)

    _durable_make_engine._durable_inmemory_shim = True
    _db._make_engine = _durable_make_engine


_install_durable_inmemory_db()


# --------------------------------------------------------------------------- #
# No-real-model guard for the "auto" session-model assessor.                   #
#                                                                              #
# Automation spawns resolve their session model via                            #
# `model_pick.resolve_model`, whose default config value is "auto" — a one-    #
# shot claude-agent-sdk query. Every orchestrator/automation test that walks   #
# a spawn path (pr-doctor fix/consolidate, backlog spawn) would therefore      #
# attempt a REAL model call unless it stubbed the assessor — a silent,         #
# nondeterministic network dependency in ~any spawn test, present or future.   #
#                                                                              #
# `assess_model` is the SOLE entry to the real SDK on this path and already    #
# has a fail-open contract (any failure → None → account default), so the      #
# guard makes "SDK unavailable" the universal test-time truth: calls without   #
# an injected `_query_fn` return None (production's own degraded behavior)     #
# instead of touching the SDK. Direct assessor tests inject `_query_fn` and    #
# exercise the real logic; resolve_model tests inject `_assess`.               #
# --------------------------------------------------------------------------- #
def _install_no_real_model_guard():
    import server.automations.model_pick as _mp

    _orig = _mp.assess_model
    if getattr(_orig, "_no_real_model_guard", False):
        return  # already guarded (conftest re-imported)

    async def _guarded_assess_model(task_summary, *, _query_fn=None, **kw):
        if _query_fn is None:
            return None  # tests never reach the real model; None = fail-open
        return await _orig(task_summary, _query_fn=_query_fn, **kw)

    _guarded_assess_model._no_real_model_guard = True
    _mp.assess_model = _guarded_assess_model


_install_no_real_model_guard()


def _install_no_real_grouping_guard():
    """Same contract as the assess_model guard, for the backlog grouping
    optimizer: ``propose_groups`` is the sole entry to the real SDK on the
    plan-refresh path, and any orchestrator/loop test that walks a refresh
    would otherwise make a REAL Sonnet call. Without an injected ``_query_fn``
    it returns ``[]`` — production's own model-failure behavior (the refresh
    seam raises and the loop backs off). Direct grouping tests inject
    ``_query_fn`` and exercise the real logic."""
    import server.automations.backlog_grouping as _bg

    _orig = _bg.propose_groups
    if getattr(_orig, "_no_real_model_guard", False):
        return  # already guarded (conftest re-imported)

    async def _guarded_propose_groups(candidates, config, *, _query_fn=None, **kw):
        if _query_fn is None:
            return []
        return await _orig(candidates, config, _query_fn=_query_fn, **kw)

    _guarded_propose_groups._no_real_model_guard = True
    _bg.propose_groups = _guarded_propose_groups


_install_no_real_grouping_guard()


# --------------------------------------------------------------------------- #
# Teardown watchdog on every TestClient exit (issue #846).                     #
#                                                                              #
# The dominant CI flake is a random test dying at pytest-timeout's 300s inside #
# TestClient.__exit__ (lifespan shutdown → anyio portal join). faulthandler    #
# shows only thread stacks — the portal loop "idle in select" — because the    #
# never-completing thing is a coroutine. Wrapping __exit__ HERE covers every   #
# TestClient in the suite with one installer: grab the app loop through the    #
# portal while it is still healthy, arm a threading.Timer, and if teardown     #
# overruns ~45s dump every asyncio task (name + coroutine await-chain frames)  #
# to the real stderr so the hang names its culprit. Happy path: timer          #
# cancelled, zero overhead. Tune via CCSWITCH_TEARDOWN_WATCHDOG_S.             #
# --------------------------------------------------------------------------- #
def _install_testclient_teardown_watchdog():
    import asyncio as _asyncio

    from fastapi.testclient import TestClient as _TestClient

    from server.tests.teardown_watchdog import TeardownWatchdog

    _orig_exit = _TestClient.__exit__
    if getattr(_orig_exit, "_teardown_watchdog_shim", False):
        return  # already wrapped (conftest re-imported)

    def _watched_exit(self, *exc):
        loop = None
        portal = getattr(self, "portal", None)
        if portal is not None:
            try:
                loop = portal.call(_asyncio.get_running_loop)
            except Exception:
                loop = None
        if loop is None:  # client never entered / portal gone — nothing to watch
            return _orig_exit(self, *exc)
        with TeardownWatchdog(loop, label="TestClient.__exit__"):
            return _orig_exit(self, *exc)

    _watched_exit._teardown_watchdog_shim = True
    _TestClient.__exit__ = _watched_exit


_install_testclient_teardown_watchdog()


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
    client_other_cert: str
    client_other_key: str
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
    co_key, co_cert = make_leaf(ca_key, ca_cert, "edge-other")   # SAME CA, different identity
    write_pem_cert(d / "client_other.pem", co_cert)
    write_pem_key(d / "client_other.key", co_key)
    rca_key, rca_cert = make_ca()
    write_pem_cert(d / "rogue_ca.pem", rca_cert)
    rc_key, rc_cert = make_leaf(rca_key, rca_cert, "rogue")
    write_pem_cert(d / "rogue_client.pem", rc_cert)
    write_pem_key(d / "rogue_client.key", rc_key)
    return MTLSCerts(
        ca=str(d / "ca.pem"),
        server_cert=str(d / "server.pem"), server_key=str(d / "server.key"),
        client_cert=str(d / "client.pem"), client_key=str(d / "client.key"),
        client_other_cert=str(d / "client_other.pem"),
        client_other_key=str(d / "client_other.key"),
        rogue_ca=str(d / "rogue_ca.pem"),
        rogue_client_cert=str(d / "rogue_client.pem"),
        rogue_client_key=str(d / "rogue_client.key"),
    )


# --------------------------------------------------------------------------- #
# Postgres-backed test harness (Task 1: server/postgres-migration)
# --------------------------------------------------------------------------- #
import pytest_asyncio


@pytest.fixture(scope="session")
def pg_url() -> str:
    """CCSWITCH_TEST_PG_URL, guarded against ever pointing at production.

    pg_engine (below) unconditionally drop_all's this database. A cutover
    operator's shell can plausibly hold BOTH the production $PG_URL and
    CCSWITCH_TEST_PG_URL at once (see docs/runbooks/postgres-cutover.md) — one
    wrong export and a stray test run would drop the production schema. This
    contradicts the "Production isolation" guarantee at the top of this file,
    so it is enforced the same way: fail fast, before any connection is made,
    rather than trust the caller got the env right.

    The only check that's actually safe here is a naming convention — refuse
    to proceed unless the *database name* in the URL contains "test". This
    does not require the database to exist or be reachable.
    """
    url = os.environ.get("CCSWITCH_TEST_PG_URL")
    if not url:
        pytest.skip("CCSWITCH_TEST_PG_URL not set")
    from urllib.parse import urlsplit
    db_name = urlsplit(url).path.lstrip("/")
    if "test" not in db_name.lower():
        pytest.fail(
            f"CCSWITCH_TEST_PG_URL targets database {db_name!r}, which does not "
            f"contain 'test'. Refusing to run: pg_engine unconditionally "
            f"drop_all's this database, and this fixture must never be pointed "
            f"at a production database. Point CCSWITCH_TEST_PG_URL at a "
            f"database whose name contains 'test' (e.g. 'ccswitch_test').")
    return url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pg_engine(pg_url):
    """A Postgres engine with the full schema, torn down after the session.

    Session-scoped because create_all/drop_all over 26 tables is far too slow to
    repeat per test. Tests that need isolation must clean up their own rows.

    Session-scoped + loop_scope="session": any test consuming this fixture MUST
    carry @pytest.mark.asyncio(loop_scope="session") on its own marker, or you'll
    hit a confusing "Future attached to a different loop" error that looks like a
    database problem but isn't (pytest-asyncio 1.4.0 strict mode).
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from server.db.models import Base

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
