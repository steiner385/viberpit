# Session-list Live Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a one-line, LLM-generated "what's happening now" summary on each session card, regenerated at most once per 60s and only while the sessions list is actually on-screen.

**Architecture:** Mirror the existing `latest_suggestions` machinery — a one-shot Haiku call under the session's own account, an mtime-keyed side cache, an in-flight guard, and a non-blocking list GET. Two new gates sit on top: a 60s throttle and a "viewer present" flag threaded from the client as `GET /api/sessions?viewing=1`. Summaries are ephemeral (in-memory cache only; no DB, no RPC `to_dict` change).

**Tech Stack:** Python (FastAPI, asyncio, claude-agent-sdk) backend; React + TanStack Query + TypeScript + Tailwind frontend; pytest + vitest.

## Global Constraints

- Never block the `/api/sessions` GET on summary generation — always fire-and-forget, return cached/empty immediately (matches `latest_suggestions`).
- Summary generation runs under the session's own account (`CLAUDE_CONFIG_DIR`); spends that account's Max quota. The only mitigations are the three gates (viewer + mtime-changed + 60s).
- Env kill-switch `CCSWITCH_SUMMARIES=0` disables generation entirely (mirror `CCSWITCH_SUGGESTIONS`).
- Backend lint gate: `ruff` + `mypy` must pass. Run from the worktree with `PYTHONPATH=<worktree>/src` against the main checkout's prebuilt venv.
- Frontend gates: `vitest run`, `tsc -b --force` (NOT `tsc --noEmit` — it checks nothing here), `eslint` (max-warnings 0).
- Any new request-layer `orch.<method>` call MUST be a callable on `RemoteOrchestrator` or `server/tests/test_remote_orchestrator_breadth.py::test_remote_orchestrator_covers_every_request_layer_call` fails.
- Run server tests with the worktree env, e.g. `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=<worktree>/src <main-checkout>/.venv/bin/python -m pytest server/tests/<file>`.

---

### Task 1: `SummaryService`, prompts, clamp, `should_regenerate` predicate, real factory

**Files:**
- Create: `server/sessions/summaries.py`
- Modify: `server/sessions/sdk_factory.py` (add `real_summary_factory` after `real_suggestion_factory`, ~line 66)
- Test: `server/tests/test_summaries.py`

**Interfaces:**
- Consumes: `server.sessions.translator.Translator`, `server.sessions.events` (as `ev`), `server.sessions.suggestions.last_turn_context` (reused).
- Produces:
  - `SUMMARY_SYSTEM_PROMPT: str`
  - `build_summary_prompt(context: str) -> str`
  - `clamp_summary(raw: str, limit: int = 100) -> str`
  - `should_regenerate(viewing: bool, cached: tuple[float, float, str] | None, signature: float, now: float, status: str) -> bool`
  - `class SummaryService` with `async def generate(self, cwd: str, config_dir: str, context: str) -> str`
  - `real_summary_factory(cwd, config_dir)` in `sdk_factory.py`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_summaries.py
import asyncio
import types
from unittest.mock import AsyncMock

from server.sessions.summaries import (
    clamp_summary,
    should_regenerate,
    SummaryService,
)


def test_clamp_summary_collapses_whitespace_and_strips_wrappers():
    assert clamp_summary('  "Refactoring   the\n auth"  ') == "Refactoring the auth"
    assert clamp_summary("```Running tests```") == "Running tests"


def test_clamp_summary_truncates_with_ellipsis():
    out = clamp_summary("x" * 250, limit=100)
    assert len(out) == 100
    assert out.endswith("…")


def test_should_regenerate_requires_viewer():
    assert should_regenerate(False, None, 10.0, 1000.0, "in_progress") is False


def test_should_regenerate_skips_terminal_status():
    assert should_regenerate(True, None, 10.0, 1000.0, "ended") is False


def test_should_regenerate_first_time_when_viewed_and_active():
    assert should_regenerate(True, None, 10.0, 1000.0, "in_progress") is True


def test_should_regenerate_false_when_signature_unchanged():
    cached = (10.0, 990.0, "old")
    assert should_regenerate(True, cached, 10.0, 1000.0, "in_progress") is False


def test_should_regenerate_throttled_within_60s():
    cached = (10.0, 990.0, "old")  # changed signature below, but only 10s elapsed
    assert should_regenerate(True, cached, 11.0, 1000.0, "in_progress") is False


def test_should_regenerate_after_60s_and_change():
    cached = (10.0, 900.0, "old")  # 100s elapsed, signature changed
    assert should_regenerate(True, cached, 11.0, 1000.0, "in_progress") is True


def test_summary_service_empty_context_returns_empty():
    svc = SummaryService(sdk_factory=lambda cwd, cfg: None)
    assert asyncio.run(svc.generate("/x", "/c", "   ")) == ""


def test_summary_service_failure_returns_empty():
    def boom(cwd, cfg):
        raise RuntimeError("no sdk")
    svc = SummaryService(sdk_factory=boom)
    assert asyncio.run(svc.generate("/x", "/c", "User: hi\n\nAssistant: hey")) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_summaries.py -q`
Expected: FAIL — `ModuleNotFoundError: server.sessions.summaries`.

- [ ] **Step 3: Create `server/sessions/summaries.py`**

```python
from __future__ import annotations
import logging

from .translator import Translator
from . import events as ev

log = logging.getLogger(__name__)

# Terminal states whose transcript won't meaningfully change again — never spend a
# summary call on them. (Dormant active sessions are already filtered by the
# mtime-signature check, so this set is intentionally tiny.)
_SUMMARY_TERMINAL_STATUSES = {"ended", "archived"}
# Minimum seconds between regenerations for one session — the "once a minute" cap.
_SUMMARY_MIN_INTERVAL_S = 60.0

SUMMARY_SYSTEM_PROMPT = (
    "You summarize what a live coding session is currently doing, for a dashboard. "
    "Reply with ONE short present-tense phrase under 100 characters — no prose, no "
    "quotes, no code fences. Example: Refactoring the auth middleware and running tests."
)


def build_summary_prompt(context: str) -> str:
    return (
        "Here is the end of a transcript between a user and an AI coding agent:\n\n"
        f"{context}\n\n"
        "In one short present-tense phrase, summarize what the agent is currently "
        "working on. Reply with the phrase only."
    )


def clamp_summary(raw: str, limit: int = 100) -> str:
    """Normalize the model's reply to a single tidy line for the card."""
    text = " ".join(raw.split())
    text = text.strip("`").strip().strip('"').strip("'").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def should_regenerate(
    viewing: bool,
    cached: tuple[float, float, str] | None,
    signature: float,
    now: float,
    status: str,
) -> bool:
    """Decide whether to (re)generate a session's summary.

    cached is the stored ``(transcript_signature, generated_at, text)`` or None.
    Regenerate only when a viewer is present, the session isn't terminal, the
    transcript changed since the cache, and at least _SUMMARY_MIN_INTERVAL_S has
    elapsed since the last generation. When no viewer is present, never regenerate
    (so an unwatched dashboard spends zero quota).
    """
    if not viewing:
        return False
    if status in _SUMMARY_TERMINAL_STATUSES:
        return False
    if cached is None:
        return True
    cached_sig, cached_at, _text = cached
    if cached_sig == signature:
        return False
    return (now - cached_at) >= _SUMMARY_MIN_INTERVAL_S


class SummaryService:
    """Generates a one-line status summary via a throwaway, resume-less SDK query.

    ``sdk_factory(cwd, config_dir) -> SDKClientProtocol`` builds a one-shot client bound
    to the session's own account config dir. Best-effort: returns "" on empty context or
    any failure.
    """

    def __init__(self, sdk_factory):
        self._factory = sdk_factory

    async def generate(self, cwd: str, config_dir: str, context: str) -> str:
        if not context.strip():
            return ""
        try:
            client = self._factory(cwd, config_dir)
            tr = Translator()
            parts: list[str] = []
            async with client:
                await client.query(build_summary_prompt(context))
                async for msg in client.receive_response():
                    for e in tr.feed(msg):
                        if isinstance(e, ev.TextDelta):
                            parts.append(e.text)
            return clamp_summary("".join(parts))
        except Exception:
            log.warning("summary generation failed cwd=%s", cwd, exc_info=True)
            return ""
```

- [ ] **Step 4: Add `real_summary_factory` to `server/sessions/sdk_factory.py`** (after `real_suggestion_factory`, ~line 66)

```python
def real_summary_factory(cwd, config_dir):
    """One-shot, resume-less client for generating a session status summary.

    Bound to the session's own account config dir (Max subscription only). Tools denied;
    model defaults to Haiku, overridable via CCSWITCH_SUMMARY_MODEL.
    """
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from .summaries import SUMMARY_SYSTEM_PROMPT
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    model = os.environ.get("CCSWITCH_SUMMARY_MODEL", "claude-haiku-4-5") or None
    opts = ClaudeAgentOptions(
        cwd=cwd, env=env, can_use_tool=_deny_all_tools,
        permission_mode="default", include_partial_messages=True,
        max_turns=1, system_prompt=SUMMARY_SYSTEM_PROMPT, model=model, resume=None,
    )
    return ClaudeSDKClient(options=opts)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_summaries.py -q`
Expected: PASS (10 passed).

- [ ] **Step 6: Lint**

Run: `<main-checkout>/.venv/bin/ruff check server/sessions/summaries.py server/sessions/sdk_factory.py && <main-checkout>/.venv/bin/mypy server/sessions/summaries.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/summaries.py server/sessions/sdk_factory.py server/tests/test_summaries.py
git commit -m "feat(summaries): SummaryService, prompt, clamp + regenerate predicate"
```

---

### Task 2: Orchestrator cache + `latest_summary` + `_generate_summary`

**Files:**
- Modify: `server/sessions/orchestrator.py` (ctor ~line 130-141 and ~159-179; add methods near `latest_suggestions` ~line 1676)
- Test: `server/tests/test_summary_cache.py`

**Interfaces:**
- Consumes: `should_regenerate`, `SummaryService` from `server.sessions.summaries`; `real_summary_factory` from `sdk_factory`; existing `replay_events`, `last_turn_context`, `compose`, `os`, `time`, `asyncio` (all already imported in orchestrator.py).
- Produces:
  - ctor kwarg `summary_factory=None`
  - `self._summarizer: SummaryService | None`
  - `self._summaries: dict[str, tuple[float, float, str]]`
  - `self._summary_inflight: set[str]`
  - `async def latest_summary(self, session_id: str, *, viewing: bool) -> str`
  - `async def _generate_summary(self, session_id, rec, path, signature) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_summary_cache.py
import asyncio
import types
from unittest.mock import AsyncMock

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch_with_record(status="in_progress"):
    orch = SessionOrchestrator()
    rec = types.SimpleNamespace(id="s1", cwd="/x", account="acct", status=status)
    orch._records["s1"] = rec
    return orch, rec


def test_latest_summary_serves_cache_without_regen_when_signature_matches():
    orch, _ = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="new"))
    orch._summaries["s1"] = (5.0, 1_000.0, "cached text")

    async def fake_path(_sid):
        return "/t.jsonl"

    orch._resolve_transcript_path = fake_path  # type: ignore[assignment]
    import server.sessions.orchestrator as om
    orig = om.os.path.getmtime
    om.os.path.getmtime = lambda _p: 5.0  # signature unchanged
    try:
        out = asyncio.run(orch.latest_summary("s1", viewing=True))
    finally:
        om.os.path.getmtime = orig
    assert out == "cached text"
    orch._summarizer.generate.assert_not_called()


def test_latest_summary_no_regen_when_not_viewing():
    orch, _ = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="new"))

    async def fake_path(_sid):
        return "/t.jsonl"

    orch._resolve_transcript_path = fake_path  # type: ignore[assignment]
    import server.sessions.orchestrator as om
    orig = om.os.path.getmtime
    om.os.path.getmtime = lambda _p: 9.0
    try:
        out = asyncio.run(orch.latest_summary("s1", viewing=False))
    finally:
        om.os.path.getmtime = orig
    assert out == ""  # no cache, no generation scheduled
    orch._summarizer.generate.assert_not_called()


def test_latest_summary_missing_session_returns_empty():
    orch = SessionOrchestrator()
    assert asyncio.run(orch.latest_summary("nope", viewing=True)) == ""


def test_generate_summary_caches_text_and_clears_inflight():
    orch, rec = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="Doing X"))
    orch._summary_inflight.add("s1")

    import server.sessions.orchestrator as om
    orig_replay = om.replay_events
    orig_ctx = om.last_turn_context
    async def fake_replay(_p):
        return []
    om.replay_events = fake_replay
    om.last_turn_context = lambda _e: ("hi", "done")
    try:
        asyncio.run(orch._generate_summary("s1", rec, "/t.jsonl", 7.0))
    finally:
        om.replay_events = orig_replay
        om.last_turn_context = orig_ctx
    sig, _at, text = orch._summaries["s1"]
    assert (sig, text) == (7.0, "Doing X")
    assert "s1" not in orch._summary_inflight
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_summary_cache.py -q`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'latest_summary'`.

- [ ] **Step 3: Wire the constructor** — add the import and the cache fields.

In the imports block (near line 47, beside the suggestions import) add:
```python
from server.sessions.summaries import SummaryService, should_regenerate
```
In `sdk_factory` import (line 45) add `real_summary_factory`:
```python
from server.sessions.sdk_factory import real_sdk_factory, real_suggestion_factory, real_summary_factory
```
Add the ctor kwarg after `title_fn` (line 140):
```python
        title_fn: Callable | None = None,
        summary_factory=None,
```
After the suggester block (after line 162) add:
```python
        if os.environ.get("CCSWITCH_SUMMARIES", "1") == "0":
            self._summarizer = None
        else:
            self._summarizer = SummaryService(summary_factory or real_summary_factory)
```
After `self._suggest_inflight` (line 179) add:
```python
        # Session status summaries: session_id -> (transcript mtime, generated_at_monotonic, text).
        # Generated lazily from the transcript, only while a viewer is present and at most
        # once / 60s (see latest_summary / should_regenerate). Ephemeral — never persisted.
        self._summaries: dict[str, tuple[float, float, str]] = {}
        self._summary_inflight: set[str] = set()
```

- [ ] **Step 4: Add `latest_summary` and `_generate_summary`** (immediately after `_generate_detached_suggestions`, ~line 1742)

```python
    async def latest_summary(self, session_id: str, *, viewing: bool) -> str:
        """Latest cached one-line status summary for a session.

        Mirrors latest_suggestions: lazy, mtime-keyed, non-blocking. Schedules a
        background regeneration only when should_regenerate() allows (viewer present,
        active status, transcript changed, >=60s since last gen). Returns the cached
        text (or "") immediately — never blocks the /sessions GET. Never raises.
        """
        rec = self._records.get(session_id)
        if rec is None:
            return ""
        cached = self._summaries.get(session_id)
        try:
            path = await self._resolve_transcript_path(session_id)
            signature = os.path.getmtime(path) if path else 0.0
        except Exception:
            return cached[2] if cached else ""
        now = time.monotonic()
        if (
            path
            and self._summarizer is not None
            and session_id not in self._summary_inflight
            and should_regenerate(viewing, cached, signature, now, rec.status)
        ):
            self._summary_inflight.add(session_id)
            asyncio.create_task(self._generate_summary(session_id, rec, path, signature))
        return cached[2] if cached else ""

    async def _generate_summary(self, session_id, rec, path, signature) -> None:
        """Generate a summary from the session's transcript (best-effort).

        Caches (signature, generated_at, text) even when text is "", so the same
        transcript state isn't re-generated until it changes AND 60s passes. Always
        clears the in-flight flag.
        """
        try:
            if self._summarizer is None:
                self._summaries[session_id] = (signature, time.monotonic(), "")
                return
            events = await replay_events(path)
            user_text, assistant_text = last_turn_context(events)
            context = f"User: {user_text}\n\nAssistant: {assistant_text}"
            text = await self._summarizer.generate(rec.cwd, str(compose(rec.account)), context)
            self._summaries[session_id] = (signature, time.monotonic(), text or "")
        except Exception:
            log.warning("summary generation failed id=%s", session_id, exc_info=True)
            self._summaries[session_id] = (signature, time.monotonic(), "")
        finally:
            self._summary_inflight.discard(session_id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_summary_cache.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Lint**

Run: `<main-checkout>/.venv/bin/ruff check server/sessions/orchestrator.py && <main-checkout>/.venv/bin/mypy server/sessions/orchestrator.py`
Expected: clean (mypy skips untyped bodies; ensure no new errors).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_summary_cache.py
git commit -m "feat(summaries): orchestrator latest_summary cache + background generation"
```

---

### Task 3: Remote/facade/RPC plumbing for `latest_summary`

**Files:**
- Modify: `server/sessions/facade.py:42` area (add delegate)
- Modify: `server/sessions/remote.py:185` area (add proxy)
- Modify: `server/ipc/orchestrator_rpc.py` (add `"latest_summary"` to the simple-methods list, ~line 82)
- Test: `server/tests/test_remote_orchestrator.py` (add one proxy test)

**Interfaces:**
- Consumes: `RemoteOrchestrator._client.call`, `InProcessOrchestratorAdapter._orch.latest_summary`.
- Produces: `latest_summary(self, session_id, *, viewing=False) -> str` on both `RemoteOrchestrator` and `InProcessOrchestratorAdapter`; RPC handler auto-registered.

- [ ] **Step 1: Write the failing test** (append to `server/tests/test_remote_orchestrator.py`)

```python
def test_remote_orchestrator_latest_summary_proxies_with_viewing():
    import asyncio
    from unittest.mock import AsyncMock
    from server.sessions.remote import RemoteOrchestrator

    ro = RemoteOrchestrator.__new__(RemoteOrchestrator)
    ro._client = types.SimpleNamespace(call=AsyncMock(return_value="Doing X"))
    out = asyncio.run(ro.latest_summary("s1", viewing=True))
    assert out == "Doing X"
    ro._client.call.assert_awaited_once_with("latest_summary", session_id="s1", viewing=True)
```

(Ensure `import types` is present at the top of the test file; add it if missing.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_remote_orchestrator.py::test_remote_orchestrator_latest_summary_proxies_with_viewing -q`
Expected: FAIL — `AttributeError: ... has no attribute 'latest_summary'`.

- [ ] **Step 3: Add the `RemoteOrchestrator` proxy** (after `latest_suggestions`, remote.py:187)

```python
    async def latest_summary(self, session_id: str, *, viewing: bool = False) -> str:
        r = await self._client.call("latest_summary", session_id=session_id, viewing=viewing)
        return r if isinstance(r, str) else ""
```

- [ ] **Step 4: Add the `InProcessOrchestratorAdapter` delegate** (after `latest_suggestions`, facade.py:42)

```python
    async def latest_summary(self, *a, **k): return await _maybe_await(self._orch.latest_summary(*a, **k))
```

- [ ] **Step 5: Register the RPC handler** — add `"latest_summary"` to the simple-method-names list in `server/ipc/orchestrator_rpc.py` (the block starting ~line 76, beside `"latest_suggestions"`).

```python
        "latest_suggestions", "seed_idle_suggestions", "latest_summary",
```

- [ ] **Step 6: Run the proxy test + the breadth guard**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_remote_orchestrator.py server/tests/test_remote_orchestrator_breadth.py -q`
Expected: PASS (the breadth test now sees `latest_summary` is a callable on `RemoteOrchestrator`).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/remote.py server/sessions/facade.py server/ipc/orchestrator_rpc.py server/tests/test_remote_orchestrator.py
git commit -m "feat(summaries): proxy latest_summary across facade, remote, and RPC"
```

---

### Task 4: API — `?viewing=1` flag + `summary` wire field

**Files:**
- Modify: `server/api/sessions.py` (`_record_to_dict` ~line 89-129; add `_resolve_summary` helper near `_resolve_prs` ~line 162; `list_sessions` ~line 186-202)
- Test: `server/tests/test_sessions_api_summary.py`

**Interfaces:**
- Consumes: `orch.latest_summary(id, viewing=bool)` (Task 3).
- Produces: `_record_to_dict(..., summary: str = "")` → wire key `"summary"`; `_resolve_summary(orch, record, viewing) -> str`; `list_sessions` reads `request.query_params.get("viewing")`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_sessions_api_summary.py
import types
from unittest.mock import AsyncMock

from server.api.sessions import _record_to_dict, _resolve_summary


def test_record_to_dict_includes_summary():
    rec = types.SimpleNamespace(
        id="s1", name="n", account="a", repo=None, status="in_progress",
        name_is_auto=True, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False,
    )
    d = _record_to_dict(rec, summary="Refactoring auth")
    assert d["summary"] == "Refactoring auth"


def test_record_to_dict_summary_defaults_empty():
    rec = types.SimpleNamespace(
        id="s1", name="n", account="a", repo=None, status="idle",
        name_is_auto=True, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False,
    )
    assert _record_to_dict(rec)["summary"] == ""


def test_resolve_summary_passes_viewing_and_tolerates_missing_method():
    import asyncio
    orch = types.SimpleNamespace(latest_summary=AsyncMock(return_value="Doing X"))
    rec = types.SimpleNamespace(id="s1")
    assert asyncio.run(_resolve_summary(orch, rec, True)) == "Doing X"
    orch.latest_summary.assert_awaited_once_with("s1", viewing=True)
    # Older shim without the method → "" (never raises)
    bare = types.SimpleNamespace()
    assert asyncio.run(_resolve_summary(bare, rec, True)) == ""
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_api_summary.py -q`
Expected: FAIL — `ImportError: cannot import name '_resolve_summary'`.

- [ ] **Step 3: Add the `summary` param to `_record_to_dict`** — change the signature (line 89-91) and add the key.

```python
def _record_to_dict(
    record, repo: Any = _UNSET, suggestions: list[str] | None = None,
    prs: list | None = None, summary: str = "",
) -> dict[str, Any]:
```
Add inside the returned dict (after `"latestSuggestions"`, line 125):
```python
        "summary": summary or "",
```

- [ ] **Step 4: Add `_resolve_summary`** (after `_resolve_prs`, ~line 174)

```python
async def _resolve_summary(orch, record, viewing: bool) -> str:
    """Resolve a record's one-line status summary via ``orch.latest_summary``.

    Returns "" for an orchestrator shim without ``latest_summary`` (never raises),
    mirroring the defensiveness of :func:`_resolve_prs`.
    """
    fn = getattr(orch, "latest_summary", None)
    if fn is None:
        return ""
    res = fn(record.id, viewing=viewing)
    if inspect.isawaitable(res):
        res = await res
    return res or ""
```

- [ ] **Step 5: Thread `viewing` through `list_sessions`** (replace the body, lines 192-202)

```python
    orch = _get_orchestrator(request)
    records = await orch.list_sessions()
    viewing = request.query_params.get("viewing") in ("1", "true", "yes")
    # Read each session's repo (live, from its cwd), cached suggestions, PR list, and
    # status summary in parallel.
    repos = await asyncio.gather(*(_resolve_repo(r) for r in records))
    suggs = await asyncio.gather(*(orch.latest_suggestions(r.id) for r in records))
    prs = await asyncio.gather(*(_resolve_prs(orch, r) for r in records))
    summaries = await asyncio.gather(*(_resolve_summary(orch, r, viewing) for r in records))
    return [
        _record_to_dict(r, repo=repo, suggestions=s, prs=pr_list, summary=summary)
        for r, repo, s, pr_list, summary in zip(records, repos, suggs, prs, summaries)
    ]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_api_summary.py server/tests/test_remote_orchestrator_breadth.py -q`
Expected: PASS (the breadth test still passes — `latest_summary` is on `RemoteOrchestrator`).

- [ ] **Step 7: Lint**

Run: `<main-checkout>/.venv/bin/ruff check server/api/sessions.py && <main-checkout>/.venv/bin/mypy server/api/sessions.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_api_summary.py
git commit -m "feat(summaries): /api/sessions?viewing flag + summary wire field"
```

---

### Task 5: Frontend — viewing signal + `summary` type

**Files:**
- Modify: `web/src/api/types.ts:55-84` (add `summary?` to `Session`)
- Create: `web/src/api/sessionsViewing.ts` (module-level viewing flag)
- Modify: `web/src/api/hooks.ts:45-58` (`useSessions` reads the flag; add `useSessionsViewing`)
- Create: `web/src/hooks/usePageVisible.ts`
- Modify: `web/src/components/Dashboard.tsx` (~line 436 + a `usePageVisible`/`useSessionsViewing` call)
- Test: `web/src/api/sessionsViewing.test.ts`, `web/src/hooks/usePageVisible.test.tsx`

**Interfaces:**
- Produces:
  - `Session.summary?: string`
  - `setSessionsViewing(v: boolean): void`, `isSessionsViewing(): boolean` (module singleton)
  - `useSessionsViewing(active: boolean): void` (effect hook in hooks.ts)
  - `usePageVisible(): boolean`
- Consumes: nothing new from earlier tasks (server already sends `summary`).

> **Why a module-level flag, not a hook arg:** `useSessions()` is called in 8 places sharing one TanStack Query cache (`QK.sessions`). Passing `viewing` per-call would either fragment the cache or let a background consumer overwrite the flag. A single module singleton, set only by the Dashboard, keeps one shared query and one source of truth. The query key stays `['sessions']`; only the fetched URL varies.

- [ ] **Step 1: Write the failing tests**

```typescript
// web/src/api/sessionsViewing.test.ts
import { describe, it, expect } from 'vitest'
import { setSessionsViewing, isSessionsViewing } from './sessionsViewing'

describe('sessionsViewing flag', () => {
  it('defaults to false and round-trips', () => {
    setSessionsViewing(false)
    expect(isSessionsViewing()).toBe(false)
    setSessionsViewing(true)
    expect(isSessionsViewing()).toBe(true)
  })
})
```

```tsx
// web/src/hooks/usePageVisible.test.tsx
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePageVisible } from './usePageVisible'

describe('usePageVisible', () => {
  it('tracks visibilitychange', () => {
    const { result } = renderHook(() => usePageVisible())
    expect(result.current).toBe(true) // jsdom default: 'visible'
    act(() => {
      Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(result.current).toBe(false)
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (in `web/`): `npx vitest run src/api/sessionsViewing.test.ts src/hooks/usePageVisible.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Create `web/src/api/sessionsViewing.ts`**

```typescript
// Module-level "is the sessions list on-screen?" flag. Set only by the Dashboard
// (see useSessionsViewing); read by useSessions' queryFn to add ?viewing=1 so the
// server regenerates summaries only while someone is actually looking.
let viewing = false

export function setSessionsViewing(v: boolean): void {
  viewing = v
}

export function isSessionsViewing(): boolean {
  return viewing
}
```

- [ ] **Step 4: Create `web/src/hooks/usePageVisible.ts`**

```typescript
import { useEffect, useState } from 'react'

/** True while the browser tab is visible (Page Visibility API). */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState === 'visible',
  )
  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState === 'visible')
    document.addEventListener('visibilitychange', onChange)
    return () => document.removeEventListener('visibilitychange', onChange)
  }, [])
  return visible
}
```

- [ ] **Step 5: Run the two tests to verify they pass**

Run (in `web/`): `npx vitest run src/api/sessionsViewing.test.ts src/hooks/usePageVisible.test.tsx`
Expected: PASS.

- [ ] **Step 6: Wire `useSessions` + add `useSessionsViewing`** in `web/src/api/hooks.ts`

Add imports near the top:
```typescript
import { useEffect } from 'react'
import { isSessionsViewing, setSessionsViewing } from './sessionsViewing'
```
(merge `useEffect` into the existing `from 'react'` import.)

Change the `useSessions` queryFn (line 49):
```typescript
    queryFn: () => api.get<Session[]>(isSessionsViewing() ? '/sessions?viewing=1' : '/sessions'),
```

Add below `useSessions` (after line 58):
```typescript
/** Tell the server to (re)generate summaries only while the sessions list is on-screen. */
export function useSessionsViewing(active: boolean): void {
  useEffect(() => {
    setSessionsViewing(active)
    return () => setSessionsViewing(false)
  }, [active])
}
```

- [ ] **Step 7: Call it from the Dashboard** — in `web/src/components/Dashboard.tsx`, near the `useSessions()` call (line 436) add:

```tsx
  const pageVisible = usePageVisible()
  useSessionsViewing(activeTab === 'sessions' && pageVisible)
```
Add the imports: `useSessionsViewing` from `@/api/hooks` (extend the existing import) and `usePageVisible` from `@/hooks/usePageVisible`. (`activeTab` is already in scope at line 452.)

- [ ] **Step 8: Add `summary` to the `Session` type** — `web/src/api/types.ts` after `latestSuggestions` (line 75):

```typescript
  /** One-line LLM status summary of what the session is doing now (empty when none). */
  summary?: string
```

- [ ] **Step 9: Gates**

Run (in `web/`): `npx vitest run src/api src/hooks && npx tsc -b --force && npx eslint src/api/hooks.ts src/api/sessionsViewing.ts src/hooks/usePageVisible.ts src/components/Dashboard.tsx`
Expected: PASS / clean.

- [ ] **Step 10: Commit**

```bash
git add web/src/api/types.ts web/src/api/sessionsViewing.ts web/src/api/hooks.ts web/src/hooks/usePageVisible.ts web/src/components/Dashboard.tsx web/src/api/sessionsViewing.test.ts web/src/hooks/usePageVisible.test.tsx
git commit -m "feat(summaries): viewing-gated ?viewing=1 fetch + summary type"
```

---

### Task 6: Frontend — render the summary line on `SessionCard`

**Files:**
- Modify: `web/src/components/SessionCard.tsx` (after the Row 1 identity `</div>`, ~line 215)
- Test: `web/src/components/SessionCard.summary.test.tsx`

**Interfaces:**
- Consumes: `Session.summary` (Task 5).

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/SessionCard.summary.test.tsx
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionCard } from './SessionCard'
import type { Session } from '@/api/types'

function makeSession(over: Partial<Session> = {}): Session {
  return {
    id: 's1', name: 'My session', accountName: 'acct', status: 'in_progress',
    createdAt: new Date().toISOString(), prs: [], ...over,
  } as Session
}

function renderCard(session: Session) {
  return render(<SessionCard session={session} onOpen={() => {}} />)
}

describe('SessionCard summary line', () => {
  it('renders the summary when present', () => {
    renderCard(makeSession({ summary: 'Refactoring the auth middleware' }))
    expect(screen.getByTestId('card-summary')).toHaveTextContent('Refactoring the auth middleware')
  })

  it('omits the summary line when empty', () => {
    renderCard(makeSession({ summary: '' }))
    expect(screen.queryByTestId('card-summary')).toBeNull()
  })
})
```

> NOTE: match `SessionCard`'s real required props. If it needs more than `session`/`onOpen` (e.g. `account`, `onReview`), read lines 1-120 of `SessionCard.tsx` and extend `renderCard` accordingly — the test must compile under `tsc -b`.

- [ ] **Step 2: Run the test to verify it fails**

Run (in `web/`): `npx vitest run src/components/SessionCard.summary.test.tsx`
Expected: FAIL — no element with testid `card-summary`.

- [ ] **Step 3: Render the summary line** — in `SessionCard.tsx`, immediately after the Row 1 identity `</div>` (line 215), add:

```tsx
        {/* Summary — one muted truncated line of "what's happening now", below the
            name. Hidden when absent so dormant cards keep their compact height. */}
        {session.summary && (
          <div
            data-testid="card-summary"
            className="truncate text-xs text-gray-500 dark:text-gray-400"
            title={session.summary}
          >
            {session.summary}
          </div>
        )}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (in `web/`): `npx vitest run src/components/SessionCard.summary.test.tsx`
Expected: PASS (2 passed).

- [ ] **Step 5: Full frontend gates**

Run (in `web/`): `npx vitest run && npx tsc -b --force && npx eslint src`
Expected: PASS / clean. (Watch for snapshot tests of `SessionCard` that need updating.)

- [ ] **Step 6: Commit**

```bash
git add web/src/components/SessionCard.tsx web/src/components/SessionCard.summary.test.tsx
git commit -m "feat(summaries): show one-line session summary on the card"
```

---

## Final verification (before PR)

- [ ] Backend: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 CCSWITCH_ENABLE_XVFB= PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/ -q` (expect only the known-environmental failures from memory, no new ones).
- [ ] Backend lint: `<main-checkout>/.venv/bin/ruff check server/ && <main-checkout>/.venv/bin/mypy server/`.
- [ ] Frontend: in `web/`, `npx vitest run && npx tsc -b --force && npx eslint src`.
- [ ] Open the app, confirm summaries appear on active-session cards within ~1 min while the sessions tab is visible, and that backgrounding the tab / switching tabs stops regeneration (no new Haiku calls in logs).

## Notes for the executor (learning mode)

Per the session's learning-mode style, when Task 1 reaches **Step 3's `should_regenerate`**, offer the user the chance to write that predicate themselves before pasting the planned version — it is the freshness-vs-quota decision where their judgment matters most. If they decline, use the version above verbatim. All other steps are mechanical.
