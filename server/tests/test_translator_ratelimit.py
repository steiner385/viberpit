"""Translator surfaces SDK RateLimitEvent as an internal ev.RateLimit."""
from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from server.sessions.translator import Translator
from server.sessions import events as ev


def _info(status="rejected", resets_at=1782177194):
    return RateLimitInfo(status=status, resets_at=resets_at, rate_limit_type="five_hour",
                         utilization=1.0, overage_status=None, overage_resets_at=None,
                         overage_disabled_reason=None, raw={})


def test_rate_limit_event_surfaced_with_reset():
    tr = Translator()
    out = tr.feed(RateLimitEvent(rate_limit_info=_info(resets_at=1782177194), uuid="u1", session_id="s1"))
    rl = [e for e in out if isinstance(e, ev.RateLimit)]
    assert len(rl) == 1
    assert rl[0].resets_at == 1782177194
    assert rl[0].status == "rejected"


def test_rate_limit_event_without_reset_still_surfaced():
    tr = Translator()
    out = tr.feed(RateLimitEvent(rate_limit_info=_info(status="allowed_warning", resets_at=None),
                                 uuid="u1", session_id="s1"))
    rl = [e for e in out if isinstance(e, ev.RateLimit)]
    assert len(rl) == 1 and rl[0].resets_at is None and rl[0].status == "allowed_warning"


def test_rate_limit_event_carries_its_type():
    tr = Translator()
    out = tr.feed(RateLimitEvent(rate_limit_info=_info(), uuid="u1", session_id="s1"))
    rl = [e for e in out if isinstance(e, ev.RateLimit)][0]
    assert rl.rate_limit_type == "five_hour"


def test_turn_failure_wire_includes_limit_type():
    tf = ev.TurnFailure(kind="rate_limit", account="a", retry_after=5.0,
                        limit_type="seven_day_opus")
    assert tf.to_wire()["limitType"] == "seven_day_opus"
    assert ev.TurnFailure(kind="auth_401", account="a").to_wire()["limitType"] is None
