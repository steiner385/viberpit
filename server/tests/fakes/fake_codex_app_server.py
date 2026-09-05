"""In-process fake `codex app-server`, driven over the real JSONL transport.

The fake speaks exactly the byte stream a real app-server child process would
write to stdout and reads exactly what the client writes to its stdin, so framing
concerns — partial lines, malformed JSON, blank lines, EOF — are reproduced
faithfully without spawning a process.

It is deliberately scriptable rather than smart:

* `send`/`send_raw`/`notify`/`request`/`respond`/`respond_error`/`eof` drive the
  server side frame by frame, including illegal frames;
* `rule` + `start_autopilot` handle the boring request/response cases;
* `receive`/`expect`/`receive_reply`/`client_frames` observe the client side;
* `drained` waits until the client has consumed everything sent so far, which is
  what lets a test assert on counters without sleeping arbitrarily.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

DEFAULT_TIMEOUT_S = 2.0
_READER_LIMIT = 1 << 20


@dataclass
class _Rule:
    result: Any = None
    error: dict[str, Any] | None = None
    delay: float = 0.0


class FakeTransport:
    """Client-side transport half wired to the fake's byte streams."""

    def __init__(self, reader: asyncio.StreamReader, server: FakeCodexAppServer) -> None:
        self._reader = reader
        self._server = server
        self._stalled = asyncio.Event()
        self._stalled.set()
        self.lines_read = 0
        self.eof_read = False
        self.closed = False

    def stall_writes(self, stalled: bool) -> None:
        """Block (or release) client writes, modelling a slow/backed-up peer.

        This is what makes reply backpressure observable: with writes stalled,
        every reply task stays alive instead of completing instantly.
        """
        if stalled:
            self._stalled.clear()
        else:
            self._stalled.set()

    async def read_line(self) -> bytes:
        line = await self._reader.readline()
        if line:
            self.lines_read += 1
        else:
            self.eof_read = True
        return line

    async def write_line(self, line: bytes) -> None:
        await self._stalled.wait()
        if self.closed:
            raise ConnectionResetError("fake app-server transport is closed")
        self._server._accept_client_line(line)

    async def aclose(self) -> None:
        self.closed = True


class FakeCodexAppServer:
    """A scriptable app-server peer for `server/codex/protocol.py`."""

    def __init__(self) -> None:
        self._reader = asyncio.StreamReader(limit=_READER_LIMIT)
        self._transport = FakeTransport(self._reader, self)
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.client_frames: list[dict[str, Any]] = []
        self.replies: dict[Any, dict[str, Any]] = {}
        self.undecodable_client_lines: list[bytes] = []
        self._rules: dict[str, _Rule] = {}
        self._autopilot: asyncio.Task[None] | None = None
        self._lines_fed = 0
        self._eof = False
        self._next_server_request_id = 1000

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop_autopilot()
        if not self._eof:
            self.eof()

    @property
    def transport(self) -> FakeTransport:
        return self._transport

    # -- server -> client --------------------------------------------------

    def send_raw(self, data: bytes) -> None:
        """Feed exact bytes, including partial lines and invalid JSON."""
        self._reader.feed_data(data)
        self._lines_fed += data.count(b"\n")

    def send(self, frame: dict[str, Any]) -> None:
        self.send_raw(json.dumps(frame, separators=(",", ":")).encode("utf-8") + b"\n")

    def notify(self, method: str, params: Any = None) -> None:
        self.send({"method": method, "params": {} if params is None else params})

    def request(
        self, method: str, params: Any = None, *, request_id: int | None = None
    ) -> int:
        if request_id is None:
            request_id = self._next_server_request_id
            self._next_server_request_id += 1
        self.send(
            {
                "id": request_id,
                "method": method,
                "params": {} if params is None else params,
            }
        )
        return request_id

    def respond(self, request_id: Any, result: Any) -> None:
        self.send({"id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str) -> None:
        self.send({"id": request_id, "error": {"code": code, "message": message}})

    def eof(self) -> None:
        if not self._eof:
            self._eof = True
            self._reader.feed_eof()

    @staticmethod
    def default_initialize_response() -> dict[str, Any]:
        return {
            "codexHome": "/tmp/ccswitch-fake-codex-home",
            "platformFamily": "unix",
            "platformOs": "linux",
            "userAgent": "codex_cli_rs/0.153.2 (fake)",
        }

    # -- client -> server --------------------------------------------------

    def _accept_client_line(self, line: bytes) -> None:
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.undecodable_client_lines.append(bytes(line))
            return
        if not isinstance(frame, dict):
            self.undecodable_client_lines.append(bytes(line))
            return
        self.client_frames.append(frame)
        if "id" in frame and "method" not in frame:
            self.replies[frame["id"]] = frame
        self._inbox.put_nowait(frame)

    def frames_for(self, method: str) -> list[dict[str, Any]]:
        return [f for f in self.client_frames if f.get("method") == method]

    async def receive(self, timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        """Pop the next client frame from the queue."""
        return await asyncio.wait_for(self._inbox.get(), timeout)

    async def expect(
        self, method: str, *, index: int = 0, timeout: float = DEFAULT_TIMEOUT_S
    ) -> dict[str, Any]:
        """Wait for the (index+1)-th client frame naming `method`, by observation."""

        def _found() -> dict[str, Any] | None:
            matches = self.frames_for(method)
            return matches[index] if len(matches) > index else None

        return await self._poll(_found, timeout, f"client never sent {method!r}")

    async def receive_reply(
        self, request_id: Any, timeout: float = DEFAULT_TIMEOUT_S
    ) -> dict[str, Any]:
        """Wait for the client's reply to one server-initiated request."""
        return await self._poll(
            lambda: self.replies.get(request_id),
            timeout,
            f"client never replied to server request {request_id!r}",
        )

    async def drained(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        """Wait until the client has read every byte sent, then let it dispatch."""

        def _caught_up() -> bool | None:
            read_all = self._transport.lines_read >= self._lines_fed
            eof_seen = not self._eof or self._transport.eof_read
            return True if (read_all and eof_seen) else None

        await self._poll(_caught_up, timeout, "client never caught up with the stream")
        # Dispatch is synchronous per frame but reply writes are tasks; give the
        # loop a few turns so counters and replies settle before assertions.
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)

    @staticmethod
    async def _poll(probe: Any, timeout: float, message: str) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = probe()
            if found is not None:
                return found
            if loop.time() >= deadline:
                raise AssertionError(message)
            await asyncio.sleep(0.001)

    # -- autopilot ---------------------------------------------------------

    def rule(
        self,
        method: str,
        *,
        result: Any = None,
        error: dict[str, Any] | None = None,
        delay: float = 0.0,
    ) -> None:
        """Register an automatic reply. A method with no rule is never answered."""
        self._rules[method] = _Rule(result=result, error=error, delay=delay)

    def start_autopilot(self) -> None:
        if self._autopilot is None:
            self._autopilot = asyncio.create_task(self._run_autopilot())

    async def stop_autopilot(self) -> None:
        task, self._autopilot = self._autopilot, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_autopilot(self) -> None:
        while True:
            frame = await self._inbox.get()
            method = frame.get("method")
            if not isinstance(method, str) or "id" not in frame:
                continue
            if method == "initialize":
                self.respond(frame["id"], self.default_initialize_response())
                continue
            rule = self._rules.get(method)
            if rule is None:
                continue
            if rule.delay:
                await asyncio.sleep(rule.delay)
            if rule.error is not None:
                self.send({"id": frame["id"], "error": rule.error})
            else:
                self.respond(frame["id"], rule.result)

    # -- handshake helper --------------------------------------------------

    async def serve_initialize(
        self,
        *,
        response: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Answer one `initialize` and consume the following `initialized`."""
        request = await self.receive(timeout)
        if request.get("method") != "initialize":
            raise AssertionError(f"expected initialize, got {request.get('method')!r}")
        self.respond(request["id"], response or self.default_initialize_response())
        notification = await self.receive(timeout)
        if notification.get("method") != "initialized":
            raise AssertionError(
                f"expected initialized, got {notification.get('method')!r}"
            )
        return request
