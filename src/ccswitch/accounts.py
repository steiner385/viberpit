"""Integration glue: token validity, usage caching, selection, sync-back, registry."""
import logging
import threading
import time
from datetime import datetime, timezone

from . import configdir, ledger, oauth, paths

log = logging.getLogger(__name__)
from .config import Config, load_config
from .creds import merge_keep_later, read_credentials, read_json, split_credentials, write_credentials, write_json_secure
from .selector import select_best

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


def _account_names() -> list[str]:
    d = paths.store_dir()
    if not d.exists():
        return []
    return sorted(p.name[: -len(".cred.json")] for p in d.glob("*.cred.json"))


def _is_fresh(blob: dict, cfg: Config) -> bool:
    return blob.get("expiresAt", 0) - int(time.time() * 1000) > cfg.expiry_skew_sec * 1000


def ensure_valid_token(name: str) -> dict:
    """Return a non-expired claudeAiOauth blob, refreshing + persisting if needed.

    Refresh is serialized per account (single-use refresh tokens); the caller that
    loses the race re-reads the freshly-persisted token instead of double-refreshing.
    """
    cfg = load_config()
    blob = read_credentials(paths.slot_path(name))
    if _is_fresh(blob, cfg):
        return blob
    with _account_lock(name):
        # Re-read inside the lock: another caller may have just refreshed + persisted.
        blob = read_credentials(paths.slot_path(name))
        if _is_fresh(blob, cfg):
            return blob
        new_blob = oauth.refresh_access_token(blob["refreshToken"], blob)
        write_credentials(paths.slot_path(name), new_blob)
        return new_blob


def usage_for(name: str) -> oauth.Usage | None:
    """Cached-or-fetched usage. Returns None and marks needs-reauth if auth fails."""
    cfg = load_config()
    now = datetime.now(timezone.utc)
    with ledger.locked_ledger() as led:
        cached = ledger.cached_usage(led, name, cfg, now)
        if cached is not None:
            return cached

    # Step 1: ensure the token is valid (may refresh if near-expiry).
    # If the refresh token is already dead, mark needs-reauth immediately — do not retry.
    try:
        blob = ensure_valid_token(name)
    except oauth.AuthError:
        with ledger.locked_ledger() as led:
            ledger.set_status(led, name, "needs-reauth")
        return None

    # Step 2: fetch usage; on 401 do exactly ONE refresh+refetch.
    try:
        u = oauth.fetch_usage(blob["accessToken"])
    except oauth.UsageError as e:
        # Transient usage-endpoint failure (429 rate-limit / 5xx). The token is
        # valid — only the usage API is unavailable. Return None (no usage) WITHOUT
        # marking needs-reauth, so callers (the accounts list) degrade gracefully
        # instead of 500-ing the whole dashboard.
        log.warning("usage_for(%s): transient usage error, leaving status unchanged: %s", name, e)
        return None
    except oauth.AuthError:
        try:
            with _account_lock(name):
                # Re-read under the lock: a concurrent caller may have refreshed.
                slot = read_credentials(paths.slot_path(name))
                slot = oauth.refresh_access_token(slot["refreshToken"], slot)
                write_credentials(paths.slot_path(name), slot)
            u = oauth.fetch_usage(slot["accessToken"])
        except oauth.UsageError as e:
            log.warning("usage_for(%s): transient usage error after refresh: %s", name, e)
            return None
        except oauth.AuthError:
            with ledger.locked_ledger() as led:
                ledger.set_status(led, name, "needs-reauth")
            return None

    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, u, now)
        ledger.set_status(led, name, "ok")
    return u


def pick_account(cfg: Config, exclude: set[str] = frozenset()) -> str:
    names = _account_names()
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        name = select_best(usages, reserved, cfg, now, exclude)
        ledger.reserve(led, name, now_ts + cfg.reservation_ttl_sec)
    return name


def sync_back(name: str) -> None:
    """Split the live config-dir creds back into the per-account slot + shared mcp store."""
    live_path = paths.account_cfg_dir(name) / ".credentials.json"
    if not live_path.exists():
        return
    claude_oauth, mcp = split_credentials(read_json(live_path))
    existing = read_credentials(paths.slot_path(name)) if paths.slot_path(name).exists() else None
    write_credentials(paths.slot_path(name), merge_keep_later(existing, claude_oauth))
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
