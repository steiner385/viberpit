"""THE rule for a user-supplied link the web renders as an ``href``: an absolute
http(s) URL with a host, at most :data:`MAX_URL_LEN` characters. Anything else
(``javascript:``, ``data:``, protocol-relative ``//host``, a bare path) is refused
at write time, so a stored value can never become a script link (stored XSS). The
web's twin gate is ``web/src/lib/safeUrl.ts:isSafeHttpUrl``, applied at render.
"""
from __future__ import annotations

from urllib.parse import urlsplit

MAX_URL_LEN = 2000


def is_absolute_http_url(value: str) -> bool:
    """True when *value* is an absolute http(s) URL with a host, within the length
    cap. Surrounding whitespace is refused, not trimmed (``urlsplit`` would strip it):
    callers trim first, so what is checked is exactly what is stored."""
    if not value or len(value) > MAX_URL_LEN or value != value.strip():
        return False
    try:
        parts = urlsplit(value)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)
