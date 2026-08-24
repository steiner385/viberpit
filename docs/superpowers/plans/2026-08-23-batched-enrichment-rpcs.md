# Batched Enrichment RPCs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the three-per-session enrichment RPCs on `GET /api/sessions` into three batched
calls, taking a 21-session page from 296 RPCs (~6s) to ~27 (well under a second).

**Architecture:** The batching logic lives once, on `SessionOrchestrator`, as three `*_many`
methods. The daemon RPC layer, the in-process facade adapter, and the remote client are all thin
wrappers over that single implementation. Per-session isolation lives inside the `*_many` methods,
so one bad session empties one field rather than the page.

**Tech Stack:** Python 3.12, FastAPI, httpx/httpcore, asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-08-23-batched-enrichment-rpcs-design.md`

## Global Constraints

- **Never use `asyncio.wait_for` around a daemon RPC.** It cancels the request; the
  `CancelledError` strands the connection's pool slot ACTIVE forever and wedges the edge
  (#734). Deadlines are pushed IN via `call_with_deadline` → `DaemonClient.call` → httpx.
- Any `RemoteOrchestrator` method accepting `timeout=` MUST carry `@rpc_deadline`, and the
  marked set is pinned by `_DEADLINE_CAPABLE_RPCS` in
  `server/tests/test_ipc_cancel_does_not_strand.py`. Adding a marker without updating that
  frozenset fails the suite — this is deliberate.
- Reuse `PullRequestInfo.to_dict()` / `.from_dict()`. Do not write a second serializer.
- Empty values are `[]` for prs and suggestions, `""` for summary.
- Run all four CI gates before pushing: `ruff check src/ server/`,
  `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- Run the server suite as `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest server/tests/`
  or API tests 401.

---

### Task 1: Orchestrator `*_many` methods

The single implementation of batching. Everything later is a wrapper over this.

**Files:**
- Modify: `server/sessions/orchestrator.py` (add after `prs_for` ~line 4370, and after
  `latest_summary` ~line 5717)
- Test: `server/tests/test_enrichment_batch.py` (create)

**Interfaces:**
- Consumes: existing `SessionOrchestrator.prs_for(session_id)` (sync),
  `latest_suggestions(session_id)` (async), `latest_summary(session_id, *, viewing)` (async).
- Produces:
  - `SessionOrchestrator.prs_for_many(session_ids: list[str]) -> dict[str, list[PullRequestInfo]]` (sync)
  - `SessionOrchestrator.latest_suggestions_many(session_ids: list[str]) -> dict[str, list[str]]` (async)
  - `SessionOrchestrator.latest_summary_many(session_ids: list[str], *, viewing: bool) -> dict[str, str]` (async)

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_enrichment_batch.py`:

```python
"""Batched enrichment: one call per field for the whole page, with per-session isolation.

The isolation tests are the point. A batch that fails whole-hog on one bad session would
turn a single wedged session into a blank page — strictly worse than the per-session calls
it replaces (issue #742, spec 2026-08-23-batched-enrichment-rpcs-design.md).
"""
import pytest

from server.github.pr_status import PullRequestInfo


class _Orch:
    """Minimal stand-in exposing only what the *_many methods call."""

    def __init__(self, *, bad: set[str] | None = None):
        self._bad = bad or set()

    def prs_for(self, session_id):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return [PullRequestInfo(1, "u", "open", "none", f"t-{session_id}")]

    async def latest_suggestions(self, session_id):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return [f"s-{session_id}"]

    async def latest_summary(self, session_id, *, viewing):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return f"sum-{session_id}-{viewing}"


def _bind(orch):
    """Bind the real *_many implementations onto the stand-in."""
    from server.sessions.orchestrator import SessionOrchestrator
    for name in ("prs_for_many", "latest_suggestions_many", "latest_summary_many"):
        setattr(type(orch), name, getattr(SessionOrchestrator, name))
    return orch


def test_prs_for_many_returns_one_entry_per_session():
    orch = _bind(_Orch())
    out = orch.prs_for_many(["a", "b"])
    assert set(out) == {"a", "b"}
    assert out["a"][0].title == "t-a"


def test_prs_for_many_isolates_a_failing_session():
    orch = _bind(_Orch(bad={"b"}))
    out = orch.prs_for_many(["a", "b"])
    assert out["a"][0].title == "t-a"
    assert out["b"] == []


@pytest.mark.asyncio
async def test_latest_suggestions_many_isolates_a_failing_session():
    orch = _bind(_Orch(bad={"b"}))
    out = await orch.latest_suggestions_many(["a", "b"])
    assert out["a"] == ["s-a"]
    assert out["b"] == []


@pytest.mark.asyncio
async def test_latest_summary_many_forwards_viewing_and_isolates():
    orch = _bind(_Orch(bad={"b"}))
    out = await orch.latest_summary_many(["a", "b"], viewing=True)
    assert out["a"] == "sum-a-True"
    assert out["b"] == ""


@pytest.mark.asyncio
async def test_empty_input_makes_no_calls():
    orch = _bind(_Orch())
    assert orch.prs_for_many([]) == {}
    assert await orch.latest_suggestions_many([]) == {}
    assert await orch.latest_summary_many([], viewing=False) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch.py -q`
Expected: FAIL — `AttributeError: type object 'SessionOrchestrator' has no attribute 'prs_for_many'`

- [ ] **Step 3: Implement**

In `server/sessions/orchestrator.py`, immediately after the `prs_for` method:

```python
    def prs_for_many(self, session_ids: list[str]) -> dict[str, list[PullRequestInfo]]:
        """Batched :meth:`prs_for` — one entry per requested id.

        Isolated per session: a failure yields [] for that id alone. This mirrors the
        blast radius of the per-session calls it replaces (#742); a batch that failed
        whole-hog would turn one bad session into a blank page.
        """
        out: dict[str, list[PullRequestInfo]] = {}
        for sid in session_ids:
            try:
                out[sid] = self.prs_for(sid)
            except Exception:
                log.warning("prs_for_many: failed for %s", sid, exc_info=True)
                out[sid] = []
        return out
```

Immediately after `latest_summary`:

```python
    async def latest_suggestions_many(self, session_ids: list[str]) -> dict[str, list[str]]:
        """Batched :meth:`latest_suggestions`, isolated per session (see prs_for_many)."""
        ids = list(session_ids)
        results = await asyncio.gather(
            *(self.latest_suggestions(sid) for sid in ids), return_exceptions=True)
        out: dict[str, list[str]] = {}
        for sid, r in zip(ids, results):
            if isinstance(r, BaseException):
                log.warning("latest_suggestions_many: failed for %s", sid, exc_info=r)
                out[sid] = []
            else:
                out[sid] = r
        return out

    async def latest_summary_many(self, session_ids: list[str], *,
                                  viewing: bool) -> dict[str, str]:
        """Batched :meth:`latest_summary`, isolated per session (see prs_for_many).

        ``viewing`` is request-level, not per-session (api/sessions.py reads one query
        param for the whole page), so it applies uniformly to the batch.
        """
        ids = list(session_ids)
        results = await asyncio.gather(
            *(self.latest_summary(sid, viewing=viewing) for sid in ids),
            return_exceptions=True)
        out: dict[str, str] = {}
        for sid, r in zip(ids, results):
            if isinstance(r, BaseException):
                log.warning("latest_summary_many: failed for %s", sid, exc_info=r)
                out[sid] = ""
            else:
                out[sid] = r
        return out
```

`PullRequestInfo`, `asyncio` and `log` are all already available at module scope in
`orchestrator.py` (lines 73, 14, 76). Use a plain annotation — `dict[str, list[PullRequestInfo]]`
— not a string one, and add no imports.

- [ ] **Step 4: Run to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_enrichment_batch.py
git commit -m "feat(orchestrator): batched enrichment reads with per-session isolation

Refs #742"
```

---

### Task 2: Daemon RPC handlers

**Files:**
- Modify: `server/ipc/orchestrator_rpc.py` (add handler near `prs_for` ~line 169; add two
  names to `_SIMPLE` ~line 271)
- Test: `server/tests/test_enrichment_batch_rpc.py` (create)

**Interfaces:**
- Consumes: Task 1's three orchestrator methods.
- Produces: three registered RPC methods —
  - `prs_for_many` params `{session_ids: [...]}` → `{"prs": {sid: [PRInfo dict]}}`
  - `latest_suggestions_many` params `{session_ids: [...]}` → `{sid: [str]}` (raw, via `_SIMPLE`)
  - `latest_summary_many` params `{session_ids: [...], viewing: bool}` → `{sid: str}` (raw, via `_SIMPLE`)

Note the asymmetry: `_SIMPLE` returns the orchestrator's value verbatim (it is already
JSON-safe for the latter two), while `prs_for_many` needs an explicit handler because
`PullRequestInfo` must go through `to_dict()`. This mirrors exactly how the singular
`prs_for` differs from singular `latest_suggestions`/`latest_summary` today.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_enrichment_batch_rpc.py`:

```python
"""The batched RPCs are registered and serialize the way their singular twins do."""
import pytest
from fastapi import FastAPI

from server.github.pr_status import PullRequestInfo
from server.ipc.orchestrator_rpc import register_orchestrator_rpc


class _Orch:
    def prs_for_many(self, session_ids):
        return {sid: [PullRequestInfo(7, "u", "open", "none", f"t-{sid}")] for sid in session_ids}

    async def latest_suggestions_many(self, session_ids):
        return {sid: [f"s-{sid}"] for sid in session_ids}

    async def latest_summary_many(self, session_ids, *, viewing):
        return {sid: f"sum-{sid}-{viewing}" for sid in session_ids}


def _app():
    app = FastAPI()
    app.state.rpc = {}
    app.state.evict = lambda sid: None
    register_orchestrator_rpc(app, _Orch())
    return app


@pytest.mark.asyncio
async def test_prs_for_many_serializes_via_to_dict():
    app = _app()
    out = await app.state.rpc["prs_for_many"]({"session_ids": ["a"]})
    assert out["prs"]["a"][0]["number"] == 7
    assert out["prs"]["a"][0]["title"] == "t-a"


@pytest.mark.asyncio
async def test_suggestions_and_summary_many_are_registered():
    app = _app()
    assert await app.state.rpc["latest_suggestions_many"]({"session_ids": ["a"]}) == {"a": ["s-a"]}
    assert await app.state.rpc["latest_summary_many"](
        {"session_ids": ["a"], "viewing": True}) == {"a": "sum-a-True"}
```

`register_orchestrator_rpc(app, orch)` is the real entrypoint (`server/ipc/orchestrator_rpc.py:15`)
— singular, not `..._rpcs`.

- [ ] **Step 2: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_rpc.py -q`
Expected: FAIL — `KeyError: 'prs_for_many'`

- [ ] **Step 3: Implement**

In `server/ipc/orchestrator_rpc.py`, directly after the existing `prs_for` handler:

```python
    async def prs_for_many(params: dict) -> dict:
        by_sid = orch.prs_for_many(params.get("session_ids") or [])
        if inspect.isawaitable(by_sid):
            by_sid = await by_sid
        return {"prs": {sid: [i.to_dict() for i in infos] for sid, infos in by_sid.items()}}
```

Register it beside the existing `register_rpc(app, "prs_for", prs_for)` line:

```python
    register_rpc(app, "prs_for_many", prs_for_many)
```

`inspect` is imported a few lines below the `prs_for` handler (`import inspect`); if the
handler above sits before that import, move the registration after it or hoist the import —
do not add a second `import inspect`.

Add the other two to `_SIMPLE`, next to their singular twins:

```python
        "latest_suggestions", "latest_suggestions_many",
        "generate_suggestions_now", "seed_idle_suggestions",
        "latest_summary", "latest_summary_many",
```

- [ ] **Step 4: Run to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_rpc.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add server/ipc/orchestrator_rpc.py server/tests/test_enrichment_batch_rpc.py
git commit -m "feat(ipc): register batched enrichment RPCs

Refs #742"
```

---

### Task 3: Remote client methods, facade passthrough, deadline allowlist

**Files:**
- Modify: `server/sessions/remote.py` (after `prs_for` ~line 300; after `latest_summary` ~line 645)
- Modify: `server/sessions/facade.py` (~line 137, beside the existing passthroughs)
- Modify: `server/tests/test_ipc_cancel_does_not_strand.py` (`_DEADLINE_CAPABLE_RPCS`, ~line 286)
- Test: `server/tests/test_enrichment_batch_client.py` (create)

**Interfaces:**
- Consumes: Task 2's registered RPC names and envelopes.
- Produces:
  - `RemoteOrchestrator.prs_for_many(session_ids, *, timeout=None) -> dict[str, list[PullRequestInfo]]`
  - `RemoteOrchestrator.latest_suggestions_many(session_ids, *, timeout=None) -> dict[str, list[str]]`
  - `RemoteOrchestrator.latest_summary_many(session_ids, *, viewing=False, timeout=None) -> dict[str, str]`
  - Matching `InProcessOrchestratorAdapter` passthroughs.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_enrichment_batch_client.py`:

```python
"""The batched client methods unwrap their envelopes and carry the deadline INWARD."""
import pytest

from server.sessions.remote import RemoteOrchestrator


class _Client:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return self._reply


def _orch(reply):
    o = RemoteOrchestrator.__new__(RemoteOrchestrator)
    o._client = _Client(reply)
    return o


@pytest.mark.asyncio
async def test_prs_for_many_unwraps_and_rehydrates():
    o = _orch({"prs": {"a": [{"number": 7, "url": "u", "state": "open",
                              "checks": "none", "title": "t"}]}})
    out = await o.prs_for_many(["a"], timeout=3.0)
    assert out["a"][0].number == 7
    method, kwargs = o._client.calls[0]
    assert method == "prs_for_many"
    assert kwargs["session_ids"] == ["a"]
    assert kwargs["timeout"] == 3.0


@pytest.mark.asyncio
async def test_summary_many_forwards_viewing():
    o = _orch({"a": "sum"})
    out = await o.latest_summary_many(["a"], viewing=True, timeout=3.0)
    assert out == {"a": "sum"}
    assert o._client.calls[0][1]["viewing"] is True


@pytest.mark.asyncio
async def test_malformed_replies_degrade_to_empty():
    assert await _orch(None).prs_for_many(["a"]) == {}
    assert await _orch("nonsense").latest_suggestions_many(["a"]) == {}
    assert await _orch(None).latest_summary_many(["a"], viewing=False) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_client.py -q`
Expected: FAIL — `AttributeError: 'RemoteOrchestrator' object has no attribute 'prs_for_many'`

- [ ] **Step 3: Implement**

In `server/sessions/remote.py`, after `prs_for`:

```python
    @rpc_deadline
    async def prs_for_many(self, session_ids: list[str], *,
                           timeout: float | None = None) -> "dict[str, list[PullRequestInfo]]":
        """Batched :meth:`prs_for`. One RPC for the whole page instead of one per session.

        ``timeout`` is the caller's deadline pushed INTO the RPC — never race this from
        outside (see server.sessions.facade.call_with_deadline)."""
        r = await self._client.call("prs_for_many", session_ids=list(session_ids),
                                    timeout=timeout)
        by_sid = (r or {}).get("prs") if isinstance(r, dict) else None
        return {sid: [PullRequestInfo.from_dict(d) for d in (v or [])]
                for sid, v in (by_sid or {}).items()}
```

After `latest_summary`:

```python
    @rpc_deadline
    async def latest_suggestions_many(self, session_ids: list[str], *,
                                      timeout: float | None = None) -> dict[str, list[str]]:
        """Batched :meth:`latest_suggestions` (deadline pushed in, never raced)."""
        r = await self._client.call("latest_suggestions_many",
                                    session_ids=list(session_ids), timeout=timeout)
        return r if isinstance(r, dict) else {}

    @rpc_deadline
    async def latest_summary_many(self, session_ids: list[str], *, viewing: bool = False,
                                  timeout: float | None = None) -> dict[str, str]:
        """Batched :meth:`latest_summary` (deadline pushed in, never raced)."""
        r = await self._client.call("latest_summary_many", session_ids=list(session_ids),
                                    viewing=viewing, timeout=timeout)
        return r if isinstance(r, dict) else {}
```

In `server/sessions/facade.py`, beside the existing passthroughs:

```python
    async def prs_for_many(self, *a, **k): return await _maybe_await(self._orch.prs_for_many(*a, **k))
    async def latest_suggestions_many(self, *a, **k): return await _maybe_await(self._orch.latest_suggestions_many(*a, **k))
    async def latest_summary_many(self, *a, **k): return await _maybe_await(self._orch.latest_summary_many(*a, **k))
```

In `server/tests/test_ipc_cancel_does_not_strand.py`, extend the pinned set:

```python
_DEADLINE_CAPABLE_RPCS = frozenset({
    "ensure_driver",
    "latest_suggestions",
    "latest_suggestions_many",
    "latest_summary",
    "latest_summary_many",
    "prs_for",
    "prs_for_many",
    "usage",
})
```

- [ ] **Step 4: Run to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_client.py server/tests/test_ipc_cancel_does_not_strand.py -q`
Expected: PASS — both the new tests and the allowlist pin.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/remote.py server/sessions/facade.py \
        server/tests/test_enrichment_batch_client.py \
        server/tests/test_ipc_cancel_does_not_strand.py
git commit -m "feat(remote): batched enrichment client methods

Refs #742"
```

---

### Task 4: Route the page through the batches

This is the fix. The RPC-count test is the deliverable — it must fail on today's code.

**Files:**
- Modify: `server/api/sessions.py` (`_enrich_one` ~line 281, `list_sessions` ~line 400-434)
- Test: `server/tests/test_enrichment_batch_route.py` (create)

**Interfaces:**
- Consumes: Task 3's three client/facade methods.
- Produces: `_enrich_batch(orch, records, viewing, timeout) -> tuple[list, list, list, list]`
  returning `(repos, suggestions, prs, summaries)` positionally aligned to `records` — the same
  four sequences `list_sessions` already unpacks, so the response-building code below is unchanged.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_enrichment_batch_route.py`:

```python
"""The page makes three enrichment RPCs, not three per session (#742).

The count assertion is the whole point of the change; everything else here guards the
shape of what those three calls return.
"""
import pytest

from server.api.sessions import _enrich_batch
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec(sid):
    return SessionRecord(id=sid, name=sid, account="a", claude_session_id="",
                         cols=80, rows=24, cwd="/tmp/wt", repo="o/r", branch="b")


class _BatchOrch:
    def __init__(self):
        self.calls = []

    async def prs_for_many(self, session_ids, *, timeout=None):
        self.calls.append("prs_for_many")
        return {s: [PullRequestInfo(1, "u", "open", "none", f"t-{s}")] for s in session_ids}

    async def latest_suggestions_many(self, session_ids, *, timeout=None):
        self.calls.append("latest_suggestions_many")
        return {s: [f"s-{s}"] for s in session_ids}

    async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
        self.calls.append("latest_summary_many")
        return {s: f"sum-{s}" for s in session_ids}


@pytest.mark.asyncio
async def test_ten_sessions_cost_three_rpcs_not_thirty():
    orch = _BatchOrch()
    records = [_rec(f"s{i}") for i in range(10)]
    await _enrich_batch(orch, records, viewing=False, timeout=3.0)
    assert len(orch.calls) == 3, f"expected 3 batched RPCs, got {len(orch.calls)}"
    assert sorted(orch.calls) == ["latest_suggestions_many", "latest_summary_many",
                                  "prs_for_many"]


@pytest.mark.asyncio
async def test_results_align_positionally_with_records():
    orch = _BatchOrch()
    records = [_rec("a"), _rec("b")]
    _repos, suggs, prs, summaries = await _enrich_batch(
        orch, records, viewing=False, timeout=3.0)
    assert list(suggs) == [["s-a"], ["s-b"]]
    assert [p[0].title for p in prs] == ["t-a", "t-b"]
    assert list(summaries) == ["sum-a", "sum-b"]


@pytest.mark.asyncio
async def test_a_session_missing_from_the_reply_degrades_to_empty():
    class _Partial(_BatchOrch):
        async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
            self.calls.append("latest_summary_many")
            return {}  # daemon knows none of these ids

    _repos, _suggs, _prs, summaries = await _enrich_batch(
        _Partial(), [_rec("a")], viewing=False, timeout=3.0)
    assert list(summaries) == [""]


@pytest.mark.asyncio
async def test_a_failing_batch_rpc_degrades_that_field_only():
    class _Broken(_BatchOrch):
        async def latest_suggestions_many(self, session_ids, *, timeout=None):
            self.calls.append("latest_suggestions_many")
            raise RuntimeError("daemon down")

    _repos, suggs, prs, _summaries = await _enrich_batch(
        _Broken(), [_rec("a")], viewing=False, timeout=3.0)
    assert list(suggs) == [[]]
    assert [p[0].title for p in prs] == ["t-a"]  # the other two still landed
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_route.py -q`
Expected: FAIL — `ImportError: cannot import name '_enrich_batch'`

- [ ] **Step 3: Implement**

In `server/api/sessions.py`, add beside `_enrich_one` (keep `_enrich_one` — Task 5 uses it as
the fallback):

```python
async def _enrich_batch(orch, records, viewing: bool, timeout: float):
    """Fetch every session's enrichment in three batched RPCs instead of three per session.

    Returns (repos, suggestions, prs, summaries), each positionally aligned to *records*.

    A session id absent from a reply degrades to that field's empty value, and a whole
    batch that fails degrades only its own field — the other two still render (#742).

    Each RPC's deadline is pushed IN via call_with_deadline. Do NOT wrap these in
    asyncio.wait_for: it cancels the request, stranding the connection's pool slot and
    wedging the edge (#734).
    """
    ids = [r.id for r in records]
    if not ids:
        return (), (), (), ()

    async def _batch(name, **extra):
        fn = getattr(orch, name, None)
        if fn is None:
            return {}
        try:
            return await call_with_deadline(fn, ids, timeout=timeout, **extra) or {}
        except Exception:
            log.warning("list_sessions: %s failed for %d sessions", name, len(ids),
                        exc_info=True)
            return {}

    repos, sugg_map, pr_map, sum_map = await asyncio.gather(
        asyncio.gather(*(_resolve_repo(r) for r in records), return_exceptions=True),
        _batch("latest_suggestions_many"),
        _batch("prs_for_many"),
        _batch("latest_summary_many", viewing=viewing),
    )
    resolved_repos = [
        repo if not isinstance(repo, BaseException) else getattr(r, "repo", None)
        for r, repo in zip(records, repos)
    ]
    return (
        resolved_repos,
        [sugg_map.get(r.id) or [] for r in records],
        [pr_map.get(r.id) or [] for r in records],
        [sum_map.get(r.id) or "" for r in records],
    )
```

Then replace the fan-out in `list_sessions` — these four lines:

```python
    enriched = await asyncio.gather(
        *(_enrich_one(orch, r, viewing, ENRICH_FETCH_TIMEOUT) for r in records)
    )
    repos, suggs, prs, summaries = zip(*enriched) if enriched else ((), (), (), ())
```

with:

```python
    repos, suggs, prs, summaries = await _enrich_batch(
        orch, records, viewing, ENRICH_FETCH_TIMEOUT)
```

Update the comment above it — it currently describes per-session parallelism that no longer
happens:

```python
    # Enrich the whole page in three batched RPCs (one per field) rather than three per
    # session: 21 sessions cost 3 daemon round trips, not 63 (#742). Each is bounded by
    # ENRICH_FETCH_TIMEOUT, pushed INTO the RPC — never raced with wait_for (#734).
```

- [ ] **Step 4: Add the socket-orphan guard for the new path**

`server/tests/test_ipc_cancel_does_not_strand.py` already proves `_enrich_one` orphans no
sockets against a blackhole daemon (`test_enrich_one_does_not_orphan_daemon_sockets`, ~line
229). That test now guards only the fallback. The batched path needs its own, and the socket
count is where the fix shows up: three, regardless of session count.

Append to that file, beside the existing test:

```python
@pytest.mark.asyncio
async def test_enrich_batch_does_not_orphan_daemon_sockets(blackhole, blackhole_tls):
    """The batched path must clean up exactly as the per-session path does — and cost
    three sockets for the whole page rather than three per session (#742)."""
    from server.api.sessions import _enrich_batch

    client = blackhole.client(blackhole_tls)
    orch = RemoteOrchestrator(client)
    try:
        records = [_record(), _record()]
        repos, suggs, prs, summaries = await _enrich_batch(
            orch, records, viewing=False, timeout=_DEADLINE)
        assert list(suggs) == [[], []]
        assert list(prs) == [[], []]
        assert list(summaries) == ["", ""]
        # Three RPCs for two sessions — six would mean the batch did not take effect.
        await blackhole.settle(expect=3)
        blackhole.assert_no_orphans()
    finally:
        await client.aclose()
```

`_record()` takes no arguments in that file and returns a single fixed record; two calls
returning the same id is fine here, since the assertion is about socket count, not mapping.
If `_record()` requires a unique id for the batch to produce two entries, give it one.

- [ ] **Step 5: Run to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_route.py server/tests/test_ipc_cancel_does_not_strand.py server/tests/test_sessions_api_summary.py server/tests/test_sessions_pr_field.py -q`
Expected: PASS — new tests, the socket guards for both paths, and the existing enrichment
tests still green.

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_enrichment_batch_route.py \
        server/tests/test_ipc_cancel_does_not_strand.py
git commit -m "perf(sessions): three enrichment RPCs per page, not three per session

Refs #742"
```

---

### Task 5: Version-skew fallback

A new edge against an old daemon must still render. Observed 2026-08-22, when a half-failed
deploy left rocky on new code and dobby on old; #740 is still open.

**Files:**
- Modify: `server/api/sessions.py` (`_enrich_batch`, `_enrich_one`)
- Test: `server/tests/test_enrichment_batch_route.py` (extend)

**Interfaces:**
- Consumes: `server.ipc.client.DaemonError`, whose `.err_type` is `"UnknownMethod"` for a
  method the daemon does not register (`server/ipc/daemon.py:336`).
- Produces: no new public names.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_enrichment_batch_route.py`:

```python
@pytest.mark.asyncio
async def test_unknown_method_falls_back_to_per_session_calls():
    """An old daemon 404s the batched method; the page must still enrich (spec D3)."""
    from server.ipc.client import DaemonError

    class _OldDaemon(_BatchOrch):
        async def latest_suggestions_many(self, session_ids, *, timeout=None):
            self.calls.append("latest_suggestions_many")
            raise DaemonError("UnknownMethod", "latest_suggestions_many")

        async def latest_suggestions(self, session_id, *, timeout=None):
            self.calls.append("latest_suggestions")
            return [f"s-{session_id}"]

    orch = _OldDaemon()
    _repos, suggs, _prs, _summaries = await _enrich_batch(
        orch, [_rec("a"), _rec("b")], viewing=False, timeout=3.0)
    assert list(suggs) == [["s-a"], ["s-b"]]
    assert orch.calls.count("latest_suggestions") == 2


@pytest.mark.asyncio
async def test_a_normal_daemon_error_does_not_trigger_fallback():
    """Only UnknownMethod means 'old daemon'. Any other error degrades to empty —
    retrying every failure per session would restore the N+1 under load."""
    from server.ipc.client import DaemonError

    class _Erroring(_BatchOrch):
        async def latest_suggestions_many(self, session_ids, *, timeout=None):
            self.calls.append("latest_suggestions_many")
            raise DaemonError("InternalError", "boom")

        async def latest_suggestions(self, session_id, *, timeout=None):
            self.calls.append("latest_suggestions")
            return ["should not be reached"]

    orch = _Erroring()
    _repos, suggs, _prs, _summaries = await _enrich_batch(
        orch, [_rec("a")], viewing=False, timeout=3.0)
    assert list(suggs) == [[]]
    assert "latest_suggestions" not in orch.calls
```

- [ ] **Step 2: Run to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_route.py -q -k "unknown or normal_daemon"`
Expected: FAIL — the first test gets `[[], []]` because the DaemonError is swallowed.

- [ ] **Step 3: Implement**

Add a module-level import in `server/api/sessions.py`:

```python
from server.ipc.client import DaemonError
```

Replace `_batch`'s `except Exception` with an UnknownMethod-aware version:

```python
    async def _batch(name, singular, **extra):
        """One batched RPC, falling back to per-session calls only on UnknownMethod.

        UnknownMethod means the daemon predates this method — a version-skewed fleet,
        which #740 makes a live possibility. Any OTHER failure degrades to empty: retrying
        every error per session would restore the N+1 precisely when the daemon is
        struggling.

        Decided per request, not remembered. A sticky flag would be another piece of
        version state to invalidate and would pin the fleet to the slow path after the
        daemon caught up.
        """
        fn = getattr(orch, name, None)
        if fn is None:
            return await singular()
        try:
            return await call_with_deadline(fn, ids, timeout=timeout, **extra) or {}
        except DaemonError as exc:
            if exc.err_type != "UnknownMethod":
                log.warning("list_sessions: %s failed (%s)", name, exc.err_type)
                return {}
            log.info("list_sessions: daemon has no %s — falling back to per-session "
                     "calls (version skew, #742)", name)
            return await singular()
        except Exception:
            log.warning("list_sessions: %s failed for %d sessions", name, len(ids),
                        exc_info=True)
            return {}
```

Add the three per-session fallbacks and pass them in:

```python
    async def _each(fn_name, empty, **extra):
        """Per-session fallback: the pre-batch path, kept for version skew only."""
        fn = getattr(orch, fn_name, None)
        if fn is None:
            return {}
        async def _one(sid):
            try:
                return await call_with_deadline(fn, sid, timeout=timeout, **extra)
            except Exception:
                return empty
        vals = await asyncio.gather(*(_one(sid) for sid in ids))
        return dict(zip(ids, vals))

    repos, sugg_map, pr_map, sum_map = await asyncio.gather(
        asyncio.gather(*(_resolve_repo(r) for r in records), return_exceptions=True),
        _batch("latest_suggestions_many",
               lambda: _each("latest_suggestions", [])),
        _batch("prs_for_many",
               lambda: _each("prs_for", [])),
        _batch("latest_summary_many",
               lambda: _each("latest_summary", "", viewing=viewing), viewing=viewing),
    )
```

Mark `_enrich_one` deprecated (it is now unreferenced by the route but stays as the
documented skew path's ancestor — if the reviewer finds it genuinely unreferenced after
`_each` replaces it, DELETE it rather than marking it; a marker on dead code is worse than
no code):

```python
# @deprecated — superseded by _enrich_batch (#742). Remove once #740's health gate is fixed
# and edge/daemon version skew is no longer observed in a deploy; tracked in the follow-up
# issue filed with this change.
```

- [ ] **Step 4: Run to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/test_enrichment_batch_route.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full gate and commit**

```bash
.venv/bin/python -m ruff check src/ server/
.venv/bin/python -m mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/ -q
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 .venv/bin/python -m pytest server/tests/ -q
```

All four must be green before commit.

```bash
git add server/api/sessions.py server/tests/test_enrichment_batch_route.py
git commit -m "fix(sessions): fall back to per-session enrichment on daemon version skew

Refs #742"
```

---

## Verification

After Task 5, confirm the whole point of the change end to end:

- `pytest server/tests/ -q` fully green (no regressions in the ~3600-test suite).
- `mypy server/ --ignore-missing-imports` clean — this gate caught what no test did on the
  previous branch.
- The RPC-count test in Task 4 genuinely fails when `_enrich_batch` is reverted to the
  per-session fan-out. Verify by reverting locally, watching it fail, and restoring.

## Follow-ups to file (not part of this plan)

- Remove `_each` / the fallback path once #740 is fixed and skew is no longer observed.
- The remaining ~24 unbatched calls (`list_sessions`, `session_queue_positions`) — a
  different shape, deliberately out of scope.
