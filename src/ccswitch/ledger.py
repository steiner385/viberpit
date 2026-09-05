"""Flock-guarded JSON state: a usage cache + soft reservations. No secrets."""
import contextlib
import fcntl
import json
import logging
import os
import tempfile
from datetime import datetime

from . import paths
from .quota import QuotaSnapshot, anthropic_snapshot, copilot_snapshot

log = logging.getLogger(__name__)


def _decode_documents(raw: str) -> list[dict]:
    """Decode every JSON document concatenated in *raw*.

    A healthy ledger holds exactly one. More than one means an interleaved write
    got through (see locked_ledger); raises for input that is not a sequence of
    JSON documents at all.
    """
    dec = json.JSONDecoder()
    docs: list[dict] = []
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(raw, i)
        docs.append(obj)
    return docs


def _load(raw: str) -> dict:
    """Parse ledger content, recovering from a concatenated (corrupted) file.

    Recovery keeps the document with the MOST accounts. The loser of an
    interleaved write is the near-empty one, never the intended state, and
    raising instead would take down account selection, usage tracking,
    reservations and enable/disable on every host at once.

    Content that is not JSON at all still raises: replacing it with an empty
    ledger would silently erase every account's state.
    """
    raw = raw.strip()
    if not raw:
        return {"accounts": {}}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        docs = [d for d in _decode_documents(raw) if isinstance(d, dict)]
        if not docs:
            raise
        best = max(docs, key=lambda d: len(d.get("accounts", {})))
        log.error(
            "ledger.json held %d concatenated documents — recovering the one with "
            "%d accounts and rewriting it atomically. This is the signature of an "
            "interleaved write; flock does not exclude across a network mount.",
            len(docs), len(best.get("accounts", {})),
        )
        return best


@contextlib.contextmanager
def locked_ledger():
    """Read ledger.json under an exclusive flock; write it back ATOMICALLY.

    The lock is held on a dedicated ``ledger.json.lock`` whose inode is stable —
    locking the data file itself would be pointless once the write replaces it.

    The write is temp-file + ``os.replace``, never an in-place truncate. The
    previous implementation opened the data file ``"a+"``, which sets O_APPEND,
    and under O_APPEND every write lands at EOF regardless of ``seek(0)``. A
    second writer that read the file mid-truncate therefore appended its own
    near-empty document after the good one, leaving two concatenated JSON
    documents and breaking every ledger read (observed in production
    2026-08-22).

    NOTE: flock does NOT exclude across hosts when the store is a network mount —
    verified on 2026-08-22, where rocky acquired the lock while dobby held it. An
    atomic write makes the failure mode a LOST UPDATE rather than a corrupt file;
    genuine cross-host exclusion needs a shared lock (e.g. a Postgres advisory
    lock) and is tracked separately.
    """
    root = paths.ensure_root()
    path = paths.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = path.with_name(path.name + ".lock")
    lf = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        data = _load(raw)
        data.setdefault("accounts", {})
        yield data

        fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".ledger-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


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


def record_usage(led: dict, name: str, u: QuotaSnapshot, now: datetime) -> None:
    """Cache one account's quota snapshot. The blob is VERSIONED (``v``) so a
    future shape change is a migration rather than a misread."""
    # A recorded fetch is by definition a successful one — reset the backoff.
    clear_usage_fetch_backoff(led, name)
    blob = u.to_dict()
    blob["fetched_at"] = now.isoformat()
    _acct(led, name)["usage"] = blob


def _provider_hint(name: str) -> str:
    """Best-effort provider for an account, for legacy-blob migration only.

    Local import + broad except on purpose: ``accounts`` imports this module, and
    a manifest that has not been reconciled yet must degrade to the historical
    default rather than break every cached read.
    """
    try:
        from . import account_registry
        return account_registry.provider_of(name)
    except Exception:  # noqa: BLE001 — see docstring
        return "anthropic"


def _migrate_legacy_usage(name: str, blob: dict) -> QuotaSnapshot:
    """Read a PRE-VERSIONING usage blob as a QuotaSnapshot.

    Live ledgers on dobby and rocky hold the old ``Usage`` encoding
    (``session_pct`` / ``session_resets_at`` / ``weekly_pct`` /
    ``weekly_resets_at`` / ``extra_usage_enabled``). Dropping those entries would
    blank every account's usage for a full cache TTL right after a deploy, and
    misreading them would feed the selector garbage — so they are migrated in
    place, on read, without a rewrite.

    Copilot entries in that shape are the fabricated duplicate this change
    removes: both dimensions hold the SAME premium-request number. They are
    migrated back to the single ``premium`` window they always were, so the
    duplicate does not survive the upgrade in cached form.
    """
    def _dt(key: str) -> datetime | None:
        v = blob.get(key)
        return datetime.fromisoformat(v) if v else None

    fetched = datetime.fromisoformat(blob["fetched_at"])
    if _provider_hint(name) == "copilot":
        return copilot_snapshot(float(blob.get("session_pct") or 0.0),
                                _dt("session_resets_at"), fetched_at=fetched)
    return anthropic_snapshot(
        float(blob.get("session_pct") or 0.0), _dt("session_resets_at"),
        float(blob.get("weekly_pct") or 0.0), _dt("weekly_resets_at"),
        bool(blob.get("extra_usage_enabled")), fetched_at=fetched,
    )


def usage_from_blob(name: str, blob: dict) -> QuotaSnapshot:
    """Decode a stored usage blob of EITHER shape. The single reader."""
    if blob.get("v"):
        return QuotaSnapshot.from_dict(blob)
    return _migrate_legacy_usage(name, blob)


def cached_usage(led: dict, name: str, cfg, now: datetime) -> QuotaSnapshot | None:
    blob = _acct(led, name).get("usage")
    if not blob:
        return None
    fetched = datetime.fromisoformat(blob["fetched_at"])
    if (now - fetched).total_seconds() > cfg.usage_cache_ttl_sec:
        return None
    try:
        return usage_from_blob(name, blob)
    except (KeyError, TypeError, ValueError):
        log.warning("ledger: unreadable cached usage for %s — treating as unknown",
                    name, exc_info=True)
        return None


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


def set_allow_extra_usage(led: dict, name: str, allow: bool) -> None:
    """Opt an account in/out of spending paid extra-usage credits past its ceiling.

    Off is stored as *absent* rather than ``false`` so the ledger stays clean and
    the default is unambiguous — see ``get_allow_extra_usage``.
    """
    acct = _acct(led, name)
    if allow:
        acct["allowExtraUsage"] = True
    else:
        acct.pop("allowExtraUsage", None)


def get_allow_extra_usage(led: dict, name: str) -> bool:
    """False unless the account is explicitly opted in.

    This is the inverse default of ``is_account_enabled``, and deliberately so:
    spending money must be opt-in, so a missing key (every pre-upgrade ledger)
    means off. Anything that is not literally ``True`` is treated as off.
    """
    return led["accounts"].get(name, {}).get("allowExtraUsage") is True


def extra_usage_allowed(led: dict) -> set[str]:
    """Names of every account opted into extra-usage credits (selector input)."""
    return {name for name in led["accounts"] if get_allow_extra_usage(led, name)}


_BLOCKED_ENV_KEYS = frozenset({"CLAUDE_CONFIG_DIR", "CREDENTIALS_DIR", "HOME"})


def set_account_defaults(
    led: dict,
    name: str,
    *,
    default_model: str | None = None,
    clear_model: bool = False,
    default_permission_mode: str | None = None,
    clear_permission_mode: bool = False,
    model_approval_action: str | None = None,
    clear_model_approval_action: bool = False,
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
    if model_approval_action is not None:
        acct["modelApprovalAction"] = model_approval_action
    elif clear_model_approval_action:
        acct.pop("modelApprovalAction", None)
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
        "modelApprovalAction": acct.get("modelApprovalAction"),
        "envOverrides": acct.get("envOverrides"),
    }


def set_account_enabled(led: dict, name: str, enabled: bool) -> None:
    """Enable/disable an account. Disabled accounts are never selected (blocked
    everywhere: new sessions, auto-rotation, and manual pick) but are kept intact
    so the toggle is reversible — distinct from ``remove_account``."""
    _acct(led, name)["enabled"] = bool(enabled)


def is_account_enabled(led: dict, name: str) -> bool:
    """True unless the account is explicitly disabled. A missing ``enabled`` key
    means enabled: existing ledgers predate the field and must not silently
    disable every account after an upgrade."""
    return led["accounts"].get(name, {}).get("enabled", True) is not False


def reserve(led: dict, name: str, until_ts: float) -> None:
    _acct(led, name)["reservationUntil"] = until_ts


def reservation_active(led: dict, name: str, now_ts: float) -> bool:
    return led["accounts"].get(name, {}).get("reservationUntil", 0) > now_ts
