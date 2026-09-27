from __future__ import annotations

import logging

import pytest


def _rec(msg="boom", *, level=logging.ERROR, name="server.sessions.orchestrator",
         exc_info=None, pathname="/repo/server/sessions/orchestrator.py", func="tick",
         **extra):
    rec = logging.LogRecord(name, level, pathname, 42, msg, (), exc_info, func=func)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _sig(**kw):
    from server.obs.error_signature import signature_of
    kw.setdefault("source", "edge")
    kw.setdefault("host", "dobby")
    rec = kw.pop("rec")
    return signature_of(rec, **kw)


@pytest.mark.parametrize("raw, want", [
    ("Session 3f2a9c1e-1b2d-4e5f-8a9b-0c1d2e3f4a5b failed after 12 retries",
     "session <id> failed after <n> retries"),
    ("token abcdef0123 rejected", "token <hex> rejected"),
    ('open "/var/log/x.log" failed', "open <str> failed"),
    ("worktree /home/t/.ccswitch/worktrees/viberpit/535694fe gone",
     "worktree /home/t/.ccswitch/worktrees/viberpit/<id> gone"),
    ("a   b\n c", "a b c"),
    # A number glued to a unit has no trailing word boundary, so `\b\d+\b` used
    # to mask only its integer part — "blocked for 0.78s" became "<n>.78s" and
    # every distinct lag minted its own fingerprint (#1848 and seven siblings).
    ("event loop blocked for 0.78s (interval=1.00s)",
     "event loop blocked for <n>s (interval=<n>s)"),
    ("event loop blocked for 4.22s (interval=1.00s)",
     "event loop blocked for <n>s (interval=<n>s)"),
    ("12 stall(s) suppressed in the last 61s, worst 4.22s",
     "<n> stall(s) suppressed in the last <n>s, worst <n>s"),
    ("upload took 1500ms (p95 250ms)", "upload took <n>ms (p95 <n>ms)"),
    ("released 1.5 of 10 slots", "released <n> of <n> slots"),
    # An address is identity noise, not a count: masked as a hex run so the
    # number masker cannot render it "<n>x7f...".
    ("thread 0x7F9C1A2B blocked", "thread <hex> blocked"),
    # Digits inside a word are part of the word, as before.
    ("sqlite3 driver busy", "sqlite3 driver busy"),
])
def test_normalize(raw, want):
    from server.obs.error_signature import normalize
    assert normalize(raw) == want


def test_normalize_caps_length():
    from server.obs.error_signature import normalize
    assert len(normalize("x" * 1000)) == 200


# The exact records quoted by #1655, #1657, #1658, #1659, #1660 and #1661 — one
# `DaemonClient._reader` retry loop, filed as SIX issue lineages because the only
# thing separating them was the exponential backoff's fractional part. Under the
# old `\b\d+\b` masker "2.00s" kept its fraction ("<n>.00s"), so every rung of the
# backoff ladder minted its own fingerprint.
#
# The ladder is the corpus on purpose: it is bounded and exhaustive (backoff
# doubles from 0.10s and caps at 2.0s in `_reader`), so a regression in the
# masker cannot hide in a rung this test skipped.
_WS_RECONNECT_BACKOFF_LADDER = [
    f"ipc: events WS error ([Errno 111] Connection refused) — reconnecting in {b:.2f}s"
    for b in (0.10, 0.20, 0.40, 0.80, 1.60, 2.00)
]


def test_one_retry_loop_is_one_fingerprint_whatever_the_backoff():
    from server.obs.error_signature import normalize
    normalized = {normalize(m) for m in _WS_RECONNECT_BACKOFF_LADDER}
    assert normalized == {
        "ipc: events ws error ([errno <n>] connection refused) — reconnecting in <n>s"
    }, f"the backoff ladder split into {len(normalized)} fingerprint(s): {sorted(normalized)}"


def test_the_backoff_ladder_would_have_split_under_the_old_masker():
    """Proves the guard above can fail — a corpus that collapses under ANY masker
    would assert nothing (the rungs must really be distinct before normalization)."""
    import re
    old = re.compile(r"\b\d+\b")
    split = {old.sub("<n>", m.lower()) for m in _WS_RECONNECT_BACKOFF_LADDER}
    assert len(split) == len(_WS_RECONNECT_BACKOFF_LADDER) == 6


def test_fingerprint_stable_across_volatile_fields():
    a = _sig(rec=_rec("db locked after 1 retries", session_id="s1", request_id="r1"))
    b = _sig(rec=_rec("db locked after 7 retries", session_id="s2", request_id="r2"))
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 16
    assert a.session_id == "s1" and b.session_id == "s2"


@pytest.mark.parametrize("change", [
    {"level": logging.WARNING},
    {"name": "server.other"},
    {"source": "daemon"},
])
def test_fingerprint_differs_on_identity_fields(change):
    base = _sig(rec=_rec("boom"))
    src = change.pop("source", "edge")
    other = _sig(rec=_rec("boom", **change), source=src)
    assert base.fingerprint != other.fingerprint


def test_event_records_hash_the_event_not_the_fields():
    a = _sig(rec=_rec("completion.question_hold", event="completion.question_hold",
                      fields={"sid": "a1", "reason": "x"}))
    b = _sig(rec=_rec("completion.question_hold", event="completion.question_hold",
                      fields={"sid": "b2", "reason": "y"}))
    assert a.fingerprint == b.fingerprint
    assert a.event == "completion.question_hold"
    assert a.title.startswith("ERROR completion.question_hold:")
    assert a.title == "ERROR completion.question_hold: sid=a1 reason=x"  # fields → title, normalized


def test_exception_class_and_app_frame_are_hashed():
    def _boom():
        raise ValueError("bad 123")
    try:
        _boom()
    except ValueError:
        import sys
        ei = sys.exc_info()
    with_exc = _sig(rec=_rec("failed", exc_info=ei))
    without = _sig(rec=_rec("failed"))
    assert with_exc.fingerprint != without.fingerprint
    assert with_exc.sample_exc and "ValueError: bad 123" in with_exc.sample_exc


def test_app_frame_prefers_innermost_project_frame():
    from server.obs.error_signature import app_frame
    import sys
    def inner():
        raise RuntimeError("x")
    try:
        inner()
    except RuntimeError:
        ei = sys.exc_info()
    frame = app_frame(ei)
    # this test file lives under server/tests → it IS a project frame
    assert frame == "test_error_signature:inner"


def test_app_frame_recognises_the_core_package_but_not_a_checkout_named_after_it(monkeypatch):
    """Phase 5a moved the core package to ``src/viberpit/`` and the marker did not
    follow, so every core-package frame stopped counting as ours. The marker is
    anchored on ``/src/``: a checkout DIRECTORY named after the product must not
    turn its ``.venv`` dependency frames into app frames."""
    import traceback
    from server.obs import error_signature
    frames = [
        traceback.FrameSummary("/home/u/GitHub/viberpit/server/api/sessions.py", 1, "route"),
        traceback.FrameSummary("/home/u/GitHub/viberpit/src/viberpit/ledger.py", 2, "record"),
        traceback.FrameSummary(
            "/home/u/GitHub/viberpit/.venv/lib/python3.12/site-packages/sqlalchemy/engine/base.py",
            3, "execute"),
    ]
    monkeypatch.setattr(error_signature.traceback, "extract_tb", lambda tb: frames)
    assert error_signature.app_frame((RuntimeError, RuntimeError("x"), object())) == "ledger:record"


def test_client_frame_parses_browser_stack_and_strips_asset_hash():
    from server.obs.error_signature import client_frame
    stack = ("TypeError: Cannot read properties of undefined (reading 'id')\n"
             "    at renderRow (https://vp.example/assets/index-Bx9k2L3f.js:12:34567)\n"
             "    at Array.map (<anonymous>)")
    assert client_frame(stack) == ("TypeError", "index.js:renderRow")
    assert client_frame("") == (None, None)


def test_client_event_record_becomes_client_signature():
    rec = _rec("client.error", name="client", event="client.error",
               fields={"msg": "TypeError: x is undefined", "stack": "TypeError: x\n    at f (https://h/assets/app-abc123.js:1:2)",
                       "url": "https://vp/sessions/1", "app_version": "4d2096a5",
                       "identity": "someone@example"})
    sig = _sig(rec=rec)
    assert sig.source == "client"
    assert sig.logger == "client"
    assert sig.context["app_version"] == "4d2096a5"
    assert "identity" not in sig.context
    assert sig.sample_exc.startswith("TypeError: x")
    # line:col do not change the fingerprint
    rec2 = _rec("client.error", name="client", event="client.error",
                fields={"msg": "TypeError: x is undefined",
                        "stack": "TypeError: x\n    at f (https://h/assets/app-zzz999.js:9:99)"})
    assert _sig(rec=rec2).fingerprint == sig.fingerprint


def test_bare_frontend_reemit_is_skipped():
    rec = _rec("TypeError: x", name="client", level=logging.ERROR, source="frontend")
    assert _sig(rec=rec) is None


def test_below_warning_and_own_logger_are_skipped():
    assert _sig(rec=_rec("info", level=logging.INFO)) is None
    assert _sig(rec=_rec("x", name="server.obs.errsink")) is None


def test_sample_is_redacted_and_capped():
    sig = _sig(rec=_rec("token sk-ant-api03-" + "A" * 80 + " " + "y" * 5000))
    assert "sk-ant-api03-AAAA" not in sig.sample_message
    assert len(sig.sample_message) <= 4000


def test_measured_values_do_not_split_one_condition_into_many():
    """#1848/#1850/#1705/#1770/#1771/#1774/#1789/#1797 were one loop_lag stall.

    Every one of those auto-errors is the same record from the same call site;
    they became eight issue lineages because the measured lag reached the
    fingerprint. A duration is context, never identity.
    """
    fps = {
        _sig(rec=_rec(f"event loop blocked for {lag:.2f}s (interval=1.00s)",
                      level=logging.WARNING, name="server.obs.loop_lag")).fingerprint
        for lag in (0.78, 4.22, 1.12, 5.03, 5.21, 3.04, 3.63, 3.13)
    }
    assert len(fps) == 1, f"one condition split into {len(fps)} fingerprints"
