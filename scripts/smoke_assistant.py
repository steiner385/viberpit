#!/usr/bin/env python3
"""Live smoke test for the AI assistant (constitution III), Phase 3 Task 3.4.

Exercises the real edge + real daemon + real DB — no mocks. Never imported by
server/; throwaway verification harness whose output is pasted into the PR body.

Requires a running dev edge (CCSWITCH_DEV=1, a tailnet allowlist that admits
the identity below, CCSWITCH_ASSISTANT_ENABLED=true) and the SAME
CCSWITCH_DB_URL the edge is using, so this script can read back the audit
trail the edge wrote.

Usage:
    CCSWITCH_DB_URL=<the edge's DB url> python scripts/smoke_assistant.py
    [--base-url http://127.0.0.1:8787] [--identity smoke@ccswitch.local]

Steps:
  1. POST /api/assistant/session — create (or resume) the caller's assistant thread.
  2. POST /api/sessions — create a throwaway coding session to archive later.
  3. Attach the assistant thread over /ws, prompt "List my sessions", wait for turnEnd.
  4. Read back an `assistant.tool_call` audit row for tool=list_sessions.
  5. Prompt "Archive session <throwaway id>", wait for a permissionRequest with
     alwaysAllowed: true, deny it.
  6. Assert the throwaway session is still live (not archived).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx
import websockets

PASS = "PASS"
FAIL = "FAIL"


class Step:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = False
        self.detail = ""

    def finish(self, ok: bool, detail: str = "") -> None:
        self.ok = ok
        self.detail = detail
        print(f"[{PASS if ok else FAIL}] {self.name}" + (f" — {detail}" if detail else ""))


def _batch_events(frame: dict) -> list[dict]:
    return frame.get("events", []) if frame.get("type") == "batch" else [frame]


async def _drain_until(ws, match, *, timeout: float = 30.0) -> list[dict]:
    """Receive frames (unwrapping ``batch``) until *match(event)* is True or timeout."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
        except asyncio.TimeoutError:
            break
        frame = json.loads(raw)
        events.extend(_batch_events(frame))
        if any(match(e) for e in events):
            return events
    raise TimeoutError(f"timed out waiting for a matching frame; saw {[e.get('type') for e in events]}")


async def _read_tool_call_audit(db_url: str, identity: str, tool: str) -> bool:
    """Best-effort read of the `assistant.tool_call` audit trail the edge just wrote."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from server.db.models import AuditEvent

    engine = create_async_engine(db_url)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            rows = (await db.execute(
                select(AuditEvent)
                .where(AuditEvent.type == "assistant.tool_call", AuditEvent.identity == identity)
                .order_by(AuditEvent.at.desc()).limit(50)
            )).scalars().all()
        return any(r.detail.get("tool") == tool for r in rows)
    finally:
        await engine.dispose()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--identity", default="smoke@ccswitch.local")
    parser.add_argument("--db-url", default=None,
                         help="Defaults to $CCSWITCH_DB_URL — must match the edge's DB.")
    args = parser.parse_args()

    import os
    db_url = args.db_url or os.environ.get("CCSWITCH_DB_URL")
    if not db_url:
        print("CCSWITCH_DB_URL (or --db-url) is required — must match the running edge's DB",
              file=sys.stderr)
        return 2

    headers = {"Tailscale-User-Login": args.identity}
    ws_url = args.base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    steps: list[Step] = []

    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=30.0) as http:
        step = Step("create/resume assistant session")
        steps.append(step)
        r = await http.post("/api/assistant/session", json={})
        if r.status_code != 201:
            step.finish(False, f"HTTP {r.status_code}: {r.text[:200]}")
            return _summarize(steps)
        assistant_id = r.json()["id"]
        step.finish(True, f"id={assistant_id} state={r.json().get('state')}")

        step = Step("create throwaway session")
        steps.append(step)
        r = await http.post("/api/sessions", json={"name": "smoke-assistant-throwaway"})
        if r.status_code != 201:
            step.finish(False, f"HTTP {r.status_code}: {r.text[:200]}")
            return _summarize(steps)
        throwaway_id = r.json()["id"]
        step.finish(True, f"id={throwaway_id}")

        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "attach", "sessionId": assistant_id}))
            await _drain_until(ws, lambda e: e.get("type") == "replayComplete")

            step = Step("prompt 'List my sessions' reaches turnEnd")
            steps.append(step)
            await ws.send(json.dumps({"type": "prompt", "sessionId": assistant_id,
                                       "text": "List my sessions"}))
            try:
                await _drain_until(ws, lambda e: e.get("type") == "turnEnd")
                step.finish(True)
            except TimeoutError as e:
                step.finish(False, str(e))
                return _summarize(steps)

            step = Step("assistant.tool_call audit row for tool=list_sessions")
            steps.append(step)
            found = await _read_tool_call_audit(db_url, args.identity, "list_sessions")
            step.finish(found, "" if found else "no matching audit row found")

            step = Step("prompt 'Archive session <id>' surfaces a permissionRequest")
            steps.append(step)
            await ws.send(json.dumps({
                "type": "prompt", "sessionId": assistant_id,
                "text": f"Archive session {throwaway_id}"}))
            try:
                events = await _drain_until(ws, lambda e: e.get("type") == "permissionRequest")
            except TimeoutError as e:
                step.finish(False, str(e))
                return _summarize(steps)
            perm = next(e for e in events if e.get("type") == "permissionRequest")
            if perm.get("alwaysAllowed") is not True:
                step.finish(False, f"alwaysAllowed={perm.get('alwaysAllowed')!r}")
                return _summarize(steps)
            step.finish(True, f"tool={perm.get('tool')} permId={perm.get('permId')}")

            step = Step("deny the permission and reach turnEnd")
            steps.append(step)
            await ws.send(json.dumps({
                "type": "permissionResponse", "sessionId": assistant_id,
                "permId": perm["permId"], "decision": "deny"}))
            try:
                await _drain_until(ws, lambda e: e.get("type") == "turnEnd")
                step.finish(True)
            except TimeoutError as e:
                step.finish(False, str(e))
                return _summarize(steps)

        step = Step("throwaway session survived the denied archive")
        steps.append(step)
        r = await http.get(f"/api/sessions/{throwaway_id}")
        status_ = r.json().get("status") if r.status_code == 200 else None
        survived = r.status_code == 200 and status_ != "archived"
        step.finish(survived, f"status={status_!r}")

        # Best-effort cleanup — the throwaway session's purpose is spent either way.
        await http.delete(f"/api/sessions/{throwaway_id}")

    return _summarize(steps)


def _summarize(steps: list[Step]) -> int:
    failed = [s for s in steps if not s.ok]
    print(f"\n{len(steps) - len(failed)}/{len(steps)} steps passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
