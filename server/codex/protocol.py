"""One async JSONL client for the Codex `app-server` protocol.

Transport. The client owns no process: it is constructed over an injected
:class:`AppServerTransport`, so `server/codex/process.py` supplies a
:class:`StreamTransport` over an `asyncio.subprocess` pair and tests supply an
in-process fake. Frames are newline-delimited JSON objects in both directions.

Policies this module fixes, because a protocol client that leaves them to callers
grows a different answer per call site:

* **Handshake gate.** `initialize` must be sent, its response received, and the
  `initialized` notification written before any other request is permitted;
  calling early raises :class:`CodexNotInitializedError`.
* **Request ids** come from one monotonic counter and are matched on an *exact*
  ``int``. `bool` is an `int` subclass in Python, so `{"id": true}` would
  otherwise pop the pending entry for id 1 — the Phase 0 spike hit exactly that.
* **Timeout / cancellation.** Every request has a deadline; both a timeout and a
  cancellation unregister the pending entry, so a late reply is counted as an
  orphan instead of resolving a caller that already gave up.
* **Malformed frames** are counted and skipped, but
  `MAX_CONSECUTIVE_MALFORMED_FRAMES` in a row is fatal: at that point the stream
  is not a JSONL stream and pending callers must fail rather than wait.
* **EOF / transport failure** fails every pending future with a typed
  :class:`CodexTransportClosed` and latches, so later calls fail closed instead
  of hanging.
* **Bounded inbound queue.** With no notification handler installed, supported
  notifications buffer in a bounded queue; on overflow the *oldest* entry is
  dropped and counted. Codex notifications are a rolling stream whose newest
  value supersedes older ones (rate limits, account state), so dropping the
  oldest keeps the freshest state; unbounded growth is a memory DoS.
* **Fail closed inbound.** Unknown notifications are counted and dropped, never
  delivered. Every server-initiated request except the one supported credential
  refresh is answered with a JSON-RPC ``method not found`` error. There is no
  generic server-request dispatch table, so no approval request can ever be
  auto-answered by construction.
* **Bounded server requests.** In-flight reply tasks are capped at
  `max_inflight_server_requests`; over the cap a request is *shed* — no task, no
  reply — which is the fail-closed direction, since an unanswered approval is
  never a granted one. Both shedding and unknown-method rejection log once per
  episode/method rather than once per frame, so a spamming peer cannot flood the
  journal of a daemon that is also serving Claude sessions.

* **External-auth gate.** `external_auth_enabled` is not just an initialize
  capability flag: it gates every frame that can carry host ChatGPT credentials,
  in **both** directions. Outbound, `EXTERNAL_AUTH_ONLY_CLIENT_REQUESTS` is
  refused at `call()` — the single door every request passes through — so no
  wrapper can be the only gated path. Inbound, an experimental-only server
  request is reported *unsupported* and takes the ordinary `-32601` path, so a
  client that advertised `experimentalApi=false` can never answer a refresh with
  a live token. Both gates matter in the default configuration, where the
  process manager installs a refresh handler unconditionally and
  `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED` defaults to false.

Nothing here reads the environment: `external_auth_enabled` is passed in.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from server.codex import redaction, schema
from server.obs.context import log_event

logger = logging.getLogger(__name__)

JSONRPC_METHOD_NOT_FOUND: Final = -32601
JSONRPC_INVALID_PARAMS: Final = -32602
JSONRPC_INTERNAL_ERROR: Final = -32603

MAX_CONSECUTIVE_MALFORMED_FRAMES: Final = 5
DEFAULT_REQUEST_TIMEOUT_S: Final = 30.0
DEFAULT_MAX_BUFFERED_NOTIFICATIONS: Final = 256
DEFAULT_MAX_INFLIGHT_SERVER_REQUESTS: Final = 8
_MAX_ERROR_MESSAGE_CHARS: Final = 200

DEFAULT_CLIENT_INFO: Final = schema.ClientInfo(
    name="ccswitch", version="1", title="ccswitch session manager"
)


# ---------------------------------------------------------------------------
# Typed errors — messages never carry a wire value
# ---------------------------------------------------------------------------


class CodexProtocolError(RuntimeError):
    """Base class for every app-server transport or dispatch failure."""


class CodexNotInitializedError(CodexProtocolError):
    """A call was attempted before the initialize/initialized handshake."""


class CodexExternalAuthDisabled(CodexProtocolError):
    """An external-token operation was attempted without the experimental API."""


class CodexRequestTimeout(CodexProtocolError):
    """A request exceeded its deadline; the pending entry was unregistered."""


class CodexTransportClosed(CodexProtocolError):
    """The app-server stream ended or failed; every pending request was failed."""


class CodexFramingError(CodexProtocolError):
    """The peer stopped producing valid JSONL frames."""


class CodexRequestFailed(CodexProtocolError):
    """The app-server answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str, *, method: str | None = None) -> None:
        self.code = code
        self.method = method
        where = f" for {method}" if method else ""
        super().__init__(f"codex app-server request failed{where} ({code}): {message}")

    @classmethod
    def from_wire(cls, payload: Any, *, method: str | None = None) -> CodexRequestFailed:
        code = 0
        message = ""
        if isinstance(payload, Mapping):
            raw_code = payload.get("code")
            if type(raw_code) is int:
                code = raw_code
            raw_message = payload.get("message")
            if isinstance(raw_message, str):
                message = raw_message
        safe = redaction.scrub_text(message)[:_MAX_ERROR_MESSAGE_CHARS]
        return cls(code, safe or "no message", method=method)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class AppServerTransport(Protocol):
    """A bidirectional newline-delimited frame stream."""

    async def read_line(self) -> bytes:
        """Return one line including its terminator, or ``b""`` at EOF."""
        ...

    async def write_line(self, line: bytes) -> None: ...

    async def aclose(self) -> None: ...


class StreamTransport:
    """`AppServerTransport` over an asyncio reader/writer pair (subprocess pipes)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read_line(self) -> bytes:
        try:
            return await self._reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise CodexFramingError(
                "codex app-server emitted a frame larger than the stream buffer"
            ) from exc

    async def write_line(self, line: bytes) -> None:
        self._writer.write(line)
        await self._writer.drain()

    async def aclose(self) -> None:
        if self._writer.is_closing():
            return
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtocolCounters:
    """An observability snapshot. Keys are method names or fixed labels only."""

    unknown_notifications: int = 0
    invalid_notifications: int = 0
    dropped_notifications: int = 0
    notification_handler_errors: int = 0
    unknown_server_requests: int = 0
    unhandled_server_requests: int = 0
    shed_server_requests: int = 0
    malformed_frames: int = 0
    orphan_responses: int = 0
    unknown_notification_methods: Mapping[str, int] = field(default_factory=dict)


@dataclass
class _MutableCounters:
    unknown_notifications: int = 0
    invalid_notifications: int = 0
    dropped_notifications: int = 0
    notification_handler_errors: int = 0
    unknown_server_requests: int = 0
    unhandled_server_requests: int = 0
    shed_server_requests: int = 0
    malformed_frames: int = 0
    orphan_responses: int = 0
    unknown_notification_methods: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> ProtocolCounters:
        return ProtocolCounters(
            unknown_notifications=self.unknown_notifications,
            invalid_notifications=self.invalid_notifications,
            dropped_notifications=self.dropped_notifications,
            notification_handler_errors=self.notification_handler_errors,
            unknown_server_requests=self.unknown_server_requests,
            unhandled_server_requests=self.unhandled_server_requests,
            shed_server_requests=self.shed_server_requests,
            malformed_frames=self.malformed_frames,
            orphan_responses=self.orphan_responses,
            unknown_notification_methods=dict(self.unknown_notification_methods),
        )


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[Any]
    method: str


NotificationHandler = Callable[[str, schema.ServerNotificationParams], None]
RefreshHandler = Callable[
    [schema.ChatgptAuthTokensRefreshParams],
    Awaitable[schema.ChatgptAuthTokensRefreshResponse],
]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CodexAppServerClient:
    """The single JSONL client for one `codex app-server` connection."""

    def __init__(
        self,
        transport: AppServerTransport,
        *,
        external_auth_enabled: bool,
        client_info: schema.ClientInfo = DEFAULT_CLIENT_INFO,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        notification_handler: NotificationHandler | None = None,
        refresh_handler: RefreshHandler | None = None,
        max_buffered_notifications: int = DEFAULT_MAX_BUFFERED_NOTIFICATIONS,
        max_inflight_server_requests: int = DEFAULT_MAX_INFLIGHT_SERVER_REQUESTS,
    ) -> None:
        self._transport = transport
        self._external_auth_enabled = external_auth_enabled
        self._client_info = client_info
        self._request_timeout_s = request_timeout_s
        self._reply_timeout_s = request_timeout_s
        self._notification_handler = notification_handler
        self._refresh_handler = refresh_handler
        self._notifications: asyncio.Queue[
            tuple[str, schema.ServerNotificationParams]
        ] = asyncio.Queue(maxsize=max(1, max_buffered_notifications))
        self._pending: dict[int, _Pending] = {}
        self._ids = itertools.count(1)
        self._counters = _MutableCounters()
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._reply_tasks: set[asyncio.Task[None]] = set()
        self._max_inflight_server_requests = max(1, max_inflight_server_requests)
        self._server_requests_saturated = False
        self._logged_unknown_server_requests: set[str] = set()
        self._consecutive_malformed = 0
        self._closed_error: CodexProtocolError | None = None
        self._initialized = False
        self._initialize_response: schema.InitializeResponse | None = None

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} initialized={self._initialized} "
            f"pending={len(self._pending)} closed={self._closed_error is not None}>"
        )

    # -- introspection -----------------------------------------------------

    @property
    def counters(self) -> ProtocolCounters:
        return self._counters.snapshot()

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def initialize_response(self) -> schema.InitializeResponse | None:
        return self._initialize_response

    @property
    def pending_request_count(self) -> int:
        return len(self._pending)

    @property
    def buffered_notification_count(self) -> int:
        return self._notifications.qsize()

    @property
    def inflight_server_request_count(self) -> int:
        return len(self._reply_tasks)

    @property
    def external_auth_enabled(self) -> bool:
        return self._external_auth_enabled

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def aclose(self) -> None:
        self._fail_all(CodexTransportClosed("codex app-server client was closed"))
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for reply in tuple(self._reply_tasks):
            reply.cancel()
        if self._reply_tasks:
            await asyncio.gather(*self._reply_tasks, return_exceptions=True)
            self._reply_tasks.clear()
        await self._transport.aclose()

    async def initialize(self) -> schema.InitializeResponse:
        """Run the mandatory handshake: initialize request, then `initialized`."""
        if self._initialized:
            raise CodexProtocolError("codex app-server client is already initialized")
        params = schema.InitializeParams(
            client_info=self._client_info,
            capabilities=schema.InitializeCapabilities(
                experimental_api=self._external_auth_enabled
            ),
        )
        raw = await self.call(schema.METHOD_INITIALIZE, params.to_wire())
        response = schema.parse_initialize_response(raw)
        await self.notify_initialized()
        self._initialized = True
        self._initialize_response = response
        log_event(
            logger,
            logging.INFO,
            "codex.protocol.initialized",
            experimental_api=self._external_auth_enabled,
            platform_os=response.platform_os,
        )
        return response

    async def notify_initialized(self) -> None:
        await self.notify(schema.METHOD_INITIALIZED)

    # -- outbound ----------------------------------------------------------

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Issue one supported client request and await its result.

        This is the one door every outbound request goes through, so the
        external-auth gate lives here rather than on individual wrappers: a
        method added to `EXTERNAL_AUTH_ONLY_CLIENT_REQUESTS` later cannot reopen
        the hole by forgetting its own check.
        """
        if method not in schema.SUPPORTED_CLIENT_REQUESTS:
            raise schema.CodexSchemaError(f"unsupported client request: {method!r}")
        if (
            method in schema.EXTERNAL_AUTH_ONLY_CLIENT_REQUESTS
            and not self._external_auth_enabled
        ):
            raise CodexExternalAuthDisabled(
                f"refusing {method}: this client did not negotiate the experimental "
                "app-server API, so it must not put external credentials on the wire"
            )
        if method != schema.METHOD_INITIALIZE and not self._initialized:
            raise CodexNotInitializedError(
                f"codex app-server call {method} before the initialize handshake"
            )
        return await self._request(method, params, timeout)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if method not in schema.SUPPORTED_CLIENT_NOTIFICATIONS:
            raise schema.CodexSchemaError(f"unsupported client notification: {method!r}")
        self._ensure_open()
        await self._write({"method": method, "params": dict(params or {})})

    async def login_with_external_tokens(
        self, params: schema.ExternalAuthLoginParams
    ) -> schema.LoginAccountResponse:
        """Send host-supplied ChatGPT tokens over `account/login/start`.

        Gated on `external_auth_enabled` by `call()`: the external-token variant
        only exists on the experimental surface, so a client that advertised
        `experimentalApi=false` at initialize must never put a raw access token on
        the wire to a process that never negotiated it. Raises
        `CodexExternalAuthDisabled` in that case.
        """
        raw = await self.call(schema.METHOD_LOGIN_START, params.to_wire())
        return schema.parse_login_account_response(raw)

    async def read_account(
        self, params: schema.GetAccountParams | None = None
    ) -> schema.GetAccountResponse:
        raw = await self.call(
            schema.METHOD_ACCOUNT_READ, (params or schema.GetAccountParams()).to_wire()
        )
        return schema.parse_get_account_response(raw)

    async def read_rate_limits(self) -> schema.GetAccountRateLimitsResponse:
        raw = await self.call(schema.METHOD_RATE_LIMITS_READ, {})
        return schema.parse_rate_limits_response(raw)

    async def list_models(
        self, params: schema.ModelListParams | None = None
    ) -> schema.ModelListResponse:
        raw = await self.call(
            schema.METHOD_MODEL_LIST, (params or schema.ModelListParams()).to_wire()
        )
        return schema.parse_model_list_response(raw)

    async def next_notification(
        self, *, timeout: float | None = None
    ) -> tuple[str, schema.ServerNotificationParams]:
        """Pop one buffered notification (only used without a handler installed)."""
        if timeout is None:
            return await self._notifications.get()
        return await asyncio.wait_for(self._notifications.get(), timeout)

    # -- request plumbing --------------------------------------------------

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        timeout: float | None,
    ) -> Any:
        self._ensure_open()
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _Pending(future, method)
        frame = {"id": request_id, "method": method, "params": dict(params or {})}

        async def _send_and_await() -> Any:
            # The write is INSIDE the deadline: `_write_lock` is shared with the
            # inbound reply path, so a reply task blocked on a stalled transport
            # would otherwise starve this call forever, with no timeout to save it.
            await self._write(frame)
            return await future

        try:
            return await asyncio.wait_for(
                _send_and_await(), timeout or self._request_timeout_s
            )
        except TimeoutError as exc:
            log_event(
                logger, logging.WARNING, "codex.protocol.request_timeout", method=method
            )
            raise CodexRequestTimeout(
                f"codex app-server request {method} exceeded its deadline"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _write(self, frame: Mapping[str, Any]) -> None:
        line = json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                await self._transport.write_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = CodexTransportClosed(
                    "codex app-server write failed: " + redaction.scrub_exception(exc)
                )
                self._fail_all(error)
                raise error from None

    def _closed_copy(self) -> CodexProtocolError:
        """Return a *fresh* instance of the latched close reason.

        Re-raising one stored exception grows a single shared ``__traceback__``
        on every raise and pins each frame's locals, so a long-lived closed
        client leaks memory proportional to how often callers keep trying.
        """
        error = self._closed_error
        assert error is not None
        try:
            return type(error)(*error.args)
        except Exception:  # noqa: BLE001 -- a subclass with a richer __init__
            # Never let reconstruction raise: `_fail_all` uses this to resolve
            # pending futures, and a raise there would strand every caller.
            return CodexProtocolError(str(error))

    def _ensure_open(self) -> None:
        if self._closed_error is not None:
            raise self._closed_copy()

    def _fail_all(self, error: CodexProtocolError) -> None:
        if self._closed_error is None:
            self._closed_error = error
        pending, self._pending = self._pending, {}
        for entry in pending.values():
            if not entry.future.done():
                entry.future.set_exception(self._closed_copy())

    # -- inbound -----------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._transport.read_line()
                if not line:
                    self._fail_all(
                        CodexTransportClosed("codex app-server closed its output stream")
                    )
                    return
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    frame = json.loads(stripped)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if self._count_malformed("invalid_json"):
                        return
                    continue
                if not isinstance(frame, dict):
                    if self._count_malformed("not_an_object"):
                        return
                    continue
                self._consecutive_malformed = 0
                if self._dispatch(frame):
                    return
        except asyncio.CancelledError:
            raise
        except CodexProtocolError as exc:
            self._fail_all(exc)
        except Exception as exc:  # noqa: BLE001 -- the reader must never escape untyped
            self._fail_all(
                CodexTransportClosed(
                    "codex app-server read failed: " + redaction.scrub_exception(exc)
                )
            )

    def _count_malformed(self, reason: str) -> bool:
        """Count one malformed frame; return True when the stream is unusable."""
        self._counters.malformed_frames += 1
        self._consecutive_malformed += 1
        log_event(
            logger,
            logging.WARNING,
            "codex.protocol.malformed_frame",
            reason=reason,
            consecutive=self._consecutive_malformed,
        )
        if self._consecutive_malformed >= MAX_CONSECUTIVE_MALFORMED_FRAMES:
            self._fail_all(
                CodexFramingError(
                    "codex app-server produced "
                    f"{self._consecutive_malformed} consecutive malformed frames"
                )
            )
            return True
        return False

    def _dispatch(self, frame: dict[str, Any]) -> bool:
        method = frame.get("method")
        has_id = "id" in frame
        if isinstance(method, str):
            if has_id:
                return self._dispatch_server_request(frame, method)
            return self._dispatch_notification(method, frame.get("params"))
        if has_id:
            return self._dispatch_response(frame)
        return self._count_malformed("unroutable_frame")

    def _dispatch_response(self, frame: dict[str, Any]) -> bool:
        request_id = frame.get("id")
        # `type(...) is int` on purpose: bool subclasses int, so `{"id": true}`
        # would otherwise pop and resolve the pending entry for id 1.
        if type(request_id) is not int:
            self._counters.orphan_responses += 1
            log_event(
                logger, logging.WARNING, "codex.protocol.orphan_response", reason="invalid_id"
            )
            return False
        if ("result" in frame) == ("error" in frame):
            return self._count_malformed("ambiguous_response")
        entry = self._pending.pop(request_id, None)
        if entry is None:
            self._counters.orphan_responses += 1
            log_event(
                logger, logging.DEBUG, "codex.protocol.orphan_response", reason="unknown_id"
            )
            return False
        if entry.future.done():
            return False
        if "error" in frame:
            entry.future.set_exception(
                CodexRequestFailed.from_wire(frame["error"], method=entry.method)
            )
        else:
            entry.future.set_result(frame.get("result"))
        return False

    def _dispatch_notification(self, method: str, params: Any) -> bool:
        if method not in schema.SUPPORTED_SERVER_NOTIFICATIONS:
            label = schema.unknown_notification_label(method)
            self._counters.unknown_notifications += 1
            self._counters.unknown_notification_methods[label] = (
                self._counters.unknown_notification_methods.get(label, 0) + 1
            )
            log_event(
                logger, logging.DEBUG, "codex.protocol.unknown_notification", method=label
            )
            return False
        try:
            parsed = schema.parse_server_notification(method, params)
        except schema.CodexSchemaError as exc:
            self._counters.invalid_notifications += 1
            log_event(
                logger,
                logging.WARNING,
                "codex.protocol.invalid_notification",
                method=method,
                error=redaction.scrub_exception(exc),
            )
            return False
        if self._notification_handler is not None:
            try:
                self._notification_handler(method, parsed)
            except Exception as exc:  # noqa: BLE001 -- a handler must not kill the reader
                self._counters.notification_handler_errors += 1
                log_event(
                    logger,
                    logging.ERROR,
                    "codex.protocol.notification_handler_failed",
                    method=method,
                    error=redaction.scrub_exception(exc),
                )
            return False
        while self._notifications.full():
            self._notifications.get_nowait()
            self._counters.dropped_notifications += 1
            log_event(
                logger, logging.WARNING, "codex.protocol.notification_dropped", method=method
            )
        self._notifications.put_nowait((method, parsed))
        return False

    def _dispatch_server_request(self, frame: dict[str, Any], method: str) -> bool:
        request_id = frame.get("id")
        if type(request_id) is not int:
            # Without a usable id there is nothing to answer; failing closed here
            # means the peer never sees a reply, which is the safe direction.
            return self._count_malformed("invalid_server_request_id")
        if not self._server_request_supported(method):
            label = (
                method if method in schema.DECLARED_SERVER_REQUESTS else "<unrecognized>"
            )
            self._counters.unknown_server_requests += 1
            # One line per distinct method, not per frame: a peer that spams an
            # approval request must not become a log flood in a daemon that is
            # also serving Claude sessions.
            if label not in self._logged_unknown_server_requests:
                self._logged_unknown_server_requests.add(label)
                log_event(
                    logger,
                    logging.WARNING,
                    "codex.protocol.unknown_server_request",
                    method=label,
                )
            self._spawn_reply(
                request_id,
                self._reply_error(
                    request_id, JSONRPC_METHOD_NOT_FOUND, "unsupported server request"
                ),
            )
            return False
        self._spawn_reply(
            request_id, self._handle_refresh_request(request_id, frame.get("params"))
        )
        return False

    def _server_request_supported(self, method: str) -> bool:
        """Decide whether this connection may answer a server-initiated request.

        A method that only exists on the experimental surface is treated as
        *unsupported* — not merely unhandled — when this client advertised
        `experimentalApi=false`, so it takes the same `-32601` path an unknown
        method takes. That keeps the fail-closed behaviour on one code path, and
        it matters because Task 2.2's manager installs a `refresh_handler`
        unconditionally while external auth defaults to disabled: without this,
        the DEFAULT configuration would answer an unsolicited refresh with a live
        access token.
        """
        if method not in schema.SUPPORTED_SERVER_REQUESTS:
            return False
        if (
            method in schema.EXTERNAL_AUTH_ONLY_SERVER_REQUESTS
            and not self._external_auth_enabled
        ):
            return False
        return True

    def _spawn_reply(self, request_id: int, coroutine: Any) -> None:
        """Start one bounded reply task, or shed the request without answering it.

        The inbound server-request path is bounded exactly like the notification
        queue. Overflow *sheds* — the coroutine is closed and no reply is written
        at all — because the only safe thing to do with a server request we cannot
        service is to leave it unanswered: the peer times out, and an approval is
        never accidentally granted. Shedding is counted, and logged once per
        saturation episode rather than once per frame.
        """
        if len(self._reply_tasks) >= self._max_inflight_server_requests:
            coroutine.close()
            self._counters.shed_server_requests += 1
            if not self._server_requests_saturated:
                self._server_requests_saturated = True
                log_event(
                    logger,
                    logging.WARNING,
                    "codex.protocol.server_requests_saturated",
                    limit=self._max_inflight_server_requests,
                )
            return
        task = asyncio.create_task(coroutine)
        self._reply_tasks.add(task)
        task.add_done_callback(self._reap_reply)

    def _reap_reply(self, task: asyncio.Task[None]) -> None:
        self._reply_tasks.discard(task)
        if not self._reply_tasks:
            # Re-arm the saturation log for the next distinct episode.
            self._server_requests_saturated = False
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            log_event(
                logger,
                logging.ERROR,
                "codex.protocol.reply_failed",
                error=redaction.scrub_exception(failure),
            )

    async def _write_reply(self, frame: Mapping[str, Any], event: str) -> None:
        """Write one reply under a bounded deadline.

        A reply task that blocks forever on a stalled transport holds the shared
        write lock and one of the bounded in-flight slots, so every reply write
        gets the same deadline an outbound request gets.
        """
        try:
            await asyncio.wait_for(self._write(frame), self._reply_timeout_s)
        except TimeoutError:
            log_event(logger, logging.WARNING, event, reason="write_deadline")
        except CodexProtocolError:
            log_event(logger, logging.WARNING, event, reason="transport_closed")

    async def _reply_error(self, request_id: int, code: int, message: str) -> None:
        await self._write_reply(
            {"id": request_id, "error": {"code": code, "message": message}},
            "codex.protocol.reply_error_undeliverable",
        )

    async def _reply_result(self, request_id: int, result: Mapping[str, Any]) -> None:
        await self._write_reply(
            {"id": request_id, "result": dict(result)},
            "codex.protocol.reply_result_undeliverable",
        )

    async def _handle_refresh_request(self, request_id: int, params: Any) -> None:
        """Answer `account/chatgptAuthTokens/refresh` — the only supported callback."""
        handler = self._refresh_handler
        if handler is None:
            self._counters.unhandled_server_requests += 1
            log_event(logger, logging.WARNING, "codex.protocol.refresh_unhandled")
            await self._reply_error(
                request_id,
                JSONRPC_METHOD_NOT_FOUND,
                "no credential authority is configured",
            )
            return
        try:
            parsed = schema.parse_chatgpt_auth_tokens_refresh_params(params)
        except schema.CodexSchemaError as exc:
            log_event(
                logger,
                logging.WARNING,
                "codex.protocol.refresh_invalid_params",
                error=redaction.scrub_exception(exc),
            )
            await self._reply_error(
                request_id, JSONRPC_INVALID_PARAMS, "invalid refresh request"
            )
            return
        try:
            response = await handler(parsed)
            if not isinstance(response, schema.ChatgptAuthTokensRefreshResponse):
                raise schema.CodexSchemaError(
                    "refresh handler returned an unsupported response type"
                )
            wire = response.to_wire()
            # Re-parse what we are about to write: the handler is external
            # code, and this is the last point before credentials go on the
            # wire, so validate against the same contract we validate inbound.
            schema.parse_chatgpt_auth_tokens_refresh_response(wire)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a handler failure must stay closed
            log_event(
                logger,
                logging.ERROR,
                "codex.protocol.refresh_failed",
                error=redaction.scrub_exception(exc),
            )
            await self._reply_error(
                request_id, JSONRPC_INTERNAL_ERROR, "credential refresh failed"
            )
            return
        log_event(logger, logging.INFO, "codex.protocol.refresh_answered")
        await self._reply_result(request_id, wire)
