"""`ccswitch ask` — one-shot prompt to the assistant thread over `/ws` (spec v2 §4.9,
Task 9.7). Mirrors the attach → prompt → stream → permission-card flow
``scripts/smoke_assistant.py`` drives against a real edge, but as a `ccas2:` bearer-token
CLI client meant for scripts rather than a throwaway verification harness.

Flow: `POST /api/assistant/session {fresh}` → connect `/ws` with the bearer → attach →
wait ``replayComplete`` → prompt with ``context.route="/cli"`` → stream ``textDelta`` to
stdout → answer any ``permissionRequest`` (``--yes``/``--no-input``/TTY) → exit on
``turnEnd`` (0) or ``turnFailure`` (1).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, TextIO

from ccswitch import assistant_mcp

# Same defaults as `ccswitch mcp` — both are external `ccas2:` clients of the same edge,
# and a second pair of constants would just be a second thing to keep in sync.
DEFAULT_URL = assistant_mcp.DEFAULT_URL
DEFAULT_TOKEN_ENV = assistant_mcp.DEFAULT_TOKEN_ENV

_NEEDS_SERVER_EXTRA = (
    "ccswitch ask: reaching the assistant needs httpx and websockets — install "
    "them with `pip install 'ccswitch[server]'`"
)


class AskUnavailable(RuntimeError):
    """httpx/websockets are not importable — the `[server]` extra is not installed."""


def _deps() -> tuple[Any, Any]:
    """`(httpx, websockets)`, imported lazily — see `assistant_mcp._assistant`'s
    docstring for why: `ccswitch` proper is installable without the `[server]` extra,
    and every other subcommand must keep working there."""
    try:
        import httpx
        import websockets
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AskUnavailable(f"{_NEEDS_SERVER_EXTRA} ({exc})") from exc
    return httpx, websockets


def ws_url(base_url: str) -> str:
    return base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws"


def batch_events(frame: dict) -> list[dict]:
    """Flatten one received frame into its event list — a `batch` carries several,
    anything else (``replayComplete``, ``textDelta``, …) is itself the one event."""
    return frame.get("events", []) if frame.get("type") == "batch" else [frame]


class AskResult:
    """Accumulates one turn's output for both the streaming and `--json` render paths."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.tools: list[dict] = []
        self.failure: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def as_json(self) -> dict:
        out: dict[str, Any] = {"text": self.text, "tools": self.tools}
        if self.failure is not None:
            out["error"] = self.failure
        return out


def decide_permission(perm: dict, *, yes: bool, no_input: bool) -> tuple[str, str]:
    """`(decision, card_text)` for a `permissionRequest` event. `--yes` always allows;
    `--no-input` always denies; a card is always rendered so the caller can show WHY
    a script exited 1 even when nothing prompted the TTY."""
    card = f"permission requested: {perm.get('tool')} {json.dumps(perm.get('input', {}))}"
    if yes:
        return "allow", card
    if no_input:
        return "deny", card
    return "", card  # "" tells the caller to prompt the TTY itself


async def run_ask(
    *, url: str, token: str, prompt: str, json_out: bool, yes: bool, no_input: bool,
    fresh: bool, out: TextIO | None = None, err: TextIO | None = None,
    ws_connect: Any = None, http_transport: Any = None,
) -> int:
    """Drive one prompt end to end; returns the process exit code (0/1).

    ``ws_connect`` and ``http_transport`` are injectable (a fake async-context-manager
    factory and an ``httpx`` transport, respectively) so tests can drive the
    frame-handling loop and the session-create call without a real socket or edge —
    the same seam ``assistant_mcp.build_api``'s ``transport=`` gives the MCP path.

    ``out``/``err`` default to ``None`` rather than binding ``sys.stdout``/``sys.stderr``
    at import time — a bound default would keep printing to whatever those names
    resolved to when this module first loaded, invisible to ``capsys`` and to any
    caller that reassigns ``sys.stdout`` later.
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    httpx, websockets = _deps()
    if ws_connect is None:
        ws_connect = websockets.connect

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=30.0,
                                  transport=http_transport) as http:
        resp = await http.post("/api/assistant/session", json={"fresh": fresh})
        if resp.status_code not in (200, 201):
            print(f"ccswitch ask: could not open the assistant thread "
                  f"(HTTP {resp.status_code}): {resp.text[:200]}", file=err)
            return 1
        session_id = resp.json()["id"]

    result = AskResult()
    async with ws_connect(ws_url(url), additional_headers=headers) as ws:
        await ws.send(json.dumps({"type": "attach", "sessionId": session_id}))
        async for raw in ws:
            if any(e.get("type") == "replayComplete" for e in batch_events(json.loads(raw))):
                break

        await ws.send(json.dumps({
            "type": "prompt", "sessionId": session_id, "text": prompt,
            "context": {"route": "/cli"},
        }))

        async for raw in ws:
            for event in batch_events(json.loads(raw)):
                t = event.get("type")
                if t == "textDelta":
                    text = event.get("text", "")
                    result.text_parts.append(text)
                    if not json_out:
                        print(text, end="", file=out)
                elif t == "toolCall":
                    result.tools.append({"name": event.get("name"), "input": event.get("input")})
                elif t == "permissionRequest":
                    decision, card = decide_permission(event, yes=yes, no_input=no_input)
                    print(card, file=err)
                    if not decision:
                        answer = await asyncio.to_thread(input, "Allow? [y/N] ")
                        decision = "allow" if answer.strip().lower() in ("y", "yes") else "deny"
                    await ws.send(json.dumps({
                        "type": "permissionResponse", "sessionId": session_id,
                        "permId": event.get("permId"), "decision": decision,
                    }))
                elif t == "turnFailure":
                    result.failure = event
                elif t == "turnEnd":
                    return _finish(result, json_out=json_out, out=out, err=err)
    return _finish(result, json_out=json_out, out=out, err=err)


def _finish(result: AskResult, *, json_out: bool, out: TextIO, err: TextIO) -> int:
    if json_out:
        print(json.dumps(result.as_json()), file=out)
    else:
        print(file=out)  # the streamed text has no trailing newline of its own
        if result.failure is not None:
            print(f"ccswitch ask: turn failed: {result.failure}", file=err)
    return 1 if result.failure is not None else 0


def run(args: argparse.Namespace) -> int:
    import os

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"ccswitch ask: no token in ${args.token_env} — mint one with "
              f"`POST /api/assistant/tokens` and export it", file=sys.stderr)
        return 2

    try:
        _deps()
    except AskUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3

    return asyncio.run(run_ask(
        url=args.url, token=token, prompt=args.prompt, json_out=args.json,
        yes=args.yes, no_input=args.no_input, fresh=args.fresh,
    ))
