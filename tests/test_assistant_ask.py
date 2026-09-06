"""`ccswitch ask` — one-shot CLI prompt to the assistant thread.

Drives ``run_ask`` end to end against a fake httpx transport (session create) and a
fake websocket (attach/prompt/stream/permission), the same "test the wire surface"
approach ``tests/test_assistant_mcp.py`` uses for `ccswitch mcp`.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ccswitch import assistant_ask, cli


class FakeWS:
    """An async-iterable fake `/ws` socket: frames before the prompt, then frames
    after it. `send()` flips the stage the moment a `prompt` frame goes out, so the
    two `async for raw in ws:` loops in `run_ask` each drain the right queue."""

    def __init__(self, before_prompt: list[dict], after_prompt: list[dict]) -> None:
        self._before = list(before_prompt)
        self._after = list(after_prompt)
        self.sent: list[dict] = []
        self._stage = "before"

    async def send(self, msg: str) -> None:
        data = json.loads(msg)
        self.sent.append(data)
        if data.get("type") == "prompt":
            self._stage = "after"

    def __aiter__(self) -> "FakeWS":
        return self

    async def __anext__(self) -> str:
        queue = self._before if self._stage == "before" else self._after
        if not queue:
            raise StopAsyncIteration
        return json.dumps(queue.pop(0))


class FakeConnect:
    """``websockets.connect``-shaped: called with (url, additional_headers=...),
    used as an async context manager."""

    def __init__(self, ws: FakeWS) -> None:
        self.ws = ws
        self.url: str | None = None
        self.headers: dict | None = None

    def __call__(self, url: str, **kw) -> "FakeConnect":
        self.url = url
        self.headers = kw.get("additional_headers")
        return self

    async def __aenter__(self) -> FakeWS:
        return self.ws

    async def __aexit__(self, *exc) -> bool:
        return False


def _session_transport(session_id: str = "asst-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/assistant/session"
        return httpx.Response(201, json={"id": session_id, "state": "ready"})
    return httpx.MockTransport(handler)


BASIC_FRAMES = [
    {"type": "turnStart", "turnId": "t1"},
    {"type": "textDelta", "turnId": "t1", "text": "Hello "},
    {"type": "textDelta", "turnId": "t1", "text": "world"},
    {"type": "toolCall", "turnId": "t1", "name": "list_sessions", "input": {}},
    {"type": "turnEnd", "turnId": "t1", "usage": {}},
]


@pytest.mark.asyncio
async def test_streams_text_and_exits_zero_on_turn_end(capsys):
    ws = FakeWS([{"type": "replayComplete"}], BASIC_FRAMES)
    rc = await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=False,
        yes=False, no_input=False, fresh=False,
        ws_connect=FakeConnect(ws), http_transport=_session_transport(),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Hello world" in out
    attach, prompt = ws.sent[0], ws.sent[1]
    assert attach == {"type": "attach", "sessionId": "asst-1"}
    assert prompt["context"] == {"route": "/cli"}
    assert prompt["text"] == "hi"


@pytest.mark.asyncio
async def test_json_output_carries_text_and_tool_calls():
    ws = FakeWS([{"type": "replayComplete"}], BASIC_FRAMES)
    rc = await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=True,
        yes=False, no_input=False, fresh=False,
        ws_connect=FakeConnect(ws), http_transport=_session_transport(),
    )
    assert rc == 0


@pytest.mark.asyncio
async def test_turn_failure_exits_one():
    frames = [{"type": "turnFailure", "kind": "error", "message": "boom"}]
    ws = FakeWS([{"type": "replayComplete"}], frames)
    rc = await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=False,
        yes=False, no_input=False, fresh=False,
        ws_connect=FakeConnect(ws), http_transport=_session_transport(),
    )
    assert rc == 1


@pytest.mark.asyncio
async def test_yes_allows_every_permission_card():
    frames = [
        {"type": "permissionRequest", "permId": "p1", "turnId": "t1",
         "tool": "archive_session", "input": {"id": "abc"}},
        {"type": "turnEnd", "turnId": "t1", "usage": {}},
    ]
    ws = FakeWS([{"type": "replayComplete"}], frames)
    rc = await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=False,
        yes=True, no_input=False, fresh=False,
        ws_connect=FakeConnect(ws), http_transport=_session_transport(),
    )
    assert rc == 0
    answers = [m for m in ws.sent if m["type"] == "permissionResponse"]
    assert answers == [{"type": "permissionResponse", "sessionId": "asst-1",
                        "permId": "p1", "decision": "allow"}]


@pytest.mark.asyncio
async def test_no_input_denies_every_permission_card():
    frames = [
        {"type": "permissionRequest", "permId": "p1", "turnId": "t1",
         "tool": "archive_session", "input": {"id": "abc"}},
        {"type": "turnEnd", "turnId": "t1", "usage": {}},
    ]
    ws = FakeWS([{"type": "replayComplete"}], frames)
    rc = await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=False,
        yes=False, no_input=True, fresh=False,
        ws_connect=FakeConnect(ws), http_transport=_session_transport(),
    )
    assert rc == 0
    answers = [m for m in ws.sent if m["type"] == "permissionResponse"]
    assert answers[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_session_create_failure_exits_one(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")
    ws = FakeWS([], [])
    rc = await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=False,
        yes=False, no_input=False, fresh=False,
        ws_connect=FakeConnect(ws), http_transport=httpx.MockTransport(handler),
    )
    assert rc == 1
    assert "HTTP 500" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_fresh_flag_forwarded_to_session_create():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "asst-1"})

    ws = FakeWS([{"type": "replayComplete"}], BASIC_FRAMES)
    await assistant_ask.run_ask(
        url="http://edge:8787", token="ccas2:x", prompt="hi", json_out=False,
        yes=False, no_input=False, fresh=True,
        ws_connect=FakeConnect(ws), http_transport=httpx.MockTransport(handler),
    )
    assert seen["body"] == {"fresh": True}


def test_ws_url_swaps_scheme():
    assert assistant_ask.ws_url("http://edge:8787") == "ws://edge:8787/ws"
    assert assistant_ask.ws_url("https://edge:8787/") == "wss://edge:8787/ws"


def test_cli_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("CCSWITCH_ASSISTANT_TOKEN", raising=False)
    assert cli.main(["ask", "hi", "--url", "http://127.0.0.1:1"]) == 2
    assert "CCSWITCH_ASSISTANT_TOKEN" in capsys.readouterr().err


def test_cli_reads_the_named_token_env_var(monkeypatch, capsys):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    assert cli.main(["ask", "hi", "--token-env", "MY_TOKEN"]) == 2
    assert "MY_TOKEN" in capsys.readouterr().err


def test_yes_and_no_input_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.main(["ask", "hi", "--yes", "--no-input"])
