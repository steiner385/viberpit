"""Integration glue: token validity, usage caching, selection, sync-back, registry."""
import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import ledger, oauth, paths
from .config import Config, load_config
from .creds import merge_keep_later, read_credentials, read_json, split_credentials, write_credentials, write_json_secure
from .selector import Picked, select_picked

log = logging.getLogger(__name__)


@dataclass
class AuthFailure:
    account: str
    status: int | None
    body: str | None
    path: str
    token_expires_at: int | None
    attempts: int


_auth_failure_sink: Callable[[AuthFailure], None] | None = None


def set_auth_failure_sink(fn: "Callable[[AuthFailure], None] | None") -> None:
    """Register (or clear) the sink that receives an AuthFailure at each needs-reauth flip.

    The core stays DB-free: the server registers a queue-backed sink at startup. When
    unset (CLI usage) emission is a no-op beyond the existing log line.
    """
    global _auth_failure_sink
    _auth_failure_sink = fn


def emit_auth_failure(f: AuthFailure) -> None:
    sink = _auth_failure_sink
    if sink is None:
        return
    try:
        sink(f)
    except Exception:  # never let instrumentation break the auth path
        log.warning("emit_auth_failure: sink raised", exc_info=True)


# Per-account refresh locks. Anthropic refresh tokens are single-use (rotate on
# refresh), so two concurrent refreshers (e.g. the web server's usage poller AND
# an API request) would race: one consumes the token, the other's refresh fails.
# Serialize refresh+persist per account, and re-check after acquiring so the loser
# uses the freshly-persisted token instead of refreshing again.
_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _account_lock(name: str) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(name, threading.Lock())


@contextmanager
def _refresh_guard(name: str):
    """Serialise an account's refresh+persist across threads AND processes.

    ``_account_lock`` only spans one process, but token refresh happens in three
    that can run concurrently against the same account — the web daemon (usage
    poller), the edge (in-process mode), and the standalone ``ccswitch`` CLI.
    Because refresh tokens are single-use, a cross-process race makes one process
    consume the token while another, holding the now-stale copy, gets
    ``invalid_grant`` and falsely flips the account to needs-reauth.

    The per-process ``threading.Lock`` serialises threads (``flock`` ownership is
    per-process, so it cannot); the ``flock`` on ``store/<name>.refresh.lock``
    serialises across processes. Advisory and auto-released on fd close / process
    death — no stale-lock cleanup needed. Callers re-read the slot inside the
    guard so the race loser uses the freshly-persisted rotated token.
    """
    with _account_lock(name):
        paths.ensure_root()
        store = paths.store_dir()
        store.mkdir(parents=True, exist_ok=True)
        os.chmod(store, 0o700)
        lock_path = store / f"{name}.refresh.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _account_names() -> list[str]:
    d = paths.store_dir()
    if not d.exists():
        return []
    return sorted(p.name[: -len(".cred.json")] for p in d.glob("*.cred.json"))


def _is_fresh(blob: dict, cfg: Config) -> bool:
    return blob.get("expiresAt", 0) - int(time.time() * 1000) > cfg.expiry_skew_sec * 1000


def _slot_expires_at(name: str) -> int | None:
    try:
        return read_credentials(paths.slot_path(name)).get("expiresAt")
    except Exception:
        return None


def ensure_valid_token(name: str) -> dict:
    """Return a non-expired claudeAiOauth blob, refreshing + persisting if needed.

    Refresh is serialized per account (single-use refresh tokens); the caller that
    loses the race re-reads the freshly-persisted token instead of double-refreshing.
    """
    cfg = load_config()
    blob = read_credentials(paths.slot_path(name))
    if _is_fresh(blob, cfg):
        return blob
    with _refresh_guard(name):
        # Re-read inside the guard: another thread OR process may have just
        # refreshed + persisted while we waited on the cross-process lock.
        blob = read_credentials(paths.slot_path(name))
        if _is_fresh(blob, cfg):
            return blob
        # A blob with no usable refreshToken (e.g. an empty `{}` slot left by a
        # reauth that failed mid-write) cannot be refreshed — it is a dead token
        # family. Surface it as AuthError, the canonical needs-reauth signal, so
        # callers route it through the existing recovery/needs-reauth path instead
        # of letting a bare KeyError escape and crash the whole usage sweep.
        if not blob.get("refreshToken"):
            raise oauth.AuthError("credential slot has no refreshToken")
        new_blob = oauth.refresh_access_token(
            blob["refreshToken"], blob,
            max_retries=cfg.retry_max_retries, max_total_sec=cfg.retry_max_total_sec)
        write_credentials(paths.slot_path(name), new_blob)
        return new_blob


def acquire_valid_token(name: str) -> dict:
    """Like ensure_valid_token, but recover a lost rotation before giving up.

    If our slot's refresh token is rejected (AuthError), the live `cfg/` dir may
    hold a token the SDK rotated that the poller never captured. sync_back pulls it
    into the slot; if that yields a fresh token we use it instead of flipping the
    account to needs-reauth. Idle accounts with no fresher copy re-raise, so the
    genuine-dead-token case still surfaces.
    """
    try:
        return ensure_valid_token(name)
    except oauth.AuthError:
        sync_back(name)
        cfg = load_config()
        blob = read_credentials(paths.slot_path(name))
        if _is_fresh(blob, cfg):
            return blob
        raise


def usage_for(name: str, *, allow_refresh: bool = True, path: str = "unknown") -> oauth.Usage | None:
    """Cached-or-fetched usage. Returns None and marks needs-reauth if auth fails.

    Args:
        name: Account name.
        allow_refresh: When True (default), expired tokens are refreshed via
            oauth.refresh_access_token exactly as before.  When False (live-session
            mode), sync_back is called first to pick up any SDK-rotated token; if
            the token is still expired after sync_back, returns None WITHOUT
            refreshing and WITHOUT marking needs-reauth (the live SDK owns the
            token family and will refresh on its next request).
    """
    cfg = load_config()
    retry_kw = {"max_retries": cfg.retry_max_retries, "max_total_sec": cfg.retry_max_total_sec}
    now = datetime.now(timezone.utc)
    with ledger.locked_ledger() as led:
        cached = ledger.cached_usage(led, name, cfg, now)
        if cached is not None:
            return cached

    if not allow_refresh:
        # Live-session path: sync_back first so we pick up the SDK's latest
        # rotation, then attempt usage with whatever is in the slot.  If the
        # token is expired we return None without touching needs-reauth — the
        # SDK process that owns the token will refresh it on its next request.
        sync_back(name)
        blob = read_credentials(paths.slot_path(name))
        if not _is_fresh(blob, cfg):
            log.debug("usage_for(%s): token expired, live session owns it — skipping", name)
            return None
        try:
            u = oauth.fetch_usage(blob["accessToken"], **retry_kw)
        except (oauth.UsageError, oauth.TransientError, oauth.AuthError) as e:
            log.warning("usage_for(%s, allow_refresh=False): usage fetch failed: %s", name, e)
            return None
        with ledger.locked_ledger() as led:
            ledger.record_usage(led, name, u, now)
            ledger.set_status(led, name, "ok")
        return u

    # Step 1: ensure the token is valid (may refresh if near-expiry).
    # If the refresh token is already dead, mark needs-reauth immediately — do not retry.
    try:
        blob = acquire_valid_token(name)
    except oauth.TransientError as e:
        # Token endpoint rate-limited (429) or unavailable (5xx). The refresh token
        # is presumed valid — leave status unchanged so a transient throttle can't
        # flip an idle account to needs-reauth.
        log.warning("usage_for(%s): transient refresh error, leaving status unchanged: %s", name, e)
        return None
    except oauth.AuthError as e:
        log.warning("usage_for(%s): refresh token dead (%s) — marking needs-reauth", name, e)
        emit_auth_failure(AuthFailure(
            account=name, status=getattr(e, "status", None), body=getattr(e, "body", None),
            path=path, token_expires_at=_slot_expires_at(name), attempts=1))
        with ledger.locked_ledger() as led:
            ledger.set_status(led, name, "needs-reauth")
        return None

    # Step 2: fetch usage; on 401 do exactly ONE refresh+refetch.
    try:
        u = oauth.fetch_usage(blob["accessToken"], **retry_kw)
    except (oauth.UsageError, oauth.TransientError) as e:
        # Transient usage-endpoint failure (429 / 5xx) or a network blip that
        # exhausted retries. The token is valid — only the usage API is
        # unavailable. Return None (no usage) WITHOUT marking needs-reauth, so
        # callers (the accounts list) degrade gracefully instead of 500-ing the
        # whole dashboard.
        log.warning("usage_for(%s): transient usage error, leaving status unchanged: %s", name, e)
        return None
    except oauth.AuthError:
        try:
            with _refresh_guard(name):
                # Re-read under the guard: a sibling thread OR process may have just
                # rotated + persisted. If the slot's access token CHANGED from the
                # one that just 401'd, reuse it rather than refreshing again —
                # refreshing would consume a single-use token we don't need to and
                # could itself draw a false invalid_grant. (A fresh-by-clock token
                # can still 401, so compare the token, not its expiry.)
                slot = read_credentials(paths.slot_path(name))
                if slot.get("accessToken") == blob.get("accessToken"):
                    slot = oauth.refresh_access_token(slot["refreshToken"], slot, **retry_kw)
                    write_credentials(paths.slot_path(name), slot)
            u = oauth.fetch_usage(slot["accessToken"], **retry_kw)
        except (oauth.UsageError, oauth.TransientError) as e:
            log.warning("usage_for(%s): transient error after refresh, leaving status unchanged: %s", name, e)
            return None
        except oauth.AuthError as e:
            log.warning("usage_for(%s): 401 survived a fresh refresh (%s) — marking needs-reauth", name, e)
            with ledger.locked_ledger() as led:
                ledger.set_status(led, name, "needs-reauth")
            return None

    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, u, now)
        ledger.set_status(led, name, "ok")
    return u


def pick_account_detailed(cfg: Config, exclude: set[str] = frozenset(),
                          load: dict[str, float] | None = None) -> Picked:
    names = _account_names()
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        unhealthy = frozenset(n for n in names if ledger.get_status(led, n) == "needs-reauth")
        picked = select_picked(usages, reserved, cfg, now, exclude, load or {},
                               unhealthy, optimistic_fallback=True)
        ledger.reserve(led, picked.name, now_ts + cfg.reservation_ttl_sec)
    return picked


def pick_account(cfg: Config, exclude: set[str] = frozenset(),
                 load: dict[str, float] | None = None) -> str:
    return pick_account_detailed(cfg, exclude, load).name


def sync_back(name: str) -> None:
    """Split the live config-dir creds back into the per-account slot + shared mcp store.

    The slot read-modify-write is serialised with _refresh_guard to prevent a
    race between the driver's sync_back (called via asyncio.to_thread after each
    turn) and the poller's sync_back (called via usage_for(allow_refresh=False)
    in a threadpool) — and, because the guard is cross-process, between the web
    daemon, the edge, and the ccswitch CLI.  Without it a sync_back that read a
    STALE config dir could overwrite the slot with an older (consumed) token
    *after* another writer had already persisted the fresh rotated one —
    recreating the needs-reauth bug the caller was trying to fix.

    The config-dir read (split_credentials / read_json) stays OUTSIDE the guard:
    it touches the live cfgdir, not the shared slot, so it does not need
    serialisation and keeping it outside reduces contention.
    """
    live_path = paths.account_cfg_dir(name) / ".credentials.json"
    if not live_path.exists():
        return
    # Read the config-dir OUTSIDE the guard (touches live cfgdir only).
    claude_oauth, mcp = split_credentials(read_json(live_path))
    # Serialise the slot read-modify-write so concurrent calls cannot interleave.
    with _refresh_guard(name):
        existing = read_credentials(paths.slot_path(name)) if paths.slot_path(name).exists() else None
        write_credentials(paths.slot_path(name), merge_keep_later(existing, claude_oauth))
    # MCP write is separate from the slot and uses its own atomic write primitive.
    if mcp:
        write_json_secure(paths.shared_mcp_path(), mcp)


def remove_account(name: str) -> None:
    import shutil
    paths.slot_path(name).unlink(missing_ok=True)
    cfg_dir = paths.account_cfg_dir(name)
    if cfg_dir.exists():
        shutil.rmtree(cfg_dir)


def list_accounts() -> list[dict]:
    out = []
    with ledger.locked_ledger() as led:
        for name in _account_names():
            acct = led["accounts"].get(name, {})
            out.append({
                "name": name,
                "status": acct.get("status", "unknown"),
                "usage": acct.get("usage"),
                "nickname": acct.get("nickname"),
            })
    return out


def set_nickname(name: str, nickname: str | None) -> None:
    with ledger.locked_ledger() as led:
        ledger.set_nickname(led, name, nickname)


def mark_reauthenticated(name: str) -> None:
    """Clear the stale ``needs-reauth`` status after a successful reauth.

    ``capture_login`` writes the fresh token to the slot but does not touch the
    ledger, so the stored ``needs-reauth`` status and pre-death cached usage
    would otherwise linger until the cache TTL expires. ``get_accounts`` reads
    the stored status *before* it refreshes usage, so the correction has to be
    synchronous: set ``ok`` and drop the cached usage so the very next fetch
    (which the UI triggers on success) is real and re-affirms the status.
    """
    with ledger.locked_ledger() as led:
        ledger.set_status(led, name, "ok")
        ledger.clear_cached_usage(led, name)


def add_account(name: str, email: str | None = None) -> None:
    """Capture-on-login: run `claude auth login` against a temp capture dir, then store the creds."""
    from . import auth_login

    print(f"Log in to the '{name}' account in the browser window that opens…")
    extra: list[str] = []
    if email:
        extra = ["--email", email]
    ok = auth_login.capture_login(name, extra_login_args=extra if extra else None)
    if not ok:
        raise RuntimeError("login did not produce credentials; aborting")


# Keys from .claude.json that are safe to share across all accounts.
_CLAUDE_JSON_SHARE_KEYS = ("mcpServers", "projects", "allowedTools")


def _merge_shared_claude_json(source: dict) -> None:
    """Overlay shareable keys from source into store/claude.shared.json (create or update).

    For dict-valued keys (mcpServers), we do a shallow update so that servers from previously
    added accounts are preserved alongside newly added ones.  For non-dict keys (projects,
    allowedTools) the incoming value wins so the shared file reflects the latest state.
    """
    shared_path = paths.shared_claude_json_path()
    existing: dict = read_json(shared_path) if shared_path.exists() else {}
    changed = False
    for key in _CLAUDE_JSON_SHARE_KEYS:
        if key not in source:
            continue
        incoming = source[key]
        if isinstance(incoming, dict) and isinstance(existing.get(key), dict):
            # Merge dicts: preserve existing entries, add/update new ones
            merged = {**existing[key], **incoming}
            if merged != existing[key]:
                existing[key] = merged
                changed = True
        else:
            if existing.get(key) != incoming:
                existing[key] = incoming
                changed = True
    if changed:
        write_json_secure(shared_path, existing)
