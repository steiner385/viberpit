# M2b — Top-tier push via external webhook sink (bite-sized)

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Channel decided: **external sink (ntfy / generic webhook)**. Backend only.

**Goal:** Fire a push notification to a configured external webhook when a session transitions *into* a top-tier "needs you" state, debounced once-per-condition, so the operator is alerted away from the desk. No new dependency; never crashes the session loop; URL is file-only config.

**Architecture:** A small fire-and-forget sink (`server/sessions/push.py`) POSTs JSON to `config.PUSH_WEBHOOK_URL` (env var — file-only by nature). The orchestrator detects the transition in the existing `_persist_status` seam (it already has the previous status in `rec.status` before overwriting) and calls the sink, gated by a per-session debounce set so it fires once until the condition clears.

**Tech stack:** Python, `urllib.request` via `asyncio.to_thread` (no new dep), pytest.

## Global Constraints
- **No new runtime dependency** — use stdlib `urllib`.
- **File-only config (NFR-4):** the webhook URL comes from `CCSWITCH_PUSH_WEBHOOK_URL` (env), added to `server/config.py`. Never web-writable; never logged in full (host-mask if logged).
- **Never crash the session loop:** the sink swallows all exceptions (log once), mirroring Plotroom's fire-and-forget notifier (5s timeout, no retry).
- **Autonomy (AUT-02):** push fires only on transition *into* the top tier; once per condition while it holds; re-fires after it clears. This is read-side (observing) — it does not propose or act on the session.
- **Single source:** the "top-tier status" set derives from the same vocabulary as `server/sessions/status.py` (`waiting_input`/`waiting_question`/`ready_review` = needs-you; `needs_attention` = crashed/failed). Do not invent a parallel status list.

Backend tests: `PYTHONPATH=$PWD/src /mnt/ssk-ssd/tony/GitHub/.ccswitch/clones/steiner385/cc-account-switcher/.venv/bin/python -m pytest ...`

---

## Task 1 — the fire-and-forget sink + config

**Files:** create `server/sessions/push.py`; modify `server/config.py`; test `server/tests/test_push.py`.

**Interfaces:**
- `config.PUSH_WEBHOOK_URL: str | None` (from `CCSWITCH_PUSH_WEBHOOK_URL`, stripped; empty → None).
- `async def push(title: str, body: str, *, url: str | None = None, opener=urllib.request.urlopen) -> bool` — POSTs `{"title","body"}` JSON to the URL (param overrides config); returns True if sent, False if no URL or on failure (never raises). `opener` is injectable for tests.

- [ ] **Step 1 — failing test** `server/tests/test_push.py`:
```python
import json
import pytest
from server.sessions import push as push_mod

@pytest.mark.asyncio
async def test_push_noop_without_url():
    assert await push_mod.push("t", "b", url=None) is False

@pytest.mark.asyncio
async def test_push_posts_json(monkeypatch):
    seen = {}
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b""
    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        return _Resp()
    ok = await push_mod.push("Needs you", "session X", url="https://ntfy.sh/mytopic", opener=opener)
    assert ok is True
    assert seen["url"] == "https://ntfy.sh/mytopic"
    assert seen["body"] == {"title": "Needs you", "body": "session X"}

@pytest.mark.asyncio
async def test_push_never_raises_on_error(monkeypatch):
    def opener(req, timeout=0): raise OSError("down")
    assert await push_mod.push("t", "b", url="https://x", opener=opener) is False
```

- [ ] **Step 2 — run RED.**

- [ ] **Step 3 — implement `server/sessions/push.py`:**
```python
"""Fire-and-forget push to an external webhook (ntfy / generic). Never raises."""
from __future__ import annotations
import asyncio, json, logging, urllib.request
from server import config

log = logging.getLogger(__name__)

def _post(url: str, payload: dict, opener) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with opener(req, timeout=5):
        pass

async def push(title: str, body: str, *, url: str | None = None, opener=urllib.request.urlopen) -> bool:
    target = url if url is not None else config.PUSH_WEBHOOK_URL
    if not target:
        return False
    try:
        await asyncio.to_thread(_post, target, {"title": title, "body": body}, opener)
        return True
    except Exception:
        log.warning("push.failed", exc_info=True)
        return False
```
Add to `server/config.py` near the other `CCSWITCH_*` reads:
```python
PUSH_WEBHOOK_URL = (os.environ.get("CCSWITCH_PUSH_WEBHOOK_URL") or "").strip() or None
```

- [ ] **Step 4 — run GREEN; ruff + mypy on the two files.**
- [ ] **Step 5 — commit** (`feat(push): fire-and-forget external webhook sink (file-only URL)`).

## Task 2 — fire on transition into the top tier (debounced)

**Files:** modify `server/sessions/orchestrator.py` (`_persist_status`, ~line 509; add a `_pushed` set in `__init__`); test `server/tests/test_orchestrator_push.py`.

**Interfaces:**
- Module-level pure helper in orchestrator (or status.py) `PUSH_STATUSES: frozenset = {"waiting_input","waiting_question","ready_review","needs_attention"}` and `def is_push_worthy(status: str) -> bool`.
- `SessionOrchestrator._pushed: set[str]` — session_ids currently in a pushed top-tier state.

- [ ] **Step 1 — failing test** `server/tests/test_orchestrator_push.py` (follow the `_make_orch`/monkeypatch pattern used in `test_session_branch_persist.py`): capture `push` calls by monkeypatching `orch_mod`'s `push` reference. Cases:
  - transition idle→waiting_input fires push ONCE; a second `_persist_status("s1","waiting_input")` does NOT fire again (AUT-02 debounce).
  - leaving (waiting_input→in_progress) clears; a later waiting_input fires again (re-fire after clear).
  - in_progress→done does NOT fire (not push-worthy).
  - needs_attention fires (crashed/failed).

- [ ] **Step 2 — run RED.**

- [ ] **Step 3 — implement.** In `__init__`: `self._pushed: set[str] = set()`. In `_persist_status`, capture `prev = rec.status if rec else None` BEFORE overwriting, and after the existing `self._publish_event({...})` line add:
```python
        await self._maybe_push(session_id, status)
```
and a method:
```python
    async def _maybe_push(self, session_id: str, status: str) -> None:
        from server.sessions.push import push
        if is_push_worthy(status):
            if session_id not in self._pushed:
                self._pushed.add(session_id)
                rec = self._records.get(session_id)
                name = rec.name if rec else session_id
                await push("ccswitch — needs you", f"{name}: {status}")
        else:
            self._pushed.discard(session_id)
```
(Define `is_push_worthy`/`PUSH_STATUSES` at module top or import from status.py. Use `from server.sessions.push import push` at call site so tests can monkeypatch `orch_mod.push`… — actually import the module: `from server.sessions import push as push_mod` at top and call `push_mod.push(...)` so the test can monkeypatch `push_mod.push`. Pick whichever the test monkeypatches; keep them consistent.)

- [ ] **Step 4 — run GREEN; ruff + mypy; run `test_session_branch_persist.py` + any `_persist_status` regression tests.**
- [ ] **Step 5 — commit** (`feat(push): alert on transition into a top-tier needs-you status (AUT-02)`).

## Out of scope (follow-on M2b.2)
- **PR-broke push.** Firing when a session's PR flips to `checks=='failing'` requires the PR poller to compare prior vs new cached state (it currently rebuilds the cache wholesale). That's a separate slice hooking the poller refresh, reusing the same `push` sink + a `_pushed_pr` debounce set. Not in this plan.
- ntfy-specific formatting (priority/tags headers) — the generic JSON body works for ntfy and a relay; add headers only if needed.

## Self-Review
- AUT-02 (push only on top-tier entry; once-per-condition; re-fire after clear) → Task 2 tests. ✓
- NFR-4 (file-only URL, never crashes loop) → Task 1 (env config, swallow-all sink). ✓
- Single source (push-status set from the status.py vocabulary; no parallel list) → Task 2 PUSH_STATUSES. ✓
- No new dep (urllib) ✓. Types consistent: `push(...)->bool`, `is_push_worthy(str)->bool`, `_pushed: set[str]`. ✓
