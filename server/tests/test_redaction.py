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


def test_redact_scrubs_long_padded_base64():
    # 64-char padded base64 blob — definitely a secret
    raw = "key=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwx=="
    result = _redact(raw)
    assert "[REDACTED]" in result


def test_redact_leaves_short_strings_intact():
    raw = "hello world"
    result = _redact(raw)
    assert result == "hello world"


# ── I2: tighter redactor — benign content must NOT be redacted ───────────────

def test_redact_leaves_git_sha_intact():
    """A 40-char unpadded hex SHA must NOT be redacted (no = padding chars)."""
    sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    assert len(sha) == 40
    result = _redact(sha)
    assert result == sha, f"Expected git SHA to be intact, got: {result!r}"


def test_redact_leaves_long_identifier_intact():
    """An unpadded identifier (no = chars) of arbitrary length must NOT be redacted."""
    # 59 base64-alphabet chars, no padding — similar length to an OAuth identifier
    # that happens to lack = padding; must not trigger the tightened rule.
    ident = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg"
    assert len(ident) == 59
    result = _redact(ident)
    assert result == ident, f"Expected unpadded identifier to be intact, got: {result!r}"


def test_redact_keeps_sk_ant_pattern():
    """sk-ant-* must still be redacted (explicit prefix pattern unchanged)."""
    raw = "sk-ant-api03-shortvalue"
    result = _redact(raw)
    assert "sk-ant" not in result
    assert "[REDACTED]" in result


def test_redact_keeps_ccenc1_pattern():
    """ccenc1:* must still be redacted (explicit prefix pattern unchanged)."""
    raw = "ccenc1:shortvalue=="
    result = _redact(raw)
    assert "ccenc1:" not in result
    assert "[REDACTED]" in result


def test_redact_scrubs_padded_64char_base64_secret():
    """A real padded 64-char base64 secret must be redacted."""
    # 64 base64 chars + == padding
    secret = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    padded = secret[:62] + "=="
    assert len(padded) == 64
    result = _redact(padded)
    assert "[REDACTED]" in result


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
    from unittest.mock import MagicMock

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
