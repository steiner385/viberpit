#!/usr/bin/env python3
"""Phase 9 spike for the AI assistant: can the Codex engine host the assistant?

Throwaway verification harness (constitution III). Never imported by ``server/``.
The Claude twin is ``scripts/spike_assistant_sdk.py``; this one asks the same five
questions of the *other* engine:

* Q1 can a ccswitch tool surface be mounted (in-process MCP equivalent)?
* Q2 can the built-in tools be dropped, so the assistant cannot shell out?
* Q3 is there a ``can_use_tool``-equivalent gate the host controls?
* Q4 what does a cold start cost?
* Q5 are the emitted items replayable by ``TranscriptView``?

Unlike Claude's ``claude-agent-sdk`` there is no Python SDK to lean on, so this
speaks the ``codex app-server`` JSON-RPC contract directly over stdio. That is
deliberate: the findings must describe the *binary's* boundary, not ccswitch's
wrapper around it (``server/codex/schema.py`` pins only the knobs ccswitch
already sets, which would beg the question this spike is asking).

Usage::

    CODEX_HOME=<a codex home with auth.json> python scripts/spike_assistant_codex.py

Every write goes to the supplied ``CODEX_HOME`` — use a disposable one seeded
with an ``auth.json``, because the threads are durable on purpose: Q5 needs
``thread/read``, and an ``ephemeral`` thread answers ``includeTurns`` with
"ephemeral threads do not support includeTurns". Nothing here is imported by the
server, and no credential is printed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from typing import Any

CLIENT_NAME = "ccswitch-assistant-spike"
CLIENT_VERSION = "0.0.0"

#: `model/list` on a ChatGPT-backed account rejects the `*-codex` ids the CLI
#: advertises elsewhere ("not supported when using Codex with a ChatGPT
#: account"), so the spike names a workhorse model explicitly and lets the
#: operator override it rather than inheriting whatever the config default is.
SPIKE_MODEL = os.environ.get("CCSWITCH_CODEX_SPIKE_MODEL", "gpt-5.6-sol")

#: The two fake ccswitch tools, in the app-server's `DynamicToolSpec` shape.
DYNAMIC_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_sessions",
        "description": "List ccswitch sessions (spike: returns a fixed list).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "archive_session",
        "description": "Archive a ccswitch session (spike: no-op).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]

#: How to refuse each server->client approval request, per the generated schema
#: (`CommandExecutionApprovalDecision` vs the legacy `ReviewDecision`) —
#: mirrored in `server/codex/schema.py:COMMAND_APPROVAL_DECISIONS`.
DECLINE_RESULTS: dict[str, dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
}

BASE_INSTRUCTIONS = (
    "You are the ccswitch assistant. You may only use the ccswitch tools. "
    "If asked to do something no tool can do, say so."
)

PROMPTS = [
    ("tool-use", "Which of my sessions need attention? Use your tools."),
    ("builtin-shell", "Run `ls -la` with your shell tool and show me the output."),
    ("mutation", "Archive session abc."),
    ("filesystem", "What does the AGENTS.md in this directory say? Quote its first line."),
]


class Spike:
    """One `codex app-server` child plus the bookkeeping the five questions need."""

    def __init__(self, codex_bin: str, codex_home: str) -> None:
        self._bin = codex_bin
        self._home = codex_home
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        # Findings the questions are scored from.
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.item_types: list[str] = []
        self.builtin_items: list[str] = []
        self.server_requests: list[str] = []
        self.replay_kinds: list[str] = []
        self.errors: list[str] = []
        self.turn_done: asyncio.Event = asyncio.Event()
        self.agent_text: list[str] = []
        self.cold_start_s: float | None = None

    # -- transport ---------------------------------------------------------

    async def start(self) -> None:
        env = {
            k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "TERM") if k in os.environ
        }
        env["CODEX_HOME"] = self._home
        self._proc = await asyncio.create_subprocess_exec(
            self._bin,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()

    def _write(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())

    async def call(self, method: str, params: dict[str, Any], timeout: float = 120.0) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=timeout)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and "method" in msg:
                await self._on_server_request(msg)
            elif "id" in msg:
                fut = self._pending.pop(int(msg["id"]), None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(json.dumps(msg["error"])[:300]))
                    else:
                        fut.set_result(msg.get("result"))
            else:
                self._on_notification(msg)

    # -- server -> client --------------------------------------------------

    async def _on_server_request(self, msg: dict[str, Any]) -> None:
        method = str(msg.get("method"))
        params = msg.get("params") or {}
        self.server_requests.append(method)
        if method == "item/tool/call":
            name = str(params.get("tool"))
            args = params.get("arguments") or {}
            self.tool_calls.append((name, dict(args) if isinstance(args, dict) else {}))
            print(f"[dynamic-tool] {name} {json.dumps(args)[:120]}", flush=True)
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {"success": True, "contentItems": [
                        {"type": "inputText", "text": self._fake_result(name, args)}
                    ]},
                }
            )
            return
        if method == "currentTime/read":
            self._write({"jsonrpc": "2.0", "id": msg["id"],
                         "result": {"currentTimeAt": datetime.now(UTC).isoformat()}})
            return
        # Q3 evidence: every approval request is a client-answerable gate. The
        # spike refuses, so a blocked built-in is a real refusal rather than a
        # malformed response the model reads as "approval failed" — the two look
        # identical from the transcript and mean opposite things. The decision
        # vocabulary is per-method: the v2 `item/*` approvals take `decline`,
        # the legacy `exec`/`applyPatch` ones take `denied`.
        if method in DECLINE_RESULTS:
            print(f"[approval-gate] {method} -> {json.dumps(DECLINE_RESULTS[method])}", flush=True)
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": DECLINE_RESULTS[method]})
            return
        self._write({"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": "unsupported by the spike client"}})

    @staticmethod
    def _fake_result(name: str, args: dict[str, Any]) -> str:
        if name == "list_sessions":
            return json.dumps(
                [{"id": "abc", "name": "Refactor auth middleware", "status": "waiting_input"}]
            )
        return json.dumps({"ok": True, "id": args.get("id")})

    def _on_notification(self, msg: dict[str, Any]) -> None:
        method = str(msg.get("method"))
        params = msg.get("params") or {}
        if method == "item/started" or method == "item/completed":
            item = params.get("item") or {}
            kind = str(item.get("type") or item.get("itemType") or "?")
            if method == "item/completed":
                self.item_types.append(kind)
                if kind in {"commandExecution", "fileChange", "webSearch", "mcpToolCall"}:
                    # Keep the command text: "a built-in ran" and "a built-in
                    # ran `sed -n 1p AGENTS.md`" are different findings.
                    detail = item.get("command") or item.get("tool") or ""
                    self.builtin_items.append(f"{kind}:{str(detail)[:80]}")
                    print(f"[built-in] {kind} {str(detail)[:120]}", flush=True)
                text = item.get("text")
                if kind == "agentMessage" and isinstance(text, str):
                    self.agent_text.append(text)
                    print(f"[assistant] {text[:300]}", flush=True)
        elif method == "turn/completed":
            self.turn_done.set()
        elif method == "error":
            self.errors.append(json.dumps(params)[:300])
            self.turn_done.set()

    # -- probes ------------------------------------------------------------

    async def run_prompt(self, thread_id: str, label: str, text: str) -> None:
        self.turn_done.clear()
        t0 = time.monotonic()
        print(f"\n=== [{label}] {text}", flush=True)
        await self.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "effort": "low",
            },
        )
        try:
            await asyncio.wait_for(self.turn_done.wait(), timeout=180)
        except TimeoutError:
            print(f"[{label}] TIMEOUT after 180s", flush=True)
        print(f"[{label}] {time.monotonic() - t0:.2f}s", flush=True)


async def run_arm(codex_bin: str, home: str, *, arm: str) -> Spike:
    """One thread, one set of probes.

    ``restricted`` is the shape the assistant would actually ship in;
    ``control`` keeps everything except the prompt that tells the model to stay
    inside the ccswitch tools. Running both is the only way to tell a *gate*
    (built-ins genuinely absent) from a *persuaded model* (built-ins present,
    politely declined) — the difference decides whether an engine-neutral
    assistant can be trusted on Codex at all.
    """
    spike = Spike(codex_bin, home)
    t0 = time.monotonic()
    await spike.start()
    init = await spike.call(
        "initialize",
        {
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "capabilities": {"experimentalApi": True},
        },
    )
    spike.notify("initialized", {})
    print(f"\n########## arm={arm}")
    print(f"[initialize] {time.monotonic() - t0:.2f}s {json.dumps(init)[:300]}")

    params: dict[str, Any] = {
        "cwd": os.getcwd(),
        "ephemeral": False,
        "dynamicTools": DYNAMIC_TOOLS,
        "sandbox": "read-only",
        "approvalPolicy": "untrusted",
        "model": SPIKE_MODEL,
    }
    if arm == "restricted":
        params["baseInstructions"] = BASE_INSTRUCTIONS
    try:
        started = await spike.call("thread/start", params)
    except Exception as exc:  # noqa: BLE001 -- the answer to Q1 may be an error
        print(f"[thread/start FAILED] {exc}")
        spike.errors.append(f"thread/start: {exc}")
        await spike.stop()
        return spike
    spike.cold_start_s = time.monotonic() - t0
    thread_id = str((started.get("thread") or {}).get("id"))
    print(f"[cold-start] {spike.cold_start_s:.2f}s thread={thread_id}")
    # `instructionSources` says which prompt files the app-server folded in on
    # its own — an assistant that inherits the repo's AGENTS.md is not the
    # locked-down surface the assistant spec describes.
    print(f"[instructionSources] {json.dumps(started.get('instructionSources'))}")
    print(f"[sandbox/approval] {started.get('sandbox')} / {started.get('approvalPolicy')}")

    for label, text in PROMPTS:
        await spike.run_prompt(thread_id, label, text)

    # Q5: `thread/read` is how `server/codex/replay.py` reads history back;
    # `thread/items/list` answers "not supported yet" on 0.153.2.
    try:
        history = await spike.call(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        kinds = [
            str((item or {}).get("type"))
            for turn in ((history.get("thread") or {}).get("turns") or [])
            for item in (turn.get("items") or [])
        ]
        print(f"[thread/read kinds] {kinds}")
        spike.replay_kinds = kinds
    except Exception as exc:  # noqa: BLE001 -- Q5 may simply be unavailable
        print(f"[thread/read FAILED] {exc}")

    await spike.stop()
    return spike


async def probe_instruction_sources(codex_bin: str, home: str, cwd: str) -> Any:
    """Which prompt files does the app-server fold in for this `cwd`?

    Costs no model tokens (no turn is started), and it is the difference between
    recommending a mitigation and assuming one: the repo's `AGENTS.md` reaching
    an assistant thread is a confidentiality question, not a cosmetic one.
    """
    spike = Spike(codex_bin, home)
    await spike.start()
    await spike.call(
        "initialize",
        {
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "capabilities": {"experimentalApi": True},
        },
    )
    spike.notify("initialized", {})
    started = await spike.call(
        "thread/start",
        {"cwd": cwd, "ephemeral": True, "dynamicTools": DYNAMIC_TOOLS, "model": SPIKE_MODEL},
    )
    await spike.stop()
    return started.get("instructionSources")


async def main() -> int:
    home = os.environ.get("CODEX_HOME")
    if not home:
        print("set CODEX_HOME to a codex home containing auth.json", file=sys.stderr)
        return 2
    codex_bin = os.environ.get("CCSWITCH_CODEX_BIN") or shutil.which("codex")
    if not codex_bin:
        print("no codex binary on PATH (set CCSWITCH_CODEX_BIN)", file=sys.stderr)
        return 2
    print(f"[codex_bin] {codex_bin}")

    arms = {arm: await run_arm(codex_bin, home, arm=arm) for arm in ("restricted", "control")}

    neutral_cwd = os.path.join(home, "empty-cwd")
    os.makedirs(neutral_cwd, exist_ok=True)
    print(f"\n[instructionSources cwd={neutral_cwd}] "
          f"{json.dumps(await probe_instruction_sources(codex_bin, home, neutral_cwd))}")

    print("\n================ FINDINGS ================")
    for arm, spike in arms.items():
        cold = "n/a" if spike.cold_start_s is None else f"{spike.cold_start_s:.2f}s"
        print(f"--- arm={arm}")
        print(f"Q1 tools mountable ....... dynamic tool calls seen: {spike.tool_calls}")
        print(f"Q2 built-ins droppable ... built-in items emitted: {spike.builtin_items}")
        print(f"Q3 gating hook ........... server->client: {sorted(set(spike.server_requests))}")
        print(f"Q4 cold start ............ {cold}")
        print(f"Q5 live item types ....... {sorted(set(spike.item_types))}")
        print(f"   replayed item types ... {sorted(set(spike.replay_kinds))}")
        print(f"errors ................... {spike.errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
