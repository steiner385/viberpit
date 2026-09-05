"""Integration glue: token validity, usage caching, selection, sync-back, registry."""
import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import account_registry, copilot, ledger, oauth, paths
from .account_names import aliases_store_root
from .config import Config, load_config
from .creds import merge_keep_later, read_credentials, read_json, split_credentials, write_credentials, write_json_secure
from .quota import QuotaSnapshot
from .selector import AllAccountsBusy, Picked, select_picked

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

# Process-local set of accounts whose refresh token returned HTTP 400 (dead grant).
# Once an account lands here, usage_for() exits immediately without touching the
# network — 400 means the token family is truly dead, not just expired.  Cleared
# only by mark_reauthenticated() so the fast-skip is sticky until the user re-auths.
_dead_accounts: set[str] = set()


def set_auth_failure_sink(fn: "Callable[[AuthFailure], None] | None") -> None:
    """Register (or clear) the sink that receives an AuthFailure at each needs-reauth flip.

    The core stays DB-free: the server registers a queue-backed sink at startup. When
    unset (CLI usage) emission is a no-op beyond the existing log line.
    """
    global _auth_failure_sink
    _auth_failure_sink = fn


_copilot_usage_reader: Callable[[str], "dict | None"] | None = None


def set_copilot_usage_reader(fn: "Callable[[str], dict | None] | None") -> None:
    """Register (or clear) the reader that fetches the proxy /usage payload for a copilot account.

    The core stays transport-free: the daemon registers a reader at startup. When
    unset (CLI usage) copilot usage_for returns None.
    """
    global _copilot_usage_reader
    _copilot_usage_reader = fn


def _copilot_usage_for(name: str) -> QuotaSnapshot | None:
    reader = _copilot_usage_reader
    if reader is None:
        # Feature not configured — not a fetch failure, so no backoff.
        return None
    # Same ledger cooldown as the Anthropic path: a dead/idle proxy must not be
    # re-hit on every sweep and every dashboard read.
    with ledger.locked_ledger() as led:
        if ledger.usage_fetch_cooldown_active(led, name, time.time()):
            log.debug("usage_for(%s): fetch-failure cooldown active — skipping", name)
            return None
    try:
        payload = reader(name)
    except Exception:
        log.warning("copilot usage read failed for %s", name, exc_info=True)
        _note_usage_fetch_failure(name)
        return None
    now = datetime.now(timezone.utc)
    u = copilot.usage_from_proxy(payload, now) if payload is not None else None
    with ledger.locked_ledger() as led:
        if u is not None:
            ledger.record_usage(led, name, u, now)
            ledger.set_status(led, name, "ok")
        else:
            ledger.note_usage_fetch_failure(led, name, time.time())
    return u


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


def _anthropic_account_names() -> list[str]:
    d = paths.store_dir()
    if not d.exists():
        return []
    # `Path.glob` — unlike a shell glob — does NOT hide dotfiles, so `*.cred.json`
    # matches a bare `.cred.json` whose stem is "". Such a slot is not an account:
    # it is shared state that a caller with an empty account name wrote over the
    # store root. Enumerating it hands the rotation picker a nameless account.
    return sorted(
        stem for stem in (p.name[: -len(".cred.json")] for p in d.glob("*.cred.json"))
        if not aliases_store_root(stem)
    )


def anthropic_slot_paths() -> list[Path]:
    """The credential slots in the store that belong to a REAL account.

    The one place the store is enumerated as FILES, mirroring
    _anthropic_account_names() (the one place it is enumerated as NAMES) so a stray
    dotfile cannot be picked up by one and skipped by the other.
    """
    return [paths.slot_path(n) for n in _anthropic_account_names()]


def provider_of(name: str) -> str:
    """The account's provider, READ FROM THE MANIFEST — never inferred.

    This used to answer ``"anthropic"`` for any name it did not recognise, so a
    typo, a stale session row, or an account type this build predates all resolved
    silently to the Claude OAuth launch path. It now raises
    :class:`~ccswitch.account_registry.UnknownAccountError`; callers that must
    degrade (the usage sweep) catch it and report "unknown", which is safe,
    instead of acting on a wrong provider.
    """
    if aliases_store_root(name):
        # A nameless/dotted name collapses every per-account path onto the shared
        # store root; it is never a real account.
        raise account_registry.UnknownAccountError(name)
    return account_registry.provider_of(name)


def provider_or_none(name: str) -> str | None:
    """``provider_of`` for callers that ask "is this account provider X?".

    Returns None — never a default provider — when the account is not in the
    manifest. Every caller compares the result against a specific provider
    (``== copilot.PROVIDER``), so an unregistered account answers "no" to every
    such question and no provider-specific machinery (a Copilot proxy, a Codex
    app-server) is started on its behalf. That is the safe answer; the old
    ``provider_of`` returning ``"anthropic"`` for an unknown name was not, because
    it made the Claude launch path the silent default for a name nobody had
    declared.

    It also degrades on a manifest FAULT, not just an unknown name. The manifest
    lives on the account store, which is the sshfs mount that produced torn reads
    on 2026-09-04 (#1005), and this function sits on hot paths — ``configdir.compose``
    (every spawn and reassign), the orchestrator's rotation and the driver — where
    ``Path.exists()`` used to swallow the same faults. A transient ``EIO``/
    ``ESTALE``/``PermissionError`` therefore answers "no provider-specific
    machinery" (the safe answer, as above) instead of aborting a spawn.
    ``provider_of`` stays strict for callers that want the error.
    """
    try:
        return provider_of(name)
    except account_registry.UnknownAccountError:
        return None
    except (account_registry.AccountRegistryError, OSError) as e:
        account_registry.warn_once(
            f"provider_or_none:{type(e).__name__}",
            "provider_or_none(%s): the account manifest is unreadable (%s) — "
            "degrading to 'no provider'", name, e)
        return None


def all_accounts() -> list[tuple[str, str]]:
    """``(name, provider)`` for every registered account, from the MANIFEST.

    Filesystem discovery (``_anthropic_account_names`` /
    ``copilot.copilot_account_names``) is reachable only through
    ``account_registry.discover_filesystem_accounts``, i.e. the importer and the
    ``ccswitch accounts import`` repair command.
    """
    return [(e.name, e.provider) for e in account_registry.all_entries()]


def _account_names() -> list[str]:
    return sorted({n for n, _ in all_accounts()})


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


# Sentinel: no setup-token exists, so the caller should perform its normal
# needs-reauth flip. Distinct from `None`, which the fallback returns to mean
# "setup-token present, account kept ok, but usage is unknown".
_NO_SETUP_TOKEN: object = object()


def _record_health(name: str, state: str, detail: str | None = None) -> None:
    """Mirror a needs-reauth / recovered transition into the account manifest.

    Best-effort and deliberately RARE: only credential-state transitions are
    recorded, never the routine "usage fetched OK" outcome. The manifest is a
    fleet-replicated file whose revision counter is meant to mean "identity
    changed", so stamping it on every poll would churn it once per account per
    minute. The ledger stays the live health signal; this is the durable,
    human-readable record of it.

    Swallows registry faults: on a runner (``ManifestNotWritable``) or for an
    account that is not registered, the auth path must proceed unchanged — this
    is bookkeeping, not a gate.
    """
    try:
        account_registry.set_health(name, state, detail=detail)
    except account_registry.AccountRegistryError:
        log.debug("account health not recorded for %s (not the manifest writer, "
                  "or account not registered)", name)
    except Exception:  # noqa: BLE001 — never let bookkeeping break the auth path
        log.warning("account health write failed for %s", name, exc_info=True)


def _note_usage_fetch_failure(name: str) -> None:
    """Record a failed usage fetch in the ledger (grows the shared cooldown)."""
    with ledger.locked_ledger() as led:
        ledger.note_usage_fetch_failure(led, name, time.time())


def _setup_token_usage_fallback(name: str, now: datetime, retry_kw: dict) -> "QuotaSnapshot | None | object":
    """Auth fallback for accounts that hold a long-lived setup-token.

    A setup-token (``sk-ant-oat01``) makes the account launch-capable via
    ``CLAUDE_CODE_OAUTH_TOKEN`` even when the slot's browser-OAuth token is dead,
    so the usage path must NOT flip such an account to needs-reauth. Best-effort
    fetch usage with the setup-token; on any failure keep the account ``ok`` with
    usage unknown — the orchestrator's in-session 401 path stays the authority on
    a token that is genuinely dead for inference too. Returns ``_NO_SETUP_TOKEN``
    when no token exists, so the caller proceeds to its normal needs-reauth flip.
    NEVER logs the token.
    """
    from . import crypto
    tok = crypto.read_oauth_token(name)
    if not tok:
        return _NO_SETUP_TOKEN
    u: QuotaSnapshot | None = None
    try:
        u = oauth.fetch_usage(tok, **retry_kw)
    except (oauth.AuthError, oauth.UsageError, oauth.TransientError) as e:
        log.warning("usage_for(%s): setup-token usage fetch failed (%s); keeping ok, usage unknown", name, e)
    with ledger.locked_ledger() as led:
        if u is not None:
            ledger.record_usage(led, name, u, now)
        else:
            ledger.note_usage_fetch_failure(led, name, time.time())
        ledger.set_status(led, name, "ok")
    return u


def usage_for(name: str, *, allow_refresh: bool = True, path: str = "unknown") -> QuotaSnapshot | None:
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
    try:
        provider = provider_of(name)
    except account_registry.UnknownAccountError:
        # The account is not registered. Report "usage unknown" rather than
        # guessing a provider — the old anthropic fallback would refresh, and
        # possibly flip to needs-reauth, against a credential slot that belongs to
        # nobody.
        log.warning("usage_for(%s): not in the account manifest — usage unknown", name)
        return None
    if provider == copilot.PROVIDER:
        return _copilot_usage_for(name)
    if name in _dead_accounts:
        log.debug("usage_for(%s): refresh token known-dead (400) — skipping without network I/O", name)
        return None
    cfg = load_config()
    retry_kw = {"max_retries": cfg.retry_max_retries, "max_total_sec": cfg.retry_max_total_sec}
    now = datetime.now(timezone.utc)
    with ledger.locked_ledger() as led:
        cached = ledger.cached_usage(led, name, cfg, now)
        if cached is not None:
            return cached
        # Failure cooldown: after a failed fetch, serve "unknown" without any
        # network I/O (token OR usage endpoint) until the backoff elapses.
        # Synchronous callers like pick_account_detailed otherwise re-fetch a
        # failing account on every probe and can sustain a 429 indefinitely.
        if ledger.usage_fetch_cooldown_active(led, name, time.time()):
            log.debug("usage_for(%s): fetch-failure cooldown active — skipping", name)
            return None

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
            _note_usage_fetch_failure(name)
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
        _note_usage_fetch_failure(name)
        return None
    except oauth.AuthError as e:
        # A live setup-token keeps the account usable — don't flip to needs-reauth.
        fb = _setup_token_usage_fallback(name, now, retry_kw)
        if fb is not _NO_SETUP_TOKEN:
            return fb  # type: ignore[return-value]
        log.warning("usage_for(%s): refresh token dead (%s) — marking needs-reauth", name, e)
        _dead_accounts.add(name)
        emit_auth_failure(AuthFailure(
            account=name, status=getattr(e, "status", None), body=getattr(e, "body", None),
            path=path, token_expires_at=_slot_expires_at(name), attempts=1))
        with ledger.locked_ledger() as led:
            ledger.set_status(led, name, "needs-reauth")
            ledger.note_usage_fetch_failure(led, name, time.time())
        _record_health(name, "needs-reauth", detail="refresh token rejected")
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
        _note_usage_fetch_failure(name)
        return None
    except oauth.AuthError:
        # Usage endpoint returned 401 — try one refresh.  Distinguish between
        # 400 from the refresh endpoint (dead grant → stop immediately) and a
        # 401 that survives the refresh (creds genuinely rejected on usage).
        _refresh_auth_err: oauth.AuthError | None = None
        slot = blob  # fallback; overwritten below if the guard runs
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
        except oauth.AuthError as _re:
            # 400 (or 401) from the token endpoint itself — the refresh grant is dead.
            # Do NOT proceed to a usage fetch with the now-dead token.
            _refresh_auth_err = _re
        except (oauth.UsageError, oauth.TransientError) as e:
            log.warning("usage_for(%s): transient error after refresh, leaving status unchanged: %s", name, e)
            _note_usage_fetch_failure(name)
            return None

        if _refresh_auth_err is not None:
            # Refresh returned 400/401 — grant is dead; fast-skip future calls too.
            fb = _setup_token_usage_fallback(name, now, retry_kw)
            if fb is not _NO_SETUP_TOKEN:
                return fb  # type: ignore[return-value]
            log.warning(
                "usage_for(%s): refresh token rejected with %s (%s) — marking needs-reauth",
                name, getattr(_refresh_auth_err, "status", "?") or "?", _refresh_auth_err,
            )
            _dead_accounts.add(name)
            emit_auth_failure(AuthFailure(
                account=name,
                status=getattr(_refresh_auth_err, "status", None),
                body=getattr(_refresh_auth_err, "body", None),
                path=path, token_expires_at=_slot_expires_at(name), attempts=2))
            with ledger.locked_ledger() as led:
                ledger.set_status(led, name, "needs-reauth")
                ledger.note_usage_fetch_failure(led, name, time.time())
            _record_health(name, "needs-reauth", detail="usage credentials rejected")
            return None

        try:
            u = oauth.fetch_usage(slot["accessToken"], **retry_kw)
        except (oauth.UsageError, oauth.TransientError) as e:
            log.warning("usage_for(%s): transient error after refresh, leaving status unchanged: %s", name, e)
            _note_usage_fetch_failure(name)
            return None
        except oauth.AuthError as e:
            # 401 survived a successful refresh — usage creds genuinely rejected.
            fb = _setup_token_usage_fallback(name, now, retry_kw)
            if fb is not _NO_SETUP_TOKEN:
                return fb  # type: ignore[return-value]
            log.warning("usage_for(%s): 401 survived a fresh refresh (%s) — marking needs-reauth", name, e)
            _dead_accounts.add(name)
            emit_auth_failure(AuthFailure(
                account=name, status=getattr(e, "status", None), body=getattr(e, "body", None),
                path=path, token_expires_at=_slot_expires_at(name), attempts=2))
            with ledger.locked_ledger() as led:
                ledger.set_status(led, name, "needs-reauth")
                ledger.note_usage_fetch_failure(led, name, time.time())
            _record_health(name, "needs-reauth", detail="usage credentials rejected")
            return None

    with ledger.locked_ledger() as led:
        ledger.record_usage(led, name, u, now)
        ledger.set_status(led, name, "ok")
    return u


def pick_account_detailed(cfg: Config, exclude: set[str] = frozenset(),
                          load: dict[str, float] | None = None,
                          *, reserve: bool = True) -> Picked:
    """Select the best available account.

    ``reserve`` (default True) writes a soft reservation on the picked account so
    a concurrent pick doesn't double-assign it. A READ-ONLY availability probe
    (e.g. "is any account healthy so I can spawn?") MUST pass ``reserve=False``:
    reserving on a check consumes the account's reservation window without
    creating a session, which — when only one account has headroom — starves both
    real spawns and interactive creates for the full reservation TTL.
    """
    names = _account_names()
    # Drop disabled accounts up front: they are blocked everywhere (never selected
    # in any fallback tier) and, being paused, must not be polled for usage either.
    with ledger.locked_ledger() as led:
        names = [n for n in names if ledger.is_account_enabled(led, n)]
    usages = {n: usage_for(n) for n in names}
    now = datetime.now(timezone.utc)
    now_ts = time.time()
    with ledger.locked_ledger() as led:
        reserved = {n for n in names if ledger.reservation_active(led, n, now_ts)}
        unhealthy = frozenset(n for n in names if ledger.get_status(led, n) == "needs-reauth")
        reserves = ledger.weekly_reserves(led)
        extra_allowed = ledger.extra_usage_allowed(led)
        def _pick(excl, rsv):
            return select_picked(usages, rsv, cfg, now, excl, load or {},
                                 unhealthy, optimistic_fallback=True, reserves=reserves,
                                 extra_usage_allowed=extra_allowed)
        try:
            picked = _pick(exclude, reserved)
        except AllAccountsBusy as _first_exc:
            if not exclude and not reserved:
                raise
            # Tiered fallback: progressively relax constraints so a single at-ceiling
            # account or a stale reservation can't starve all new sessions.
            #   1. Remove quarantine exclusion (rate-limited account might have reset).
            #   2. Remove soft reservation (prevents concurrent double-picks, not meant
            #      to block sequential spawns when only one account has headroom).
            _picked = None
            for excl, rsv in [(frozenset(), reserved), (exclude, frozenset()),
                              (frozenset(), frozenset())]:
                try:
                    _picked = _pick(excl, rsv)
                    break
                except AllAccountsBusy:
                    pass
            if _picked is None:
                raise _first_exc
            picked = _picked
        if reserve:
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
    ct = paths.copilot_token_dir(name)
    if ct.exists():
        shutil.rmtree(ct)
    # Drop the ledger entry FIRST, and unconditionally: the health watchdog alerts
    # on ledger status, so a removed account left behind at e.g. needs-reauth
    # alerts forever (2026-07-21: long-deleted primary/secondary paged reauth all
    # day). The credential slot and copilot dir are already gone by this line, so
    # a manifest fault below must not be able to strand that ledger row — hence
    # the ordering, not just the except clause.
    with ledger.locked_ledger() as led:
        led["accounts"].pop(name, None)
    # Drop the manifest entry: the manifest is now what `all_accounts()` reads, so
    # leaving it behind would keep listing an account whose credentials are gone.
    # (Reconcile-on-read is additive only and would never remove it.)
    try:
        account_registry.forget_account(name)
    except account_registry.ManifestNotWritable:
        log.warning("remove_account(%s): runner host cannot edit accounts.json; "
                    "the primary will drop the entry", name)
    except (account_registry.AccountRegistryError, OSError):
        # An unwritable/corrupt manifest or a store-mount fault. The reauth-paging
        # state is already cleared above; re-running `ccswitch accounts remove`
        # (or repairing the manifest) drops the leftover entry.
        log.warning("remove_account(%s): could not drop the accounts.json entry; "
                    "credentials and ledger state are gone, the entry is not",
                    name, exc_info=True)


def _normalized_usage_blob(name: str, acct: dict) -> dict | None:
    """The cached usage blob in the CURRENT (versioned) shape, or None.

    A live ledger still holds pre-versioning entries; decoding and re-encoding
    here means every consumer of ``list_accounts()`` sees one shape and none of
    them has to know the legacy encoding existed.
    """
    blob = acct.get("usage")
    if not blob:
        return None
    try:
        return ledger.usage_from_blob(name, blob).to_dict()
    except (KeyError, TypeError, ValueError):
        log.warning("list_accounts(%s): unreadable cached usage blob", name, exc_info=True)
        return None


def list_accounts() -> list[dict]:
    out = []
    with ledger.locked_ledger() as led:
        for name in _account_names():
            acct = led["accounts"].get(name, {})
            out.append({
                "name": name,
                "status": acct.get("status", "unknown"),
                "usage": _normalized_usage_blob(name, acct),
                "nickname": acct.get("nickname"),
                "provider": provider_of(name),
                "weeklyReservePct": ledger.get_weekly_reserve(led, name),
                "allowExtraUsage": ledger.get_allow_extra_usage(led, name),
                "accountDefaults": ledger.get_account_defaults(led, name),
                "enabled": ledger.is_account_enabled(led, name),
            })
    return out


def set_nickname(name: str, nickname: str | None) -> None:
    with ledger.locked_ledger() as led:
        ledger.set_nickname(led, name, nickname)


def set_weekly_reserve(name: str, pct: float | None) -> None:
    with ledger.locked_ledger() as led:
        ledger.set_weekly_reserve(led, name, pct)


def set_allow_extra_usage(name: str, allow: bool) -> None:
    """Opt an account in/out of spending paid extra-usage credits past its ceiling."""
    with ledger.locked_ledger() as led:
        ledger.set_allow_extra_usage(led, name, allow)


def set_account_enabled(name: str, enabled: bool) -> None:
    """Enable/disable an account without deleting it (reversible pause)."""
    with ledger.locked_ledger() as led:
        ledger.set_account_enabled(led, name, enabled)


def set_account_defaults(
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
    """Set or clear per-account spawn defaults in the ledger."""
    with ledger.locked_ledger() as led:
        ledger.set_account_defaults(
            led, name,
            default_model=default_model,
            clear_model=clear_model,
            default_permission_mode=default_permission_mode,
            clear_permission_mode=clear_permission_mode,
            model_approval_action=model_approval_action,
            clear_model_approval_action=clear_model_approval_action,
            env_overrides=env_overrides,
            clear_env_overrides=clear_env_overrides,
        )


def mark_reauthenticated(name: str) -> None:
    """Clear the stale ``needs-reauth`` status after a successful reauth.

    ``capture_login`` writes the fresh token to the slot but does not touch the
    ledger, so the stored ``needs-reauth`` status and pre-death cached usage
    would otherwise linger until the cache TTL expires. ``get_accounts`` reads
    the stored status *before* it refreshes usage, so the correction has to be
    synchronous: set ``ok`` and drop the cached usage so the very next fetch
    (which the UI triggers on success) is real and re-affirms the status.
    """
    _dead_accounts.discard(name)
    with ledger.locked_ledger() as led:
        ledger.set_status(led, name, "ok")
        ledger.clear_cached_usage(led, name)
    # A successful re-auth replaces the credential behind this account, so the
    # non-secret generation counter advances alongside the health record.
    _record_health(name, "ok")
    try:
        account_registry.bump_generation(name)
    except account_registry.AccountRegistryError:
        pass


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
