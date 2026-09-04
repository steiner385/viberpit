#!/usr/bin/env python3
"""Phase 0 spike for the AI assistant: verify SDK options against the real binary.

Throwaway verification harness (constitution III). Never imported by server/.
Usage:
    CLAUDE_CONFIG_DIR=<an account cfg dir> python scripts/spike_assistant_sdk.py

WARNING: as written this hangs at ClaudeSDKClient init until the timeout kills it, because of
setting_sources=[] below — see docs/superpowers/spikes/2026-09-04-assistant-sdk-findings.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
    ResultMessage, StreamEvent, SystemMessage, create_sdk_mcp_server, tool,
)
from mcp.types import ToolAnnotations

SEEN_TOOL_CALLS: list[tuple[str, dict]] = []


@tool("list_sessions", "List ccswitch sessions (spike: returns a fixed list).", {},
      annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def list_sessions(args: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(
        [{"id": "abc", "name": "Refactor auth middleware", "status": "waiting_input"}])}]}


@tool("archive_session", "Archive a session (spike: no-op).", {"id": str},
      annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def archive_session(args: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"ok": True, "id": args["id"]})}]}


async def can_use_tool(tool_name, tool_input, ctx=None):
    SEEN_TOOL_CALLS.append((tool_name, dict(tool_input or {})))
    print(f"[can_use_tool] {tool_name} {json.dumps(tool_input)[:120]}", flush=True)
    return PermissionResultAllow()


async def run_prompt(client: ClaudeSDKClient, prompt: str) -> None:
    t0 = time.monotonic()
    first_delta = None
    await client.query(prompt)
    async for msg in client.receive_messages():
        if isinstance(msg, StreamEvent) and first_delta is None:
            first_delta = time.monotonic() - t0
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                text = getattr(block, "text", None)
                if text:
                    print(f"[assistant] {text[:300]}")
        if isinstance(msg, ResultMessage):
            print(f"[result] turns={msg.num_turns} first_delta={first_delta:.2f}s "
                  f"total={time.monotonic() - t0:.2f}s")
            return


async def main() -> int:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if not cfg:
        print("set CLAUDE_CONFIG_DIR to an account config dir", file=sys.stderr)
        return 2
    server = create_sdk_mcp_server("ccswitch", version="0.0.0",
                                   tools=[list_sessions, archive_session])
    # Without an explicit cli_path the SDK spawns its own vendored
    # _bundled/claude (observed: 2.1.183) instead of the system CLI on PATH
    # (2.1.259) — same shadow pattern as the project's claude_bin.py pin
    # (docs: sdk-bundled-cli-shadows-system-claude). Pin it explicitly.
    cli_path = shutil.which("claude")
    print(f"[cli_path] {cli_path}")
    opts = ClaudeAgentOptions(
        cwd=os.getcwd(),
        env=dict(os.environ, CLAUDE_CONFIG_DIR=cfg),
        cli_path=cli_path,
        system_prompt="You are the ccswitch assistant. You may only use the ccswitch tools. "
                      "If asked to do something no tool can do, say so.",
        tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "Read",
                          "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite"],
        mcp_servers={"ccswitch": server},
        strict_mcp_config=True,
        setting_sources=[],
        can_use_tool=can_use_tool,
        permission_mode="default",
        max_turns=6,
        effort="low",
        model="sonnet",
        include_partial_messages=True,
    )
    t0 = time.monotonic()
    async with ClaudeSDKClient(options=opts) as client:
        async for msg in client.receive_messages():
            if isinstance(msg, SystemMessage):
                print(f"[cold-start] {time.monotonic() - t0:.2f}s subtype={msg.subtype}")
                break
        info = await client.get_server_info()
        print("[server_info.tools]", json.dumps((info or {}).get("tools"), indent=None)[:800])
        print("[server_info.mcp]", json.dumps((info or {}).get("mcp_servers"))[:400])
        await run_prompt(client, "Which of my sessions need attention? Use your tools.")
        await run_prompt(client, "Run `ls -la` with bash and show me the output.")
        await run_prompt(client, "Archive session abc.")
        await run_prompt(client, "What does my CLAUDE.md say? Quote its first line.")
    print("[seen tool calls]", SEEN_TOOL_CALLS)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
