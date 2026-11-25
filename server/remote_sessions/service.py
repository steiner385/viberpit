"""The remote sessions service: one per process, used only on the refresh-authority host.

* **Poller.** While enabled, every ``POLL_S`` each signed-in account's session list is
  read and cached (``listing``). An account whose read fails backs off exponentially up
  to ``MAX_BACKOFF_S``. Started lazily by the first list or open, so a process nobody asks
  never calls claude.ai.
* **Watchers.** Opening a session starts (single-flight) one watcher for it: it reads the
  session's whole events log, then polls ``/events?after_id=`` every ``WATCH_POLL_S``,
  feeding each event through :class:`~server.remote_sessions.translate.RemoteEventTranslator`.
  Each wire event is kept in the watcher's bounded log (the snapshot a new viewer gets) and
  published to the viewers: on the daemon, the event bus stream ``remote:<session id>``
  (``set_publisher``); in process, local queues. A watcher no viewer touched for
  ``LEASE_S`` stops and frees its log.
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
#: Events kept per watched session for a new viewer's snapshot (oldest dropped first).
MAX_LOG = 4000
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


@dataclass
class _Watcher:
    session_id: str
    account: str
    epoch: int
    translator: RemoteEventTranslator = field(default_factory=RemoteEventTranslator)
    log: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG))
    last_id: str | None = None
    touched_at: float = 0.0
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


Publisher = Callable[[str, dict], Awaitable[None]]


class RemoteSessionsService:
    def __init__(self, *, client: SessionsApiClient | None = None,
                 accounts: Callable[[], Iterable[str]] = _default_accounts,
                 credentials: Callable[[str], Credentials | None] = credentials_for,
                 owned_urls: Callable[[], Iterable[str | None]] = lambda: (),
                 enabled: Callable[[], bool] = enabled_by_config,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 poll_s: float = POLL_S, watch_poll_s: float = WATCH_POLL_S) -> None:
        self._client = client
        self._accounts_fn = accounts
        self._credentials_fn = credentials
        self._owned_urls_fn = owned_urls
        self._enabled_fn = enabled
        self._clock = clock
        self._wall = wall_clock
        self._poll_s = poll_s
        self._watch_poll_s = watch_poll_s
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
        return {"enabled": True, "sessions": self._summaries(),
                "accounts": [{"account": a, "status": s.status}
                             for a, s in sorted(self._accounts.items())]}

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
        events will exceed."""
        self._require_enabled()
        w = self._watchers.get(session_id)
        if w is None or w.task is None or w.task.done():
            account = await self._account_of(session_id)
            w = self._watchers.get(session_id)   # another open may have won the race
            if w is None or w.task is None or w.task.done():
                w = self._start_watcher(session_id, account)
        w.touched_at = self._clock()
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
        """Extend a watcher's lease. ``{epoch}``; NOT_WATCHED when it already stopped."""
        self._require_enabled()
        w = self._watchers.get(session_id)
        if w is None or w.task is None or w.task.done():
            raise RemoteSessionsError(NOT_WATCHED, "This remote session is no longer watched.")
        w.touched_at = self._clock()
        return {"epoch": w.epoch}

    def _start_watcher(self, session_id: str, account: str) -> _Watcher:
        epoch = self._epochs.get(session_id, 0) + 1
        self._epochs[session_id] = epoch
        w = _Watcher(session_id=session_id, account=account, epoch=epoch,
                     touched_at=self._clock())
        self._watchers[session_id] = w
        w.task = asyncio.get_running_loop().create_task(
            self._watch(w), name=f"remote-session-watch-{session_id}")
        return w

    async def _watch(self, w: _Watcher) -> None:
        delay = self._watch_poll_s
        key = ("watch", w.session_id)
        try:
            while self.enabled() and self._clock() - w.touched_at <= LEASE_S:
                creds = await self._creds(w.account)
                if creds is None:
                    self._note(key + ("login",), "remote sessions: %s lost its claude.ai "
                               "login; session %s unavailable", w.account, w.session_id)
                    delay = min(MAX_BACKOFF_S, delay * 2)
                else:
                    try:
                        page = await self._http().fetch_events(creds, w.session_id,
                                                               after_id=w.last_id)
                    except UpstreamError as exc:
                        if exc.kind == KIND_NOT_FOUND:
                            await self._emit(w, ev.ErrorEvent(
                                "remote_session_gone",
                                "This remote session no longer exists on claude.ai.").to_wire())
                            return
                        delay = min(MAX_BACKOFF_S, max(delay * 2, exc.retry_after or 0.0))
                        self._note(key + (exc.kind,), "remote sessions: reading session %s "
                                   "failed (%s); retrying", w.session_id, exc,
                                   level=logging.INFO if exc.kind in (KIND_RATE_LIMITED, KIND_AUTH)
                                   else logging.WARNING)
                    else:
                        for raw in page.data:
                            for wire in w.translator.feed(raw):
                                await self._emit(w, wire)
                        if page.last_id is not None:
                            w.last_id = page.last_id
                        for k in [k for k in self._conditions if k[:2] == key]:
                            self._clear(k, "remote sessions: session %s readable again",
                                        w.session_id)
                        delay = 0.0 if page.has_more else self._watch_poll_s
                w.ready.set()
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a watcher bug ends that watch, nothing else
            self._note(key + ("internal",), "remote sessions: watcher for %s failed (%s)",
                       w.session_id, type(exc).__name__)
        finally:
            w.ready.set()
            if self._watchers.get(w.session_id) is w:
                del self._watchers[w.session_id]
                if self._release is not None:
                    try:
                        self._release(stream_id(w.session_id))
                    except Exception:  # noqa: BLE001
                        log.debug("remote sessions: ring release failed", exc_info=True)

    async def _emit(self, w: _Watcher, wire: dict) -> None:
        seq = self._seq.get(w.session_id, -1) + 1
        self._seq[w.session_id] = seq
        stamped = {**wire, "_remote": {"epoch": w.epoch, "seq": seq}}
        w.log.append(stamped)
        for q in list(self._local.get(w.session_id, ())):
            q.put_nowait(stamped)
        if self._publish is not None:
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
        if w is None:
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
