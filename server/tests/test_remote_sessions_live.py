"""Live smoke for remote sessions (constitution III), against the REAL claude.ai sessions API
and a REAL ``claude`` CLI. Skipped unless ``VIBERPIT_TEST_REMOTE_SESSIONS=1``.

It starts its OWN throwaway Remote Control session — a ``claude`` stream-json process with
a scratch config dir, signed in with the operator's ``~/.claude`` claude.ai login (held in
memory, never printed or written) — and then drives it only through
``RemoteSessionsService``: the session is listed, its transcript streams, a prompt sent
from ViberPit runs, and a tool approval answered from ViberPit resolves in the CLI. It
never sends anything to a session it did not start: every call names the throwaway id.
"""
from __future__ import annotations

import asyncio
import json
import os
import pwd
import shutil
import tempfile

import pytest

from server.remote_sessions import service as rs
from server.remote_sessions.client import Credentials, SessionsApiClient, valid_session_id
from server.remote_sessions.view import open_in_process

pytestmark = pytest.mark.skipif(os.environ.get("VIBERPIT_TEST_REMOTE_SESSIONS") != "1",
                                reason="live claude.ai test: set VIBERPIT_TEST_REMOTE_SESSIONS=1")

MARKER = "rs_live_marker.txt"


def _real_home() -> str:
    # The suite's conftest points HOME at a scratch dir; the login lives in the real one.
    return pwd.getpwuid(os.getuid()).pw_dir


def _login() -> Credentials:
    with open(os.path.join(_real_home(), ".claude", ".credentials.json")) as f:
        oauth = json.load(f)["claudeAiOauth"]
    org = oauth.get("organizationUuid")
    if not org:
        with open(os.path.join(_real_home(), ".claude.json")) as f:
            org = json.load(f).get("oauthAccount", {}).get("organizationUuid")
    return Credentials(token=oauth["accessToken"], org_uuid=org)


async def _start_throwaway(creds: Credentials, cwd: str):
    claude = shutil.which("claude")
    assert claude, "the claude CLI is not on PATH"
    cfg = tempfile.mkdtemp(prefix="rs-live-cfg-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_")}
    env.update(CLAUDE_CONFIG_DIR=cfg, CLAUDE_CODE_OAUTH_TOKEN=creds.token,
               CLAUDE_CODE_ORGANIZATION_UUID=creds.org_uuid)
    proc = await asyncio.create_subprocess_exec(
        claude, "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--permission-prompt-tool", "stdio", "--replay-user-messages",
        cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=1 << 24)
    assert proc.stdin is not None and proc.stdout is not None
    for req in ({"subtype": "initialize", "hooks": None},
                {"subtype": "remote_control", "enabled": True}):
        rid = req["subtype"]
        proc.stdin.write((json.dumps({"type": "control_request", "request_id": rid,
                                      "request": req}) + "\n").encode())
    await proc.stdin.drain()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 90
    while loop.time() < deadline:
        line = await asyncio.wait_for(proc.stdout.readline(), 90)
        msg = json.loads(line)
        resp = msg.get("response") or {}
        if msg.get("type") == "control_response" and resp.get("request_id") == "remote_control":
            url = (resp.get("response") or {}).get("session_url") or ""
            sid = url.rsplit("/", 1)[-1]
            assert valid_session_id(sid), "no Remote Control session URL"
            return proc, sid, cfg
    raise AssertionError("the CLI never opened its Remote Control bridge")


async def _drain(proc) -> None:
    """Keep reading the CLI's stdout so it never blocks on a full pipe. Its stdio
    permission asks are deliberately never answered: only the remote answer may."""
    while True:
        if not await proc.stdout.readline():
            return


@pytest.mark.asyncio
async def test_live_throwaway_session_is_listed_streamed_prompted_and_answered() -> None:
    creds = _login()
    cwd = tempfile.mkdtemp(prefix="rs-live-cwd-")
    proc, sid, cfg = await _start_throwaway(creds, cwd)
    drain = asyncio.create_task(_drain(proc))
    svc = rs.RemoteSessionsService(client=SessionsApiClient(), accounts=lambda: ["live"],
                                   credentials=lambda a: creds, enabled=lambda: True,
                                   watch_poll_s=1.0)
    try:
        # 1. Listed.
        listed: list[dict] = []
        for _ in range(20):
            svc._forced_at = float("-inf")
            listed = [s for s in (await svc.list_sessions())["sessions"] if s["id"] == sid]
            if listed:
                break
            await asyncio.sleep(2)
        assert listed and listed[0]["connection"] == "connected"
        assert listed[0]["origin"] in ("sdk", "repl", "auto")

        # 2. Its transcript streams; 3. a prompt sent from ViberPit runs.
        view = await open_in_process(svc, sid)
        got: list[dict] = list(view.snapshot)

        async def consume() -> None:
            async for e in view.events():
                got.append(e.to_wire())

        consumer = asyncio.create_task(consume())
        await svc.send_prompt(sid, "Reply with exactly the word: pong")
        await _wait(lambda: any(w.get("type") == "textDelta" and "pong" in w.get("text", "")
                                for w in got), 120)
        await _wait(lambda: any(w.get("type") == "turnEnd" for w in got), 60)

        # 4. A tool approval answered in ViberPit resolves in the CLI.
        await svc.send_prompt(sid, "Use the Bash tool to run exactly: echo ok > " + MARKER +
                              "   Then reply with the word done.")
        await _wait(lambda: any(w.get("type") == "permissionRequest" for w in got), 120)
        card = next(w for w in got if w.get("type") == "permissionRequest")
        out = await svc.answer(sid, card["permId"], decision="allow")
        assert out["sent"] is True
        await _wait(lambda: os.path.exists(os.path.join(cwd, MARKER)), 60)
        await _wait(lambda: any(w.get("type") == "permissionResolved"
                                and w.get("permId") == card["permId"] for w in got), 60)
        await _wait(lambda: sum(w.get("type") == "turnEnd" for w in got) >= 2, 60)

        # 5. A question answered in ViberPit reaches the model (the deny-with-answer
        # message a ViberPit session's AskUserQuestion answer uses).
        await svc.send_prompt(sid, "Use the AskUserQuestion tool to ask me which color I "
                              "prefer, with exactly two options: red and blue. After I "
                              "answer, reply with only the color I chose, in capitals.")
        await _wait(lambda: any(w.get("type") == "question" for w in got), 120)
        q = next(w for w in got if w.get("type") == "question")
        picked = next(o["label"] for o in q["options"] if "blue" in o["label"].lower())
        qout = await svc.answer(sid, q["questionId"], selected=[picked])
        assert qout["sent"] is True
        await _wait(lambda: any(w.get("type") == "textDelta" and "BLUE" in w.get("text", "")
                                for w in got), 120)
        assert any(w.get("type") == "questionResolved" and w.get("selected") == [picked]
                   for w in got)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        # Evidence for a `-s` run: ids and event types only, never content or the token.
        from collections import Counter
        print(f"\nlive remote session {sid}: listed={listed[0]['connection']}/"
              f"{listed[0]['origin']} events={dict(Counter(w.get('type') for w in got))} "
              f"answer={out} question={qout} "
              f"marker={os.path.exists(os.path.join(cwd, MARKER))}")
    finally:
        await svc.aclose()
        drain.cancel()
        proc.terminate()
        await proc.wait()
        shutil.rmtree(cfg, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)


async def _wait(pred, timeout: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.5)
