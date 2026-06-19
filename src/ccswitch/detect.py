"""Detect a usage/rate limit from headless result JSON or a transcript JSONL line."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

_LIMIT_RE = re.compile(r"usage limit|rate[ _]?limit|\b429\b", re.IGNORECASE)
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


@dataclass
class ResetInfo:
    resets_at: datetime | None
    scope: str | None


def _parse_reset(blob: str) -> datetime | None:
    m = _ISO_RE.search(blob)
    if not m:
        return None
    s = m.group(0)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def _limit_from(obj: dict) -> ResetInfo | None:
    blob = json.dumps(obj)
    status = None
    err = obj.get("error")
    if isinstance(err, dict):
        status = err.get("status")
    hit = _LIMIT_RE.search(blob) is not None or status == 429
    if not hit:
        return None
    return ResetInfo(resets_at=_parse_reset(blob), scope=None)


def detect_limit_in_result(result: dict) -> ResetInfo | None:
    if not result.get("is_error"):
        return None
    return _limit_from(result)


def detect_limit_in_jsonl_line(line: dict) -> ResetInfo | None:
    if line.get("subtype") == "rate_limit" or line.get("type") == "rate_limit":
        return ResetInfo(resets_at=_parse_reset(json.dumps(line)), scope=None)
    # tolerant fallback: an explicit error line carrying a limit phrase
    if line.get("is_error") or line.get("type") == "system":
        return _limit_from(line)
    return None
