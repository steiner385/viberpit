# Resume Interrupted Sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator resume daemon-restart-interrupted sessions in one click (batch) and, opt-in, auto-resume them on daemon startup — both over one shared orchestrator primitive.

**Architecture:** Add one `resume_interrupted()` primitive (the autopilot `ensure_driver` + `send_prompt` nudge, extracted) plus one `_resume_eligible()` predicate. Expose it via a `POST /api/sessions/batch/resume` endpoint (mirroring `batch/archive`) with a frontend "Resume interrupted (N)" button, and via a flag-gated startup pass folded into the existing rearm phase. Eligibility is restricted to genuinely mid-turn work states, recovered via a new transient `interrupted_from_status` field that `rehydrate()` now captures.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (async) backend; pytest. React + TS + TanStack Query + Tailwind frontend; vitest + React Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-02-resume-interrupted-sessions-design.md`

## Global Constraints

- **Single source of truth (Principle VI):** exactly one nudge-text constant, one eligibility predicate, one eligible-status set — both entry points route through them.
- **Nudge text (verbatim):** `"Your previous turn was interrupted by a daemon restart. Please continue where you left off."`
- **Eligible statuses (verbatim):** `{"in_progress", "working", "condensing"}` — excludes `waiting_input`, `waiting_question`, `ready_review` (which normalize to idle but are not resumable work), and inherently excludes `limited`/`waiting_external` (never flagged interrupted).
- **Auto-resume env flag:** `CCSWITCH_AUTO_RESUME_ON_RESTART`, default **off**. Truthy = `{"1","true","yes","on"}` (case-insensitive).
- **Concurrency reuse:** the auto pass uses `CCSWITCH_REARM_CONCURRENCY` (default 3), same as `rearm_background_tasks`.
- **`interrupted_from_status` is transient** — memory + RPC round-trip only (mirrors `interrupted_by_restart`); NOT a DB column.
- **Four-step gate before pushing:** `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Web: `vitest run`, `tsc --noEmit`, `eslint` (run in `web/`).
- **Env note:** run server tests against the prebuilt venv per CLAUDE.md; API auth tests need `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1`.

---

### Task 1: Capture original status + expose resumability

Plumb a transient `interrupted_from_status` through the record, capture it in `rehydrate()`, clear it with the existing flag, and add the eligible-status set + a computed `resumable` wire field so the frontend can count honestly.

**Files:**
- Modify: `server/sessions/types.py` — add field, `RESUME_ELIGIBLE_STATUSES`, `to_dict`/`from_dict` entries.
- Modify: `server/sessions/orchestrator.py:5834-5861` (capture) and `:2169-2170` (clear).
- Test: `server/tests/test_resume_interrupted.py` (new).

**Interfaces:**
- Produces: `SessionRecord.interrupted_from_status: str | None`; `RESUME_ELIGIBLE_STATUSES: frozenset[str]` (in `server/sessions/types.py`); wire key `"resumable": bool` in `to_dict()`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_resume_interrupted.py`:

```python
from server.sessions.types import SessionRecord, RESUME_ELIGIBLE_STATUSES


def _rec(**kw) -> SessionRecord:
    base = dict(id="s1", name="n", account="a", claude_session_id="c",
                cols=80, rows=24, cwd="/tmp", repo=None)
    base.update(kw)
    return SessionRecord(**base)


def test_eligible_statuses_are_work_states_only():
    assert RESUME_ELIGIBLE_STATUSES == frozenset({"in_progress", "working", "condensing"})


def test_resumable_true_only_for_flagged_work_state():
    r = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    assert r.to_dict()["resumable"] is True


def test_resumable_false_when_from_status_not_work():
    r = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="ready_review")
    assert r.to_dict()["resumable"] is False


def test_resumable_false_without_flag():
    r = _rec(status="in_progress", interrupted_by_restart=False, interrupted_from_status="in_progress")
    assert r.to_dict()["resumable"] is False


def test_interrupted_from_status_round_trips():
    r = _rec(interrupted_by_restart=True, interrupted_from_status="working")
    assert SessionRecord.from_dict(r.to_dict()).interrupted_from_status == "working"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_resume_interrupted.py -v`
Expected: FAIL — `ImportError: cannot import name 'RESUME_ELIGIBLE_STATUSES'`.

- [ ] **Step 3: Add the field, set, and wire mapping**

In `server/sessions/types.py`, near `SYSTEM_IDENTITY` (top of module) add:

```python
# Original pre-restart status is normalized to "idle" by rehydrate(); these are the
# subset that represents genuinely dropped WORK (vs awaiting-human waiting_input /
# waiting_question / ready_review, which normalize identically but must NOT be nudged).
RESUME_ELIGIBLE_STATUSES = frozenset({"in_progress", "working", "condensing"})
```

Add the transient field beside `interrupted_by_restart` (after line 102):

```python
    # transient companion to interrupted_by_restart: the ORIGINAL pre-restart status
    # (before rehydrate normalized it to "idle"). Drives resume-eligibility only — a
    # session is resumable iff it was flagged AND this is in RESUME_ELIGIBLE_STATUSES.
    # Never persisted; cleared with the flag on the next turn.
    interrupted_from_status: str | None = None
```

In `to_dict()` (after the `"interruptedByRestart"` entry, line 167) add:

```python
            "interruptedFromStatus": self.interrupted_from_status,
            "resumable": (self.interrupted_by_restart
                          and self.interrupted_from_status in RESUME_ELIGIBLE_STATUSES),
```

In `from_dict()` (after the `interrupted_by_restart=` entry, line 206) add:

```python
            interrupted_from_status=d.get("interruptedFromStatus"),
```

- [ ] **Step 4: Capture it in rehydrate and clear it on next turn**

In `server/sessions/orchestrator.py`, change the `_record_from_row` call at line 5860-5861 to also thread the original status when interrupted:

```python
            self._records[row.id] = self._record_from_row(
                row, status=status, branch=branch,
                interrupted_by_restart=was_interrupted,
                interrupted_from_status=(row.status if was_interrupted else None))
```

In `_persist_status`, extend the existing flag-clear at lines 2169-2170:

```python
            if status != "idle":
                rec.interrupted_by_restart = False
                rec.interrupted_from_status = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_resume_interrupted.py -v`
Expected: PASS (all 5).

- [ ] **Step 6: Commit**

```bash
git add server/sessions/types.py server/sessions/orchestrator.py server/tests/test_resume_interrupted.py
git commit -m "feat(sessions): capture interrupted_from_status + resumable wire field"
```

---

### Task 2: The `resume_interrupted` primitive + eligibility predicate

Extract the "re-attach + nudge" pattern into one orchestrator method both entry points call.

**Files:**
- Modify: `server/sessions/orchestrator.py` (add two methods + a module nudge constant near the class).
- Test: `server/tests/test_resume_interrupted.py` (extend).

**Interfaces:**
- Consumes: `RESUME_ELIGIBLE_STATUSES`, `SessionRecord.interrupted_from_status` (Task 1); existing `ensure_driver(session_id, identity) -> SessionDriver | None`, `send_prompt(session_id, text) -> str` (returns `"sent"`/`"no_driver"`/`"draining"`), `SYSTEM_IDENTITY`.
- Produces:
  - `SessionOrchestrator._resume_eligible(rec: SessionRecord) -> str | None` — returns a skip-reason string when ineligible, `None` when eligible.
  - `SessionOrchestrator.resume_interrupted(session_id: str, identity: str = SYSTEM_IDENTITY) -> str` — returns `"resumed"`, `"skipped:<reason>"`, or `"no_driver"`.
  - Module constant `RESUME_NUDGE: str`.

- [ ] **Step 1: Write the failing test**

Extend `server/tests/test_resume_interrupted.py`. Use a lightweight fake orchestrator that reuses the real methods via `types.SimpleNamespace`-style binding:

```python
import pytest
from server.sessions.orchestrator import SessionOrchestrator, RESUME_NUDGE


class _FakeDriver: ...


def _orch_with(rec):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {rec.id: rec} if rec else {}
    orch._drivers = {}
    calls = {"ensure": [], "prompt": []}

    async def ensure_driver(sid, identity="system"):
        calls["ensure"].append((sid, identity))
        d = _FakeDriver()
        orch._drivers[sid] = d
        return d

    async def send_prompt(sid, text, attachment_ids=None):
        calls["prompt"].append((sid, text))
        return "sent"

    orch.ensure_driver = ensure_driver          # type: ignore[assignment]
    orch.send_prompt = send_prompt               # type: ignore[assignment]
    return orch, calls


@pytest.mark.asyncio
async def test_resume_interrupted_eligible_nudges():
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, calls = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == "resumed"
    assert calls["ensure"] == [("s1", "system")]
    assert calls["prompt"] == [("s1", RESUME_NUDGE)]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_status,reason", [
    ("ready_review", "not-resumable-status"),
    ("waiting_question", "not-resumable-status"),
    ("waiting_input", "not-resumable-status"),
])
async def test_resume_interrupted_skips_non_work(from_status, reason):
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status=from_status)
    orch, calls = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == f"skipped:{reason}"
    assert calls["ensure"] == [] and calls["prompt"] == []


@pytest.mark.asyncio
async def test_resume_interrupted_skips_unflagged():
    rec = _rec(status="idle", interrupted_by_restart=False, interrupted_from_status=None)
    orch, _ = _orch_with(rec)
    assert await orch.resume_interrupted("s1") == "skipped:not-interrupted"


@pytest.mark.asyncio
async def test_resume_interrupted_unknown_session():
    orch, _ = _orch_with(None)
    assert await orch.resume_interrupted("nope") == "skipped:not-found"


@pytest.mark.asyncio
async def test_resume_interrupted_no_driver_propagates():
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="working")
    orch, _ = _orch_with(rec)
    async def ensure_none(sid, identity="system"): return None
    orch.ensure_driver = ensure_none             # type: ignore[assignment]
    assert await orch.resume_interrupted("s1") == "no_driver"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_resume_interrupted.py -k resume_interrupted -v`
Expected: FAIL — `ImportError: cannot import name 'RESUME_NUDGE'`.

- [ ] **Step 3: Implement the primitive**

In `server/sessions/orchestrator.py`, add the nudge constant near the other module constants (e.g. after `_TURN_DROPPED_GIVEUP`, ~line 113), importing the eligible set:

```python
from .types import RESUME_ELIGIBLE_STATUSES  # add to existing `from .types import ...`

# Sent to a session whose in-flight turn was dropped by a daemon restart, to continue
# the work. One text for human + automation sessions (design 2026-09-02).
RESUME_NUDGE = ("Your previous turn was interrupted by a daemon restart. "
                "Please continue where you left off.")
```

Add both methods to the `SessionOrchestrator` class (near `ensure_driver`, ~line 6859):

```python
    def _resume_eligible(self, rec: "SessionRecord") -> str | None:
        """Skip-reason when a session is NOT resume-eligible, else None. Single gate
        shared by the batch endpoint, the auto pass, and resume_interrupted itself."""
        if not rec.interrupted_by_restart:
            return "not-interrupted"
        if rec.interrupted_from_status not in RESUME_ELIGIBLE_STATUSES:
            return "not-resumable-status"
        return None

    async def resume_interrupted(self, session_id: str,
                                 identity: str = SYSTEM_IDENTITY) -> str:
        """Re-attach a restart-interrupted session and nudge it to continue.
        Returns "resumed" | "skipped:<reason>" | "no_driver". Idempotent: a
        session that already resumed fails _resume_eligible ("not-interrupted")."""
        rec = self._records.get(session_id)
        if rec is None:
            return "skipped:not-found"
        reason = self._resume_eligible(rec)
        if reason is not None:
            return f"skipped:{reason}"
        if await self.ensure_driver(session_id, identity) is None:
            return "no_driver"
        result = await self.send_prompt(session_id, RESUME_NUDGE)
        return "resumed" if result == "sent" else result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_resume_interrupted.py -k resume_interrupted -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_resume_interrupted.py
git commit -m "feat(sessions): resume_interrupted primitive + eligibility predicate"
```

---

### Task 3: Batch resume endpoint

Expose the primitive as `POST /api/sessions/batch/resume`, mirroring `batch/archive`.

**Files:**
- Modify: `server/api/sessions.py` (add route after `batch_archive_sessions`, before `@router.get("/{session_id}")` at line 844 — must stay above the catch-all route).
- Test: `server/tests/test_sessions_batch_resume.py` (new).

**Interfaces:**
- Consumes: `BatchSessionIdsRequest` (line 744), `orch.resume_interrupted` (Task 2), `_get_orchestrator`, `record_audit`, `current_identity`, `get_db`.
- Produces: `POST /api/sessions/batch/resume` → `{"succeeded": [id...], "failed": [{"id","error"}...]}`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_sessions_batch_resume.py`. Follow the existing batch-archive test's app/orchestrator fixtures (copy its `client` + fake-orchestrator setup from `server/tests/test_sessions_batch.py` if present; otherwise use the standard `create_app` TestClient fixture used across `server/tests/`). Core assertions:

```python
def test_batch_resume_partitions_by_outcome(client, fake_orch):
    # fake_orch.resume_interrupted returns "resumed" for s1, "skipped:not-interrupted" for s2
    fake_orch.resume_map = {"s1": "resumed", "s2": "skipped:not-interrupted"}
    resp = client.post("/api/sessions/batch/resume", json={"ids": ["s1", "s2", "s1"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["succeeded"] == ["s1"]              # deduped, only resumed
    assert body["failed"] == [{"id": "s2", "error": "skipped:not-interrupted"}]


def test_batch_resume_reports_no_driver_as_failure(client, fake_orch):
    fake_orch.resume_map = {"s1": "no_driver"}
    body = client.post("/api/sessions/batch/resume", json={"ids": ["s1"]}).json()
    assert body["succeeded"] == []
    assert body["failed"] == [{"id": "s1", "error": "no_driver"}]
```

The fake orchestrator's `resume_interrupted(sid, identity)` returns `self.resume_map.get(sid, "skipped:not-found")`.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_batch_resume.py -v`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Implement the endpoint**

In `server/api/sessions.py`, after `batch_archive_sessions` (line 841):

```python
@router.post("/batch/resume")
async def batch_resume_sessions(
    body: BatchSessionIdsRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Resume restart-interrupted sessions (re-attach + nudge). Best-effort:
    a resumed session lands in `succeeded`; a skip / no_driver lands in `failed`."""
    orch = _get_orchestrator(request)
    succeeded: list[str] = []
    failed: list[dict] = []
    for session_id in dict.fromkeys(body.ids):  # dedupe, preserve order
        try:
            result = await orch.resume_interrupted(session_id, identity)
        except Exception as exc:  # noqa: BLE001 — best-effort batch must not abort
            log.warning("batch resume failed for %s: %s", session_id, exc)
            failed.append({"id": session_id, "error": str(exc)})
            continue
        if result == "resumed":
            succeeded.append(session_id)
            await record_audit(
                db=db, type="session.resume", identity=identity,
                detail={"session_id": session_id},
            )
        else:
            failed.append({"id": session_id, "error": result})
    return {"succeeded": succeeded, "failed": failed}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_batch_resume.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_batch_resume.py
git commit -m "feat(api): POST /sessions/batch/resume — batch resume interrupted"
```

---

### Task 4: Opt-in auto-resume on restart

After rehydrate + rearm, resume every eligible session — gated by `CCSWITCH_AUTO_RESUME_ON_RESTART`, concurrency-bounded like rearm.

**Files:**
- Modify: `server/sessions/orchestrator.py` (add `auto_resume_interrupted()` near `rearm_background_tasks`, ~line 5941).
- Modify: `server/ipc/serve.py:116-120` (call it after rearm).
- Test: `server/tests/test_resume_interrupted.py` (extend).

**Interfaces:**
- Consumes: `resume_interrupted`, `_resume_eligible` (Task 2); `self._records`; `CCSWITCH_AUTO_RESUME_ON_RESTART`, `CCSWITCH_REARM_CONCURRENCY`.
- Produces: `SessionOrchestrator.auto_resume_interrupted() -> int` (count resumed; `0` when flag off).

- [ ] **Step 1: Write the failing test**

Extend `server/tests/test_resume_interrupted.py`:

```python
@pytest.mark.asyncio
async def test_auto_resume_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_AUTO_RESUME_ON_RESTART", raising=False)
    rec = _rec(status="idle", interrupted_by_restart=True, interrupted_from_status="in_progress")
    orch, calls = _orch_with(rec)
    assert await orch.auto_resume_interrupted() == 0
    assert calls["prompt"] == []


@pytest.mark.asyncio
async def test_auto_resume_resumes_eligible_when_enabled(monkeypatch):
    monkeypatch.setenv("CCSWITCH_AUTO_RESUME_ON_RESTART", "true")
    eligible = _rec(id="s1", status="idle", interrupted_by_restart=True,
                    interrupted_from_status="working")
    parked = _rec(id="s2", status="idle", interrupted_by_restart=True,
                  interrupted_from_status="ready_review")
    orch, calls = _orch_with(eligible)
    orch._records["s2"] = parked
    assert await orch.auto_resume_interrupted() == 1
    assert [sid for sid, _ in calls["prompt"]] == ["s1"]   # s2 skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_resume_interrupted.py -k auto_resume -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'auto_resume_interrupted'`.

- [ ] **Step 3: Implement the auto pass**

Add a truthy helper + the method in `server/sessions/orchestrator.py`:

```python
    @staticmethod
    def _env_truthy(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    async def auto_resume_interrupted(self) -> int:
        """Opt-in: resume every eligible restart-interrupted session on startup.
        Off unless CCSWITCH_AUTO_RESUME_ON_RESTART is truthy. Bounded by
        CCSWITCH_REARM_CONCURRENCY. Run AFTER rearm so replayed pending_prompt
        sessions are already un-interrupted and skip naturally."""
        if not self._env_truthy("CCSWITCH_AUTO_RESUME_ON_RESTART"):
            return 0
        eligible = [sid for sid, rec in list(self._records.items())
                    if self._resume_eligible(rec) is None]
        if not eligible:
            return 0
        sem = asyncio.Semaphore(int(os.environ.get("CCSWITCH_REARM_CONCURRENCY", "3")))

        async def _one(sid: str) -> bool:
            async with sem:
                try:
                    return await self.resume_interrupted(sid, SYSTEM_IDENTITY) == "resumed"
                except Exception:
                    log.warning("auto-resume: session %s failed", sid, exc_info=True)
                    return False

        results = await asyncio.gather(*(_one(sid) for sid in eligible))
        n = sum(1 for ok in results if ok)
        log.info("auto-resume: resumed %d/%d interrupted session(s)", n, len(eligible))
        return n
```

- [ ] **Step 4: Wire it into the lifespan**

In `server/ipc/serve.py`, after the `rearm_background_tasks()` try/except block (line 118), add:

```python
    try:
        await orchestrator.auto_resume_interrupted()
    except Exception:
        log.error("daemon auto_resume_interrupted() failed", exc_info=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `<main-checkout>/.venv/bin/python -m pytest server/tests/test_resume_interrupted.py -k auto_resume -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/orchestrator.py server/ipc/serve.py server/tests/test_resume_interrupted.py
git commit -m "feat(sessions): opt-in auto-resume interrupted sessions on restart"
```

---

### Task 5: Frontend — Resume interrupted button

Add the `resumable` field to the client type, a `useResumeInterrupted` hook, and a "Resume interrupted (N)" button beside "Archive done".

**Files:**
- Modify: `web/src/api/types.ts:127-129` (add `resumable`).
- Modify: `web/src/ws/client.ts:524-540` (clear `resumable` when a status patch un-interrupts).
- Modify: `web/src/api/hooks.ts` (add `useResumeInterrupted` after `useBulkArchiveSessions`, ~line 690).
- Modify: `web/src/components/SessionList.tsx` (count + button + handler).
- Test: `web/src/api/__tests__/useResumeInterrupted.test.ts` (new) and extend `web/src/components/__tests__/SessionList.test.tsx` (or the nearest existing SessionList test).

**Interfaces:**
- Consumes: `BatchResult` (`web/src/api/types.ts:154`), `Session`, `useBulkArchiveSessions` pattern.
- Produces: `Session.resumable?: boolean`; `useResumeInterrupted()` mutation posting `string[]` to `/sessions/batch/resume`.

- [ ] **Step 1: Write the failing test**

Create `web/src/api/__tests__/useResumeInterrupted.test.ts` mirroring the bulk-archive hook test (same `renderHook` + mocked `api.post` harness the repo uses). Assert it posts the id list to the right path:

```ts
it('posts the eligible id list to /sessions/batch/resume', async () => {
  const post = vi.spyOn(api, 'post').mockResolvedValue({ succeeded: ['s1'], failed: [] })
  const { result } = renderHook(() => useResumeInterrupted(), { wrapper })
  await act(async () => { await result.current.mutateAsync(['s1', 's2']) })
  expect(post).toHaveBeenCalledWith('/sessions/batch/resume', { ids: ['s1', 's2'] })
})
```

Add to the SessionList test a case: given two sessions with `resumable: true` and one without, the "Resume interrupted (2)" button renders; with zero resumable, it does not render.

- [ ] **Step 2: Run tests to verify they fail**

Run (in `web/`): `npx vitest run src/api/__tests__/useResumeInterrupted.test.ts`
Expected: FAIL — `useResumeInterrupted` is not exported.

- [ ] **Step 3: Add the type + hook**

In `web/src/api/types.ts`, in the `Session` interface beside `interruptedByRestart?` (line 129):

```ts
  /** Server-computed: this session was interrupted mid-work by a restart and can be resumed. */
  resumable?: boolean
```

In `web/src/api/hooks.ts`, after `useBulkArchiveSessions` (line 663):

```ts
export function useResumeInterrupted() {
  const qc = useQueryClient()
  return useMutation<BatchResult, ApiRequestError, string[]>({
    mutationFn: (ids) => api.post<BatchResult>('/sessions/batch/resume', { ids }),
    onSuccess: (result) => {
      const n = result.succeeded.length
      const f = result.failed.length
      if (f > 0) toast.error(`Resumed ${n} · ${f} skipped`)
      else toast.success(`Resumed ${n}`)
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}
```

- [ ] **Step 4: Keep the live count honest**

In `web/src/ws/client.ts` `_patchSessionStatus` (line 524), when the patch clears the interrupted flag, also clear `resumable` so a session that resumes on its own drops off the count without a refetch. In the object spread at line 539, extend the existing conditional:

```ts
                 ...(interruptedByRestart !== undefined
                     ? { interruptedByRestart, ...(interruptedByRestart ? {} : { resumable: false }) }
                     : {}) }
```

- [ ] **Step 5: Add the button + handler in SessionList**

In `web/src/components/SessionList.tsx`:

Near `const bulkArchive = useBulkArchiveSessions()` (line 152) add:

```ts
  const resumeInterrupted = useResumeInterrupted()
```

Near `archivableDoneCount` (line 217) add:

```ts
  const resumableIds = sessions.filter((s) => s.resumable).map((s) => s.id)
  const handleResumeInterrupted = () => {
    if (resumableIds.length > 0) resumeInterrupted.mutate(resumableIds)
  }
```

Add the import of `useResumeInterrupted` to the existing hooks import (line 4) and `RotateCcw` to the `lucide-react` import. Then, in the toolbar `<div className="flex items-center justify-end gap-2">` (line 301), before the "Archive done" button (line 302):

```tsx
        {!selectMode && resumableIds.length > 0 && (
          <button
            type="button"
            onClick={handleResumeInterrupted}
            disabled={resumeInterrupted.isPending}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 py-1"
          >
            <RotateCcw size={12} aria-hidden="true" />
            Resume interrupted ({resumableIds.length})
          </button>
        )}
```

- [ ] **Step 6: Run tests to verify they pass**

Run (in `web/`): `npx vitest run src/api/__tests__/useResumeInterrupted.test.ts src/components/__tests__/SessionList.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/ws/client.ts web/src/components/SessionList.tsx web/src/api/__tests__/useResumeInterrupted.test.ts web/src/components/__tests__/SessionList.test.tsx
git commit -m "feat(web): Resume interrupted (N) button + useResumeInterrupted hook"
```

---

### Task 6: Full-gate verification

**Files:** none (verification only).

- [ ] **Step 1: Server gate (four steps)**

Run from repo root against the prebuilt venv:

```bash
<main-checkout>/.venv/bin/python -m ruff check src/ server/
<main-checkout>/.venv/bin/python -m mypy server/ --ignore-missing-imports
<main-checkout>/.venv/bin/python -m pytest tests/
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/
```

Expected: all green. (mypy in particular must pass — it catches type slips no test does.)

- [ ] **Step 2: Web gate**

Run in `web/`:

```bash
npx vitest run
npx tsc --noEmit
npx eslint .
```

Expected: all green.

- [ ] **Step 3: Open the PR**

Branch is already `resume-interrupted-sessions` off `origin/master`. Push, open a **non-draft** PR with `gh pr create` (body ends with the Claude Code trailer; include a one-line note that auto-resume is opt-in via `CCSWITCH_AUTO_RESUME_ON_RESTART`), then arm auto-merge:

```bash
gh pr merge <#> --squash --auto
```

## Self-Review

**Spec coverage:**
- §"Shared primitive" → Task 2 (`resume_interrupted` + `_resume_eligible`, `RESUME_NUDGE`). ✓
- §"Batch entry point" (endpoint + hook + button) → Task 3 (endpoint) + Task 5 (hook/button). ✓
- §"Auto entry point" (flag, folded after rearm, rearm concurrency) → Task 4. ✓
- §"Eligibility" (work states, exclusions, honest count) → Task 1 (set + `resumable`) + Task 2 (predicate). ✓
- §"Capturing the original status" (transient `interrupted_from_status`) → Task 1. ✓
- §"No double-drive / idempotent batch" → covered by `_resume_eligible` returning `not-interrupted` post-resume (Task 2 test `test_resume_interrupted_skips_unflagged`) and auto pass running after rearm (Task 4). ✓
- §Testing matrix → Tasks 1-5 tests. ✓

**Placeholder scan:** No TBD/TODO; every code + test step carries real content. ✓

**Type consistency:** `resume_interrupted` / `_resume_eligible` / `auto_resume_interrupted` / `RESUME_NUDGE` / `RESUME_ELIGIBLE_STATUSES` / `interrupted_from_status` / `resumable` used identically across tasks. Return strings (`"resumed"`, `"skipped:<reason>"`, `"no_driver"`) consistent between primitive (Task 2), endpoint (Task 3), and auto pass (Task 4). ✓
