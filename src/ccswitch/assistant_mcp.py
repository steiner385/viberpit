"""`ccswitch mcp` — serve the assistant's tool table to any MCP client over stdio.

The tools, their JSON schemas, their trimming and their untrusted-data framing are the
very objects the in-process drawer uses (`server/assistant/tools.py`, built into an MCP
server by `server/assistant/server.py`). This module adds exactly two things: a
transport (stdio) and a credential (a `ccas2:` user token). It deliberately does NOT
re-declare the tool surface — a second copy of the schema-building would drift from the
drawer's the first time a tool changed shape.

It is not a privilege boundary of its own either: every call still goes to the edge over
HTTP with the user's token, where `server/auth/middleware.py` re-checks the token's
scope and the route allowlist. What this file *does* decide is what to even offer —
READ tools only unless `--allow-mutations` — because nothing between an external agent
and these tools shows the human a permission card the way the drawer does.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_TOKEN_ENV = "CCSWITCH_ASSISTANT_TOKEN"

_NEEDS_SERVER_EXTRA = (
    "ccswitch mcp: the assistant tool table needs the server extra — "
    "install it with `pip install 'ccswitch[server]'`"
)


class McpUnavailable(RuntimeError):
    """`server.assistant` is not importable — the `[server]` extra is not installed."""


def _assistant() -> tuple[Any, Any]:
    """`(AssistantApi, build_server)`, imported lazily.

    Lazy because `ccswitch` proper is installable without the `[server]` extra, and
    every other subcommand must keep working there; only this one needs httpx,
    SQLAlchemy and the rest of `server/`.
    """
    try:
        from server.assistant.client import AssistantApi
        from server.assistant.server import build_server
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise McpUnavailable(f"{_NEEDS_SERVER_EXTRA} ({exc})") from exc
    return AssistantApi, build_server


def build_api(*, url: str, token: str, transport: Any | None = None) -> Any:
    """An `AssistantApi` bound to a remote edge instead of loopback.

    `session_id` labels the caller in the `X-CCSwitch-Assistant-Session` header and
    keys the per-client tool-call rate limiter, so one is minted per process rather
    than shared: two `ccswitch mcp` clients get a bucket each.
    """
    AssistantApi, _ = _assistant()
    return AssistantApi(base_url=url.rstrip("/"), token=token,
                        session_id=f"mcp-cli-{uuid.uuid4().hex[:8]}", transport=transport)


def build_mcp_server(api: Any, *, allow_mutations: bool) -> Any:
    """The low-level `mcp` server, ready to be run over any transport."""
    _, build_server = _assistant()
    return build_server(api, read_only=not allow_mutations)["instance"]


async def serve(api: Any, *, allow_mutations: bool) -> None:
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(api, allow_mutations=allow_mutations)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await api.aclose()


def _banner(url: str, *, allow_mutations: bool) -> str:
    if allow_mutations:
        return (f"ccswitch mcp: serving READ + MUTATION tools from {url}. Mutations run "
                f"IMMEDIATELY — ccswitch shows no permission card for them on this path, "
                f"so your MCP client is the only place a human can stop one.")
    return (f"ccswitch mcp: serving read-only tools from {url} "
            f"(pass --allow-mutations to also serve mutating tools). "
            f"Operator-tier tools are never served here.")


def run(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"ccswitch mcp: no token in ${args.token_env} — mint one with "
              f"`POST /api/assistant/tokens` and export it", file=sys.stderr)
        return 2

    # stdout is the JSON-RPC channel: one stray byte on it corrupts the session. Every
    # human-facing line — banner, warnings, tracebacks — goes to stderr, which MCP
    # clients surface as server logs.
    #
    # `server.assistant.tools` logs a warning per call when its best-effort
    # `assistant.tool_call` audit cannot reach a database. Out here there is no
    # database to reach and never will be, so that warning is pure noise — and it is
    # not the record of record anyway: the edge writes each action's own audit row.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("server.assistant.tools").setLevel(logging.ERROR)

    try:
        api = build_api(url=args.url, token=token)
    except McpUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(_banner(args.url, allow_mutations=args.allow_mutations), file=sys.stderr)
    try:
        asyncio.run(serve(api, allow_mutations=args.allow_mutations))
    except KeyboardInterrupt:
        pass
    return 0
