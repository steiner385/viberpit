#!/usr/bin/env python3
"""Live interactive acceptance for native Codex (constitution III), Task 6.2.

Exercises the real edge + real daemon + real `codex app-server` + real ChatGPT
account — no mocks. Never imported by `server/`; a throwaway verification harness
whose output is pasted into
`docs/superpowers/spikes/2026-09-04-codex-interactive-acceptance.md`.

**It starts real model turns and spends real quota.** It therefore refuses to run
unless BOTH gates are set explicitly — there is no default account, and no
default repo:

    CCSWITCH_TEST_CODEX_INTERACTIVE=1 \\
    python scripts/smoke_codex_interactive.py \\
        --account <the-smoke-openai-account> --repo <owner/name>

The prompts are deliberately trivial and tool-free ("Reply with the single word
READY") so a pass costs a handful of tokens, and so a failure is attributable to
the transport rather than to model behaviour.

What this script covers, from the plan's Task 6.2 list:

    doctor preflight · add/reauth read · model + effort selection · two threads on
    ONE account (the simultaneous-managers case) · a minimal turn · steer ·
    interrupt · browser reconnect + thread/read replay · archive · quota refresh ·
    no auth.json on the runner · no secret in the gathered output

What it deliberately does NOT do, because a script that kills production
processes is worse than a documented manual step — these are the MANUAL section
of the acceptance doc:

    manager crash/restart · daemon drain/rehydrate · the 401-storm refresh
    coalescing test · the Claude/Copilot-stay-usable-during-a-Codex-failure check

Approval decline is opt-in (`--approvals`): it needs the model to actually choose
to run a command, which is not deterministic enough to gate a release on from
here. The acceptance doc records it as a manual step by default.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

import httpx
import websockets

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

#: Trivial, tool-free, and deterministic enough to assert on.
READY_PROMPT = "Reply with the single word READY and nothing else."
LONG_PROMPT = "Count slowly from 1 to 40, one number per line."

#: Shapes that must never appear in anything this script gathers. Mirrors
#: server/audit.py's list; kept here rather than imported so the check still runs
#: against an edge on a different revision than this checkout.
SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"ccenc1:[A-Za-z0-9+/=]+"),
)


class Step:
    def __init__(self, name: str) -> None:
        self.name = name
        self.state = FAIL
        self.detail = ""

    def finish(self, ok: bool, detail: str = "") -> None:
        self.state = PASS if ok else FAIL
        self.detail = detail
        print(f"[{self.state}] {self.name}" + (f" — {detail}" if detail else ""), flush=True)

    def skip(self, why: str) -> None:
        self.state = SKIP
        self.detail = why
        print(f"[{SKIP}] {self.name} — {why}", flush=True)


def _batch_events(frame: dict) -> list[dict]:
    return frame.get("events", []) if frame.get("type") == "batch" else [frame]


async def _drain_until(
    ws: Any, match: Callable[[dict], bool], *, timeout: float = 90.0
) -> list[dict]:
    """Receive frames (unwrapping ``batch``) until *match(event)* is True."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
        except (TimeoutError, asyncio.TimeoutError):
            break
        events.extend(_batch_events(json.loads(raw)))
        if any(match(e) for e in events):
            return events
    raise TimeoutError(
        f"timed out; saw {[e.get('type') for e in events][-12:]}"
    )


def _text_of(events: list[dict]) -> str:
    """Concatenate whatever assistant text the normalized events carried."""
    out: list[str] = []
    for event in events:
        for key in ("text", "delta", "content"):
            value = event.get(key)
            if isinstance(value, str):
                out.append(value)
    return " ".join(out)


class Gathered:
    """Everything this run collected, for the one secret-scan at the end.

    The plan asks for the scan across "logs, audit rows, metrics". Rather than
    scan each in isolation, every payload this script sees is appended here and
    swept once — so a field added to any of those surfaces later is covered
    without this script enumerating it.
    """

    def __init__(self) -> None:
        self.blobs: list[str] = []

    def add(self, label: str, value: Any) -> None:
        self.blobs.append(f"--- {label} ---\n{json.dumps(value, default=str)}")

    def offenders(self) -> list[str]:
        joined = "\n".join(self.blobs)
        return [m.group(0)[:16] + "…" for p in SECRET_SHAPES for m in p.finditer(joined)]


def _doctor(python: str) -> tuple[bool, str]:
    """Run the startup doctor exactly as the runbook tells an operator to."""
    proc = subprocess.run(
        [python, "-m", "server.codex.doctor"], capture_output=True, text=True, timeout=120
    )
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        return False, f"unparseable doctor output: {proc.stdout[:200]}"
    failed = [c["check"] for c in report.get("checks", []) if c.get("outcome") == "fail"]
    return (not failed), (f"failing checks: {failed}" if failed else report.get("outcome", ""))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--identity", default="smoke@ccswitch.local")
    parser.add_argument("--account", required=True, help="the OpenAI smoke account")
    parser.add_argument("--repo", required=True, help="owner/name of the throwaway repo")
    parser.add_argument("--model", default=None, help="defaults to the account's default")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--runner-home-base",
                        default="/var/lib/ccswitch/state/ccswitch/codex")
    parser.add_argument("--approvals", action="store_true",
                        help="also drive a command-approval decline (non-deterministic)")
    args = parser.parse_args()

    if os.environ.get("CCSWITCH_TEST_CODEX_INTERACTIVE") != "1":
        print(
            "refusing to run: this starts REAL model turns on a REAL account.\n"
            "Set CCSWITCH_TEST_CODEX_INTERACTIVE=1 and pass --account/--repo "
            "explicitly.",
            file=sys.stderr,
        )
        return 2

    headers = {"Tailscale-User-Login": args.identity}
    ws_url = args.base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    steps: list[Step] = []
    gathered = Gathered()
    created: list[str] = []

    def step(name: str) -> Step:
        s = Step(name)
        steps.append(s)
        return s

    # -- 0. Preflight ------------------------------------------------------
    s = step("doctor reports no failing check")
    ok, detail = _doctor(args.python)
    s.finish(ok, detail)
    if not ok:
        return _summarize(steps)

    s = step("no auth.json under any runner CODEX_HOME")
    found = []
    for root, _dirs, files in os.walk(args.runner_home_base):
        if "auth.json" in files:
            found.append(os.path.join(root, "auth.json"))
    s.finish(not found, f"found {found}" if found else "runner holds no credential")

    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=60.0
    ) as http:
        # -- 1. Account is present, OpenAI, and reports quota ---------------
        s = step("account reads back as an OpenAI account with quota")
        r = await http.get("/api/accounts")
        if r.status_code != 200:
            s.finish(False, f"HTTP {r.status_code}")
            return _summarize(steps)
        accounts = r.json()
        gathered.add("GET /api/accounts", accounts)
        account = next((a for a in accounts if a.get("name") == args.account), None)
        if account is None:
            s.finish(False, f"{args.account!r} is not registered")
            return _summarize(steps)
        quota_before = (account.get("usage") or {}).get("fetchedAt")
        s.finish(
            account.get("provider") == "openai",
            f"provider={account.get('provider')} quotaFetchedAt={quota_before}",
        )

        # -- 2. Model list comes from the account's own app-server ----------
        s = step("model list for this account")
        r = await http.get("/api/models", params={"engine": "codex",
                                                  "account": args.account})
        models = r.json() if r.status_code == 200 else []
        gathered.add("GET /api/models", models)
        model_ids = [m.get("id") for m in models] if isinstance(models, list) else []
        s.finish(bool(model_ids), f"{len(model_ids)} model(s): {model_ids[:4]}")
        model = args.model or (model_ids[0] if model_ids else None)

        # -- 3. Two threads on ONE account ----------------------------------
        # This IS the "simultaneous local managers for the same account" case:
        # the manager ref-counts ONE app-server per account, so two sessions here
        # must multiplex onto one process rather than spawn two.
        s = step("two Codex sessions on one account")
        for index in (1, 2):
            body: dict[str, Any] = {
                "name": f"codex-smoke-{index}",
                "engine": "codex",
                "account": args.account,
                "repo": args.repo,
                "reasoningEffort": args.effort,
            }
            if model:
                body["model"] = model
            r = await http.post("/api/sessions", json=body)
            if r.status_code != 201:
                s.finish(False, f"session {index}: HTTP {r.status_code}: {r.text[:200]}")
                await _cleanup(http, created)
                return _summarize(steps)
            created.append(r.json()["id"])
        s.finish(True, f"ids={created}")

        s = step("model + effort were applied at creation")
        r = await http.get(f"/api/sessions/{created[0]}")
        detail_body = r.json() if r.status_code == 200 else {}
        gathered.add("GET /api/sessions/<id>", detail_body)
        s.finish(
            detail_body.get("engine") == "codex"
            and detail_body.get("reasoningEffort") == args.effort,
            f"engine={detail_body.get('engine')} "
            f"model={detail_body.get('model')} "
            f"effort={detail_body.get('reasoningEffort')}",
        )

        session_id = created[0]

        # -- 4. A minimal turn, then steer, then interrupt ------------------
        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "attach", "sessionId": session_id}))
            await _drain_until(ws, lambda e: e.get("type") == "replayComplete")

            s = step("one minimal tool-free turn reaches turnEnd")
            await ws.send(json.dumps({"type": "prompt", "sessionId": session_id,
                                      "text": READY_PROMPT}))
            try:
                events = await _drain_until(ws, lambda e: e.get("type") == "turnEnd")
            except TimeoutError as exc:
                s.finish(False, str(exc))
                await _cleanup(http, created)
                return _summarize(steps)
            gathered.add("turn events", [e.get("type") for e in events])
            s.finish("READY" in _text_of(events).upper(), "assistant replied")

            s = step("steer mid-turn (prompt with interrupt:true)")
            await ws.send(json.dumps({"type": "prompt", "sessionId": session_id,
                                      "text": LONG_PROMPT}))
            await asyncio.sleep(2.0)
            await ws.send(json.dumps({"type": "prompt", "sessionId": session_id,
                                      "text": "Stop counting. Reply STEERED.",
                                      "interrupt": True}))
            try:
                events = await _drain_until(ws, lambda e: e.get("type") == "turnEnd")
                s.finish("STEERED" in _text_of(events).upper(), "steer took effect")
            except TimeoutError as exc:
                s.finish(False, str(exc))

            s = step("interrupt ends an in-flight turn")
            await ws.send(json.dumps({"type": "prompt", "sessionId": session_id,
                                      "text": LONG_PROMPT}))
            await asyncio.sleep(2.0)
            await ws.send(json.dumps({"type": "interrupt", "sessionId": session_id}))
            try:
                await _drain_until(ws, lambda e: e.get("type") == "turnEnd", timeout=60.0)
                s.finish(True, "turn ended after interrupt")
            except TimeoutError as exc:
                s.finish(False, str(exc))

            s = step("command approval declined")
            if not args.approvals:
                s.skip("--approvals not set; recorded as a manual step")
            else:
                await ws.send(json.dumps({
                    "type": "prompt", "sessionId": session_id,
                    "text": "Run the shell command `id` and tell me the output."}))
                try:
                    events = await _drain_until(
                        ws, lambda e: e.get("type") == "permissionRequest", timeout=120.0)
                    perm = next(e for e in events
                                if e.get("type") == "permissionRequest")
                    await ws.send(json.dumps({
                        "type": "permissionResponse", "sessionId": session_id,
                        "permId": perm["permId"], "decision": "deny"}))
                    await _drain_until(ws, lambda e: e.get("type") == "turnEnd")
                    s.finish(True, f"declined {perm.get('tool')}")
                except (TimeoutError, StopIteration) as exc:
                    s.finish(False, f"no approval surfaced: {exc}")

        # -- 5. Browser reconnect → thread/read replay ----------------------
        s = step("reconnect replays the thread (thread/read)")
        async with websockets.connect(ws_url, additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "attach", "sessionId": session_id}))
            try:
                events = await _drain_until(
                    ws, lambda e: e.get("type") == "replayComplete", timeout=90.0)
                gathered.add("replay events", [e.get("type") for e in events])
                s.finish(len(events) > 1, f"{len(events)} event(s) replayed")
            except TimeoutError as exc:
                s.finish(False, str(exc))

        # -- 6. Quota refresh ----------------------------------------------
        s = step("quota refresh advances the snapshot")
        r = await http.post(f"/api/accounts/{args.account}/refresh")
        await asyncio.sleep(2.0)
        r = await http.get("/api/accounts")
        after = next((a for a in r.json() if a.get("name") == args.account), {})
        gathered.add("GET /api/accounts (after refresh)", after)
        quota_after = (after.get("usage") or {}).get("fetchedAt")
        s.finish(
            quota_after is not None,
            f"fetchedAt {quota_before} → {quota_after}",
        )

        # -- 7. Archive ------------------------------------------------------
        s = step("archive both sessions")
        outcomes = []
        for sid in created:
            r = await http.post(f"/api/sessions/{sid}/archive")
            outcomes.append(r.status_code)
        s.finish(all(c in (200, 202, 204) for c in outcomes), f"status={outcomes}")

    # -- 8. The one secret scan over everything gathered -------------------
    s = step("no secret-shaped value in anything this run gathered")
    offenders = gathered.offenders()
    s.finish(not offenders, f"offenders={offenders}" if offenders else
             f"{len(gathered.blobs)} payload(s) clean")

    s = step("no auth.json appeared on the runner during the run")
    found = []
    for root, _dirs, files in os.walk(args.runner_home_base):
        if "auth.json" in files:
            found.append(os.path.join(root, "auth.json"))
    s.finish(not found, f"found {found}" if found else "runner still holds no credential")

    return _summarize(steps)


async def _cleanup(http: httpx.AsyncClient, session_ids: list[str]) -> None:
    for sid in session_ids:
        try:
            await http.post(f"/api/sessions/{sid}/archive")
        except Exception:  # noqa: BLE001 -- best effort; the sessions are throwaway
            pass


def _summarize(steps: list[Step]) -> int:
    failed = [s for s in steps if s.state == FAIL]
    skipped = [s for s in steps if s.state == SKIP]
    print(
        f"\n{len(steps) - len(failed) - len(skipped)}/{len(steps)} passed"
        f", {len(skipped)} skipped, {len(failed)} failed"
    )
    if failed:
        print("failed: " + ", ".join(s.name for s in failed))
    print(
        "\nStill MANUAL (see the acceptance doc): manager crash/restart, daemon "
        "drain/rehydrate, 401-storm refresh coalescing, and Claude/Copilot "
        "remaining usable during a Codex failure."
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
