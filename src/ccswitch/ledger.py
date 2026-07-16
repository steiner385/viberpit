"""Flock-guarded JSON state: a usage cache + soft reservations. No secrets."""
import contextlib
import fcntl
import json
from datetime import datetime

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


# Fetch-failure backoff: base doubles per consecutive failure, clamped to the
# cap. Base must exceed the usage endpoint's observed Retry-After (~61s) by the
# second failure or a synchronous caller loop (pick_account_detailed probes)
# re-hammers the endpoint through its rate-limit window and the 429 never clears.
_USAGE_BACKOFF_BASE_SEC = 60.0
_USAGE_BACKOFF_CAP_SEC = 900.0


def note_usage_fetch_failure(led: dict, name: str, now_ts: float) -> float:
    """Record a failed usage fetch: bump the fail count, extend the cooldown.

    Returns the applied cooldown in seconds. Lives in the ledger (not process
    memory) so every usage_for caller — poller, pick_account_detailed, CLI,
    launcher — shares one cooldown per account.
    """
    acct = _acct(led, name)
    fails = int(acct.get("usageFetchFails", 0)) + 1
    acct["usageFetchFails"] = fails
    delay = min(_USAGE_BACKOFF_BASE_SEC * (2 ** (fails - 1)), _USAGE_BACKOFF_CAP_SEC)
    acct["usageFetchCooldownUntil"] = now_ts + delay
    return delay


def usage_fetch_cooldown_active(led: dict, name: str, now_ts: float) -> bool:
    return led["accounts"].get(name, {}).get("usageFetchCooldownUntil", 0) > now_ts


def clear_usage_fetch_backoff(led: dict, name: str) -> None:
    acct = led["accounts"].get(name)
    if acct:
        acct.pop("usageFetchFails", None)
        acct.pop("usageFetchCooldownUntil", None)


def record_usage(led: dict, name: str, u: Usage, now: datetime) -> None:
    # A recorded fetch is by definition a successful one — reset the backoff.
    clear_usage_fetch_backoff(led, name)
    _acct(led, name)["usage"] = {
        "session_pct": u.session_pct,
        "session_resets_at": u.session_resets_at.isoformat() if u.session_resets_at else None,
        "weekly_pct": u.weekly_pct,
        "weekly_resets_at": u.weekly_resets_at.isoformat() if u.weekly_resets_at else None,
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
    sr = blob.get("session_resets_at")
    wr = blob.get("weekly_resets_at")
    return Usage(
        blob["session_pct"], datetime.fromisoformat(sr) if sr else None,
        blob["weekly_pct"], datetime.fromisoformat(wr) if wr else None,
        blob["extra_usage_enabled"],
    )


def set_status(led: dict, name: str, status: str) -> None:
    _acct(led, name)["status"] = status


def get_status(led: dict, name: str) -> str | None:
    return _acct(led, name).get("status")


def clear_cached_usage(led: dict, name: str) -> None:
    """Drop cached usage AND the failure cooldown so the next read really fetches.

    Callers use this to force a fresh fetch (e.g. after a re-auth); leaving the
    cooldown behind would keep the account usage-dark for its remaining tail.
    """
    _acct(led, name).pop("usage", None)
    clear_usage_fetch_backoff(led, name)


def set_nickname(led: dict, name: str, nickname: str | None) -> None:
    acct = _acct(led, name)
    if nickname:
        acct["nickname"] = nickname
    else:
        acct.pop("nickname", None)


def set_weekly_reserve(led: dict, name: str, pct: float | None) -> None:
    """Set or clear the per-account weekly usage reserve threshold (0–100 pct)."""
    acct = _acct(led, name)
    if pct is None:
        acct.pop("weeklyReservePct", None)
    else:
        acct["weeklyReservePct"] = float(pct)


def get_weekly_reserve(led: dict, name: str) -> float | None:
    """Read one account's reserve; anything non-numeric is treated as unset."""
    pct = led["accounts"].get(name, {}).get("weeklyReservePct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return float(pct)


def weekly_reserves(led: dict) -> dict[str, float]:
    """All configured reserves, keyed by account name (unset accounts omitted)."""
    out: dict[str, float] = {}
    for name in led["accounts"]:
        pct = get_weekly_reserve(led, name)
        if pct is not None:
            out[name] = pct
    return out


_BLOCKED_ENV_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "CREDENTIALS_DIR", "HOME"})


def set_account_defaults(
    led: dict,
    name: str,
    *,
    default_model: str | None = None,
    clear_model: bool = False,
    default_permission_mode: str | None = None,
    clear_permission_mode: bool = False,
    env_overrides: dict | None = None,
    clear_env_overrides: bool = False,
) -> None:
    """Set per-account spawn defaults in the ledger."""
    acct = _acct(led, name)
    if default_model is not None:
        acct["defaultModel"] = default_model
    elif clear_model:
        acct.pop("defaultModel", None)
    if default_permission_mode is not None:
        acct["defaultPermissionMode"] = default_permission_mode
    elif clear_permission_mode:
        acct.pop("defaultPermissionMode", None)
    if env_overrides is not None:
        acct["envOverrides"] = env_overrides
    elif clear_env_overrides:
        acct.pop("envOverrides", None)


def get_account_defaults(led: dict, name: str) -> dict:
    """Return per-account spawn defaults (any may be None if unset)."""
    acct = led["accounts"].get(name, {})
    return {
        "defaultModel": acct.get("defaultModel"),
        "defaultPermissionMode": acct.get("defaultPermissionMode"),
        "envOverrides": acct.get("envOverrides"),
    }


def reserve(led: dict, name: str, until_ts: float) -> None:
    _acct(led, name)["reservationUntil"] = until_ts


def reservation_active(led: dict, name: str, now_ts: float) -> bool:
    return led["accounts"].get(name, {}).get("reservationUntil", 0) > now_ts
