"""The remote sessions service: one per process, used only on the refresh-authority host.

* **Poller.** While enabled, every ``POLL_S`` each signed-in account's session list is
  read and cached (``listing``). An account whose read fails backs off exponentially up
  to ``MAX_BACKOFF_S``. Started lazily by the first list or open, so a process nobody asks
  never calls claude.ai.
* **Watchers.** One watcher per remote session (single-flight) reads the session's
  events log through ``/events?after_id=``, feeding each event through
  :class:`~server.remote_sessions.translate.RemoteEventTranslator`. Each wire event is kept
  in the watcher's bounded log (``MAX_LOG``; the snapshot a new viewer gets) and, while the
  session has a viewer, published to the viewers: on the daemon, the event bus stream
  ``remote:<session id>`` (``set_publisher``); in process, local queues.
* **Background sync.** After every list read, each listed session (the ``MAX_WARM`` most
  recently updated) gets a *warm* watcher, so opening one serves its snapshot at once.
  Without a viewer a warm watcher is gentle: its first catch-up (the whole log) waits for
  one of ``CATCHUP_CONCURRENCY`` slots, ``CATCHUP_STAGGER_S`` apart; after that it reads
  ``after_id`` only when the list's ``updatedAt`` for the session moved, or — connected
  and ``running`` / ``requires_action`` — at most every ``BUSY_POLL_S``. A disconnected
  session's ``updatedAt`` stands still, so it is read once and then left alone. An account
  in list backoff, or rate limited on events, holds every background read for it. A viewer
  (``open`` / ``touch``, lease ``LEASE_S``) switches the watcher to ``WATCH_POLL_S``. A
  watcher stops when its session leaves the listing or the warm set (unless a viewer still
  holds it, then when the lease lapses) or claude.ai answers 404.
* **Send / answer.** A prompt is a ``user`` event POSTed into the session; an answer is a
  ``control_response`` for the pending ask's ``request_id`` (both verified live).

Every published event carries ``_remote: {epoch, seq}`` — the watcher generation and a
per-session sequence that only grows — so a viewer can drop what its snapshot already had
and notice a restarted watcher. :class:`~server.remote_sessions.view.RemoteSessionView`
strips it before anything reaches a browser.

Robustness (constitution I): the kill switch is read on every call and every tick; an
unexpected status or shape marks that account or session unavailable, logged once per
condition (and once more when it clears); nothing here may raise out of a background task.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid as _uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from server.aio import cancel_and_reap
from server.remote_sessions import listing
from server.remote_sessions.client import (KIND_AUTH, KIND_NOT_FOUND, KIND_RATE_LIMITED,
                                           Credentials, SessionsApiClient, UpstreamError,
                                           credentials_for)
from server.remote_sessions.translate import (PendingAsk, RemoteEventTranslator,
                                              split_question_id)
from server.sessions import events as ev

log = logging.getLogger(__name__)

#: The event-bus stream / ``/ws`` session id prefix for a remote session's live view.
STREAM_PREFIX = "remote:"

POLL_S = 15.0
WATCH_POLL_S = 2.0
MAX_BACKOFF_S = 300.0
#: A watcher no viewer touched for this long stops (viewers touch every ``view.TOUCH_S``).
LEASE_S = 180.0
#: How long a first open waits for the watcher's first read, so the snapshot has history.
FIRST_READ_WAIT_S = 10.0
#: Events kept per watched session for a new viewer's snapshot (oldest dropped first). The
#: web has no "load earlier" for a remote session, so this is generous; a session past it
#: opens on its newest ``MAX_LOG`` events (logged once).
MAX_LOG = 4000
#: Background sync: at most this many warm sessions (most recently updated first; logged
#: once when the cap applies). A session with a viewer is kept regardless.
MAX_WARM = 50
#: A connected session claude.ai reports as working is read at most this often unviewed.
BUSY_POLL_S = 10.0
BUSY_STATUSES = frozenset({"running", listing.STATUS_REQUIRES_ACTION})
#: Unviewed first catch-ups (whole-log reads) run at most this many at a time, started at
#: least ``CATCHUP_STAGGER_S`` apart, so enabling the feature never bursts claude.ai.
CATCHUP_CONCURRENCY = 2
CATCHUP_STAGGER_S = 1.0
MAX_PROMPT_CHARS = 100_000
#: An unknown id triggers an early list read at most this often (no hammering on bad ids).
FORCED_REFRESH_MIN_S = 5.0
DENY_MESSAGE = "The user denied this in ViberPit."

# RemoteSessionsError codes (and the HTTP status the API maps each to).
DISABLED = "REMOTE_SESSIONS_DISABLED"              # 409: kill switch off / not the primary
NOT_FOUND = "REMOTE_SESSION_NOT_FOUND"             # 404: not a listed remote session
NOT_WATCHED = "REMOTE_SESSION_NOT_WATCHED"         # 409: answer/touch with no live watcher
ASK_NOT_PENDING = "REMOTE_ASK_NOT_PENDING"         # 409: no such pending ask (stale card)
BAD_REQUEST = "REMOTE_SESSION_BAD_REQUEST"         # 400: malformed prompt / answer
UNAVAILABLE = "REMOTE_SESSIONS_UNAVAILABLE"        # 502: claude.ai refused or failed
CODES = frozenset({DISABLED, NOT_FOUND, NOT_WATCHED, ASK_NOT_PENDING, BAD_REQUEST,
                   UNAVAILABLE})
HTTP_STATUS = {DISABLED: 409, NOT_FOUND: 404, NOT_WATCHED: 409, ASK_NOT_PENDING: 409,
               BAD_REQUEST: 400, UNAVAILABLE: 502}


class RemoteSessionsError(Exception):
    """An expected refusal; ``code`` is one of ``CODES``. ``error_data`` rides the daemon
    RPC envelope so the edge rebuilds it (``remote._remote_sessions_error``)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if code not in CODES:
            raise ValueError(f"unknown remote sessions code {code!r}")
        super().__init__(message or code)
        self.code = code
        self.error_data: dict[str, Any] = {"code": code}


def stream_id(session_id: str) -> str:
    return STREAM_PREFIX + session_id


def is_stream_id(value: str) -> bool:
    return isinstance(value, str) and value.startswith(STREAM_PREFIX) and \
        len(value) > len(STREAM_PREFIX)


def session_of(stream: str) -> str:
    return stream[len(STREAM_PREFIX):]


def enabled_by_config() -> bool:
    """The ``VIBERPIT_REMOTE_SESSIONS_ENABLED`` kill switch (default off) AND this
    process being the refresh authority — the only place the login may be used."""
    from server.config import get_settings
    from viberpit import accounts as _accounts
    return bool(get_settings().remote_sessions_enabled) and _accounts.is_refresh_authority()


def _default_accounts() -> list[str]:
    from viberpit import accounts as _accounts
    return [a["name"] for a in _accounts.list_accounts() if isinstance(a.get("name"), str)]


@dataclass
class _AccountState:
    status: str = "pending"        # ok | no_login | error:<kind>
    failures: int = 0
    next_at: float = 0.0
    sessions: list[dict] = field(default_factory=list)
    #: Background event reads for this account wait until then (a 429 on events).
    events_hold_until: float = 0.0


@dataclass(eq=False)   # identity semantics: kept in sets, compared with `is`
class _Watcher:
    session_id: str
    account: str
    epoch: int
    translator: RemoteEventTranslator = field(default_factory=RemoteEventTranslator)
    log: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG))
    last_id: str | None = None
    #: The last viewer touch (open / touch / send); -inf for a background start.
    touched_at: float = float("-inf")
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    #: Woken by a listing change, a viewer, a freed catch-up slot or a retire.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    #: In the background-sync set (listed, within ``MAX_WARM``).
    warm: bool = False
    retired: bool = False
    #: Events go to the viewers' stream (set by ``open``; cleared, and the bus ring freed,
    #: when the lease lapses).
    publishing: bool = False
    #: Read to the end of the log at least once.
    caught_up: bool = False
    has_more: bool = False
    # The listing's view of the session (``_apply_listing``).
    list_updated: str | None = None
    list_status: str | None = None
    list_connected: bool = False
    #: ``list_updated`` as of the last successful read's start: a newer one means unread events.
    synced_updated: str | None = None
    fetched_at: float = float("-inf")
    synced_wall: float | None = None
    failures: int = 0
    backoff_until: float = 0.0


Publisher = Callable[[str, dict], Awaitable[None]]


class RemoteSessionsService:
    def __init__(self, *, client: SessionsApiClient | None = None,
                 accounts: Callable[[], Iterable[str]] = _default_accounts,
                 credentials: Callable[[str], Credentials | None] = credentials_for,
                 owned_urls: Callable[[], Iterable[str | None]] = lambda: (),
                 enabled: Callable[[], bool] = enabled_by_config,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 poll_s: float = POLL_S, watch_poll_s: float = WATCH_POLL_S,
                 busy_poll_s: float = BUSY_POLL_S, max_warm: int = MAX_WARM,
                 catchup_concurrency: int = CATCHUP_CONCURRENCY,
                 catchup_stagger_s: float = CATCHUP_STAGGER_S) -> None:
        self._client = client
        self._accounts_fn = accounts
        self._credentials_fn = credentials
        self._owned_urls_fn = owned_urls
        self._enabled_fn = enabled
        self._clock = clock
        self._wall = wall_clock
        self._poll_s = poll_s
        self._watch_poll_s = watch_poll_s
        self._busy_poll_s = busy_poll_s
        self._max_warm = max_warm
        self._catchup_concurrency = max(1, catchup_concurrency)
        self._catchup_stagger_s = catchup_stagger_s
        self._catchups = 0
        self._catchup_next_at = float("-inf")
        self._catchup_waiters: set[_Watcher] = set()
        # Sessions claude.ai answered 404 for: not re-warmed while the listing still shows
        # them (dropped once they leave it).
        self._gone: set[str] = set()
        self._accounts: dict[str, _AccountState] = {}
        self._polled_once = asyncio.Event()
        self._poller: asyncio.Task | None = None
        self._refresh_lock = asyncio.Lock()
        self._watchers: dict[str, _Watcher] = {}
        # Per session, the last sequence number handed out (process lifetime, never reset).
        self._seq: dict[str, int] = {}
        self._epochs: dict[str, int] = {}
        self._publish: Publisher | None = None
        self._release: Callable[[str], None] | None = None
        # In-process viewers' queues, per session (the daemon uses the event bus instead).
        self._local: dict[str, set[asyncio.Queue]] = {}
        self._conditions: set[tuple] = set()
        self._closed = False
        self._forced_at = float("-inf")

    # -- wiring --------------------------------------------------------------------

    def set_publisher(self, publish: Publisher | None,
                      release: Callable[[str], None] | None = None) -> None:
        """Daemon wiring: publish each event to the event bus (``app.state.publish``)
        and free a stopped watcher's ring (``app.state.release_ring``)."""
        self._publish = publish
        self._release = release

    def enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:  # noqa: BLE001 — a broken settings read is "off", never a crash
            self._note(("enabled",), "remote sessions: kill switch unreadable; treating as off")
            return False

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise RemoteSessionsError(DISABLED, "Remote sessions are turned off on this server.")

    def _http(self) -> SessionsApiClient:
        if self._client is None:
            self._client = SessionsApiClient()
        return self._client

    # -- once-per-condition logging ------------------------------------------------

    def _note(self, key: tuple, msg: str, *args: Any, level: int = logging.WARNING) -> None:
        if key in self._conditions:
            return
        self._conditions.add(key)
        log.log(level, msg, *args)

    def _clear(self, key: tuple, msg: str, *args: Any) -> None:
        if key in self._conditions:
            self._conditions.discard(key)
            log.info(msg, *args)

    # -- credentials ---------------------------------------------------------------

    async def _creds(self, account: str) -> Credentials | None:
        from server.sessions.account_auth import account_store_io
        try:
            return await account_store_io(self._credentials_fn, account, default=None)
        except Exception as exc:  # noqa: BLE001
            self._note(("creds", account), "remote sessions: reading %s's login failed (%s)",
                       account, type(exc).__name__)
            return None

    # -- poller --------------------------------------------------------------------

    def _ensure_poller(self) -> None:
        if self._closed or (self._poller is not None and not self._poller.done()):
            return
        self._poller = asyncio.get_running_loop().create_task(
            self._poll_loop(), name="remote-sessions-poller")

    async def _poll_loop(self) -> None:
        while True:
            try:
                if self.enabled():
                    await self.refresh()
                else:
                    # Kill switch off: an idle warm watcher waits only for a wake, so stop
                    # every watcher here rather than leave them to notice.
                    for w in list(self._watchers.values()):
                        self._retire(w)
            except Exception as exc:  # noqa: BLE001 — the poller must outlive any bug
                self._note(("poll",), "remote sessions: poll failed (%s)", type(exc).__name__)
            await asyncio.sleep(self._poll_s)

    async def refresh(self) -> None:
        """Re-read every due account's session list (one at a time; concurrent callers
        share the pass)."""
        if self._refresh_lock.locked():
            async with self._refresh_lock:
                return
        async with self._refresh_lock:
            try:
                names = list(self._accounts_fn())
            except Exception as exc:  # noqa: BLE001
                self._note(("accounts",), "remote sessions: listing accounts failed (%s)",
                           type(exc).__name__)
                names = []
            for gone in set(self._accounts) - set(names):
                del self._accounts[gone]
            for account in names:
                state = self._accounts.setdefault(account, _AccountState())
                if self._clock() < state.next_at:
                    continue
                await self._refresh_account(account, state)
            self._polled_once.set()
            self._sync_warm()

    async def _refresh_account(self, account: str, state: _AccountState) -> None:
        creds = await self._creds(account)
        if creds is None:
            state.status, state.sessions = "no_login", []
            state.next_at = self._clock() + self._poll_s
            self._note(("login", account), "remote sessions: %s has no usable claude.ai "
                       "login here; skipping it", account, level=logging.INFO)
            return
        self._clear(("login", account), "remote sessions: %s's claude.ai login is usable again",
                    account)
        try:
            raw = await self._http().list_sessions(creds)
        except UpstreamError as exc:
            state.failures += 1
            state.status = f"error:{exc.kind}"
            delay = min(MAX_BACKOFF_S, self._poll_s * (2 ** state.failures))
            if exc.retry_after:
                delay = max(delay, min(MAX_BACKOFF_S, exc.retry_after))
            state.next_at = self._clock() + delay
            self._note(("list", account, exc.kind),
                       "remote sessions: listing %s failed (%s); unavailable, retrying in %.0fs",
                       account, exc, delay,
                       level=logging.INFO if exc.kind == KIND_RATE_LIMITED else logging.WARNING)
            return
        except Exception as exc:  # noqa: BLE001
            state.failures += 1
            state.status = "error:internal"
            state.next_at = self._clock() + MAX_BACKOFF_S
            self._note(("list", account, "internal"), "remote sessions: listing %s raised %s",
                       account, type(exc).__name__)
            return
        for key in [k for k in self._conditions if k[:2] == ("list", account)]:
            self._clear(key, "remote sessions: listing %s works again", account)
        state.failures, state.status = 0, "ok"
        state.next_at = self._clock() + self._poll_s
        now = self._wall()
        state.sessions = [s for s in (listing.summarize(r, account, now=now) for r in raw)
                          if s is not None]

    def _owned(self) -> frozenset[str]:
        try:
            return listing.owned_keys(self._owned_urls_fn())
        except Exception as exc:  # noqa: BLE001
            self._note(("owned",), "remote sessions: reading ViberPit's own sessions failed "
                       "(%s)", type(exc).__name__)
            return frozenset()

    def _summaries(self) -> list[dict]:
        owned = self._owned()
        seen: set[str] = set()
        out: list[dict] = []
        for state in self._accounts.values():
            for s in state.sessions:
                if s["id"] in owned or (s.get("url") and listing.session_key(s["url"]) in owned):
                    continue
                if s["id"] not in seen:
                    seen.add(s["id"])
                    out.append(s)
        out.sort(key=listing.sort_key)
        return out

    async def list_sessions(self) -> dict:
        """``{enabled, sessions, accounts}``. Disabled: ``{enabled: False, sessions: [],
        accounts: []}`` — not an error, so the UI can simply hide the group."""
        if not self.enabled():
            return {"enabled": False, "sessions": [], "accounts": []}
        self._ensure_poller()
        if not self._polled_once.is_set():
            try:
                await asyncio.wait_for(self._polled_once.wait(), FIRST_READ_WAIT_S)
            except asyncio.TimeoutError:
                pass
        return {"enabled": True, "sessions": [self._with_sync(s) for s in self._summaries()],
                "accounts": [{"account": a, "status": s.status}
                             for a, s in sorted(self._accounts.items())]}

    def _with_sync(self, summary: dict) -> dict:
        """*summary* plus its background-sync freshness: ``synced`` — a warm snapshot that
        has read to the end of the log at least once is held (opening it needs no full
        read) — and ``lastSyncedAt``, the ISO time of the last successful read."""
        w = self._watchers.get(summary["id"])
        live = w if w is not None and not w.retired else None
        wall = live.synced_wall if live is not None else None
        return {**summary, "synced": bool(live is not None and live.caught_up),
                "lastSyncedAt": (datetime.fromtimestamp(wall, timezone.utc).isoformat()
                                 .replace("+00:00", "Z") if wall is not None else None)}

    async def _account_of(self, session_id: str) -> str:
        """The account a listed (shown) session belongs to. Waits for a read already in
        flight (or the very first one), and makes one early re-read of the healthy
        accounts — never one in backoff — at most every ``FORCED_REFRESH_MIN_S`` for an id
        not cached yet (a session started since the last poll). NOT_FOUND otherwise: a
        session ViberPit runs itself, an archived one, a cloud one or an unknown id are
        all refused alike."""
        def cached() -> str | None:
            for s in self._summaries():
                if s["id"] == session_id:
                    return str(s["account"])
            return None

        self._ensure_poller()
        if not self._polled_once.is_set() or self._refresh_lock.locked():
            await self.refresh()
        account = cached()
        if account is None and self._clock() - self._forced_at >= FORCED_REFRESH_MIN_S:
            self._forced_at = self._clock()
            for state in self._accounts.values():
                if state.failures == 0:
                    state.next_at = 0.0
            await self.refresh()
            account = cached()
        if account is None and self._refresh_lock.locked():
            await self.refresh()      # a concurrent caller's forced read: share it
            account = cached()
        if account is None:
            raise RemoteSessionsError(NOT_FOUND, "No remote session with that id.")
        return account

    # -- watchers ------------------------------------------------------------------

    async def open(self, session_id: str) -> dict:
        """Start (or join) the session's watcher and touch its lease. Returns the
        snapshot ``{epoch, head, events}``: every event kept so far, each stamped
        ``_remote: {epoch, seq}``, and ``head`` — the last seq handed out, which live
        events will exceed. A warm (background-synced) session answers at once."""
        self._require_enabled()
        w = self._watchers.get(session_id)
        if w is None or w.task is None or w.task.done() or w.retired:
            account = await self._account_of(session_id)
            w = self._watchers.get(session_id)   # another open may have won the race
            if w is None or w.task is None or w.task.done() or w.retired:
                w = self._start_watcher(session_id, account)
        w.touched_at = self._clock()
        # Before the snapshot: every later event reaches the viewers' stream.
        w.publishing = True
        w.wake.set()          # a background watcher switches to the viewer cadence now
        if not w.ready.is_set():
            try:
                await asyncio.wait_for(w.ready.wait(), FIRST_READ_WAIT_S)
            except asyncio.TimeoutError:
                pass
        return self._snapshot(w)

    def _snapshot(self, w: _Watcher) -> dict:
        return {"epoch": w.epoch, "head": self._seq.get(w.session_id, -1),
                "events": list(w.log)}

    def touch(self, session_id: str) -> dict:
        """Extend a watcher's lease. ``{epoch}``; NOT_WATCHED when it stopped, or when its
        lease lapsed (it stopped publishing, so the viewer may have missed events and must
        re-open)."""
        self._require_enabled()
        w = self._watchers.get(session_id)
        if w is None or w.task is None or w.task.done() or w.retired or not w.publishing:
            raise RemoteSessionsError(NOT_WATCHED, "This remote session is no longer watched.")
        w.touched_at = self._clock()
        return {"epoch": w.epoch}

    def _start_watcher(self, session_id: str, account: str, *,
                       summary: dict | None = None) -> _Watcher:
        epoch = self._epochs.get(session_id, 0) + 1
        self._epochs[session_id] = epoch
        w = _Watcher(session_id=session_id, account=account, epoch=epoch)
        if summary is not None:
            w.warm = True
            self._apply_listing(w, summary)
        self._watchers[session_id] = w
        w.task = asyncio.get_running_loop().create_task(
            self._watch(w), name=f"remote-session-watch-{session_id}")
        return w

    def _viewed(self, w: _Watcher) -> bool:
        return self._clock() - w.touched_at <= LEASE_S

    # -- background sync -----------------------------------------------------------

    def _apply_listing(self, w: _Watcher, summary: dict) -> None:
        updated = summary.get("updatedAt")
        status = summary.get("status")
        connected = summary.get("connection") == listing.CONNECTED
        changed = (updated != w.list_updated or status != w.list_status
                   or connected != w.list_connected)
        w.list_updated, w.list_status, w.list_connected = updated, status, connected
        if changed:
            w.wake.set()

    def _sync_warm(self) -> None:
        """Reconcile the warm watchers with the listing just read: start one for each
        newly listed session within ``MAX_WARM``, hand each the listing's ``updatedAt`` /
        status (waking it when they moved), and stop the ones no longer listed or warm
        (a viewed one is kept until its lease lapses)."""
        if self._closed or not self.enabled():
            return
        summaries = self._summaries()
        listed = {s["id"]: s for s in summaries}
        self._gone &= set(listed)
        candidates = sorted((s for s in summaries if s["id"] not in self._gone),
                            key=lambda s: -(listing.updated_epoch(s) or 0.0))
        keep = candidates[:self._max_warm]
        if len(candidates) > self._max_warm:
            self._note(("warm_cap",), "remote sessions: %d sessions listed; background sync "
                       "keeps the %d most recently updated warm", len(candidates),
                       self._max_warm, level=logging.INFO)
        else:
            self._clear(("warm_cap",), "remote sessions: every listed session is warm again")
        keep_ids = {s["id"] for s in keep}
        for sid, w in list(self._watchers.items()):
            s = listed.get(sid)
            if s is not None:
                self._apply_listing(w, s)
            if sid in keep_ids:
                w.warm = True
                continue
            w.warm = False
            if self._viewed(w):
                w.wake.set()
            else:
                self._retire(w)
        for s in keep:
            if s["id"] not in self._watchers:
                self._start_watcher(s["id"], str(s["account"]), summary=s)

    def _retire(self, w: _Watcher) -> None:
        """Stop *w* (its loop exits at the next wake) and free its log and bus ring now."""
        w.retired, w.warm = True, False
        self._catchup_waiters.discard(w)
        if self._watchers.get(w.session_id) is w:
            del self._watchers[w.session_id]
            self._release_ring(w.session_id)
        w.wake.set()

    def _release_ring(self, session_id: str) -> None:
        if self._release is not None:
            try:
                self._release(stream_id(session_id))
            except Exception:  # noqa: BLE001
                log.debug("remote sessions: ring release failed", exc_info=True)

    def _wait_s(self, w: _Watcher, viewed: bool) -> float | None:
        """Seconds until *w* should read again (0: now; None: only when woken)."""
        now = self._clock()
        until = w.backoff_until
        if not viewed:
            state = self._accounts.get(w.account)
            if state is not None:
                until = max(until, state.events_hold_until,
                            state.next_at if state.failures else 0.0)
        if now < until:
            return until - now
        if w.has_more:
            return 0.0
        if viewed:
            return max(0.0, w.fetched_at + self._watch_poll_s - now)
        if not w.caught_up or w.list_updated != w.synced_updated:
            return 0.0
        if w.list_connected and w.list_status in BUSY_STATUSES:
            return max(0.0, w.fetched_at + self._busy_poll_s - now)
        return None

    def _catchup_wait(self) -> float | None:
        """Seconds until an unviewed catch-up may start (None: wait for a freed slot)."""
        if self._catchups >= self._catchup_concurrency:
            return None
        return max(0.0, self._catchup_next_at - self._clock())

    def _free_catchup_slot(self) -> None:
        self._catchups -= 1
        waiters, self._catchup_waiters = self._catchup_waiters, set()
        for other in waiters:
            other.wake.set()

    @staticmethod
    async def _doze(w: _Watcher, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(w.wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _watch(self, w: _Watcher) -> None:
        key = ("watch", w.session_id)
        try:
            while self.enabled() and not w.retired:
                w.wake.clear()
                self._catchup_waiters.discard(w)
                viewed = self._viewed(w)
                if w.publishing and not viewed:
                    # The lease lapsed: stop feeding the viewers' stream, free its ring.
                    w.publishing = False
                    self._release_ring(w.session_id)
                if not viewed and not w.warm:
                    return
                wait = self._wait_s(w, viewed)
                if wait is None or wait > 0:
                    await self._doze(w, wait)
                    continue
                slot = not viewed and not w.caught_up
                if slot:
                    wait = self._catchup_wait()
                    if wait is None or wait > 0:
                        self._catchup_waiters.add(w)
                        await self._doze(w, wait)
                        continue
                    self._catchups += 1
                    self._catchup_next_at = self._clock() + self._catchup_stagger_s
                try:
                    await self._read(w, key)
                finally:
                    if slot:
                        self._free_catchup_slot()
                w.ready.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a watcher bug ends that watch, nothing else
            self._note(key + ("internal",), "remote sessions: watcher for %s failed (%s)",
                       w.session_id, type(exc).__name__)
        finally:
            w.ready.set()
            self._catchup_waiters.discard(w)
            if self._watchers.get(w.session_id) is w:
                del self._watchers[w.session_id]
                self._release_ring(w.session_id)

    async def _read(self, w: _Watcher, key: tuple) -> None:
        """One ``/events?after_id=`` read for *w*; a failure sets its backoff."""
        creds = await self._creds(w.account)
        if creds is None:
            self._note(key + ("login",), "remote sessions: %s lost its claude.ai "
                       "login; session %s unavailable", w.account, w.session_id)
            self._back_off(w, None)
            return
        listed_as = w.list_updated
        try:
            page = await self._http().fetch_events(creds, w.session_id, after_id=w.last_id)
        except UpstreamError as exc:
            if exc.kind == KIND_NOT_FOUND:
                await self._emit(w, ev.ErrorEvent(
                    "remote_session_gone",
                    "This remote session no longer exists on claude.ai.").to_wire())
                self._gone.add(w.session_id)
                self._retire(w)
                return
            delay = self._back_off(w, exc.retry_after)
            if exc.kind == KIND_RATE_LIMITED:
                state = self._accounts.get(w.account)
                if state is not None:     # the limit is the account's, not this session's
                    state.events_hold_until = max(state.events_hold_until,
                                                  self._clock() + delay)
            self._note(key + (exc.kind,), "remote sessions: reading session %s "
                       "failed (%s); retrying", w.session_id, exc,
                       level=logging.INFO if exc.kind in (KIND_RATE_LIMITED, KIND_AUTH)
                       else logging.WARNING)
            return
        for raw in page.data:
            for wire in w.translator.feed(raw):
                await self._emit(w, wire)
        if page.last_id is not None:
            w.last_id = page.last_id
        for k in [k for k in self._conditions if k[:2] == key]:
            self._clear(k, "remote sessions: session %s readable again", w.session_id)
        w.failures, w.backoff_until = 0, 0.0
        w.fetched_at, w.synced_wall = self._clock(), self._wall()
        w.synced_updated = listed_as
        w.has_more = page.has_more
        if not page.has_more:
            w.caught_up = True

    def _back_off(self, w: _Watcher, retry_after: float | None) -> float:
        w.failures += 1
        delay = min(MAX_BACKOFF_S, max(self._watch_poll_s * (2 ** w.failures),
                                       retry_after or 0.0))
        w.backoff_until = self._clock() + delay
        return delay

    async def _emit(self, w: _Watcher, wire: dict) -> None:
        seq = self._seq.get(w.session_id, -1) + 1
        self._seq[w.session_id] = seq
        stamped = {**wire, "_remote": {"epoch": w.epoch, "seq": seq}}
        if len(w.log) == w.log.maxlen:
            self._note(("log_cap",), "remote sessions: session %s passed %d events; a view "
                       "opens on the newest %d (logged once)", w.session_id, MAX_LOG,
                       MAX_LOG, level=logging.INFO)
        w.log.append(stamped)
        for q in list(self._local.get(w.session_id, ())):
            q.put_nowait(stamped)
        if self._publish is not None and w.publishing:
            try:
                await self._publish(stream_id(w.session_id), stamped)
            except Exception:  # noqa: BLE001
                self._note(("publish",), "remote sessions: event bus publish failed")

    # -- in-process viewers --------------------------------------------------------

    def subscribe_local(self, session_id: str) -> asyncio.Queue:
        """An in-process viewer's queue of stamped events (no event bus in process)."""
        q: asyncio.Queue = asyncio.Queue()
        self._local.setdefault(session_id, set()).add(q)
        return q

    def unsubscribe_local(self, session_id: str, q: asyncio.Queue) -> None:
        queues = self._local.get(session_id)
        if queues is not None:
            queues.discard(q)
            if not queues:
                del self._local[session_id]

    # -- send / answer -------------------------------------------------------------

    async def _post(self, session_id: str, account: str, event: dict) -> None:
        creds = await self._creds(account)
        if creds is None:
            raise RemoteSessionsError(UNAVAILABLE, f"{account} has no usable claude.ai login "
                                      "here.")
        try:
            await self._http().post_events(creds, session_id, [event])
        except UpstreamError as exc:
            if exc.kind == KIND_NOT_FOUND:
                raise RemoteSessionsError(NOT_FOUND, "No remote session with that id.") from None
            raise RemoteSessionsError(UNAVAILABLE, f"claude.ai refused the request ({exc}).") \
                from None

    async def send_prompt(self, session_id: str, text: Any) -> dict:
        """Send *text* into the session as a user prompt. ``{ok: True, account}``."""
        self._require_enabled()
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS:
            raise RemoteSessionsError(BAD_REQUEST, "A prompt must be non-empty text.")
        account = await self._account_of(session_id)
        await self._post(session_id, account, {
            "uuid": str(_uuid.uuid4()), "session_id": session_id, "type": "user",
            "parent_tool_use_id": None, "message": {"role": "user", "content": text}})
        w = self._watchers.get(session_id)
        if w is not None:
            w.touched_at = self._clock()
        return {"ok": True, "account": account}

    async def answer(self, session_id: str, ask_id: str, *, decision: str | None = None,
                     message: str | None = None, selected: list[str] | None = None,
                     mode: str = "option") -> dict:
        """Answer a pending ask of a watched session. *ask_id* is a permission card's
        ``permId`` (with ``decision`` ``allow`` / ``deny`` and an optional deny
        ``message``) or a question card's ``questionId`` (with ``selected`` labels and
        ``mode``). A multi-question ask is sent once every question has an answer.
        ``{ok: True, sent, account, kind}``."""
        self._require_enabled()
        w = self._watchers.get(session_id)
        # A warm watcher without a viewer may be behind the log: only an open view's
        # cards are answerable (the contract predates background sync).
        if w is None or w.retired or not w.publishing:
            raise RemoteSessionsError(NOT_WATCHED, "Open this remote session to answer it.")
        if not isinstance(ask_id, str) or not ask_id:
            raise RemoteSessionsError(BAD_REQUEST, "An answer needs the card's id.")
        tr = w.translator
        q = split_question_id(ask_id)
        ask: PendingAsk | None
        if q is not None and (ask := tr.pending.get(q[0])) is not None and ask.is_question:
            return await self._answer_question(w, ask, q[1], selected, mode)
        ask = tr.pending.get(ask_id)
        if ask is None or ask.is_question:
            raise RemoteSessionsError(ASK_NOT_PENDING, "That request is no longer pending.")
        if decision not in ("allow", "deny"):
            raise RemoteSessionsError(BAD_REQUEST, "decision must be allow or deny.")
        if decision == "allow":
            inner: dict = {"behavior": "allow", "updatedInput": ask.input,
                           "tool_name": ask.tool_name}
        else:
            note = message.strip() if isinstance(message, str) and message.strip() else None
            inner = {"behavior": "deny", "message": note or DENY_MESSAGE}
        await self._send_control_response(w, ask, inner)
        tr.note_answer(ask.request_id, "permission", decision == "allow")
        return {"ok": True, "sent": True, "account": w.account, "kind": "permission"}

    async def _answer_question(self, w: _Watcher, ask: PendingAsk, index: int,
                               selected: list[str] | None, mode: str) -> dict:
        if index >= len(ask.questions):
            raise RemoteSessionsError(ASK_NOT_PENDING, "That question is no longer pending.")
        if (not isinstance(selected, list) or not selected
                or not all(isinstance(s, str) for s in selected)):
            raise RemoteSessionsError(BAD_REQUEST, "An answer needs at least one choice.")
        if mode not in ("option", "freeform", "chat"):
            raise RemoteSessionsError(BAD_REQUEST, "mode must be option, freeform or chat.")
        ask.answers[index] = (list(selected), mode)
        if len(ask.answers) < len(ask.questions):
            return {"ok": True, "sent": False, "account": w.account, "kind": "question"}
        # The same deny-message framing a ViberPit session's AskUserQuestion answer uses.
        from server.sessions.claude_driver import ClaudeAgentDriver as _D
        texts = [str(qq.get("question") or "") for qq in ask.questions]
        if len(texts) == 1:
            body = _D._format_answer_message(texts[0], *ask.answers[0])
        else:
            body = "\n".join(["[User answered via the question UI]"] + [
                _D._format_answer_line(texts[i], *ask.answers[i]) for i in range(len(texts))])
        try:
            await self._send_control_response(w, ask, {"behavior": "deny", "message": body})
        except RemoteSessionsError:
            ask.answers.clear()   # nothing was sent: the card stays answerable from scratch
            raise
        w.translator.note_answer(ask.request_id, "question", dict(ask.answers))
        return {"ok": True, "sent": True, "account": w.account, "kind": "question"}

    async def _send_control_response(self, w: _Watcher, ask: PendingAsk, inner: dict) -> None:
        await self._post(w.session_id, w.account, {
            "uuid": str(_uuid.uuid4()), "session_id": w.session_id,
            "type": "control_response",
            "response": {"subtype": "success", "request_id": ask.request_id,
                         "response": inner}})
        w.touched_at = self._clock()

    # -- shutdown ------------------------------------------------------------------

    async def aclose(self) -> None:
        self._closed = True
        tasks = [self._poller] + [w.task for w in list(self._watchers.values())]
        for t in tasks:
            if t is not None:
                await cancel_and_reap(t, phase="remote-sessions", timeout=2.0)
        self._watchers.clear()
        if self._client is not None:
            await self._client.aclose()


def service_for(orchestrator: Any) -> RemoteSessionsService:
    """The process's service, kept on the session orchestrator so the daemon RPCs and the
    in-process facade share one. ViberPit's own sessions (excluded from the listing)
    are the orchestrator's records with a ``remote_control_url``."""
    svc = getattr(orchestrator, "_remote_sessions", None)
    if not isinstance(svc, RemoteSessionsService):
        def _owned_urls() -> list[str | None]:
            records = orchestrator.list_sessions(include_assistant=True)
            return [getattr(r, "remote_control_url", None) for r in records]
        svc = RemoteSessionsService(owned_urls=_owned_urls)
        orchestrator._remote_sessions = svc
    return svc
