"""Audit redaction tests (T025).

Asserts:
- _redact / redact_detail strips sk-ant-* tokens
- _redact / redact_detail strips ccenc1:* tokens
- record_audit() stores redacted detail (not raw token)
- RedactingFilter scrubs a log record containing a token
"""
from __future__ import annotations

import logging
import pytest

from server.audit import (
    RedactingFilter,
    _redact,
    record_audit,
    redact_detail,
    install_redacting_filter,
)


# ---------------------------------------------------------------------------
# _redact() / redact_detail() string-level tests
# ---------------------------------------------------------------------------


def test_redact_scrubs_sk_ant_token():
    raw = "Authorization: Bearer sk-ant-api03-ABCDEF1234567890abcdefGHIJKL"
    result = _redact(raw)
    assert "sk-ant" not in result
    assert "[REDACTED]" in result


def test_redact_scrubs_ccenc1_token():
    raw = "token=ccenc1:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=="
    result = _redact(raw)
    assert "ccenc1:" not in result
    assert "[REDACTED]" in result


def test_redact_scrubs_long_base64():
    # 50-char base64-like string
    raw = "key=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijkl"
    result = _redact(raw)
    assert "[REDACTED]" in result


def test_redact_leaves_short_strings_intact():
    raw = "hello world"
    result = _redact(raw)
    assert result == "hello world"


def test_redact_detail_scrubs_nested():
    detail = {
        "msg": "token=sk-ant-api03-SECRETSECRET12345678abcdefg",
        "meta": {"encoded": "ccenc1:ABCDEFGHIJKLMNOPabcdefghijklmnopqrst12345678=="},
        "count": 5,  # non-string should pass through
    }
    clean = redact_detail(detail)
    assert "sk-ant" not in clean["msg"]
    assert "ccenc1:" not in clean["meta"]["encoded"]
    assert clean["count"] == 5


# ---------------------------------------------------------------------------
# record_audit() DB test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_audit_stores_redacted_detail():
    """record_audit() should not persist raw token material in detail."""
    from unittest.mock import AsyncMock, MagicMock

    mock_db = MagicMock()
    mock_db.add = MagicMock()

    raw_detail = {
        "note": "user provided token sk-ant-api03-MYSECRETTOKEN9876543210abcXYZ",
    }

    event = await record_audit(mock_db, "auth.ok", "user@example.com", raw_detail)

    # The add() was called with the event
    mock_db.add.assert_called_once_with(event)

    # Raw token must not appear in stored detail
    assert "sk-ant" not in str(event.detail)
    assert "[REDACTED]" in str(event.detail)

    # identity should be intact (no token in identity here)
    assert event.identity == "user@example.com"
    assert event.type == "auth.ok"


# ---------------------------------------------------------------------------
# RedactingFilter log test
# ---------------------------------------------------------------------------


def test_redacting_filter_scrubs_log_record():
    """A log record containing a token should be scrubbed by RedactingFilter."""
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname="test.py",
        lineno=1,
        msg="Refreshing token sk-ant-api03-ABCDEF12345678901234567890abcXYZ",
        args=(),
        exc_info=None,
    )
    filt.filter(record)
    assert "sk-ant" not in record.msg
    assert "[REDACTED]" in record.msg


def test_redacting_filter_scrubs_log_args():
    """Log record args (%-style) are also scrubbed."""
    filt = RedactingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname="test.py",
        lineno=1,
        msg="token=%s",
        args=("sk-ant-api03-SECRETSECRETABCDEF12345678901234",),
        exc_info=None,
    )
    filt.filter(record)
    assert "sk-ant" not in str(record.args)
    assert "[REDACTED]" in str(record.args)


def test_install_redacting_filter_idempotent():
    """install_redacting_filter() can be called multiple times without duplicating the filter."""
    install_redacting_filter()
    install_redacting_filter()
    root = logging.getLogger()
    count = sum(1 for f in root.filters if isinstance(f, RedactingFilter))
    assert count == 1
