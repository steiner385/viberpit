# server/tests/test_ws_prompt_after_release.py
"""A second prompt on an already-open socket must never vanish (final review, C2).

`_ensure_live` short-circuits with `return True` when the session is in this socket's
`live_tasks`. That entry was only ever removed by `_teardown_live_task` (detach /
rebind / close) — never when the forwarding task ended on its own. But it DOES end on
its own: releasing a driver calls `aclose()`, which pushes `_STREAM_DONE`, so
`stream.events()` returns and `_forward_live` finishes while the socket is still open
and attached.

So after a completion released the driver, the next prompt took the short-circuit
(believing the session was still live), reached `orchestrator.send_prompt`, found no
driver, and got back the `"no_driver"` sentinel — which the prompt handler did not
check. The user's message disappeared with no error frame and no delivery.

Two regressions are covered here:
  1. the live_tasks entry is cleared when the forwarding task ends, so `_ensure_live`
     resumes the session properly on the next prompt;
  2. a `"no_driver"` result is surfaced to the client as a `promptRejected` frame (the
     same rejection convention already used for `"draining"`), never dropped.
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

REPO_ROOT = Path(__file__).parent.parent.parent
_IDENTITY_HEADERS = {"Tailscale-User-Login": "wstest@example.com"}


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone

    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str, tmp_path) -> None:
    import time as _time

    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    now_ms = int(_time.time() * 1000)
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok",
        "refreshToken": f"{name}-ref",
        "expiresAt": now_ms + 9_000_000,
        "subscriptionType": "max",
    })
    shared_mcp = paths.shared_mcp_path()
    if not shared_mcp.exists():
        write_json_secure(shared_mcp, {})


def _drain_until(ws, until_type: str, timeout: float = 8.0) -> list[dict]:
    """Drain frames until an event of *until_type* is seen (batches flattened)."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = ws.receive_text()
        except Exception:
            break
        msg = json.loads(raw)
        if msg.get("type") == "batch":
            events.extend(msg.get("events", []))
        else:
            events.append(msg)
        if any(e.get("type") == until_type for e in events):
            return events
    return events


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class _Harness:
    """The env/account/factory scaffolding shared by both tests below."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def __enter__(self):
        tmp_path = self.tmp_path
        store_home = tmp_path / "store"
        store_home.mkdir()
        shared_base = tmp_path / "dot-claude"
        shared_base.mkdir()
        (shared_base / "settings.json").write_text("{}")
        (shared_base / "projects").mkdir()
        cfg_dir = tmp_path / "cfg" / "alpha"
        cfg_dir.mkdir(parents=True)

        from ccswitch.selector import Picked

        self._patches = [
            patch.dict(os.environ, {
                "CCSWITCH_HOME": str(store_home),
                "CLAUDE_CONFIG_DIR": str(shared_base),
                "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
            }),
            patch("ccswitch.accounts.usage_for", side_effect=_fake_usage),
            patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
            patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
            patch("server.sessions.orchestrator.configdir.trust_project"),
        ]
        for p in self._patches:
            p.start()
        _seed_account("alpha", tmp_path)

        def factory(cwd, config_dir, can_use_tool, mode, resume,
                    extra_env=None, *, on_pre_compact=None):
            return FakeSDKClient([f.sys_init("sess-1")], on_can_use_tool=can_use_tool)

        self.orch = SessionOrchestrator(
            sdk_factory=factory,
            work_dir=str(REPO_ROOT),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
        )
        self.app = create_app(orchestrator=self.orch)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _release_finished(orch) -> bool:
    """True once every completion-driver-release task has run to completion — the
    point at which aclose() has pushed _STREAM_DONE and the socket's forwarding task
    is about to end. Observable from the test thread, unlike the (closure-local)
    live_tasks map."""
    return all(t.done() for t in list(orch._bg_tasks)
               if t.get_name() == "completion-driver-release")


def test_prompt_after_completion_release_still_reaches_the_session(tmp_path):
    """The end-to-end shape of the bug: attach → the session completes and its driver
    is released → prompt again on the SAME socket. The prompt must resume the session
    and be delivered, not silently disappear."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "prompt-after-release")
            sid = session.id
            # Sticky opt-out: keep auto-archive out of this test entirely, so the only
            # thing that releases the driver is the completion transition itself.
            orch.record(sid).auto_archive_opt_out_reason = "manual-unarchive"

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                events = _drain_until(ws, until_type="replayComplete")
                assert any(e.get("type") == "replayComplete" for e in events)
                assert sid in orch._drivers

                # Completion releases the driver (production path), which ends this
                # socket's forwarding task via _STREAM_DONE.
                client.portal.call(orch._persist_status, sid, "complete")
                assert sid not in orch._drivers          # popped synchronously
                assert _wait_until(lambda: _release_finished(orch)), "release never finished"
                time.sleep(0.3)   # let the forwarding task observe _STREAM_DONE and end

                # THE PROMPT. Before the fix _ensure_live short-circuited on the stale
                # live_tasks entry, send_prompt returned "no_driver", and this frame
                # produced nothing at all — no driver, no error, no delivery.
                ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                         "text": "keep going"}))
                assert _wait_until(lambda: sid in orch._drivers), (
                    "a prompt on an open socket must resume the released session — "
                    "it was silently dropped")
                # Delivery is a separate step from registration: _ensure_live
                # registers the rebuilt driver BEFORE the bridge calls send_prompt,
                # so an immediate read here races the send on a loaded runner
                # (CI, 2026-08-31). Wait for it like the registration wait above.
                assert _wait_until(lambda: orch._last_prompt.get(sid) == "keep going"), (
                    "the prompt must actually be delivered to the driver")
                # C4's invariant, observed end to end: the resumed session left
                # `complete`, so it counts against the cap again.
                assert orch.record(sid).status != "complete"
                assert orch._active_session_count() == 1

            client.portal.call(orch.close_session, sid)


def test_undeliverable_prompt_is_rejected_to_the_client(tmp_path):
    """A `"no_driver"` send result must reach the browser as a promptRejected frame —
    the same rejection convention `"draining"` already uses — so the text is re-queued
    instead of vanishing. Forced here (the race it covers is rare by design)."""
    with _Harness(tmp_path) as h:
        orch = h.orch
        with TestClient(h.app, raise_server_exceptions=False) as client:
            session = client.portal.call(orch.create_session, "reject-no-driver")
            sid = session.id

            with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
                ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                assert any(e.get("type") == "replayComplete"
                           for e in _drain_until(ws, until_type="replayComplete"))

                async def _no_driver(session_id, text, attachment_ids=None):
                    return "no_driver"

                with patch.object(orch, "send_prompt", _no_driver):
                    ws.send_text(json.dumps({"type": "prompt", "sessionId": sid,
                                             "text": "lost message"}))
                    # Frames are handled in order, so a re-attach behind the prompt
                    # gives the drain a guaranteed terminator: without it, a regression
                    # that emits nothing would HANG the suite on receive_text() instead
                    # of failing it.
                    ws.send_text(json.dumps({"type": "detach", "sessionId": sid}))
                    ws.send_text(json.dumps({"type": "attach", "sessionId": sid}))
                    events = _drain_until(ws, until_type="replayComplete")
                rejected = [e for e in events if e.get("type") == "promptRejected"]
                assert rejected, (
                    f"an undeliverable prompt must be rejected, not dropped; got "
                    f"{[e.get('type') for e in events]}")
                assert rejected[0]["reason"] == "no_driver"
                assert rejected[0]["text"] == "lost message"

            client.portal.call(orch.close_session, sid)
