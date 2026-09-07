"""#1423: the session daemon must configure structured logging like the edge does.
Without it the root logger has no handler, INFO is dropped and WARNING+ leaks as
bare text through logging.lastResort."""
from __future__ import annotations

import logging

from fastapi import FastAPI


def test_serve_configures_json_logging(monkeypatch, tmp_path):
    from server.ipc import serve as serve_mod
    from server.obs import errsink, jsonlog

    root = logging.getLogger()
    before = list(root.handlers)
    before_process = jsonlog.PROCESS
    monkeypatch.setattr(jsonlog, "_configured", False)
    monkeypatch.setattr(serve_mod, "build_daemon", lambda: (FastAPI(), object()))
    monkeypatch.setattr(serve_mod.uvicorn, "run", lambda *a, **k: None)
    try:
        serve_mod.serve(str(tmp_path / "d.sock"))
        assert any(isinstance(h.formatter, jsonlog.JSONFormatter) for h in root.handlers), \
            "daemon entrypoint did not install the JSON handler"
        assert jsonlog.PROCESS == "daemon"
    finally:
        jsonlog.PROCESS = before_process
        errsink.reset_for_tests()  # configure_logging() installs the error sink singleton
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_configure_logging_defaults_to_edge(monkeypatch):
    from server.obs import errsink, jsonlog
    root = logging.getLogger()
    before = list(root.handlers)
    before_process = jsonlog.PROCESS
    monkeypatch.setattr(jsonlog, "_configured", False)
    try:
        jsonlog.configure_logging()
        assert jsonlog.PROCESS == "edge"
    finally:
        jsonlog.PROCESS = before_process
        errsink.reset_for_tests()  # configure_logging() installs the error sink singleton
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
