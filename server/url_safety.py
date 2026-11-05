"""THE rule for a user-supplied link the web renders as an ``href``: an absolute
http(s) URL with a host, optionally length-capped by the caller. Anything else
(``javascript:``, ``data:``, protocol-relative ``//host``, a bare path) is refused
at write time, so a stored value can never become a script link (stored XSS). The
web's twin gate is ``web/src/lib/safeUrl.ts:isSafeHttpUrl``, applied at render.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# The cap milestone notes and ticks pass; a caller with no stored-size concern (a
# shared artifact, which may be a long presigned URL) passes none.
MAX_URL_LEN = 2000


def is_absolute_http_url(value: str, *, max_len: int | None = None) -> bool:
    """True when *value* is an absolute http(s) URL with a host and, when *max_len*
    is given, at most that many characters. Surrounding whitespace is refused, not
    trimmed (``urlsplit`` would strip it): callers trim first, so what is checked is
    exactly what is stored."""
    if not value or (max_len is not None and len(value) > max_len) or value != value.strip():
        return False
    try:
        parts = urlsplit(value)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)
