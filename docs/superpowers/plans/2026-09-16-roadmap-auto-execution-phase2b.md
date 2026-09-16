# Roadmap auto-execution phase 2b — low-risk gates, the unfinished half

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish spec §13 phase 2 — arm GitHub auto-merge on stage artifact PRs, apply the
unattended model floor at spawn, and show the gate table on the product card — so a product at
`balanced` carries an initiative from spec through merge without a human tap.

**Architecture:** Phase 2a already executes `Advance` through a compare-and-set seam
(`orchestrator._sdlc_advance_stage`). This phase adds the *other* low-risk gate the engine already
decides but nobody executes: `MergeArtifact`. The loop gains one injected seam
`arm_auto_merge(repo, pr_number)` that reuses the shipped `SessionOrchestrator.ready_merge`
(the Plotroom "Ready + auto-merge" action) rather than writing a second `gh pr merge` call, and
stamps `InitiativeArtifact.auto_merge_armed_at` so a tick never re-runs `gh` for the same PR.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy (SQLite dev/tests, Postgres prod) · pytest ·
React + TypeScript + Vitest · `gh` CLI through `ccswitch.gitrepos._run_gh`

**Spec:** `docs/superpowers/specs/2026-09-14-roadmap-auto-execution-design.md` (§4 gates, §5
engine, §6 loop + "Arming auto-merge", §8 recording, §9b unattended model floor, §11 UI, §13
phasing). Phase 1 plan: `docs/superpowers/plans/2026-09-14-roadmap-auto-execution-phase1.md`.

## Global Constraints

- **Risk vocabulary is fixed:** `RISK_TOLERANCES = ("cautious", "balanced", "bold")`;
  `_CEILING = {"cautious": None, "balanced": "low", "bold": "medium"}`. `high` is never auto.
- **`artifact.merge` is risk `low`** — it resolves under `balanced` and `bold`, never `cautious`.
- **Kill switch:** `VIBERPIT_GATE_AUTORESOLVE_ENABLED` (settings key `gate_autoresolve_enabled`,
  default `true`), read **per tick** through the injected `autoresolve_enabled` seam. An unwired
  seam is fail-closed (`cautious`).
- **New settings take the `VIBERPIT_` prefix.** A new `CCSWITCH_*` env var trips the brand
  ratchet's env-prefix ceiling. This phase adds no new env var.
- **DB clock rule:** never `func.now()`. Use `server/db/clock.py:db_utc_now()`. Every column is
  naive UTC.
- **New columns must be nullable** so `_reconcile_columns` adds them on both dialects. A NOT NULL
  column needs a real migration and a `RECONCILE_EXEMPT` entry.
- **Migration number:** `0048`. Verified free on `origin/master` (newest is
  `0047_roadmap_auto_execution`) with no open PR claiming it. **Re-check immediately before
  merging** — two branches sharing a number merge with zero git conflict and leave alembic two
  heads, turning master red (`test_migration_chain_is_linear_with_exactly_one_head`).
- **No direct `--merge` fallback.** If `gh` refuses to arm auto-merge, record the refusal and
  leave the artifact awaiting a human. Merging with no CI gate is not low risk.
- **Never add a second `gh pr merge` implementation.** `ready_merge` is the one writer.
- **The `lint-and-test` gate is four steps:** `ruff check src/ server/`,
  `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`.
- **Run server tests with `PYTHONPATH=$PWD/src`** — the shared venv otherwise imports `ccswitch`
  from the deploy worktree, so edits under `src/ccswitch/` are silently ignored.

---

## File Structure

| File | Responsibility |
|---|---|
| `server/db/models.py` | + `InitiativeArtifact.auto_merge_armed_at` (nullable datetime) |
| `server/db/migrations/versions/0048_artifact_auto_merge_armed.py` | reviewable record of that column |
| `server/sessions/orchestrator.py` | `ready_merge` surfaces `error`; new `_sdlc_arm_auto_merge` seam; wire it into the loop; §9b floor at `_sdlc_spawn_synth` / `_sdlc_spawn_materialize` |
| `server/automations/sdlc_synthesizer.py` | the `MergeArtifact` branch: call the seam, stamp, record, count |
| `server/sdlc/gates.py` | `_summary` already covers `artifact.merge`; no change expected |
| `web/src/features/roadmap/ProductCard.tsx` (or the product detail pane) | the D10 gate table |
| `server/tests/test_sdlc_synthesizer_loop.py` | loop-branch tests |
| `server/tests/test_sdlc_wiring.py` | seam-wiring test |
| `server/tests/test_orchestrator_ready_merge.py` | `ready_merge` error passthrough |

---

## Task 1: Verify what we actually depend on in `gh` (Principle III)

Spec §6 says the refusal wording is "verified live in the plan's first task". Constitution
Principle III forbids designing against assumptions about a real binary.

**The honest resolution:** do **not** depend on refusal wording at all. `ready_merge` already
string-matches exactly one phrase — `"already enabled"` — and that match is load-bearing in
shipped production code today. Everything else is captured verbatim and surfaced, never parsed.
So this task verifies the two facts the design rests on and explicitly records that no new string
match is introduced.

**Files:**
- Modify: none (investigation task; findings go into the PR body and the docstring in Task 3)

- [ ] **Step 1: Confirm `allow_auto_merge` per repo (read-only)**

```bash
for r in steiner385/viberpit cairnea/KinDash; do
  echo -n "$r: "; gh api "repos/$r" --jq '{allow_auto_merge, allow_update_branch, squash_merge_allowed}' 2>&1
done
```

Expected: `viberpit` reports `allow_auto_merge: true` (auto-merge is the repo's documented
completion workflow). Record the actual values. A repo with `allow_auto_merge: false` is the case
that produces a refusal.

**Do NOT run `gh pr merge --auto` against a live PR to provoke a refusal** — that is a real
mutation on someone's PR. The refusal path is covered by a fake seam in tests instead.

- [ ] **Step 2: Confirm the one string match is already shipped**

```bash
grep -n "already enabled" server/sessions/orchestrator.py
```

Expected: the existing `ready_merge` comment + match at ~line 12320. This is pre-existing
behaviour this phase reuses, not a new dependency.

- [ ] **Step 3: Record the finding**

Write one paragraph for the PR body: which repos allow auto-merge, that refusal text is captured
verbatim and never parsed, and that a refusal does **not** stamp `auto_merge_armed_at` so the next
tick retries (self-healing for a transient `gh` failure).

- [ ] **Step 4: Commit**

No code change. Skip the commit; carry the finding into Task 3's docstring and the PR body.

---

## Task 2: `auto_merge_armed_at` on the artifact row

**Files:**
- Modify: `server/db/models.py:1361-1385` (`InitiativeArtifact`)
- Create: `server/db/migrations/versions/0048_artifact_auto_merge_armed.py`
- Test: `server/tests/test_sdlc_synthesizer_loop.py` (column presence is exercised via Task 4)

**Interfaces:**
- Produces: `InitiativeArtifact.auto_merge_armed_at: datetime | None` — set once the loop has
  armed auto-merge on that artifact's PR. `None` means "never armed"; the loop uses it as the
  idempotence key so `gh` runs at most once per PR.

- [ ] **Step 1: Add the column**

In `server/db/models.py`, inside `InitiativeArtifact`, directly after the `model` column:

```python
    #: When the synthesizer armed GitHub auto-merge on ``pr_number`` (spec
    #: 2026-09-14 §6). Nullable so ``_reconcile_columns`` adds it on both
    #: dialects; its ONLY job is idempotence — a set value means the loop has
    #: already spent its one ``gh`` call on this PR and must never re-run it.
    #: A refusal deliberately leaves it None so the next tick retries.
    auto_merge_armed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None)
```

- [ ] **Step 2: Write the migration**

Create `server/db/migrations/versions/0048_artifact_auto_merge_armed.py`:

```python
"""Artifact auto-merge arming stamp (roadmap auto-execution phase 2b).

``initiative_artifacts.auto_merge_armed_at`` (nullable datetime) records that the
SDLC synthesizer armed GitHub auto-merge on this artifact's PR, so a later tick
never spends a second ``gh`` call on it.

Nothing runs ``alembic upgrade`` at deploy time: ``init_db``'s
``_reconcile_columns`` ALTER-ADDs the nullable column on both dialects. This
revision is the reviewable record of that change.
"""
from alembic import op
import sqlalchemy as sa

revision = "0048_artifact_auto_merge_armed"
down_revision = "0047_roadmap_auto_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("initiative_artifacts",
                  sa.Column("auto_merge_armed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("initiative_artifacts", "auto_merge_armed_at")
```

- [ ] **Step 3: Verify the chain still has exactly one head**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/ -k migration_chain -q`
Expected: PASS — one head, `0048` chained off `0047`.

- [ ] **Step 4: Verify reconcile coverage**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_schema_reconcile_coverage.py -q`
Expected: PASS with no new `RECONCILE_EXEMPT` entry — the column is nullable, so reconcile
synthesizes it.

- [ ] **Step 5: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0048_artifact_auto_merge_armed.py
git commit -m "feat(db): record when the synthesizer armed auto-merge on an artifact PR"
```

---

## Task 3: `arm_auto_merge` seam — reuse `ready_merge`, surface the refusal

`ready_merge` already does `gh pr ready` + `gh pr merge --squash --auto` and tolerates
already-ready / already-armed. It discards `stderr`, which §6 needs for `gate_refused`. Extend it
additively — do not write a second merge path.

**Files:**
- Modify: `server/sessions/orchestrator.py:12310-12329` (`ready_merge`)
- Modify: `server/sessions/orchestrator.py` (new `_sdlc_arm_auto_merge` near `_sdlc_advance_stage`, ~line 8689)
- Test: `server/tests/test_orchestrator_ready_merge.py`

**Interfaces:**
- Consumes: `InitiativeArtifact.auto_merge_armed_at` (Task 2).
- Produces:
  - `ready_merge(repo, number) -> dict` gains key `"error": str` — `gh`'s stderr when arming
    failed, `""` otherwise. Existing keys unchanged.
  - `SessionOrchestrator._sdlc_arm_auto_merge(artifact_id: str, repo: str, pr_number: int) -> dict`
    returning `{"armed": bool, "error": str}`. On `armed=True` it stamps `auto_merge_armed_at`;
    on `armed=False` it stamps nothing.

- [ ] **Step 1: Write the failing test for the error passthrough**

Create `server/tests/test_orchestrator_ready_merge.py`:

```python
"""`ready_merge` must surface gh's refusal text (roadmap auto-execution §6).

The SDLC synthesizer records `gate_refused` with GitHub's own message, so the
one shared merge path has to hand it back instead of swallowing it.
"""
import subprocess
from types import SimpleNamespace

import pytest

from server.sessions.orchestrator import SessionOrchestrator


def _proc(code: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=code, stdout="", stderr=stderr)


@pytest.mark.asyncio
async def test_ready_merge_reports_gh_refusal_text():
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    calls = []

    def fake_gh(args, *a, **kw):
        calls.append(args)
        if args[1] == "ready":
            return _proc(0)
        return _proc(1, "Auto-merge is not allowed for this repository")

    orch._gh_run = fake_gh
    out = await SessionOrchestrator.ready_merge(orch, "o/r", 7)

    assert out["autoMergeArmed"] is False
    assert out["error"] == "Auto-merge is not allowed for this repository"


@pytest.mark.asyncio
async def test_ready_merge_already_armed_is_success_with_no_error():
    orch = SessionOrchestrator.__new__(SessionOrchestrator)

    def fake_gh(args, *a, **kw):
        if args[1] == "ready":
            return _proc(1)          # already ready — not an error
        return _proc(1, "auto-merge is already enabled for this pull request")

    orch._gh_run = fake_gh
    out = await SessionOrchestrator.ready_merge(orch, "o/r", 7)

    assert out["autoMergeArmed"] is True
    assert out["alreadyArmed"] is True
    assert out["error"] == ""
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_orchestrator_ready_merge.py -q`
Expected: FAIL with `KeyError: 'error'`.

- [ ] **Step 3: Add the key to `ready_merge`**

In `server/sessions/orchestrator.py`, replace the return block of `ready_merge`:

```python
            already = merge.returncode != 0 and "already enabled" in (merge.stderr or "")
            armed = merge.returncode == 0 or already
            # gh's refusal text verbatim — the SDLC synthesizer records it as
            # `gate_refused` (spec 2026-09-14 §6). Captured, never parsed: the
            # only phrase this code matches is "already enabled" above, which
            # predates this change.
            error = "" if armed else (merge.stderr or "").strip()
            # cleanReadyToMerge requires a mergeability probe (Phase 3); honest False
            # until that probe exists — never fabricate.
            return {"markedReady": marked, "autoMergeArmed": armed,
                    "alreadyArmed": already, "cleanReadyToMerge": False,
                    "error": error}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_orchestrator_ready_merge.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Add the orchestrator seam**

In `server/sessions/orchestrator.py`, immediately after `_sdlc_advance_stage` (~line 8689):

```python
    async def _sdlc_arm_auto_merge(self, artifact_id: str, repo: str,
                                   pr_number: int) -> dict:
        """The synthesizer's ``MergeArtifact`` write (spec 2026-09-14 §6): arm
        GitHub auto-merge (squash) on the stage artifact PR, then stamp
        ``auto_merge_armed_at`` so no later tick spends a second ``gh`` call on
        the same PR.

        Reuses ``ready_merge`` — the ONE ``gh pr merge --squash --auto`` writer
        in this codebase (the Plotroom PrRow action) — rather than a second copy.
        GitHub applies the repo's branch protection, so "auto" stays bounded by
        CI; there is deliberately no ``--merge`` fallback.

        A refusal returns ``armed=False`` with gh's text and stamps NOTHING, so
        a transient failure simply retries next tick while a permanent one
        (repo has auto-merge disabled) re-reports the same message each tick and
        the artifact stays at ``AwaitApproval``.
        """
        from server.db.clock import db_utc_now
        from sqlalchemy import update
        from server.audit import record_audit
        from server.db.models import InitiativeArtifact

        out = await self.ready_merge(repo, int(pr_number))
        armed = bool(out.get("autoMergeArmed"))
        if not armed:
            return {"armed": False, "error": str(out.get("error") or "")}
        async with db_session() as db:
            await db.execute(update(InitiativeArtifact).where(
                InitiativeArtifact.id == artifact_id).values(
                    auto_merge_armed_at=db_utc_now()))
            await record_audit(
                db=db, type="sdlc.gate.auto_resolve", identity="sdlc-synthesizer",
                detail={"artifactId": artifact_id, "gate": "artifact.merge",
                        "repo": repo, "pr": int(pr_number),
                        "alreadyArmed": bool(out.get("alreadyArmed"))})
            await db.commit()
        return {"armed": True, "error": ""}
```

- [ ] **Step 6: Run ruff and mypy**

Run: `ruff check server/ && mypy server/ --ignore-missing-imports`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_orchestrator_ready_merge.py
git commit -m "feat(sdlc): arm auto-merge on artifact PRs through the shared ready_merge path"
```

---

## Task 4: The loop's `MergeArtifact` branch

Today `MergeArtifact` falls into the `awaiting` count and nothing executes. This is the change
that unblocks the artifact PRs currently stuck as "awaiting human action".

**Files:**
- Modify: `server/automations/sdlc_synthesizer.py` (type aliases ~line 68; `__init__` ~line 185-224; `_run` ~line 270-280)
- Modify: `server/sessions/orchestrator.py:1183-1204` (wire the seam)
- Test: `server/tests/test_sdlc_synthesizer_loop.py`, `server/tests/test_sdlc_wiring.py`

**Interfaces:**
- Consumes: `_sdlc_arm_auto_merge(artifact_id, repo, pr_number) -> dict` (Task 3).
- Produces: loop seam `ArmAutoMerge = Callable[[str, str, int], Awaitable[dict]]`, constructor
  kwarg `arm_auto_merge=None`, and the `auto_resolved` / `gate_refused` events.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_sdlc_synthesizer_loop.py`:

```python
# --------------------------------------------------------------------------- #
# MergeArtifact (spec 2026-09-14 §6) — phase 2b                                #
# --------------------------------------------------------------------------- #

def _merge_ready(rec, slug="alpha", iid="i-1"):
    """An initiative whose spec artifact has an OPEN PR: the engine returns
    MergeArtifact once the product's tolerance covers `artifact.merge`."""
    rec.policies["prod"] = {"risk_tolerance": "balanced", "dispatch": "auto"}
    rec.autoresolve = True
    rec.artifacts[iid] = [{"id": "a-1", "kind": "spec", "pr_number": 42,
                           "pr_url": "https://github.com/o/r/pull/42",
                           "commit_sha": None, "session_id": None,
                           "auto_merge_armed_at": None}]
    return rec


@pytest.mark.asyncio
async def test_merge_artifact_arms_auto_merge_once():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == [("a-1", "o/r", 42)]
    assert loop._ctx.sdlc_synthesizer_counts["auto_resolved"] == 1


@pytest.mark.asyncio
async def test_merge_artifact_refusal_records_gate_refused_and_does_not_stamp():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.arm_result = {"armed": False, "error": "Auto-merge is not allowed"}
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == [("a-1", "o/r", 42)]
    assert loop._ctx.sdlc_synthesizer_counts["auto_resolved"] == 0
    assert loop._ctx.sdlc_synthesizer_counts["awaiting"] == 1
    kinds = [e.get("kind") for e in rec.events]
    assert "gate_refused" in kinds


@pytest.mark.asyncio
async def test_merge_artifact_skips_an_already_armed_pr():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.artifacts["i-1"][0]["auto_merge_armed_at"] = "2026-09-16T00:00:00+00:00"
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == []          # the one gh call is never spent twice


@pytest.mark.asyncio
async def test_cautious_never_arms():
    rec = Recorder([{"id": "i-1", "slug": "alpha", "stage": "design",
                     "status": "active", "repo": "o/r", "project_slug": "prod"}],
                   mode="on")
    _merge_ready(rec)
    rec.policies["prod"]["risk_tolerance"] = "cautious"
    loop = _loop(rec)
    await loop.tick()

    assert rec.armed == []
    assert loop._ctx.sdlc_synthesizer_counts["awaiting"] == 1
```

Extend `Recorder.__init__` with:

```python
        #: (artifact_id, repo, pr_number) per arm attempt; `arm_result` False
        #: simulates gh refusing (repo has auto-merge disabled).
        self.armed: list[tuple[str, str, int]] = []
        self.arm_result: dict = {"armed": True, "error": ""}
        #: emitted activity events, so a test can assert `gate_refused`.
        self.events: list[dict] = []
```

and the methods:

```python
    async def arm_auto_merge(self, artifact_id, repo, pr_number):
        self.armed.append((artifact_id, repo, int(pr_number)))
        return self.arm_result
```

If `Recorder` has no event capture yet, give the fake ctx an `emit` that appends
`{"level": level, "summary": summary, **kw}` to `rec.events`.

Extend `_loop(...)` to pass `arm_auto_merge=rec.arm_auto_merge`.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_sdlc_synthesizer_loop.py -k merge_artifact -q`
Expected: FAIL — `SdlcSynthesizerLoop` has no `arm_auto_merge` kwarg.

- [ ] **Step 3: Add the seam type and constructor wiring**

In `server/automations/sdlc_synthesizer.py`, after the `AdvanceStage` alias (~line 68):

```python
#: (artifact_id, repo, pr_number) → {"armed": bool, "error": str}. Arms GitHub
#: auto-merge on the stage artifact PR and stamps `auto_merge_armed_at` (§6).
ArmAutoMerge = Callable[[str, str, int], Awaitable[dict]]
```

Add `arm_auto_merge: ArmAutoMerge | None = None,` to `__init__`'s signature (beside
`advance_stage`), and in the body beside `self._advance_stage`:

```python
        self._arm_auto_merge = arm_auto_merge
        #: slugs already warned that the engine wanted auto-merge this
        #: deployment cannot arm — once, like the advance seam.
        self._arm_unwired: set[str] = set()
```

- [ ] **Step 4: Add the `_arm` method**

After `_advance` in the same file:

```python
    async def _arm(self, ini: dict, state: InitiativeView, action: MergeArtifact,
                   artifacts: list[dict], tolerance: str) -> bool:
        """Execute the engine's ``MergeArtifact`` (spec §6): arm GitHub
        auto-merge on the stage artifact PR exactly once.

        Returns whether a gate was resolved this tick. ``False`` means the
        caller counts the artifact as ``awaiting``, which is precisely today's
        behaviour — so an unwired seam, an already-armed PR and a refusal all
        degrade to "a human merges it", never to a lost initiative.
        """
        slug = str(ini.get("slug") or "")
        if self._arm_auto_merge is None:
            if slug not in self._arm_unwired:
                self._arm_unwired.add(slug)
                await self._emit("warn", f"{slug}: the artifact PR would auto-merge under "
                                 f"{tolerance} but arming is not wired — merge it by hand",
                                 kind="arm_unwired", target=slug)
            return False
        art = next((a for a in artifacts if a.get("kind") == action.kind), None)
        if art is None or not art.get("pr_number"):
            return False
        # The one gh call per PR: a stamped row has already been armed.
        if art.get("auto_merge_armed_at"):
            return False
        repo = str(ini.get("repo") or state.repo or "")
        if not repo:
            return False
        out = await self._arm_auto_merge(str(art["id"]), repo, int(art["pr_number"]))
        if not out.get("armed"):
            await self._emit("warn", f"{slug}: auto-merge unavailable on {repo} — "
                             f"merge {action.kind} PR #{art['pr_number']} by hand",
                             kind="gate_refused", target=slug,
                             detail={"gate": "artifact.merge", "tolerance": tolerance,
                                     "pr": art["pr_number"],
                                     "error": str(out.get("error") or "")})
            return False
        await self._emit("info", f"{slug}: armed auto-merge on {action.kind} "
                         f"PR #{art['pr_number']} under {tolerance} (CI is still the gate)",
                         kind="auto_resolved", target=slug,
                         detail={"gate": "artifact.merge", "tolerance": tolerance,
                                 "pr": art["pr_number"]})
        return True
```

Import `MergeArtifact` from `server.sdlc.engine` alongside the existing action imports.

- [ ] **Step 5: Branch on it in `_run`**

In `_run`, the current block counts `MergeArtifact` as awaiting. Replace the
`isinstance(action, AwaitApproval | MergeArtifact) or (...)` condition so `MergeArtifact` is
handled explicitly, mirroring the `Advance` shape:

```python
            if isinstance(action, AwaitApproval) or (
                    isinstance(action, NeedsHuman)
                    and (state.status == "blocked" or state.stage == "build")):
                counts["awaiting"] += 1
            if isinstance(action, MergeArtifact):
                if await self._arm(ini, state, action, artifacts, tolerance):
                    counts["auto_resolved"] += 1
                else:
                    counts["awaiting"] += 1
```

Delete the now-stale comment above it ("`MergeArtifact` counts as awaiting until the arm-auto-merge
seam lands (phase 2b)") — that seam is this change. Leaving it is half-dead documentation.

- [ ] **Step 6: Run the loop tests**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_sdlc_synthesizer_loop.py -q`
Expected: PASS, including the four new tests and every pre-existing one.

- [ ] **Step 7: Wire the seam in the orchestrator**

In `server/sessions/orchestrator.py`, in the `SdlcSynthesizerLoop(...)` construction, beside
`advance_stage=self._sdlc_advance_stage,`:

```python
            arm_auto_merge=self._sdlc_arm_auto_merge,
```

- [ ] **Step 8: Assert the wiring**

Add to `server/tests/test_sdlc_wiring.py` (matching the file's existing style for
`advance_stage`):

```python
def test_synthesizer_loop_receives_the_arm_auto_merge_seam():
    """An unwired seam is fail-closed, so the wiring itself is the contract."""
    orch = _orchestrator()
    assert orch._sdlc_synthesizer_loop._arm_auto_merge == orch._sdlc_arm_auto_merge
```

- [ ] **Step 9: Run the four-step gate**

```bash
ruff check src/ server/
mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD/src python -m pytest tests/ -q
PYTHONPATH=$PWD/src python -m pytest server/tests/ -m "not slow" -q
```
Expected: all clean.

- [ ] **Step 10: Commit**

```bash
git add server/automations/sdlc_synthesizer.py server/sessions/orchestrator.py \
        server/tests/test_sdlc_synthesizer_loop.py server/tests/test_sdlc_wiring.py
git commit -m "feat(sdlc): execute the artifact-merge gate instead of parking it"
```

**→ This is the PR boundary the autopilot dispatch asked for. Open the PR after Task 4.**

---

## Task 5: Apply the unattended model floor at spawn (§9b)

The policy key `unattended_model_floor` has shipped since phase 1 but is **stored only** — the
phase 1 plan says "applied in phase 2". This applies it.

**Files:**
- Modify: `server/sessions/orchestrator.py` (`_sdlc_spawn_synth`, `_sdlc_spawn_materialize`)
- Modify: `server/sdlc/gates.py` (+ `highest_tier` if no shared helper exists — grep first)
- Test: `server/tests/test_sdlc_synthesizer_loop.py` / a spawn-focused test module

**Interfaces:**
- Produces: `unattended_floor_for(policy: dict, gate_id: str, *, enabled: bool) -> str | None` —
  the policy's `unattended_model_floor` when *gate_id* auto-resolves under the policy's effective
  tolerance, else `None`.

- [ ] **Step 1: Find the existing tier ordering — do not add a second one**

```bash
grep -rn "MODEL_TIERS\|def highest_tier\|tier_rank" server/ src/ --include=*.py | grep -v tests
```

Route through whatever this finds. Adding a parallel tier ordering is the anti-pattern the repo's
`eliminating-parallel-implementations` rule exists to stop. Only if nothing exists, add
`highest_tier` to `server/sdlc/gates.py` beside the other pure helpers.

- [ ] **Step 2: Write the failing test**

```python
def test_unattended_floor_applies_only_when_the_consuming_gate_auto_resolves():
    balanced = {"risk_tolerance": "balanced", "unattended_model_floor": "opus"}
    cautious = {"risk_tolerance": "cautious", "unattended_model_floor": "opus"}

    # spec/plan output is consumed by artifact.merge (low → covered by balanced)
    assert unattended_floor_for(balanced, "artifact.merge", enabled=True) == "opus"
    # under cautious nothing auto-resolves, so the floor is inert
    assert unattended_floor_for(cautious, "artifact.merge", enabled=True) is None
    # the kill switch forces cautious
    assert unattended_floor_for(balanced, "artifact.merge", enabled=False) is None
    # decompose output is consumed by materialization.accept (medium → bold only)
    assert unattended_floor_for(balanced, "materialization.accept", enabled=True) is None
```

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_sdlc_gates.py -k unattended -q`
Expected: FAIL — `unattended_floor_for` is not defined.

- [ ] **Step 4: Implement in `server/sdlc/gates.py`**

```python
def unattended_floor_for(policy: dict, gate_id: str, *, enabled: bool) -> str | None:
    """The §9b unattended model floor: the product's ``unattended_model_floor``
    when the gate that will CONSUME this session's output resolves itself, else
    ``None``.

    "This output ships unread" is the whole justification for raising the model,
    so the floor is tied to the consuming gate — `artifact.merge` for spec/plan
    sessions, `materialization.accept` for decompose — never to the spawn itself.
    """
    tolerance = effective_tolerance(policy.get("risk_tolerance"), enabled=enabled)
    if not auto_resolves(gate_id, tolerance):
        return None
    floor = policy.get("unattended_model_floor")
    return None if floor in (None, "", "none") else str(floor)
```

- [ ] **Step 5: Run to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest server/tests/test_sdlc_gates.py -k unattended -q`
Expected: PASS.

- [ ] **Step 6: Apply it at the two spawn sites**

In `_sdlc_spawn_synth` (consuming gate `artifact.merge`) and `_sdlc_spawn_materialize`
(`materialization.accept`), pass the floor into the existing `resolve_model(config, summary,
floor=…)`, maxed with the D9 history floor via the helper found in Step 1. Per §9b the unattended
floor also raises a **pinned** lower `default_model`, and is capped by the model-approval policy:
a tier `resolve_model_policy` would block or park is lowered to the highest allowed tier, because
a parked launch is exactly the idle wait this spec removes. Extend the `sdlc.spawn` audit detail
to `{assessed, pinned, history_floor, unattended_floor, applied, capped}`.

- [ ] **Step 7: Run the gate and commit**

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD/src python -m pytest server/tests/ -m "not slow" -q
git add -A && git commit -m "feat(sdlc): floor the model when a session's output ships unread"
```

---

## Task 6: The gate table on the product card (D10 / §11)

**Files:**
- Modify: the product detail pane under `web/src/features/roadmap/` (grep for where
  `risk_tolerance` is edited today — the table belongs beside that control)
- Modify: `server/api/pm.py` or the product-policy route, to serve the registry
- Test: a Vitest file beside the component

**Interfaces:**
- Produces: `GET`-able gate rows `{id, label, risk, action, reverse, autoResolves}` where
  `autoResolves` is computed for the product's *effective* tolerance.

- [ ] **Step 1: Serve the registry from the existing gates module**

The table's rows must come from `server/sdlc/gates.py:GATES` — the single source of truth — never
a hand-written copy in TypeScript. Add a serializer beside the policy route:

```python
def gate_rows(policy: dict, *, enabled: bool) -> list[dict]:
    """The D10 gate table: every gate in the registry with whether THIS product's
    effective tolerance resolves it. Includes the `risk=None` policy gates
    (`dispatch`, `triage.accept`) so the table is complete and nobody adds a
    second copy of them."""
    tolerance = effective_tolerance(policy.get("risk_tolerance"), enabled=enabled)
    return [{"id": g.id, "label": g.label, "risk": g.risk, "action": g.action,
             "reverse": g.reverse,
             "autoResolves": bool(g.risk) and auto_resolves(g.id, tolerance)}
            for g in GATES]
```

- [ ] **Step 2: Write the failing component test**

Assert the table renders one row per gate, marks `artifact.merge` as automatic at `balanced` and
manual at `cautious`, and shows each gate's reverse text (the "how do I undo this" column is the
point of the table).

- [ ] **Step 3: Run it, implement the component, run it again**

Run: `(cd web && npm run test -- gate-table)` — note the subshell; a persisted `cd web` breaks the
Bash hook for every later call.

- [ ] **Step 4: Run the full web gate**

```bash
(cd web && npm run typecheck && npm run lint && npm run test && npm run build)
```
Run the **full** `npm run test`, not just changed files — ~47 web test files mock `@/api/hooks`,
so a new hook on a shared component breaks tests in files you never touched.

- [ ] **Step 5: Commit**

```bash
git add web/src server/api && git commit -m "feat(roadmap): show which gates this product resolves itself"
```

---

## Self-Review

**Spec coverage:** §6 "Arming auto-merge" → Tasks 3+4. §6 `auto_merge_armed_at` → Task 2. §8
`gate_refused` event → Task 4 Step 4. §9b unattended floor → Task 5. §11/D10 gate table → Task 6.
§13 phase 2's `stage.advance` / `last_auto_gate` / chip / audits → already shipped in phase 2a
(#1760), deliberately not repeated here.

**Known gaps, deliberately out of this plan:** the per-product Synthesizer card lines
(`{slug, tolerance, producing, cap, next}` from §6 step 5) are listed in §13 phase 2 but are a
status-surface change with no gate behaviour; they belong with Task 6's UI work and should be
folded in if Task 6 grows a backend change anyway. Phase 3 (materialization accept, model upgrade)
is out of scope by §13.

**Type consistency:** `arm_auto_merge` is `(artifact_id: str, repo: str, pr_number: int) ->
dict` at every mention — the alias (Task 4 Step 3), the orchestrator method (Task 3 Step 5), the
`Recorder` fake (Task 4 Step 1) and the wiring (Task 4 Step 7). `ready_merge`'s new key is
`"error"` (not `"stderr"`) in both the test and the implementation.

**Risk note for the executor:** Task 4 Step 5 changes an `isinstance` condition that currently
folds `MergeArtifact` into `awaiting`. Get this wrong in the other direction and a `cautious`
product starts arming auto-merge on PRs nobody approved. `test_cautious_never_arms` is the guard —
do not delete or weaken it.
