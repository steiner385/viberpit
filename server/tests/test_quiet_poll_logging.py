"""#831: steady-state poll traffic between edge and daemon stays out of the journal.

Two sources dominated it with one browser tab open: the edge's httpx client logging
an INFO line per daemon RPC (~190/min), and the daemon's uvicorn access log echoing
the same calls (~130/min).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from server.ipc.serve import QUIET_ACCESS_PATHS, QuietPollAccessFilter

_REPO = Path(__file__).resolve().parents[2]


def _access_record(path: str, status: int) -> logging.LogRecord:
    # Same shape uvicorn's h11/httptools protocols emit.
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, '%s - "%s %s HTTP/%s" %d',
        ("100.64.0.1:5000", "POST", path, "1.1", status), None)


@pytest.mark.parametrize("path", ["/rpc/list_sessions", "/rpc/all_prs", "/info",
                                  "/rpc/prs_for_many?x=1"])
def test_successful_polled_read_is_dropped(path):
    assert QuietPollAccessFilter().filter(_access_record(path, 200)) is False


@pytest.mark.parametrize("path,status", [
    ("/rpc/list_sessions", 500),     # a failing poll is exactly what an operator needs
    ("/rpc/list_sessions", 404),
    ("/rpc/send_prompt", 200),       # mutations keep their line
    ("/rpc/create_session", 200),
    ("/events", 101),
])
def test_failures_and_mutations_still_log(path, status):
    assert QuietPollAccessFilter().filter(_access_record(path, status)) is True


def test_non_access_shaped_records_pass_through():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 0, "plain", None, None)
    assert QuietPollAccessFilter().filter(rec) is True


def test_every_quiet_rpc_is_a_real_daemon_method():
    """A renamed RPC would silently start logging again; a typo would never match."""
    src = "\n".join(p.read_text() for p in (_REPO / "server" / "ipc").glob("*.py")
                    if p.name != "serve.py")
    for path in QUIET_ACCESS_PATHS:
        if not path.startswith("/rpc/"):
            continue
        name = path.removeprefix("/rpc/")
        assert re.search(rf'["\']{re.escape(name)}["\']', src), f"{name} is not a daemon RPC"


def test_serve_installs_the_filter_on_the_uvicorn_access_logger(monkeypatch):
    from server.ipc import serve as serve_mod

    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    class _App:
        class router:
            lifespan_context = None

    monkeypatch.setattr(serve_mod, "configure_logging", lambda **_: None)
    monkeypatch.setattr(serve_mod, "build_daemon", lambda: (_App(), object()))
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(serve_mod, "uvicorn_run_kwargs", lambda *a, **k: {})
    try:
        serve_mod.serve(socket_path="/tmp/unused.sock")
        assert any(isinstance(f, QuietPollAccessFilter) for f in access.filters)
    finally:
        for f in list(access.filters):
            if f not in before:
                access.removeFilter(f)


def test_configure_logging_demotes_per_request_httpx_lines(monkeypatch):
    from server.obs import errsink, jsonlog

    monkeypatch.setenv("VIBERPIT_LOG_DIR", "")
    monkeypatch.setattr(jsonlog, "_configured", False)
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    before_process = jsonlog.PROCESS
    try:
        jsonlog.configure_logging(process="edge")
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)
        assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)
    finally:
        jsonlog.PROCESS = before_process
        errsink.reset_for_tests()
        for h in list(root.handlers):
            if h not in before_handlers:
                h.close()
                root.removeHandler(h)
        for n, lvl in before_levels.items():
            logging.getLogger(n).setLevel(lvl)
