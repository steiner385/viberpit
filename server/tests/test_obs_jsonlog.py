import asyncio
import json
import logging
import sys

from server.audit import redact_text
from server.obs.context import bind_session
from server.obs.jsonlog import ContextFilter, JSONFormatter, _PoolCancelledFilter


def _format(record: logging.LogRecord) -> dict:
    f = JSONFormatter()
    ContextFilter().filter(record)
    return json.loads(f.format(record))


def test_redact_text_strips_api_key():
    out = redact_text("token sk-ant-api03-SECRETSECRET here")
    assert "sk-ant" not in out and "[REDACTED]" in out


def test_format_basic_fields():
    rec = logging.LogRecord("n", logging.WARNING, __file__, 1, "hello", None, None)
    d = _format(rec)
    assert d["level"] == "WARNING"
    assert d["msg"] == "hello"
    assert d["proc_epoch"] and d["ts"].endswith("+00:00")


def test_format_injects_session_id():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "x", None, None)
    with bind_session("sess-42"):
        ContextFilter().filter(rec)
    d = json.loads(JSONFormatter().format(rec))
    assert d["session_id"] == "sess-42"


def test_format_event_and_fields():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "session.start", None, None)
    rec.event = "session.start"
    rec.fields = {"reason": "new", "obj": object()}  # non-serializable obj coerced
    d = _format(rec)
    assert d["event"] == "session.start"
    assert d["fields"]["reason"] == "new"
    assert isinstance(d["fields"]["obj"], str)  # repr fallback


def test_format_redacts_structured_fields():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "x", None, None)
    rec.fields = {"creds": "sk-ant-api03-LEAKLEAK"}
    d = _format(rec)
    assert "sk-ant" not in json.dumps(d)


def test_format_includes_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("n", logging.ERROR, __file__, 1, "failed", None, None)
        rec.exc_info = sys.exc_info()
    d = _format(rec)
    assert "ValueError: boom" in d["exc"]


def _make_pool_record(exc_type=None, level=logging.ERROR) -> logging.LogRecord:
    rec = logging.LogRecord(
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
        level,
        __file__,
        1,
        "Exception during reset or similar",
        None,
        None,
    )
    if exc_type is not None:
        try:
            raise exc_type()
        except exc_type:
            rec.exc_info = sys.exc_info()
    return rec


def test_pool_cancelled_filter_suppresses_cancelled_error():
    f = _PoolCancelledFilter()
    rec = _make_pool_record(asyncio.CancelledError)
    assert f.filter(rec) is False


def test_pool_cancelled_filter_passes_other_errors():
    f = _PoolCancelledFilter()
    rec = _make_pool_record(RuntimeError)
    assert f.filter(rec) is True


def test_pool_cancelled_filter_passes_non_error_levels():
    f = _PoolCancelledFilter()
    rec = _make_pool_record(asyncio.CancelledError, level=logging.WARNING)
    assert f.filter(rec) is True


def test_pool_cancelled_filter_passes_records_without_exc_info():
    f = _PoolCancelledFilter()
    rec = logging.LogRecord(
        "sqlalchemy.pool", logging.ERROR, __file__, 1, "bare error", None, None
    )
    assert f.filter(rec) is True


def test_configure_logging_installs_pool_cancelled_filter(monkeypatch, tmp_path):
    import server.obs.jsonlog as jl
    monkeypatch.setattr(jl, "_configured", False)
    monkeypatch.setenv("VIBERPIT_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    pool_logger = logging.getLogger("sqlalchemy.pool")
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_pool_filters = pool_logger.filters[:]
    try:
        jl.configure_logging()
        assert any(isinstance(f, _PoolCancelledFilter) for f in pool_logger.filters)
    finally:
        for h in root.handlers[:]:
            if h not in saved_handlers:
                h.close()
                root.removeHandler(h)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        pool_logger.filters[:] = saved_pool_filters


def test_configure_logging_idempotent_and_adds_handlers(monkeypatch, tmp_path):
    import logging as _logging
    import server.obs.jsonlog as jl
    monkeypatch.setattr(jl, "_configured", False)
    monkeypatch.setenv("VIBERPIT_LOG_DIR", str(tmp_path))
    root = _logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    try:
        jl.configure_logging()
        jl.configure_logging()  # idempotent — second call is a no-op
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, jl.JSONFormatter)]
        assert json_handlers  # at least the stream handler attached
        # Named per process since #1010 — the edge and the daemon must never share
        # one rotating handle. This call defaults to process="edge".
        assert (tmp_path / "viberpit-edge.jsonl").exists()
    finally:
        # configure_logging mutates the root logger; restore it so other tests see a clean root.
        for h in root.handlers[:]:
            if h not in saved:
                h.close()
                root.removeHandler(h)
        root.handlers[:] = saved
        root.setLevel(saved_level)


def test_file_sink_writes_on_a_listener_thread_not_the_caller(monkeypatch, tmp_path):
    """#2356: RotatingFileHandler.emit stats the file (shouldRollover) and writes on
    the caller's thread, so a log.* on the event loop stalled it whenever the log dir's
    filesystem was slow. The caller must only format and enqueue; the file handler runs
    on the listener thread. Context ids and tracebacks must still reach the JSON line."""
    import threading

    import server.obs.jsonlog as jl
    from server.obs import errsink
    monkeypatch.setattr(jl, "_configured", False)
    monkeypatch.setenv("VIBERPIT_LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    saved, saved_level = root.handlers[:], root.level
    emit_threads: list[str] = []
    real_emit = logging.handlers.RotatingFileHandler.emit

    def _spy(self, record):
        emit_threads.append(threading.current_thread().name)
        return real_emit(self, record)

    monkeypatch.setattr(logging.handlers.RotatingFileHandler, "emit", _spy)
    try:
        jl.configure_logging()
        added = [h for h in root.handlers if h not in saved]
        queued = [h for h in added if isinstance(h, jl._QueuedFileHandler)]
        assert len(queued) == 1
        with bind_session("sess-2356"):
            try:
                raise ValueError("boom")
            except ValueError:
                logging.getLogger("t2356").error("file sink check", exc_info=True)
        for h in added:
            h.close()           # stops the listener, draining the queue
            root.removeHandler(h)
        assert emit_threads and threading.main_thread().name not in emit_threads
        lines = [json.loads(ln) for ln in
                 (tmp_path / "viberpit-edge.jsonl").read_text().splitlines()]
        rec = next(r for r in lines if r["msg"] == "file sink check")
        assert rec["session_id"] == "sess-2356"
        assert "ValueError: boom" in rec["exc"]
    finally:
        errsink.reset_for_tests()
        for h in root.handlers[:]:
            if h not in saved:
                h.close()
                root.removeHandler(h)
        root.handlers[:] = saved
        root.setLevel(saved_level)
