# Roadmap: dispositioning blocked initiatives and issues — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a human a way to act on every "blocked" row the roadmap shows: see what blocks it, remove a GitHub blocked-by dependency from the app, clear a materialize-failure block, and step an initiative back a stage — and stop the Advance button doubling as the only (wrong) unblock.

**Architecture:** Server first. The pure tier module (`server/pm/tree.py`) gains a *blocked reason* so the map says *why* a row is red; the SDLC router gains `step-back` and `unblock` and refuses `advance` on a blocked initiative; the reconciler's mutation vocabulary gains `remove_blocked_by`, applied through the same journaled `mutate` seam the materialize apply uses (`server/pm/dep_remove.py`, new, shaped like `materialize_apply.py`), exposed as `DELETE /api/pm/work-items/{id}/deps?blocker=<ref>`. Web second: the Needs-you list includes blocked initiatives (design spec item 17), the leaf row lists its blockers as links with a Remove control, the initiative page's Epic tab renders those leaves instead of the bare children list, and the header grows Unblock and Step back beside Advance.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy async (SQLite in tests) · React + TypeScript + React Query + vitest/RTL · GitHub GraphQL `removeBlockedBy` (verified live 2026-09-11: `RemoveBlockedByInput { issueId: ID!, blockingIssueId: ID! }`).

**Spec:** `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` — §8 (write surface table: "Add / remove issue dependency", "Advance / step back a stage"), §10 write endpoints (`POST /api/sdlc/initiatives/{slug}/step-back`, `DELETE /api/pm/work-items/{id}/deps`), item 17 ("Work the 'needs you' pile: … blocked items"), §13 Phase 4. Tracked by finish-line row 8, issue #1472 (partial: `Refs #1472`, not `Fixes`).

## Global Constraints

- Constitution VI: one source of truth. The tier and reason are computed ONLY in `server/pm/tree.py`; the web maps names to labels/tones. Do not compute a tier or reason in the web.
- Constitution III: verify against the real API. The GraphQL mutation name and input were introspected live; do not rename them.
- DB clock rule: never `func.now()`; these tasks use `datetime.now(timezone.utc)` the way the sibling routes in `server/api/sdlc.py` do.
- The assistant route allowlist (`server/assistant/policy.py`) is deny-by-default and exact-anchored. Every new mutating route in this plan is a HUMAN decision: add a `False` regression row for each to `server/tests/test_assistant_policy.py`; never add an allow entry.
- Audit every mutation with `record_audit(db=db, type=…, identity=identity, detail={…})`.
- Web copy rules: no em-dashes in new UI strings; nouns come from `web/src/features/products/vocab.ts` (`INITIATIVE`, `EPIC`, `ISSUE`); `lg` is the only Button size with a 44px target and is REQUIRED for a primary confirm in a Dialog footer.
- The `lint-and-test` gate is four steps: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/`. Web: `npm run typecheck`, `npm run lint`, `npm run test` (FULL run), `npm run build`, all from `(cd web && …)` subshells.
- Run server tests with the shared env scrubbed: `env -u CCSWITCH_TAILNET_ALLOWLIST -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_CLIENT_CERT -u CCSWITCH_SESSIOND_CLIENT_KEY -u CCSWITCH_SESSIOND_CA CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …`. The worktree has no venv; the prebuilt one is at `/home/tony/GitHub/viberpit-deploy/.venv` (or whichever main checkout has `.venv`; `ls /home/tony/GitHub/*/.venv/bin/python`).
- Web `node_modules`: symlink from the deploy checkout if missing: `ln -s /home/tony/GitHub/viberpit-deploy/web/node_modules web/node_modules`.
- Commit after every task. Commit messages go in a file and are passed with `-F` (the Bash hook denies command text pairing a deploy verb with a prod word). Trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File structure

| File | Responsibility |
|---|---|
| `server/pm/tree.py` | + `initiative_blocked_reason()`; `_initiative_node` attaches it as `reason` when the tier is `blocked` |
| `server/sdlc/engine.py` | + `step_back_stage()` beside `advance_stage()` |
| `server/api/sdlc.py` | `advance` refuses blocked; + `POST …/step-back`; + `POST …/unblock` |
| `server/pm/gh_mutations.py` | renders `remove_blocked_by` |
| `server/pm/reconcile_diff.py` | + `remove_blocked_by_mutation()`; `compile_edges` honours an applied removal |
| `server/pm/dep_remove.py` (new) | journaled, resumable-by-retry removal of one blocked-by edge through the `mutate` seam; deactivates the local `WorkItemDep` rows |
| `server/sessions/orchestrator.py`, `facade.py`, `ipc/orchestrator_rpc.py`, `sessions/remote.py` | the `pm_remove_dependency` seam, all four layers, mirroring `sdlc_materialize_apply` |
| `server/api/pm.py` | `DELETE /api/pm/work-items/{item_id}/deps?blocker=` |
| `web/src/features/products/vocab.ts` | reason labels for `blocked` / `epic_blocked` |
| `web/src/features/roadmap/detail/needsYou.ts` | blocked-tier initiatives are Needs-you rows |
| `web/src/api/sdlc.ts`, `web/src/api/pm.ts`, `web/src/api/hooks.ts` | fetchers + hooks: `stepBackInitiative`, `unblockInitiative`, `removeDependency` |
| `web/src/features/roadmap/detail/LeafList.tsx` | blockers as links + Remove |
| `web/src/features/roadmap/InitiativePage.tsx` | Epic tab renders `LeafList`; header Unblock / Step back; Advance disabled when blocked |
| `web/src/features/roadmap/rows/InitiativeCard.tsx` | reason text toned by tier |
| `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` | §8 table rows updated to what shipped |

---

### Task 1: The map says WHY an initiative is blocked

**Files:**
- Modify: `server/pm/tree.py` (after `initiative_reason`, ~line 43; and `_initiative_node`, ~line 130)
- Test: `server/tests/test_pm_tree.py`

**Interfaces:**
- Produces: `initiative_blocked_reason(status: str | None, epic_blocked: int) -> str | None` returning `"blocked"` (the row's own status) or `"epic_blocked"` (≥1 sub-issue with board Status *Blocked* / an open blocker) or `None`. The map's `InitiativeNode.reason` may now be `'advance' | 'blocked' | 'epic_blocked' | null`. Task 6 labels these.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_pm_tree.py`:

```python
def test_initiative_blocked_reason_names_the_cause():
    from server.pm.tree import initiative_blocked_reason
    assert initiative_blocked_reason("blocked", 0) == "blocked"
    # The row's own status wins over the epic count: the human clears the row first.
    assert initiative_blocked_reason("blocked", 3) == "blocked"
    assert initiative_blocked_reason("active", 2) == "epic_blocked"
    assert initiative_blocked_reason("active", 0) is None
    assert initiative_blocked_reason(None, 0) is None


def test_blocked_initiative_node_carries_its_reason():
    """The map row is the only place the reason is computed (constitution VI):
    a blocked row says why, and a needs-you row keeps the advance reason it had."""
    from server.pm.tree import InitiativeIn, _initiative_node
    epic = {"ref": "o/r#1", "url": "u", "total": 3, "done": 1, "blocked": 2, "needsHuman": 0}
    node = _initiative_node(InitiativeIn("x", "X", "build", "active", None, (), epic), "p")
    assert node["tier"] == "blocked"
    assert node["reason"] == "epic_blocked"
    own = _initiative_node(InitiativeIn("y", "Y", "plan", "blocked", None, (), None), "p")
    assert (own["tier"], own["reason"]) == ("blocked", "blocked")
    # Needs-you outranks blocked and keeps its own reason.
    ready = {"ref": "o/r#1", "url": "u", "total": 3, "done": 3, "blocked": 0, "needsHuman": 0}
    adv = _initiative_node(InitiativeIn("z", "Z", "build", "active", None, (), ready), "p")
    assert (adv["tier"], adv["reason"]) == ("needs-you", "advance")
    # An awaiting_human row with a blocked sub-issue is needs-you with NO reason:
    # the reason field says why it needs a human beyond its status, and it does not.
    park = _initiative_node(InitiativeIn("w", "W", "build", "awaiting_human", None, (), epic), "p")
    assert (park["tier"], park["reason"]) == ("needs-you", None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_tree.py -q -k "blocked_reason or carries_its_reason"`
Expected: FAIL — `ImportError: cannot import name 'initiative_blocked_reason'`.

- [ ] **Step 3: Implement**

In `server/pm/tree.py`, directly after `initiative_reason`:

```python
def initiative_blocked_reason(status: str | None, epic_blocked: int) -> str | None:
    """Why a `blocked`-tier initiative is blocked — the disposition differs.

    ``"blocked"`` is the row's OWN status (today only a failed materialize apply
    writes it; `server/pm/materialize_apply.py:fail`), cleared by the unblock
    route. ``"epic_blocked"`` means ≥1 sub-issue of its epic carries an open
    blocked-by dependency, cleared by removing the dependency or closing the
    blocker. The row's own status wins: the human clears the row first."""
    if status == "blocked":
        return "blocked"
    if epic_blocked > 0:
        return "epic_blocked"
    return None
```

In `_initiative_node`, replace the two-key computation so the reason is attached only when the row's tier is `blocked` (a needs-you row keeps the advance reason; a needs-you row for another cause keeps `None`):

```python
    epic_blocked, epic_needs = int(epic.get("blocked", 0)), int(epic.get("needsHuman", 0))
    tier = initiative_tier(ini.status, ini.stage, epic_blocked, epic_needs,
                           epic_total=total, epic_done=done)
    reason = initiative_reason(ini.status, ini.stage, total, done)
    if reason is None and tier == "blocked":
        reason = initiative_blocked_reason(ini.status, epic_blocked)
    return {
        "slug": ini.slug, "title": ini.title, "stage": ini.stage, "status": ini.status,
        "projectSlug": project_slug, "primaryCapability": ini.primary_slug,
        "capabilities": list(ini.capability_slugs),
        "tier": tier,
        "reason": reason,
        "epic": ini.epic,
    }
```

Keep the existing `NOTE:` comment above the dict.

- [ ] **Step 4: Run the whole tree test file**

Run: `… -m pytest server/tests/test_pm_tree.py server/tests/test_pm_map_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```
feat(pm): map rows carry a blocked reason (own status vs blocked sub-issues)

Refs #1472
```

---

### Task 2: `step-back`, `unblock`, and Advance refuses a blocked initiative

**Files:**
- Modify: `server/sdlc/engine.py` (after `advance_stage`, ~line 151)
- Modify: `server/api/sdlc.py` (`advance_initiative` ~line 406; add two routes after it, before the materialization section ~line 437)
- Test: `server/tests/test_sdlc_engine.py`, `server/tests/test_sdlc_api.py`, `server/tests/test_assistant_policy.py`

**Interfaces:**
- Produces: `step_back_stage(stage: str) -> str | None` (None at `intake`; `ValueError` on unknown).
- Produces: `POST /api/sdlc/initiatives/{slug}/step-back` → initiative dict (same shape as advance); 404; 409 `{"error": "at_first_stage"}`; 409 `{"error": "blocked"}`.
- Produces: `POST /api/sdlc/initiatives/{slug}/unblock` → initiative dict; 404; 409 `{"error": "not_blocked"}`; 409 `{"error": "proposal_pending"}`. On success `status == "awaiting_human"`, stage untouched.
- Changes: `POST …/advance` on `status == "blocked"` → 409 `{"error": "blocked", "message": "Clear the block first (unblock, or accept/reject the pending materialization)"}`.
- Audit types: `sdlc.initiative.step_back` (detail `{slug, stage}`), `sdlc.initiative.unblock` (detail `{slug}`).

- [ ] **Step 1: Engine test**

Append to `server/tests/test_sdlc_engine.py`:

```python
def test_step_back_stage_walks_the_sequence_backwards():
    from server.sdlc.engine import step_back_stage
    assert step_back_stage("design") == "intake"
    assert step_back_stage("done") == "maintain"
    assert step_back_stage("intake") is None
    with pytest.raises(ValueError):
        step_back_stage("nope")
```

(Add `import pytest` at the top if the file lacks it.)

- [ ] **Step 2: API tests**

Append to `server/tests/test_sdlc_api.py` (it already has `_client`, `IDENTITY_HEADERS`, and a `_seed`-style pattern using `db_session` + `init_db`; the `_audits(c, type)` helper exists near line 806 — reuse it):

```python
def _seed_initiative(db_url: str, **fields):
    """One Initiative row before the client opens (its loop must not race ours)."""
    from server.db import db_session, init_db
    from server.db.models import Initiative

    async def _go():
        await init_db(db_url)
        async with db_session() as db:
            db.add(Initiative(**{"slug": "dark-mode", "repo": "o/r", "title": "Dark mode",
                                 "stage": "plan", "status": "active", **fields}))
    asyncio.run(_go())


def test_step_back_moves_the_stage_back_one_and_activates():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="build", status="awaiting_human")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), _client() as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/step-back", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert (r.json()["stage"], r.json()["status"]) == ("plan", "active")
            assert _audits(c, "sdlc.initiative.step_back") == [{"slug": "dark-mode", "stage": "plan"}]


def test_step_back_refuses_at_intake_and_when_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="intake")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), _client() as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/step-back", headers=IDENTITY_HEADERS)
            assert r.status_code == 409 and r.json()["detail"]["error"] == "at_first_stage"
            assert c.post("/api/sdlc/initiatives/nope/step-back", headers=IDENTITY_HEADERS).status_code == 404
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="plan", status="blocked")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), _client() as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/step-back", headers=IDENTITY_HEADERS)
            assert r.status_code == 409 and r.json()["detail"]["error"] == "blocked"


def test_advance_refuses_a_blocked_initiative():
    """Advance was the accidental unblock: it flipped status to active AND moved
    the gate. A blocked row must be cleared on purpose, by the route for it."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="plan", status="blocked")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), _client() as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/advance", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "blocked"
            got = c.get("/api/sdlc/initiatives/dark-mode", headers=IDENTITY_HEADERS).json()
            assert (got["stage"], got["status"]) == ("plan", "blocked")


def test_unblock_returns_a_blocked_initiative_to_the_parked_state():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        _seed_initiative(db_url, stage="plan", status="blocked")
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), _client() as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert (r.json()["stage"], r.json()["status"]) == ("plan", "awaiting_human")
            assert _audits(c, "sdlc.initiative.unblock") == [{"slug": "dark-mode"}]
            # Not blocked any more → a second tap is a conflict, not a silent no-op.
            r2 = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r2.status_code == 409 and r2.json()["detail"]["error"] == "not_blocked"


def test_unblock_refuses_while_a_materialization_is_pending():
    """A pending proposal already has its two dispositions (accept resumes the
    apply, reject abandons it); unblocking underneath it would leave the card up
    on a row the loop thinks is free to decompose again."""
    from server.db import db_session, init_db
    from server.db.models import Initiative, MaterializationProposal

    async def _seed(db_url: str):
        await init_db(db_url)
        async with db_session() as db:
            ini = Initiative(slug="dark-mode", repo="o/r", title="Dark mode",
                             stage="plan", status="blocked")
            db.add(ini)
            await db.flush()
            db.add(MaterializationProposal(initiative_id=ini.id, status="pending",
                                           session_id="s", payload={"tasks": []}))

    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        asyncio.run(_seed(db_url))
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url}), _client() as c:
            r = c.post("/api/sdlc/initiatives/dark-mode/unblock", headers=IDENTITY_HEADERS)
            assert r.status_code == 409, r.text
            assert r.json()["detail"]["error"] == "proposal_pending"
```

Check how `_client()` in this file takes its DB URL: it builds its own `tempfile` and patches `CCSWITCH_DB_URL` inside. If it does not accept a caller URL, add an optional `db_url: str | None = None` parameter exactly as `server/tests/test_pm_map_api.py::_client` does (use its own temp dir only when None) and drop the outer `patch.dict` from the tests above, passing `_client(db_url)` instead.

Also add these regression rows to the `test_route_allowlist` parametrize list in `server/tests/test_assistant_policy.py`, beside the `("PATCH", "/api/pm/work-items/w1/overlay", False)` row:

```python
    # Blocked-row dispositions (finish-line row 8, #1472) are a human's calls:
    # stage step-back, clearing a block, and deleting a GitHub dependency.
    ("POST", "/api/sdlc/initiatives/dark-mode/step-back", False),
    ("POST", "/api/sdlc/initiatives/dark-mode/unblock", False),
    ("DELETE", "/api/pm/work-items/w1/deps?blocker=o/r%2312", False),
```

- [ ] **Step 3: Run to verify they fail**

Run: `… -m pytest server/tests/test_sdlc_engine.py server/tests/test_sdlc_api.py -q -k "step_back or unblock or refuses_a_blocked"`
Expected: FAIL (ImportError for `step_back_stage`; 404s from the router for the new paths; 200 from advance).

- [ ] **Step 4: Implement the engine helper**

In `server/sdlc/engine.py` after `advance_stage`:

```python
def step_back_stage(stage: str) -> str | None:
    """Predecessor stage for the human step-back tap; None at the first stage.

    ``done`` steps back to ``maintain`` — nothing is absorbing in this direction,
    a human undoing a mis-tap is the whole point. Raises ``ValueError`` on an
    unknown stage, as ``advance_stage`` does."""
    if stage not in STAGE_SEQUENCE:
        raise ValueError(f"unknown stage {stage!r}")
    idx = STAGE_SEQUENCE.index(stage)
    return STAGE_SEQUENCE[idx - 1] if idx > 0 else None
```

- [ ] **Step 5: Implement the routes**

In `server/api/sdlc.py`, import `step_back_stage` alongside `advance_stage`, and add `MaterializationProposal` to the model imports if it is not already there (it is used by `_latest_materialization`, so it is). Edit `advance_initiative`: after the `already_done` check add

```python
    if ini.status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "blocked",
                    "message": "Clear the block first (unblock, or accept/reject "
                               "the pending materialization)"},
        )
```

and update its docstring: `"""Advance the initiative to the next stage. 409 when already done or blocked — a blocked row is cleared on purpose (``unblock``), never by moving the gate over it."""`.

Add after `advance_initiative`:

```python
async def _load_initiative(db, slug: str) -> Initiative:
    ini = (await db.execute(select(Initiative).where(Initiative.slug == slug))).scalar_one_or_none()
    if ini is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    return ini


@router.post("/initiatives/{slug}/step-back")
async def step_back_initiative(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Move the human gate BACK one stage (roadmap spec §8, §10): the undo for a
    mis-tapped Advance. Status becomes ``active`` exactly as advance does, so the
    stage engine re-enters the earlier stage on its next tick. 409 at the first
    stage, and 409 while blocked: a block is cleared by ``unblock``, not walked
    around in either direction."""
    ini = await _load_initiative(db, slug)
    if ini.status == "blocked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "blocked", "message": "Clear the block first"})
    prev = step_back_stage(str(ini.stage or "intake"))
    if prev is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "at_first_stage", "message": "Initiative is already at the first stage"})
    ini.stage = prev
    ini.status = "active"
    await db.flush()
    await record_audit(db=db, type="sdlc.initiative.step_back", identity=identity,
                       detail={"slug": slug, "stage": prev})
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d


@router.post("/initiatives/{slug}/unblock")
async def unblock_initiative(
    slug: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Clear a ``blocked`` initiative back to ``awaiting_human`` — the parked
    state the plan stage already uses (see ``reject_materialization``) — without
    touching the stage. Today the only writer of ``blocked`` is a failed
    materialize apply; after a partial apply is REJECTED the row was a dead end
    (``next_action`` answers NeedsHuman for it and nothing offered a way out).

    409 ``proposal_pending``: a pending proposal already carries its own two
    dispositions (accept resumes the apply, reject abandons it) and the card for
    them stays up; clearing the row underneath would tell the loop to decompose
    again while a human is still deciding. The web confirm says out loud that a
    re-decomposition creates a second epic if the failed apply's issues are still
    open on GitHub — closing those is the human's call, and this route does not
    touch GitHub."""
    ini = await _load_initiative(db, slug)
    if ini.status != "blocked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "not_blocked", "message": f"Initiative is {ini.status!r}, not blocked"})
    pending = (await db.execute(select(MaterializationProposal.id).where(
        MaterializationProposal.initiative_id == ini.id,
        MaterializationProposal.status == "pending"))).scalars().first()
    if pending is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={
            "error": "proposal_pending",
            "message": "Accept or reject the pending materialization first"})
    ini.status = "awaiting_human"
    await db.flush()
    await record_audit(db=db, type="sdlc.initiative.unblock", identity=identity,
                       detail={"slug": slug})
    d = _initiative_to_dict(ini)
    await _decorate(db, [ini], [d])
    return d
```

Then make `advance_initiative` use `_load_initiative` too (delete its inline 404 block — no second copy of the lookup).

- [ ] **Step 6: Run the tests**

Run: `… -m pytest server/tests/test_sdlc_engine.py server/tests/test_sdlc_api.py server/tests/test_assistant_policy.py -q`
Expected: PASS. If `test_advance_moves_stage_and_activates` still passes (it advances a `proposed` row) nothing else changes.

- [ ] **Step 7: Commit**

```
feat(sdlc): step-back and unblock routes; advance refuses a blocked initiative

Refs #1472
```

---

### Task 3: The reconciler vocabulary learns `remove_blocked_by`

**Files:**
- Modify: `server/pm/gh_mutations.py` (`_render`, ~line 52)
- Modify: `server/pm/reconcile_diff.py` (`compile_edges`, ~line 100; add helper above it)
- Test: `server/tests/test_gh_mutations.py`, `server/tests/test_reconcile_diff.py`

**Interfaces:**
- Produces: `reconcile_diff.remove_blocked_by_mutation(blocked_node: str, blocker_node: str, *, initiated_by: str) -> Mutation` with `kind="remove_blocked_by"`, `target_node_id=blocked_node`, `new_value=blocker_node`. Its `key()` ignores `initiated_by` (existing `Mutation.key`), so Task 4 and `compile_edges` compute the same key.
- Changes: `compile_edges(deps, node_ids, already)` also skips a pair whose REMOVAL key is in `already`.

- [ ] **Step 1: Failing tests**

In `server/tests/test_gh_mutations.py`, find the existing `add_blocked_by` render test and add beside it:

```python
def test_render_remove_blocked_by_matches_the_live_schema():
    """`removeBlockedBy(input: {issueId, blockingIssueId})` — introspected live
    2026-09-11 (RemoveBlockedByInput: issueId ID!, blockingIssueId ID!). Ignores
    the project arguments exactly as add_blocked_by does."""
    from server.pm.gh_mutations import build_batch
    from server.pm.reconcile_diff import remove_blocked_by_mutation
    m = remove_blocked_by_mutation("I_blocked", "I_blocker", initiated_by="dep-remove:o/r#5")
    doc, alias_map = build_batch([m], project_node_id="", field_cache={})
    assert doc == ('mutation { m0: removeBlockedBy(input: {issueId: "I_blocked", '
                   'blockingIssueId: "I_blocker"}) { clientMutationId } }')
    assert alias_map == {"m0": m}
```

In `server/tests/test_reconcile_diff.py`, after `test_compile_edges_prose_without_native_twin`:

```python
def test_compile_edges_never_re_adds_an_edge_a_human_removed_from_the_app():
    """A prose dep whose native twin was deleted through DELETE …/deps must stay
    deleted: the applied removal key is in `already`, and it outranks the body."""
    from server.pm.reconcile_diff import DepView, compile_edges, remove_blocked_by_mutation
    deps = [DepView("o/r#5", "o/r#3", "prose", True)]
    nodes = {"o/r#5": "N5", "o/r#3": "N3"}
    assert len(compile_edges(deps, nodes, already=set())) == 1
    removed = remove_blocked_by_mutation("N5", "N3", initiated_by="x").key()
    assert compile_edges(deps, nodes, already={removed}) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_gh_mutations.py server/tests/test_reconcile_diff.py -q -k "remove_blocked_by or removed_from_the_app"`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement**

`server/pm/reconcile_diff.py`, above `compile_edges`:

```python
def remove_blocked_by_mutation(blocked_node: str, blocker_node: str, *,
                               initiated_by: str) -> Mutation:
    """The inverse of an `add_blocked_by` edge, as the reconciler's own `Mutation`.

    Same field layout as the add (target = the blocked issue, new_value = the
    blocker) so ONE key shape covers both directions, and `compile_edges` can
    recognise an applied removal by key and stop re-compiling the edge from a
    prose dep that still names it in the issue body."""
    return Mutation("remove_blocked_by", blocked_node, None, None, blocker_node, initiated_by)
```

In `compile_edges`, after `m = Mutation("add_blocked_by", …)`:

```python
        if m.key() in already:
            continue
        # A human deleted this edge from the app (`dep_remove`): the applied
        # removal is the newer decision, and the body's prose does not re-add it.
        if remove_blocked_by_mutation(src, dst, initiated_by="").key() in already:
            continue
```

`server/pm/gh_mutations.py::_render`, after the `add_blocked_by` branch:

```python
    if m.kind == "remove_blocked_by":
        return ("removeBlockedBy(input: {issueId: %s, blockingIssueId: %s}) { clientMutationId }"
                % (_q(m.target_node_id), _q(m.new_value)))
```

Also update the `prune_superseded_applied` docstring in `server/pm/journal.py` (the sentence "``add_item`` and ``add_blocked_by`` rows are the permanent record ``applied_keys`` reads") to name `remove_blocked_by` too — `OWNED_FIELDS` already excludes it, so no code change.

- [ ] **Step 4: Run**

Run: `… -m pytest server/tests/test_gh_mutations.py server/tests/test_reconcile_diff.py server/tests/test_journal.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```
feat(pm): remove_blocked_by mutation; compile_edges honours an applied removal

Refs #1472
```

---

### Task 4: Journaled dependency removal through the `mutate` seam

**Files:**
- Create: `server/pm/dep_remove.py`
- Modify: `server/sessions/orchestrator.py` (next to `_sdlc_materialize_apply`, ~line 7588), `server/sessions/facade.py` (~line 243), `server/ipc/orchestrator_rpc.py` (~line 391), `server/sessions/remote.py` (~line 1170)
- Test: `server/tests/test_pm_dep_remove.py` (new)

**Interfaces:**
- Produces: `dep_remove.remove(db: AsyncSession, *, src: WorkItem, blocker_ref: str, mutate: Mutate, now: datetime) -> dict` → `{"ok": True, "src": "o/r#5", "blocker": "o/r#3"}` or `{"ok": False, "step": "resolve" | "node_id" | "mutate", "error": str}`. `Mutate` is `materialize_apply.Mutate` (`Callable[[owner: str, doc: str], Awaitable[dict]]`) — import it, do not redefine it.
- Produces: orchestrator seam `_pm_remove_dependency(work_item_id: str, blocker_ref: str) -> dict`, public as `sessions.pm_remove_dependency(work_item_id, blocker_ref)` on the facade, RPC name `pm_remove_dependency`, and the remote wrapper returning `{"ok": False, "step": "rpc", "error": …}` on `DaemonUnavailable` (same shape as `sdlc_materialize_apply`).

Behaviour of `remove()`:
1. Resolve the blocker `WorkItem` by ref (`repo`, `number`, `kind == "issue"`). Missing → `("resolve", "blocker o/r#3 is not mirrored")`.
2. Both sides need `gh_node_id`; missing → `("node_id", "o/r#5 has no GitHub node id yet; wait for the next sync")`.
3. Build `remove_blocked_by_mutation(src.gh_node_id, dst.gh_node_id, initiated_by=f"dep-remove:{src_ref}->{blocker_ref}")`, journal it pending with `batch_id=f"dep-remove:{uuid4()}"` (write-ahead: `await db.commit()` after `record_pending`, exactly as `materialize_apply.create_issue` does), render with `build_batch([m], project_node_id="", field_cache={})`, call `mutate(owner, doc)` (owner = `src.repo.partition("/")[0]`), `parse_batch`. A transport exception becomes `{"errors": [{"message": str(e)}]}` — same idiom as `create_issue`.
4. Per-alias error → `journal.mark_failed`, return `("mutate", error)`. Success → `journal.mark_applied`, then set `active = False` on EVERY `WorkItemDep` row with `(src_ref, dst_ref) == (src_ref, blocker_ref)` regardless of `source` (the native row so the tier flips on the next map read; the prose row so `compile_edges` has nothing to compile — the journal key is the belt, this is the braces), then `request_project_sync(db, project_id, now)` for each project owning `src.repo` (`server/pm/repo_project.py:owning_projects_by_repo` — read its signature; it is already imported by `server/api/pm.py`) so the mirror re-reads GitHub's view. Do NOT commit inside step 4: the caller (the orchestrator seam) commits once at the end.

- [ ] **Step 1: Failing tests**

Create `server/tests/test_pm_dep_remove.py`, modelled on `test_pm_materialize_apply.py` (its `db_env` fixture, `NOW`, `db_session`/`init_db`):

```python
"""Journaled removal of one blocked-by edge (roadmap spec §8/§10, #1472).

Every GitHub write goes through a fake ``mutate`` seam — nothing here may reach
GitHub. What is pinned: a journal row BEFORE the call, marked applied or failed
after it, the local dep rows deactivated only on success, and the sync request.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import GhWriteJournal, Project, ProjectRepo, WorkItem, WorkItemDep

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_env():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{tmp}/t.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": url, "CCSWITCH_DEV": "1",
                                     "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            yield url


async def _seed(db_url: str, *, blocker_node: str | None = "N3") -> str:
    """Returns the blocked item's id. #5 is blocked by #3, natively AND in prose."""
    await init_db(db_url)
    async with db_session() as db:
        p = Project(slug="ccswitch", name="ccswitch", onboarding_state="live")
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo="o/r", is_primary=True))
        src = WorkItem(repo="o/r", number=5, kind="issue", title="Blocked", state="open",
                       gh_node_id="N5")
        dst = WorkItem(repo="o/r", number=3, kind="issue", title="Blocker", state="open",
                       gh_node_id=blocker_node)
        db.add_all([src, dst])
        await db.flush()
        for source in ("native", "prose"):
            db.add(WorkItemDep(src_ref="o/r#5", dst_ref="o/r#3", source=source, active=True,
                               src_work_item_id=src.id, dst_work_item_id=dst.id))
        return src.id


def _mutate(payload: dict, calls: list[tuple[str, str]]):
    async def mutate(owner: str, doc: str) -> dict:
        calls.append((owner, doc))
        return payload
    return mutate


async def _run(db_url: str, src_id: str, mutate):
    from server.pm.dep_remove import remove
    async with db_session() as db:
        src = (await db.execute(select(WorkItem).where(WorkItem.id == src_id))).scalar_one()
        result = await remove(db, src=src, blocker_ref="o/r#3", mutate=mutate, now=NOW)
        await db.commit()
        return result


@pytest.mark.asyncio
async def test_success_journals_applied_deactivates_every_row_and_requests_sync(db_env):
    src_id = await _seed(db_env)
    calls: list[tuple[str, str]] = []
    result = await _run(db_env, src_id, _mutate({"data": {"m0": {"clientMutationId": None}}}, calls))
    assert result == {"ok": True, "src": "o/r#5", "blocker": "o/r#3"}
    assert calls == [("o", 'mutation { m0: removeBlockedBy(input: {issueId: "N5", '
                            'blockingIssueId: "N3"}) { clientMutationId } }')]
    async with db_session() as db:
        rows = (await db.execute(select(GhWriteJournal))).scalars().all()
        assert [(r.field, r.applied, r.error) for r in rows] == [("remove_blocked_by", True, None)]
        deps = (await db.execute(select(WorkItemDep))).scalars().all()
        assert {d.source: d.active for d in deps} == {"native": False, "prose": False}
        repo = (await db.execute(select(ProjectRepo))).scalar_one()
        assert repo.sync_requested_at is not None


@pytest.mark.asyncio
async def test_github_refusal_marks_the_row_failed_and_keeps_the_deps(db_env):
    src_id = await _seed(db_env)
    payload = {"data": {"m0": None}, "errors": [{"path": ["m0"], "message": "no such edge"}]}
    result = await _run(db_env, src_id, _mutate(payload, []))
    assert result == {"ok": False, "step": "mutate", "error": "no such edge"}
    async with db_session() as db:
        row = (await db.execute(select(GhWriteJournal))).scalar_one()
        assert (row.applied, row.error) == (False, "no such edge")
        deps = (await db.execute(select(WorkItemDep))).scalars().all()
        assert all(d.active for d in deps)


@pytest.mark.asyncio
async def test_missing_node_id_is_refused_before_any_call(db_env):
    src_id = await _seed(db_env, blocker_node=None)
    calls: list[tuple[str, str]] = []
    result = await _run(db_env, src_id, _mutate({}, calls))
    assert result["ok"] is False and result["step"] == "node_id"
    assert calls == []
    async with db_session() as db:
        assert (await db.execute(select(GhWriteJournal))).scalars().all() == []


@pytest.mark.asyncio
async def test_unmirrored_blocker_is_refused(db_env):
    src_id = await _seed(db_env)
    from server.pm.dep_remove import remove
    async with db_session() as db:
        src = (await db.execute(select(WorkItem).where(WorkItem.id == src_id))).scalar_one()
        result = await remove(db, src=src, blocker_ref="o/r#999", mutate=_mutate({}, []), now=NOW)
    assert result["ok"] is False and result["step"] == "resolve"
```

Check the `ProjectRepo` sync-request column name in `server/pm/sync_request.py` (`request_project_sync` stamps it) and use that name in the first test in place of `sync_requested_at` if it differs.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_dep_remove.py -q`
Expected: FAIL — `ModuleNotFoundError: server.pm.dep_remove`.

- [ ] **Step 3: Implement `server/pm/dep_remove.py`**

```python
"""Remove one blocked-by edge on GitHub, journaled (roadmap spec §8/§10, #1472).

The human disposition for a `blocked` issue leaf. Shaped like
``materialize_apply``: the write is journaled BEFORE the call, the payload comes
back through the same ``mutate`` seam, and the local ``WorkItemDep`` rows are
deactivated only once GitHub has agreed — the map's tier reads those rows, so a
refused removal leaves the row red rather than lying green for a sync cycle.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.models import WorkItem, WorkItemDep

from . import journal
from .gh_mutations import build_batch, parse_batch
from .materialize_apply import Mutate, _error_text
from .reconcile_diff import remove_blocked_by_mutation
from .repo_project import owning_projects_by_repo
from .sync_request import request_project_sync

INITIATED_BY_PREFIX = "dep-remove:"


def _split_ref(ref: str) -> tuple[str, int] | None:
    repo, _, num = ref.rpartition("#")
    return (repo, int(num)) if repo and num.isdigit() else None


async def remove(db: AsyncSession, *, src: WorkItem, blocker_ref: str,
                 mutate: Mutate, now: datetime) -> dict:
    """Delete the native edge ``src`` ← blocked by ← ``blocker_ref``.

    Returns ``{"ok": True, "src", "blocker"}`` or ``{"ok": False, "step", "error"}``
    with ``step`` in ``resolve`` (blocker not mirrored), ``node_id`` (a side has
    no GitHub node id yet) or ``mutate`` (GitHub refused). Does not commit."""
    src_ref = f"{src.repo}#{src.number}"
    parsed = _split_ref(blocker_ref)
    dst = None
    if parsed is not None:
        dst = (await db.execute(select(WorkItem).where(
            WorkItem.repo == parsed[0], WorkItem.number == parsed[1],
            WorkItem.kind == "issue"))).scalar_one_or_none()
    if dst is None:
        return {"ok": False, "step": "resolve", "error": f"blocker {blocker_ref} is not mirrored"}
    for item, ref in ((src, src_ref), (dst, blocker_ref)):
        if not item.gh_node_id:
            return {"ok": False, "step": "node_id",
                    "error": f"{ref} has no GitHub node id yet; wait for the next sync"}
    mutation = remove_blocked_by_mutation(
        str(src.gh_node_id), str(dst.gh_node_id),
        initiated_by=f"{INITIATED_BY_PREFIX}{src_ref}->{blocker_ref}")
    batch_id = f"{INITIATED_BY_PREFIX}{uuid4()}"
    row_id = (await db.run_sync(lambda s: journal.record_pending(
        s, [mutation], batch_id=batch_id, now=now, dry_run=False)))[0]
    await db.commit()  # write-ahead: the row must outlive a crash mid-call
    doc, alias_map = build_batch([mutation], project_node_id="", field_cache={})
    owner = str(src.repo or "").partition("/")[0]
    try:
        payload = await mutate(owner, doc)
    except Exception as e:  # transport failure — still a recorded attempt
        payload = {"errors": [{"message": str(e)}]}
    error = parse_batch(payload, alias_map).errors.get("m0") or ""
    if error:
        error = error if error != "no data returned" else (_error_text(payload) or error)
        await db.run_sync(lambda s: journal.mark_failed(s, row_id, error, now))
        return {"ok": False, "step": "mutate", "error": error}
    await db.run_sync(lambda s: journal.mark_applied(s, [row_id], now))
    rows = (await db.execute(select(WorkItemDep).where(
        WorkItemDep.src_ref == src_ref, WorkItemDep.dst_ref == blocker_ref,
        WorkItemDep.active.is_(True)))).scalars().all()
    for row in rows:
        row.active = False
    for project_id in await owning_projects_by_repo(db, str(src.repo or "")):
        await request_project_sync(db, project_id, now)
    return {"ok": True, "src": src_ref, "blocker": blocker_ref}
```

Read `owning_projects_by_repo`'s real signature/return type in `server/pm/repo_project.py` before using it; adapt the last loop to what it returns (it may return project rows or a mapping). If `_error_text` is private in `materialize_apply`, it is still the one implementation — import it rather than copy it.

- [ ] **Step 4: The four seam layers**

`server/sessions/orchestrator.py`, directly after `_sdlc_materialize_apply_locked`:

```python
    async def _pm_remove_dependency(self, work_item_id: str, blocker_ref: str) -> dict:
        """Delete one blocked-by edge on GitHub (roadmap spec §8, #1472).

        THE seam the API calls, for the same reason as ``_sdlc_materialize_apply``:
        the router decides a human asked, this owns the transport
        (``_pm_reconcile_mutate``) and the journal. A failure is data, not an
        exception — the route turns it into a 502 and the row stays blocked."""
        from sqlalchemy import select
        from server.db.models import WorkItem
        from server.pm.dep_remove import remove
        now = self._pm_sync_now_utc()
        async with db_session() as db:
            src = (await db.execute(select(WorkItem).where(
                WorkItem.id == work_item_id))).scalar_one_or_none()
            if src is None:
                return {"ok": False, "step": "load", "error": f"no such work item: {work_item_id}"}
            result = await remove(db, src=src, blocker_ref=blocker_ref,
                                  mutate=self._pm_reconcile_mutate, now=now)
            await db.commit()
        return result
```

`server/sessions/facade.py`, after the `sdlc_materialize_apply` line:

```python
    async def pm_remove_dependency(self, *a, **k): return await _maybe_await(self._orch._pm_remove_dependency(*a, **k))
```

`server/ipc/orchestrator_rpc.py`, after `register_rpc(app, "sdlc_materialize_apply", …)`:

```python
    async def pm_remove_dependency(params: dict) -> dict:
        # Private seam, same reasoning as sdlc_materialize_apply above.
        return await _maybe_await(orch._pm_remove_dependency(**params))

    register_rpc(app, "pm_remove_dependency", pm_remove_dependency)
```

`server/sessions/remote.py`, after `sdlc_materialize_apply`:

```python
    async def pm_remove_dependency(self, work_item_id: str, blocker_ref: str) -> dict:
        """Delete one blocked-by edge, via the daemon (its gh ladder, its journal).
        A dead RPC is a failed removal, not an exception — the route answers 502
        and the row stays blocked, which is the truth."""
        try:
            result = await self._client.call("pm_remove_dependency",
                                             work_item_id=work_item_id,
                                             blocker_ref=blocker_ref,
                                             timeout=MATERIALIZE_RPC_TIMEOUT)
        except DaemonUnavailable as exc:
            log.warning("pm_remove_dependency RPC unavailable: %s", exc)
            return {"ok": False, "step": "rpc", "error": f"session daemon unavailable: {exc}"}
        if not isinstance(result, dict):
            return {"ok": False, "step": "rpc", "error": "session daemon did not answer"}
        return result
```

Check whether `server/tests/test_pm_rpc_serialization.py` or `test_sdlc_wiring.py` enumerates RPC names / facade methods (grep for `sdlc_materialize_apply` in `server/tests/`); if a list asserts parity between the facade, the RPC registry and the remote client, add `pm_remove_dependency` there too.

- [ ] **Step 5: Run**

Run: `… -m pytest server/tests/test_pm_dep_remove.py server/tests/test_pm_rpc_serialization.py server/tests/test_sdlc_wiring.py server/tests/test_pm_orchestrator_seams.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```
feat(pm): journaled blocked-by removal through the reconciler mutate seam

Refs #1472
```

---

### Task 5: `DELETE /api/pm/work-items/{id}/deps?blocker=`

**Files:**
- Modify: `server/api/pm.py` (after `patch_overlay`, end of file)
- Test: `server/tests/test_pm_deps_api.py`

**Interfaces:**
- Produces: `DELETE /api/pm/work-items/{item_id}/deps?blocker=<owner/repo#N>` → `{"ok": true, "src": "o/r#5", "blocker": "o/r#3"}`; 404 `not_found` (work item) / `dep_not_found` (no ACTIVE dep row for the pair); 422 `bad_blocker` (ref not `owner/repo#N`); 502 `dep_remove_failed` with `step` + `message` when the seam says no. Audit `pm.workitem.dep_remove` `{workItemId, blocker, ok, step}` — written and COMMITTED before raising the 502, exactly as `accept_materialization` does.

- [ ] **Step 1: Failing tests**

Append to `server/tests/test_pm_deps_api.py` (it has `_client` + `IDENTITY_HEADERS`; seeding runs inside the client block with `asyncio.run` in the first test — follow whichever seeding idiom the file's later tests use, preferring seed-before-client as `test_pm_map_api.py` explains). The seam is stubbed on `app.state.sessions`:

```python
def _seed_blocked_pair(db_url: str) -> str:
    """#5 blocked by #3, native; returns #5's work item id."""
    import asyncio
    from server.db import db_session, init_db
    from server.db.models import WorkItem, WorkItemDep

    async def _go() -> str:
        await init_db(db_url)
        async with db_session() as db:
            src = WorkItem(repo="o/r", number=5, kind="issue", state="open", gh_node_id="N5")
            dst = WorkItem(repo="o/r", number=3, kind="issue", state="open", gh_node_id="N3")
            db.add_all([src, dst])
            await db.flush()
            db.add(WorkItemDep(src_ref="o/r#5", dst_ref="o/r#3", source="native", active=True,
                               src_work_item_id=src.id, dst_work_item_id=dst.id))
            return src.id
    return asyncio.run(_go())


def test_delete_dep_calls_the_seam_and_audits():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        wid = _seed_blocked_pair(db_url)
        with _client(db_url) as c:
            calls: list[tuple[str, str]] = []

            async def fake(work_item_id: str, blocker_ref: str) -> dict:
                calls.append((work_item_id, blocker_ref))
                return {"ok": True, "src": "o/r#5", "blocker": "o/r#3"}
            c.app.state.sessions.pm_remove_dependency = fake
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%233", headers=IDENTITY_HEADERS)
            assert r.status_code == 200, r.text
            assert r.json() == {"ok": True, "src": "o/r#5", "blocker": "o/r#3"}
            assert calls == [(wid, "o/r#3")]
            audits = c.get("/api/audit?type=pm.workitem.dep_remove", headers=IDENTITY_HEADERS).json()
            assert [a["detail"] for a in audits] == [
                {"workItemId": wid, "blocker": "o/r#3", "ok": True, "step": None}]


def test_delete_dep_refusals():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        wid = _seed_blocked_pair(db_url)
        with _client(db_url) as c:
            async def refuse(work_item_id: str, blocker_ref: str) -> dict:
                return {"ok": False, "step": "mutate", "error": "no such edge"}
            c.app.state.sessions.pm_remove_dependency = refuse
            assert c.delete("/api/pm/work-items/nope/deps?blocker=o/r%233",
                            headers=IDENTITY_HEADERS).status_code == 404
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%239",
                         headers=IDENTITY_HEADERS)
            assert r.status_code == 404 and r.json()["detail"]["error"] == "dep_not_found"
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=garbage", headers=IDENTITY_HEADERS)
            assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_blocker"
            r = c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%233", headers=IDENTITY_HEADERS)
            assert r.status_code == 502, r.text
            assert r.json()["detail"] == {"error": "dep_remove_failed", "step": "mutate",
                                          "message": "no such edge"}
            # The failed attempt is still in the trail (committed before the raise).
            audits = c.get("/api/audit?type=pm.workitem.dep_remove", headers=IDENTITY_HEADERS).json()
            assert [a["detail"]["ok"] for a in audits] == [False]
            assert c.delete(f"/api/pm/work-items/{wid}/deps?blocker=o/r%233").status_code == 401
```

Confirm the audit list endpoint path and its `type` filter by reading how `_audits()` in `server/tests/test_sdlc_api.py` fetches rows, and use the same call. If `_client` in this file has no `db_url` parameter, add the optional parameter as in `test_pm_map_api.py`. Note the 401 assertion: the fixture sets `CCSWITCH_DEV=1` with an empty allowlist, which the sibling test in `test_sdlc_api.py` (line ~472) shows still 401s a request with no identity header; if this file's fixture differs, drop that one line rather than fight it.

- [ ] **Step 2: Run to verify they fail**

Run: `… -m pytest server/tests/test_pm_deps_api.py -q -k delete_dep`
Expected: FAIL — 405/404 from the router.

- [ ] **Step 3: Implement**

Append to `server/api/pm.py`:

```python
_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+$")


@router.delete("/work-items/{item_id}/deps")
async def delete_work_item_dep(
    item_id: str,
    request: Request,
    blocker: str,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Remove the blocked-by edge ``item ← blocker`` on GitHub (roadmap spec §8,
    §10; #1472) — the disposition for a `blocked` leaf. ``blocker`` is the
    canonical ``owner/repo#N`` ref the leaf's ``blockedBy`` carries; a query
    parameter because a ref has a slash and a hash in it.

    The GitHub write belongs to the orchestrator seam (its gh ladder, its
    journal); this route decides that a human asked and records that in the
    audit trail whether or not GitHub agreed. A refusal is a 502 carrying the
    seam's ``step`` so the web can say what to do (an unmirrored side means
    "wait for the sync", a GitHub error means "look at the issue")."""
    if not _REF_RE.match(blocker):
        raise HTTPException(status_code=422, detail={
            "error": "bad_blocker", "message": "blocker must be owner/repo#N"})
    wi = (await db.execute(select(WorkItem).where(WorkItem.id == item_id))).scalar_one_or_none()
    if wi is None:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    src_ref = f"{wi.repo}#{wi.number}"
    dep = (await db.execute(select(WorkItemDep.id).where(
        WorkItemDep.src_ref == src_ref, WorkItemDep.dst_ref == blocker,
        WorkItemDep.active.is_(True)))).scalars().first()
    if dep is None:
        raise HTTPException(status_code=404, detail={
            "error": "dep_not_found", "message": f"{src_ref} is not blocked by {blocker}"})
    result = await request.app.state.sessions.pm_remove_dependency(item_id, blocker)
    await record_audit(db=db, type="pm.workitem.dep_remove", identity=identity,
                       detail={"workItemId": item_id, "blocker": blocker,
                               "ok": bool(result.get("ok")), "step": result.get("step")})
    if not result.get("ok"):
        await db.commit()  # the failed attempt is exactly what the trail must keep
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={
            "error": "dep_remove_failed", "step": result.get("step"),
            "message": str(result.get("error") or "dependency removal failed")})
    return result
```

`re` and `Request` are already imported in `server/api/pm.py`.

- [ ] **Step 4: Run**

Run: `… -m pytest server/tests/test_pm_deps_api.py server/tests/test_assistant_policy.py -q`
Expected: PASS (the policy rows from Task 2 cover this route).

- [ ] **Step 5: Server gate, then commit**

Run all four: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/ -q`, and the server suite `pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/blocked-disposition-server.log` (one run at a time; expect the known rocky-only flakes listed in memory if on rocky — re-run those alone). Fix anything red.

```
feat(pm): DELETE /api/pm/work-items/{id}/deps removes a blocked-by edge

Refs #1472
```

---

### Task 6: Needs-you includes blocked initiatives; reasons have words

**Files:**
- Modify: `web/src/features/products/vocab.ts` (`REASON_LABEL`, ~line 102)
- Modify: `web/src/features/roadmap/detail/needsYou.ts`
- Modify: `web/src/features/roadmap/rows/InitiativeCard.tsx` (~line 72)
- Modify: `web/src/api/roadmap.ts` (`InitiativeNode.reason` doc comment, ~line 60)
- Test: create `web/src/features/roadmap/detail/needsYou.test.ts`; `web/src/features/roadmap/detail/PortfolioDetail.test.tsx` (existing tile/badge parity tests keep passing)

**Interfaces:**
- `REASON_LABEL` gains `blocked: 'Blocked after a failed materialize'` and `epic_blocked: 'Blocked by open dependencies'`.
- `needsYouRows(products).initiatives` now includes `tier === 'blocked'` rows, after the `needs-you` rows (a decision outranks a stall, same order as `ROADMAP_TIERS`).

- [ ] **Step 1: Failing test**

Create `web/src/features/roadmap/detail/needsYou.test.ts`:

```ts
// "Needs you" is every pending human decision AND every stall a human can clear
// (design spec item 17): needs-you rows first, blocked rows after them.
import { describe, it, expect } from 'vitest'
import { ini, product } from '../__fixtures__/map'
import { needsYouCount, needsYouRows } from './needsYou'

const p = product({
  slug: 'kindash', name: 'KinDash',
  initiatives: [
    ini({ slug: 'stuck', projectSlug: 'kindash', tier: 'blocked', reason: 'epic_blocked' }),
    ini({ slug: 'ready', projectSlug: 'kindash', tier: 'needs-you', reason: 'advance' }),
    ini({ slug: 'moving', projectSlug: 'kindash', tier: 'in-flight' }),
  ],
})

describe('needsYouRows', () => {
  it('lists needs-you rows first, then blocked rows, never in-flight ones', () => {
    const rows = needsYouRows([p]).initiatives.map((r) => r.initiative.slug)
    expect(rows).toEqual(['ready', 'stuck'])
  })
  it('counts what it lists', () => {
    expect(needsYouCount([p])).toBe(2)
  })
})
```

Read `web/src/features/roadmap/__fixtures__/map.ts` for the exact `ini()` / `product()` factory signatures before writing the fixture calls; adjust field names to match.

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/detail/needsYou.test.ts)`
Expected: FAIL — rows equal `['ready']`.

- [ ] **Step 3: Implement**

`needsYou.ts`:

```ts
/** Needs-you first (a decision), then blocked (a stall a human can clear) —
 *  the design spec's item 17 puts both in this pile, in tier order. */
const NEEDS_YOU_TIERS: readonly RoadmapTier[] = ['needs-you', 'blocked']

export function needsYouRows(products: readonly ProductNode[]): NeedsYouRows {
  return {
    initiatives: NEEDS_YOU_TIERS.flatMap((tier) =>
      products.flatMap((p) =>
        p.initiatives.filter((i) => i.tier === tier).map((i) => ({ product: p, initiative: i })))),
    pseudo: products.filter((p) => p.counts.awaitingTriage > 0 || p.counts.needsHome > 0),
  }
}
```

Import `RoadmapTier` from `@/api/roadmap`. Update the header comment: "Initiatives the server tiered `needs-you` or `blocked`".

`vocab.ts` `REASON_LABEL`:

```ts
export const REASON_LABEL: Record<string, string> = {
  advance: 'Ready to advance',
  blocked: 'Blocked after a failed materialize',
  epic_blocked: 'Blocked by open dependencies',
}
```

`roadmap.ts` `InitiativeNode.reason` comment: `/** Why it needs a human beyond its status: 'advance', or for a blocked row 'blocked' | 'epic_blocked'. */` and the type `'advance' | 'blocked' | 'epic_blocked' | string | null`.

`InitiativeCard.tsx`: the reason span's class becomes `initiative.tier === 'blocked' ? 'text-danger' : 'text-warning'` (a stall is red like its dot, not amber like a decision). `NeedsYouList.tsx` has the same `text-warning` span — apply the same ternary there.

- [ ] **Step 4: Run the roadmap tests**

Run: `(cd web && npx vitest run src/features/roadmap)`
Expected: PASS. If a PortfolioDetail/ProductDetail parity test now sees a different Needs-you number, it is because the fixture has a blocked initiative — both the tile and the badge come from `needsYouCount`, so update only the expected literal.

- [ ] **Step 5: Commit**

```
feat(roadmap): Needs-you lists blocked initiatives with a reason

Refs #1472
```

---

### Task 7: Fetchers and hooks for the three dispositions

**Files:**
- Modify: `web/src/api/sdlc.ts` (after `advanceInitiative`, ~line 127)
- Modify: `web/src/api/pm.ts` (end of fetchers, ~line 150)
- Modify: `web/src/api/hooks.ts` (after `useAdvanceInitiative`, ~line 1512; and after `useLeaves`, ~line 1690)

**Interfaces:**
- `stepBackInitiative(slug): Promise<Initiative>` → `POST /sdlc/initiatives/{slug}/step-back`
- `unblockInitiative(slug): Promise<Initiative>` → `POST /sdlc/initiatives/{slug}/unblock`
- `removeDependency({ workItemId, blocker }): Promise<{ ok: boolean; src: string; blocker: string }>` → `DELETE /pm/work-items/{id}/deps?blocker=`
- `useStepBackInitiative()`, `useUnblockInitiative()`: `useMutation<Initiative, ApiRequestError, string>`, invalidating `QK.initiative(slug)` + `QK.roadmapMap`, `meta: PM_MUTATION_META`.
- `useRemoveDependency()`: `useMutation<RemoveDependencyResult, ApiRequestError, RemoveDependencyInput>`, invalidating every leaves query (`qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })` — the prefix; the scope key is not known to the row) and `QK.roadmapMap`, `meta: PM_MUTATION_META`.

- [ ] **Step 1: Implement**

`sdlc.ts`:

```ts
/** The undo for a mis-tapped Advance: one stage back. 409 at intake or while blocked. */
export function stepBackInitiative(slug: string): Promise<Initiative> {
  return api.post<Initiative>(`/sdlc/initiatives/${encodeURIComponent(slug)}/step-back`)
}

/** Clear a `blocked` initiative back to the parked state without moving the gate.
 *  409 unless blocked, and 409 while a materialization proposal is pending (that
 *  card has its own accept/reject). */
export function unblockInitiative(slug: string): Promise<Initiative> {
  return api.post<Initiative>(`/sdlc/initiatives/${encodeURIComponent(slug)}/unblock`)
}
```

`pm.ts`:

```ts
export interface RemoveDependencyInput { workItemId: string; blocker: string }
export interface RemoveDependencyResult { ok: boolean; src: string; blocker: string }

/** Delete the blocked-by edge `workItem ← blocker` on GitHub through the
 *  reconciler's journaled seam. A 502 carries `detail.step` (`node_id` means
 *  "not mirrored yet, wait for the sync"). */
export function removeDependency({ workItemId, blocker }: RemoveDependencyInput): Promise<RemoveDependencyResult> {
  return api.del<RemoveDependencyResult>(
    `/pm/work-items/${encodeURIComponent(workItemId)}/deps?blocker=${encodeURIComponent(blocker)}`)
}
```

`hooks.ts` — import the new fetchers and types, then after `useAdvanceInitiative`:

```ts
/** One stage back. Same invalidations as advance: the map lists it, the detail shows it. */
export function useStepBackInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug) => stepBackInitiative(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}

/** Clear a block. The row leaves the Needs-you pile's blocked section, so the map refreshes too. */
export function useUnblockInitiative() {
  const qc = useQueryClient()
  return useMutation<Initiative, ApiRequestError, string>({
    mutationFn: (slug) => unblockInitiative(slug),
    onSuccess: (updated) => {
      void qc.invalidateQueries({ queryKey: QK.initiative(updated.slug) })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}
```

After `useLeaves`:

```ts
/** Remove a blocked-by edge from a leaf. Every leaves list is invalidated by
 *  prefix — the row does not know which scope(s) it is rendered under — and the
 *  map, whose blocked counts and initiative tiers read the same dep rows. */
export function useRemoveDependency() {
  const qc = useQueryClient()
  return useMutation<RemoveDependencyResult, ApiRequestError, RemoveDependencyInput>({
    mutationFn: (input) => removeDependency(input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['pm', 'leaves'] })
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    meta: PM_MUTATION_META,
  })
}
```

- [ ] **Step 2: Typecheck**

Run: `(cd web && npm run typecheck)`
Expected: clean.

- [ ] **Step 3: Commit**

```
feat(web): fetchers and hooks for step-back, unblock and dependency removal

Refs #1472
```

---

### Task 8: The leaf row names its blockers and can remove one

**Files:**
- Modify: `web/src/features/roadmap/detail/LeafList.tsx`
- Modify: `web/src/lib/githubUrl.ts` (add `issueUrl`)
- Test: `web/src/features/roadmap/detail/CapabilityDetail.test.tsx` (mock + one new case), `web/src/lib/githubUrl.test.ts` if it exists (else skip)

**Interfaces:**
- `issueUrl(repo: string, number: number): string` → `https://github.com/{repo}/issues/{number}`, beside `prUrl`.
- `LeafRow` renders, under the title when `blockedBy.length > 0`, a `<ul aria-label="Blocked by">` with one `<li>` per ref: a link (text `#N` when the blocker's repo equals the leaf's repo, else the full ref) and a ghost `sm` Button `aria-label={\`Remove blocker ${ref}\`}` text `Remove`. The existing count Badge stays.
- Failure: a Banner (tone danger) inside the row with `extractMutationErrorMessage(error, 'Could not remove the dependency')`; only the row whose removal failed shows it (the mutation is per-row: call `useRemoveDependency()` inside `LeafRow`).

- [ ] **Step 1: Failing test**

In `CapabilityDetail.test.tsx`: add `useRemoveDependency` to the `vi.mock('@/api/hooks', …)` factory, backed by a hoisted `removeMutate = vi.fn()` and per-test `removeState = { isPending: false, isError: false, error: null as unknown }`:

```ts
  useRemoveDependency: () => ({ mutate: removeMutate, ...removeState }),
```

Then, beside the existing `'Issues: open work first, with its blockers, score and live session'` test:

```ts
  it('Issues: a blocked leaf names each blocker and offers to remove it', () => {
    mount(tasks)
    fireEvent.click(screen.getByRole('tab', { name: /Issues/ }))
    const list = screen.getByRole('list', { name: 'Blocked by' })
    // Same repo as the leaf → short form; the link goes to the blocker on GitHub.
    expect(within(list).getByRole('link', { name: '#7' }))
      .toHaveAttribute('href', 'https://github.com/cairnea/KinDash/issues/7')
    fireEvent.click(screen.getByRole('button', { name: 'Remove blocker cairnea/KinDash#7' }))
    expect(removeMutate).toHaveBeenCalledWith({ workItemId: 'w2', blocker: 'cairnea/KinDash#7' })
  })

  it('Issues: a refused removal says so on that row only', () => {
    Object.assign(removeState, {
      isError: true,
      error: new ApiRequestError(502, { detail: { error: 'dep_remove_failed', step: 'node_id', message: 'o/r#7 has no GitHub node id yet; wait for the next sync' } }),
    })
    mount(tasks)
    fireEvent.click(screen.getByRole('tab', { name: /Issues/ }))
    expect(screen.getByText('o/r#7 has no GitHub node id yet; wait for the next sync')).toBeInTheDocument()
  })
```

Check `ApiRequestError`'s constructor signature in `web/src/api/client.ts` and how `extractMutationErrorMessage` (`web/src/api/queryClient.ts`) reads the body; construct the error the way the materialization test (`InitiativePage.materialization.test.tsx`) does for its 502 case. Import `within` from `@testing-library/react`. Find how that file opens the Issues tab (the existing blockers test does it) and copy that.

- [ ] **Step 2: Run to verify it fails**

Run: `(cd web && npx vitest run src/features/roadmap/detail/CapabilityDetail.test.tsx)`
Expected: FAIL — no list named "Blocked by".

- [ ] **Step 3: Implement**

`githubUrl.ts`, beside `prUrl`:

```ts
export function issueUrl(repo: string, number: number): string {
  return `https://github.com/${repo}/issues/${number}`
}
```

`LeafList.tsx` — `LeafRow` becomes:

```tsx
/** `owner/repo#N` → `#N` when it is the leaf's own repo; the full ref otherwise. */
function shortRef(ref: string, ownRepo: string): string {
  const [repo, num] = ref.split('#')
  return repo === ownRepo ? `#${num}` : ref
}

function BlockerRow({ leaf, ref }: { leaf: Leaf; ref: string }) {
  const remove = useRemoveDependency()
  const [repo, num] = ref.split('#')
  return (
    <li className="flex flex-col gap-1">
      <span className="flex items-center gap-2">
        <a
          href={issueUrl(repo ?? '', Number(num))}
          target="_blank"
          rel="noreferrer"
          className="text-ink hover:underline"
        >
          {shortRef(ref, leaf.repo)}
        </a>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={`Remove blocker ${ref}`}
          loading={remove.isPending}
          onClick={() => remove.mutate({ workItemId: leaf.id, blocker: ref })}
        >
          Remove
        </Button>
      </span>
      {remove.isError && (
        <Banner tone="danger" title={extractMutationErrorMessage(remove.error, 'Could not remove the dependency')} />
      )}
    </li>
  )
}

function LeafRow({ leaf }: { leaf: Leaf }) {
  const blockers = leaf.blockedBy.length
  return (
    <li className="flex flex-col gap-1 rounded-md border border-edge bg-surface p-2 text-sm">
      <div className="flex items-center gap-2">
        {/* …the existing row content, unchanged… */}
      </div>
      {blockers > 0 && (
        // GitHub owns the edge, but removing it is the one disposition a blocked
        // leaf has, so the row offers it instead of pointing at a blocker count.
        <ul aria-label="Blocked by" className="flex flex-wrap gap-x-4 gap-y-1 pl-6 text-xs text-ink-muted">
          {leaf.blockedBy.map((ref) => <BlockerRow key={ref} leaf={leaf} ref={ref} />)}
        </ul>
      )}
    </li>
  )
}
```

Move the existing `<li …>` body into the inner `<div>` verbatim (the `TierDot`, link/span, sr-only tier, session badge, blocker-count badge, score). `ref` is a reserved prop name in React — call the prop `blockerRef` instead. Imports: `Button` from `@/ui/Button`, `useRemoveDependency` from `@/api/hooks`, `extractMutationErrorMessage` from `@/api/queryClient`, `issueUrl` from `@/lib/githubUrl`.

- [ ] **Step 4: Run**

Run: `(cd web && npx vitest run src/features/roadmap)`
Expected: PASS.

- [ ] **Step 5: Commit**

```
feat(roadmap): leaf rows link each blocker and can remove the dependency

Refs #1472
```

---

### Task 9: The initiative page: Epic tab shows leaves; header gains Unblock and Step back

**Files:**
- Modify: `web/src/features/roadmap/InitiativePage.tsx` (`EpicTab` ~line 243; header actions ~line 387; dialogs ~line 461)
- Test: `web/src/features/roadmap/InitiativePage.test.tsx` (mock + cases), `web/src/features/roadmap/InitiativePage.materialization.test.tsx` (mock only)

**Interfaces:**
- `EpicTab` keeps the epic link and the four tiles, and replaces the `children` `<ul>` with `<LeafList scope={{ initiative: initiative.slug }} />`. The `InitiativeEpicChild` type and `epic.children` stay in `api/sdlc.ts` (the assistant reads them from the detail endpoint); only this page stops rendering them.
- Header `actions` becomes a `<div className="flex items-center gap-2">` with, in order: **Step back** (ghost, sm; disabled when `!prev || blocked || stepBack.isPending`; title `Move the gate back to {stageLabel(prev)}`; opens its own confirm), **Unblock** (rendered only when `initiative.status === 'blocked'`; `variant="danger"` sm; disabled while the materialization proposal is `pending` with title `Accept or reject the pending materialization first`; opens its own confirm), **Advance** (existing; additionally `disabled` when blocked with title `Clear the block first`).
- `prev` = `prevStage(initiative.stage)` — add `prevStage` to `vocab.ts` beside `nextStage` (same shape, `i - 1`, `null` at index 0 or unknown).
- Confirm copy. Step back: title `Step back to {stageLabel(prev)}?`, description `This moves the human gate back one stage. The stage engine re-enters it on its next tick.`, confirm button `aria-label="Confirm step back"` text `Step back`. Unblock: title `Clear the block?`, description `This returns the initiative to awaiting-human without moving the gate. If the failed materialize left an epic or sub-issues on GitHub, close them first: the next decomposition creates a fresh set.`, confirm button `aria-label="Confirm unblock"` text `Unblock`, `variant="danger"`, size `lg`.
- Errors: `stepBack.isError` / `unblock.isError` render the same Banner idiom as `advance.isError`, with fallbacks `'Could not step back'` / `'Could not clear the block'`.
- The page needs the proposal's status for the Unblock guard: `const { data: proposal } = useMaterialization(initiativeSlug || null)` at page level (the `MaterializationCard` already calls it; React Query dedupes — one request).

- [ ] **Step 1: Failing tests**

`InitiativePage.test.tsx`: extend the hoisted bag with `stepBackMutate`, `unblockMutate`, `state.proposal: null`, and the mock with

```ts
  useLeaves: () => ({ data: h.state.leaves, isLoading: false, isError: false }),
  useRemoveDependency: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useStepBackInitiative: () => ({ mutate: h.stepBackMutate, isPending: false, isError: false, error: null }),
  useUnblockInitiative: () => ({ mutate: h.unblockMutate, isPending: false, isError: false, error: null }),
```

with `h.state.leaves` defaulting to `[]` and `useMaterialization: () => ({ data: h.state.proposal })`. Reset all of it in `beforeEach`. Then add:

```ts
  it('Epic: renders the leaves under the epic, not the bare children list', () => {
    h.state.leaves = [{
      id: 'w1', ref: 'cairnea/KinDash#1', repo: 'cairnea/KinDash', number: 1, kind: 'issue',
      title: 'Child one', url: 'https://x/1', state: 'open', triageState: 'classified', score: 3,
      reach: null, impact: null, effort: null, confidence: null, tier: 'blocked',
      blockedBy: ['cairnea/KinDash#9'], parentRef: 'cairnea/KinDash#15727', sessionStatus: null, ghCreatedAt: null,
    }]
    mount('/initiatives/views?tab=epic')
    expect(screen.getByRole('link', { name: '#1 Child one' })).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Blocked by' })).toBeInTheDocument()
  })

  it('steps back only after confirming, and never past intake', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: /Step back/ }))
    expect(h.stepBackMutate).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm step back' }))
    expect(h.stepBackMutate).toHaveBeenCalledWith('views', expect.anything())
    h.state.data = { ...h.initiative, stage: 'intake' }
    mount()
    expect(screen.getAllByRole('button', { name: /Step back/ }).at(-1)).toBeDisabled()
  })

  it('a blocked initiative offers Unblock, disables Advance, and confirms before clearing', () => {
    h.state.data = { ...h.initiative, status: 'blocked' }
    mount()
    expect(screen.getByRole('button', { name: /Advance/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Unblock' }))
    expect(h.unblockMutate).not.toHaveBeenCalled()
    expect(screen.getByText(/close them first/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm unblock' }))
    expect(h.unblockMutate).toHaveBeenCalledWith('views', expect.anything())
  })

  it('Unblock waits for a pending materialization to be decided', () => {
    h.state.data = { ...h.initiative, status: 'blocked' }
    h.state.proposal = { id: 'p1', status: 'pending', tasks: [], created: null }
    mount()
    expect(screen.getByRole('button', { name: 'Unblock' })).toBeDisabled()
  })

  it('an unblocked initiative has no Unblock button', () => {
    mount()
    expect(screen.queryByRole('button', { name: 'Unblock' })).not.toBeInTheDocument()
  })
```

Check how the existing test opens a tab (`?tab=epic` is what `useTabParam` reads — confirm in `useTabParam.ts`). The existing `'Epic tab lists sub-issues'`-style test (if present) must be updated to the leaves shape or deleted — the children list is gone. For the mis-mounted second render in the intake case, prefer `cleanup()` between mounts if the file already uses it.

`InitiativePage.materialization.test.tsx`: add the same four hook stubs to its `vi.mock` (`useLeaves: () => ({ data: [], isLoading: false, isError: false })`, and `vi.fn()`-backed `useRemoveDependency`, `useStepBackInitiative`, `useUnblockInitiative`).

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/features/roadmap/InitiativePage)`
Expected: FAIL — missing buttons / list.

- [ ] **Step 3: Implement**

`vocab.ts` beside `nextStage`:

```ts
/** The stage the step-back tap would return an initiative to, or null at the first stage. */
export function prevStage(stage: string | null | undefined): InitiativeStage | null {
  const i = INITIATIVE_STAGES.indexOf(stage as InitiativeStage)
  return i <= 0 ? null : INITIATIVE_STAGES[i - 1]!
}
```

`InitiativePage.tsx`:

- Imports: add `useStepBackInitiative, useUnblockInitiative` to the hooks import, `prevStage` to the vocab import, `LeafList` from `./detail/LeafList`.
- `EpicTab`: delete `const children = epic.children ?? []` and the `children.length === 0 ? … : <ul …>` block; render `<LeafList scope={{ initiative: initiative.slug }} />` in its place. Update the function's leading comment: "The sub-issues come from the leaves endpoint (tier, blockers, live session, and the Remove disposition), not from the detail payload's `children`, which stays for the assistant."
- Page body, after `const advance = useAdvanceInitiative()`:

```tsx
  const stepBack = useStepBackInitiative()
  const unblock = useUnblockInitiative()
  const { data: proposal } = useMaterialization(initiativeSlug || null)
  const [confirmingBack, setConfirmingBack] = useState(false)
  const [confirmingUnblock, setConfirmingUnblock] = useState(false)
  const prev = prevStage(initiative?.stage)
  const blocked = initiative?.status === 'blocked'
  const proposalPending = proposal?.status === 'pending'
```

- Header `actions`:

```tsx
        actions={initiative && (
          <div className="flex items-center gap-2">
            {/* The undo for a mis-tapped Advance. Disabled, not absent, at intake
                for the same reason Advance is at done: the row of controls must
                not change shape as the initiative moves. */}
            <Button
              size="sm"
              variant="ghost"
              disabled={!prev || blocked || stepBack.isPending}
              onClick={() => setConfirmingBack(true)}
              title={blocked ? 'Clear the block first' : prev ? `Move the gate back to ${stageLabel(prev)}` : 'Already at the first stage'}
            >
              Step back
            </Button>
            {/* Only a blocked row can be unblocked; a button that is usually
                disabled would read as a control that never works. */}
            {blocked && (
              <Button
                size="sm"
                variant="danger"
                disabled={proposalPending || unblock.isPending}
                onClick={() => setConfirmingUnblock(true)}
                title={proposalPending ? 'Accept or reject the pending materialization first' : 'Clear the block without moving the gate'}
              >
                Unblock
              </Button>
            )}
            <Button
              size="sm"
              disabled={!next || blocked || advance.isPending}
              onClick={() => setConfirming(true)}
              title={blocked ? 'Clear the block first' : next ? `Move the gate to ${stageLabel(next)}` : 'Already at the final stage'}
            >
              {next ? `Advance → ${stageLabel(next)}` : 'Complete'}
            </Button>
          </div>
        )}
```

- Error banners beside the existing `advance.isError` one:

```tsx
        {stepBack.isError && (
          <Banner tone="danger" className="mb-4" title={extractMutationErrorMessage(stepBack.error, 'Could not step back')} />
        )}
        {unblock.isError && (
          <Banner tone="danger" className="mb-4" title={extractMutationErrorMessage(unblock.error, 'Could not clear the block')} />
        )}
```

- Dialogs after the Advance one:

```tsx
      {initiative && prev && (
        <Dialog
          open={confirmingBack}
          onClose={() => setConfirmingBack(false)}
          title={`Step back to ${stageLabel(prev)}?`}
          size="sm"
          description="This moves the human gate back one stage. The stage engine re-enters it on its next tick."
        >
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirmingBack(false)}>Cancel</Button>
            <Button
              size="lg"
              aria-label="Confirm step back"
              loading={stepBack.isPending}
              onClick={() => stepBack.mutate(initiative.slug, { onSettled: () => setConfirmingBack(false) })}
            >
              Step back
            </Button>
          </div>
        </Dialog>
      )}
      {initiative && blocked && (
        <Dialog
          open={confirmingUnblock}
          onClose={() => setConfirmingUnblock(false)}
          title="Clear the block?"
          size="sm"
          description={
            'This returns the initiative to awaiting-human without moving the gate. '
            + `If the failed materialize left an ${EPIC.singular.toLowerCase()} or sub-issues on GitHub, `
            + 'close them first: the next decomposition creates a fresh set.'
          }
        >
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setConfirmingUnblock(false)}>Cancel</Button>
            <Button
              size="lg"
              variant="danger"
              aria-label="Confirm unblock"
              loading={unblock.isPending}
              onClick={() => unblock.mutate(initiative.slug, { onSettled: () => setConfirmingUnblock(false) })}
            >
              Unblock
            </Button>
          </div>
        </Dialog>
      )}
```

- Update the file's header comment (line 5: "…and it carries the human gate (Advance)…") to name the three gate controls: Advance, Step back, and Unblock.

- [ ] **Step 4: Run the FULL web suite plus lint, typecheck, build**

```
(cd web && npm run typecheck && npm run lint && npm run test 2>&1 | tee /tmp/blocked-disposition-web.log; echo "exit=${PIPESTATUS[0]}")
(cd web && npm run build)
```

Expected: all green. `npm run test` must be the full run (shared-hook mocks elsewhere break when a component gains a hook — that is why `InitiativePage.materialization.test.tsx` got the stubs).

- [ ] **Step 5: Commit**

```
feat(roadmap): initiative page gains Step back and Unblock; Epic tab shows leaves

Refs #1472
```

---

### Task 10: Spec table reflects what shipped; branch, PR

**Files:**
- Modify: `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md` (§8 table, ~line 236–238)

- [ ] **Step 1: Update the two §8 rows**

Replace the "Add / remove issue dependency" row with:

`| Remove issue dependency | \`Remove\` beside each blocker on the leaf row | GitHub native \`removeBlockedBy\` through the reconciler journal (\`DELETE /api/pm/work-items/{id}/deps?blocker=\`); add stays a GitHub-side edit for now |`

and the "Advance / step back a stage" row with:

`| Advance / step back / unblock | Header buttons with confirm; Advance and Step back refuse a blocked row, Unblock returns it to awaiting-human without moving the gate | ccswitch DB (\`/advance\`, \`/step-back\`, \`/unblock\`); back-synced to the board Stage field |`

- [ ] **Step 2: Finish per the completion workflow**

1. `git fetch origin && git checkout -b feat/roadmap-blocked-disposition origin/master` — do this BEFORE the first commit if the executor is still on the session branch; otherwise cherry-pick the task commits onto it. Never branch off local `master`.
2. Re-run the server four-step gate and the four web scripts once more on the final tree.
3. Push, `gh pr create` with the body written to a file (`--body-file`), title `feat(roadmap): disposition blocked initiatives and issues (row 8, part)`. Body: the four gaps from the assessment, what each task shipped, `Refs #1472`, the Claude Code trailer.
4. `gh pr ready <#>` if needed, then `gh pr merge <#> --squash --auto`.

---

## Self-review

**Spec coverage.** §8 "Remove issue dependency" → Tasks 3–5, 8. §8 "step back a stage" and §10 `step-back` endpoint → Tasks 2, 7, 9. Item 17 "blocked items" in the Needs-you pile → Tasks 1, 6. §14 "step-back guards" → Task 2 tests. The user's fourth finding (Advance doubling as unblock) → Task 2 + Task 9. Not in scope, stated: adding a dependency from the app, editing initiative title/capabilities (`PATCH /initiatives/{slug}`), map-changes PRs — the rest of row 8.

**Placeholder scan.** Every code step carries code. Two "read the real signature first" instructions remain (`owning_projects_by_repo`, `_client(db_url)` parity, `ApiRequestError` constructor, the `ProjectRepo` sync column) because copying a guessed signature into the plan would be worse than telling the implementer to look.

**Type consistency.** `remove_blocked_by_mutation(blocked_node, blocker_node, *, initiated_by)` — same in Tasks 3 and 4. `remove(db, *, src, blocker_ref, mutate, now)` — same in Task 4's tests and implementation. Seam name `pm_remove_dependency(work_item_id, blocker_ref)` — facade, RPC, remote, and the Task 5 route/test all agree. Web: `removeDependency({ workItemId, blocker })` in Task 7 and Task 8's assertion. `prevStage` defined in Task 9 before use. Reason strings `'blocked'` / `'epic_blocked'` — Task 1 server, Task 6 labels.
