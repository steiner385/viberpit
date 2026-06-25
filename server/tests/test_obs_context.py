import logging

from server.obs.context import (
    PROC_EPOCH, bind_request, bind_session, log_event,
    new_request_id, request_id_var, session_id_var,
)


def test_bind_session_sets_and_resets():
    assert session_id_var.get() is None
    with bind_session("sess-1"):
        assert session_id_var.get() == "sess-1"
    assert session_id_var.get() is None


def test_bind_request_resets_on_exception():
    try:
        with bind_request("req-1"):
            assert request_id_var.get() == "req-1"
            raise ValueError("boom")
    except ValueError:
        pass
    assert request_id_var.get() is None


def test_new_request_id_is_short_and_unique():
    a, b = new_request_id(), new_request_id()
    assert a != b and len(a) == 12


def test_proc_epoch_stable():
    from server.obs.context import PROC_EPOCH as again
    assert PROC_EPOCH == again and "-" in PROC_EPOCH


def test_log_event_attaches_event_and_fields(caplog):
    logger = logging.getLogger("test.evt")
    with caplog.at_level(logging.INFO):
        log_event(logger, logging.INFO, "session.start", session_id="s1", reason="new")
    rec = caplog.records[-1]
    assert rec.getMessage() == "session.start"
    assert rec.event == "session.start"
    assert rec.fields == {"session_id": "s1", "reason": "new"}
