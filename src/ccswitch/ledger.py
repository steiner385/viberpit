"""Flock-guarded JSON state: a usage cache + soft reservations. No secrets."""
import contextlib
import fcntl
import json
from datetime import datetime, timezone

from . import paths
from .oauth import Usage


@contextlib.contextmanager
def locked_ledger():
    """Open ledger.json under an exclusive flock; persist the dict on exit."""
    paths.ensure_root()
    path = paths.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        data = json.loads(raw) if raw else {"accounts": {}}
        data.setdefault("accounts", {})
        yield data
        f.seek(0)
        f.truncate()
        f.write(json.dumps(data, indent=2))
        f.flush()
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def _acct(led: dict, name: str) -> dict:
    return led["accounts"].setdefault(name, {})


def record_usage(led: dict, name: str, u: Usage, now: datetime) -> None:
    _acct(led, name)["usage"] = {
        "session_pct": u.session_pct,
        "session_resets_at": u.session_resets_at.isoformat(),
        "weekly_pct": u.weekly_pct,
        "weekly_resets_at": u.weekly_resets_at.isoformat(),
        "extra_usage_enabled": u.extra_usage_enabled,
        "fetched_at": now.isoformat(),
    }


def cached_usage(led: dict, name: str, cfg, now: datetime) -> Usage | None:
    blob = _acct(led, name).get("usage")
    if not blob:
        return None
    fetched = datetime.fromisoformat(blob["fetched_at"])
    if (now - fetched).total_seconds() > cfg.usage_cache_ttl_sec:
        return None
    return Usage(
        blob["session_pct"], datetime.fromisoformat(blob["session_resets_at"]),
        blob["weekly_pct"], datetime.fromisoformat(blob["weekly_resets_at"]),
        blob["extra_usage_enabled"],
    )


def set_status(led: dict, name: str, status: str) -> None:
    _acct(led, name)["status"] = status


def reserve(led: dict, name: str, until_ts: float) -> None:
    _acct(led, name)["reservationUntil"] = until_ts


def reservation_active(led: dict, name: str, now_ts: float) -> bool:
    return led["accounts"].get(name, {}).get("reservationUntil", 0) > now_ts
