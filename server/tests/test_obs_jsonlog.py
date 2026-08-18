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
    monkeypatch.setenv("CCSWITCH_LOG_DIR", str(tmp_path))
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
    monkeypatch.setenv("CCSWITCH_LOG_DIR", str(tmp_path))
    root = _logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    try:
        jl.configure_logging()
        jl.configure_logging()  # idempotent — second call is a no-op
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, jl.JSONFormatter)]
        assert json_handlers  # at least the stream handler attached
        assert (tmp_path / "ccswitch.jsonl").exists()
    finally:
        # configure_logging mutates the root logger; restore it so other tests see a clean root.
        for h in root.handlers[:]:
            if h not in saved:
                h.close()
                root.removeHandler(h)
        root.handlers[:] = saved
        root.setLevel(saved_level)
