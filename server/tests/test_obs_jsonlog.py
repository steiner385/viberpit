import json
import logging

from server.audit import redact_text
from server.obs.context import bind_session
from server.obs.jsonlog import ContextFilter, JSONFormatter


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
        import sys
        rec.exc_info = sys.exc_info()
    d = _format(rec)
    assert "ValueError: boom" in d["exc"]
