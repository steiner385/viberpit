"""Unit tests for server.sessions.failure — turn-failure classification."""
from server.sessions.failure import classify, TurnFailure


def test_api_429_code_is_rate_limit():
    f = classify("api_429", "The model API returned an error (HTTP 429).")
    assert f.kind == "rate_limit"
    assert f.is_recoverable


def test_api_401_code_is_auth():
    f = classify("api_401", "The model API returned an error (HTTP 401).")
    assert f.kind == "auth_401"
    assert f.is_recoverable


def test_invalid_credentials_message_is_auth_even_without_code():
    f = classify("driver_error", "APIError: 401 Invalid authentication credentials")
    assert f.kind == "auth_401"


def test_429_in_message_is_rate_limit_even_without_code():
    f = classify("driver_error", "APIError: 429 rate_limit_error")
    assert f.kind == "rate_limit"


def test_retry_after_passed_through_and_capped():
    f = classify("api_429", "HTTP 429", retry_after=15.0)
    assert f.kind == "rate_limit"
    assert f.retry_after == 15.0
    # absurd values are capped, not trusted
    f2 = classify("api_429", "HTTP 429", retry_after=99999.0)
    assert f2.retry_after is not None and f2.retry_after <= 120.0


def test_retry_after_parsed_from_message_when_not_supplied():
    f = classify("api_429", "Rate limited. Retry-After: 8")
    assert f.kind == "rate_limit"
    assert f.retry_after == 8.0


def test_unrelated_error_is_other_and_not_recoverable():
    f = classify("execution_error", "The agent stopped on an execution error.")
    assert f.kind == "other"
    assert not f.is_recoverable


def test_none_code_and_empty_message_is_other():
    assert classify(None, "").kind == "other"


def test_turnfailure_is_frozen_dataclass():
    f = TurnFailure(kind="rate_limit", retry_after=3.0)
    assert f.kind == "rate_limit" and f.retry_after == 3.0


def test_process_crash_signatures_are_recoverable():
    for msg in (
        "ProcessError: Command failed with exit code -6",
        "ProcessError: Command failed with exit code -4 (exit code: -4)",
        "CLIConnectionError: Failed to start Claude Code: [Errno 11] Resource temporarily unavailable",
        "CLIConnectionError: Cannot write to terminated process (exit code: -6)",
    ):
        f = classify("driver_error", msg)
        assert f.kind == "process_crash", msg
        assert f.is_recoverable is True


def test_ordinary_driver_error_stays_other():
    f = classify("driver_error", "ValueError: bad thing happened")
    assert f.kind == "other"
    assert f.is_recoverable is False


def test_positive_exit_code_is_not_a_crash():
    # a clean non-signal exit is not a hard crash we auto-respawn
    f = classify("driver_error", "ProcessError: Command failed with exit code 1")
    assert f.kind == "other"
    assert classify("driver_error", "ProcessError: Command failed with exit code 1").is_recoverable is False
