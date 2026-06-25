# PR Auto-fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a session's PR fails CI, react per a global policy — surface a one-tap "Fix PR #N" action (`propose`) or autonomously inject a branch-aware fix prompt into the owning session (`auto`), with a loop guard that prevents repeated auto-attempts.

**Architecture:** A new `AppSettings.pr_autofix_mode` (`off`|`propose`|`auto`, default `propose`) gates behavior. Both triggers — the existing `_on_pr_change` poller callback (auto) and a new `POST /api/sessions/{id}/fix-pr` endpoint (propose tap) — converge on one orchestrator method `request_pr_fix(session_id, pr_number)` that builds a branch-aware prompt via the pure `build_pr_fix_prompt(repo, pr)` and injects it through the existing `send_prompt` path. The auto path adds an in-memory `_pr_fix_attempts` loop guard (one attempt per failure event, re-armed on recovery) living in `_on_pr_change`.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (async, SQLite) backend; React + TypeScript + Vite + Tailwind + React Query frontend; pytest (backend), vitest + tsc (frontend).

## Global Constraints

- **Nullable columns only:** `init_db` backfills missing nullable columns; `create_all` never ALTERs. New `AppSettings` column MUST be nullable; code treats `NULL`/unknown as the default. (CLAUDE.md "Dev / testing".)
- **No parallel implementations (constitution VI):** route both triggers through one `request_pr_fix`; reuse `anyChecksFailing`/`isActionablePrState` for failing-PR gating, `send_prompt` for injection, `AppSettings`/`SettingsPanel` for config. Do not add a second failing-PR predicate or a second prompt-injection path.
- **Default mode is `propose`.** `off` = today's notify-only behavior; `propose` = surface action; `auto` = inject.
- **Owning session does the work**, matched by `(repo, branch)`. Never spawn a new session.
- **Auto loop guard:** exactly one auto-attempt per distinct failure event; re-arm only after the PR's checks go `passing` or the PR is `merged`/`closed`. The guard is **auto-path only** — a manual propose tap is always allowed (human-initiated).
- **Auto no-driver fallback:** in `auto` mode, if the owning session has no live driver (or is draining), do NOT resume it — fall back to the push notification.
- **Backend lint gates (run before pushing):** `ruff`, `mypy`, `pytest server/tests/`. Worktree backend tests need `PYTHONPATH=<worktree>/src` and may need the main checkout's prebuilt venv python.
- **Frontend gates:** `vitest run`, `tsc -b` (NOT `tsc --noEmit` — it checks nothing), `eslint` (max-warnings 0), all run in `web/`.
- **Test auth:** API tests 401 unless `CCSWITCH_TAILNET_ALLOWLIST` includes the test identity; run with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest …` or pin the allowlist per-test (follow existing `test_settings.py` / `test_sessions*.py` patterns).
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

**Backend**
- `server/db/models.py` — add `pr_autofix_mode` column to `AppSettings` (Task 1).
- `server/api/settings.py` — expose/validate `prAutofixMode` in GET/PUT (Task 1).
- `server/sessions/pr_fix.py` — **new**: pure `build_pr_fix_prompt(repo, pr)` prompt builder + `VALID_AUTOFIX_MODES` constant (Task 2).
- `server/sessions/orchestrator.py` — `request_pr_fix()` method, `_pr_autofix_mode()` reader, `_pr_fix_attempts` guard, wire `_on_pr_change` (Task 3).
- `server/api/sessions.py` — `POST /{session_id}/fix-pr` endpoint (Task 4).

**Frontend**
- `web/src/api/hooks.ts` — add `prAutofixMode` to `AppSettings`; add `useFixPr()` mutation (Tasks 5, 6).
- `web/src/components/settings/SettingsPanel.tsx` — "PR auto-fix" dropdown (Task 5).
- `web/src/components/SessionCard.tsx` — render "Fix PR #N" action when mode is `propose` and a PR is failing-and-actionable (Task 6).

---

## Task 1: Settings field `pr_autofix_mode`

**Files:**
- Modify: `server/db/models.py:176-187` (`AppSettings`)
- Modify: `server/api/settings.py:18-60`
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Produces: `AppSettings.pr_autofix_mode: Mapped[str | None]`; wire field `prAutofixMode` in `/api/settings` GET (always present, default `"propose"`) and PUT (validated against `{"off","propose","auto"}`).

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_settings.py` (follow the existing client/auth fixtures in that file):

```python
@pytest.mark.asyncio
async def test_pr_autofix_mode_defaults_to_propose(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["prAutofixMode"] == "propose"


@pytest.mark.asyncio
async def test_pr_autofix_mode_roundtrip(client):
    resp = await client.put("/api/settings", json={"prAutofixMode": "auto"})
    assert resp.status_code == 200
    assert resp.json()["prAutofixMode"] == "auto"
    assert (await client.get("/api/settings")).json()["prAutofixMode"] == "auto"


@pytest.mark.asyncio
async def test_pr_autofix_mode_rejects_invalid(client):
    await client.put("/api/settings", json={"prAutofixMode": "auto"})
    resp = await client.put("/api/settings", json={"prAutofixMode": "bogus"})
    assert resp.status_code == 200
    # Invalid value silently ignored — prior good value preserved.
    assert resp.json()["prAutofixMode"] == "auto"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/880d4c55 && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_settings.py -k pr_autofix -v`
Expected: FAIL — `KeyError: 'prAutofixMode'` (field not in response).

- [ ] **Step 3: Add the column**

In `server/db/models.py`, inside `class AppSettings`, after the `default_tool_detail` line (185):

```python
    # 'off' | 'propose' | 'auto' — how to react when a session's PR fails CI.
    # Nullable: init_db backfills nullable columns; code defaults NULL → 'propose'.
    pr_autofix_mode: Mapped[str | None] = mapped_column(String, nullable=True)
```

- [ ] **Step 4: Wire the API**

In `server/api/settings.py`:

After line 15 add the valid set:
```python
_VALID_AUTOFIX = {"off", "propose", "auto"}
```

In `class SettingsBody` add a field:
```python
    prAutofixMode: str | None = None
```

In `_to_dict` add (NULL/unknown → default `"propose"`):
```python
        "prAutofixMode": s.pr_autofix_mode or "propose",
```

In `put_settings_row`, before `await db.flush()`:
```python
    if body.prAutofixMode is not None and body.prAutofixMode in _VALID_AUTOFIX:
        row.pr_autofix_mode = body.prAutofixMode
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_settings.py -v`
Expected: PASS (new tests + existing settings tests).

- [ ] **Step 6: Lint**

Run: `ruff check server/api/settings.py server/db/models.py && mypy server/api/settings.py server/db/models.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add server/db/models.py server/api/settings.py server/tests/test_settings.py
git commit -m "feat(settings): add pr_autofix_mode (off/propose/auto, default propose)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Pure fix-prompt builder

**Files:**
- Create: `server/sessions/pr_fix.py`
- Test: `server/tests/test_pr_fix.py`

**Interfaces:**
- Consumes: `PullRequestInfo` from `server.github.pr_status` (fields `.number`, `.url`, `.state`, `.checks`, `.title`).
- Produces:
  - `VALID_AUTOFIX_MODES: frozenset[str]` = `{"off","propose","auto"}`
  - `build_pr_fix_prompt(repo: str, pr: PullRequestInfo) -> str`

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_pr_fix.py`:

```python
from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import build_pr_fix_prompt, VALID_AUTOFIX_MODES


def _pr(**kw):
    base = dict(number=42, url="https://github.com/o/r/pull/42",
               state="open", checks="failing", title="Add widget")
    base.update(kw)
    return PullRequestInfo(**base)


def test_modes_constant():
    assert VALID_AUTOFIX_MODES == frozenset({"off", "propose", "auto"})


def test_prompt_names_repo_pr_and_branch_checkout():
    text = build_pr_fix_prompt("owner/repo", _pr())
    # Identifies the target PR and repo.
    assert "#42" in text
    assert "owner/repo" in text
    # Branch-aware: must instruct checking out the PR branch first, since the
    # worktree HEAD may have moved on to a newer feature branch.
    assert "gh pr checkout 42" in text
    # Directs the agent to inspect the failing checks and push a fix.
    assert "gh pr checks 42" in text
    assert "push" in text.lower()


def test_prompt_includes_title_for_context():
    text = build_pr_fix_prompt("owner/repo", _pr(title="Fix login race"))
    assert "Fix login race" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_fix.py -v`
Expected: FAIL — `ModuleNotFoundError: server.sessions.pr_fix`.

- [ ] **Step 3: Implement the builder**

Create `server/sessions/pr_fix.py`:

```python
"""Pure helpers for the PR auto-fix feature: the policy-mode vocabulary and the
branch-aware fix prompt. No I/O — kept pure so it's trivially testable and so the
orchestrator method that wires it stays small."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo

# The single source of truth for the pr_autofix_mode vocabulary. The settings
# API validates against the same set (kept in sync there for the HTTP layer).
VALID_AUTOFIX_MODES: frozenset[str] = frozenset({"off", "propose", "auto"})


def build_pr_fix_prompt(repo: str, pr: PullRequestInfo) -> str:
    """A system-originated prompt instructing the owning session to restore a
    failing PR.

    Branch-aware on purpose: ccswitch's completion workflow moves the worktree
    onto a fresh feat/* branch after opening a PR, so by the time an older PR's
    CI fails the worktree HEAD is usually NOT on the PR's branch. The prompt
    therefore tells the agent to check the PR branch out FIRST rather than
    assuming the current diff is the PR.
    """
    return (
        f"Your pull request {repo}#{pr.number} (\"{pr.title}\") has failing CI checks. "
        f"Investigate and restore it.\n\n"
        f"Steps:\n"
        f"1. Check out the PR branch first — your worktree HEAD may have moved on: "
        f"`gh pr checkout {pr.number}` (then `git pull` to get the latest).\n"
        f"2. Inspect what's red: `gh pr checks {pr.number}` and reproduce the failing "
        f"job locally (run the relevant tests/lint/build).\n"
        f"3. Fix the root cause, commit, and push so CI re-runs.\n"
        f"4. Briefly report what was broken and what you changed.\n\n"
        f"PR: {pr.url}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_fix.py -v`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `ruff check server/sessions/pr_fix.py server/tests/test_pr_fix.py && mypy server/sessions/pr_fix.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/sessions/pr_fix.py server/tests/test_pr_fix.py
git commit -m "feat(pr-fix): branch-aware fix-prompt builder + autofix mode vocabulary

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `request_pr_fix` + loop guard, wired into `_on_pr_change`

**Files:**
- Modify: `server/sessions/orchestrator.py:240` (init), `:682-704` (`_on_pr_change`), add `request_pr_fix` + `_pr_autofix_mode` near `prs_for` (`:1032-1057`)
- Test: `server/tests/test_pr_autofix.py`

**Interfaces:**
- Consumes: `build_pr_fix_prompt`, `VALID_AUTOFIX_MODES` (Task 2); existing `self.pr_for(repo, branch) -> PullRequestInfo | None`, `self._drivers`, `self.send_prompt(session_id, text) -> str`, `self._records`, `is_actionable` via `PullRequestInfo.state in ("open","draft")`.
- Produces:
  - `async def request_pr_fix(self, session_id: str, pr_number: int) -> str` — returns one of `"sent" | "no_driver" | "draining" | "no_pr" | "not_actionable" | "no_session"`. Resolves repo+branch from the session's authored branches, builds the prompt, injects via `send_prompt`. Does NOT itself resume a driver and does NOT apply the loop guard (caller's concern).
  - `async def _pr_autofix_mode(self) -> str` — reads `AppSettings.pr_autofix_mode`, NULL/unknown → `"propose"`.
  - `self._pr_fix_attempts: set[tuple[str, str]]` — auto-attempt guard keyed by `(repo, branch)`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_pr_autofix.py`. Use the orchestrator test harness already used by `server/tests/` (mirror the construction in the existing `_on_pr_change` / PR-poller tests — search for `_on_pr_change(` in `server/tests/` and reuse that fixture/builder). The tests below assume a helper `orch` with one record whose `repo`/`branch`/`authored_branches` are set, a stubbed `pr_for`, a recording `send_prompt`, and a settable autofix mode.

```python
import pytest
from server.github.pr_status import PullRequestInfo


def _pr(number=7, state="open", checks="failing"):
    return PullRequestInfo(number=number, url=f"https://x/pull/{number}",
                           state=state, checks=checks, title="t")


@pytest.mark.asyncio
async def test_request_pr_fix_injects_for_actionable_failing(orch_with_failing_pr):
    orch, sent = orch_with_failing_pr  # sent: list capturing send_prompt(text)
    result = await orch.request_pr_fix("sess1", 7)
    assert result == "sent"
    assert len(sent) == 1
    assert "gh pr checkout 7" in sent[0]


@pytest.mark.asyncio
async def test_request_pr_fix_skips_terminal_pr(orch_with_pr):
    orch, sent = orch_with_pr(_pr(state="merged", checks="failing"))
    assert await orch.request_pr_fix("sess1", 7) == "not_actionable"
    assert sent == []


@pytest.mark.asyncio
async def test_request_pr_fix_no_pr_number_match(orch_with_pr):
    orch, sent = orch_with_pr(_pr(number=7))
    assert await orch.request_pr_fix("sess1", 999) == "no_pr"
    assert sent == []


@pytest.mark.asyncio
async def test_auto_mode_injects_once_per_failure_event(orch_with_failing_pr):
    orch, sent = orch_with_failing_pr
    await orch.set_autofix_mode("auto")
    # First failure → one auto-attempt.
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(sent) == 1
    # Second failing signal without recovery → NO second attempt (guard holds).
    await orch._on_pr_change("o/r", "feat/x", "failing", "failing")
    assert len(sent) == 1
    # Recovery clears the guard; a fresh break re-attempts.
    await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_off_mode_never_injects(orch_with_failing_pr):
    orch, sent = orch_with_failing_pr
    await orch.set_autofix_mode("off")
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert sent == []


@pytest.mark.asyncio
async def test_propose_mode_never_auto_injects(orch_with_failing_pr):
    orch, sent = orch_with_failing_pr
    await orch.set_autofix_mode("propose")
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert sent == []  # propose surfaces an action; it does not auto-inject


@pytest.mark.asyncio
async def test_auto_mode_no_live_driver_falls_back(orch_with_failing_pr_no_driver):
    orch, sent, pushed = orch_with_failing_pr_no_driver  # pushed: list of notify titles
    await orch.set_autofix_mode("auto")
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert sent == []                # no injection without a live driver
    assert any("PR broke" in t for t in pushed)  # fell back to the notification
```

Add the fixtures at the top of the test file (build on the existing orchestrator construction used elsewhere in `server/tests/`; the key is a record with `repo="o/r"`, `branch="feat/x"`, `authored_branches=["feat/x"]`, a `pr_for` returning the given PR, a `send_prompt` that appends `text` to `sent`, a `push_mod.notify` patched to append to `pushed`, and a live entry in `orch._drivers["sess1"]` except in the `_no_driver` fixture). Provide `orch.set_autofix_mode(m)` as a thin helper that writes the AppSettings row (or monkeypatches `_pr_autofix_mode`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_autofix.py -v`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'request_pr_fix'`.

- [ ] **Step 3: Init the guard set**

In `server/sessions/orchestrator.py`, right after line 240 (`self._pushed_pr: set[tuple[str, str]] = set()`):

```python
        # Auto-fix loop guard: (repo, branch) we've already auto-attempted since
        # the last recovery. Sibling of _pushed_pr; in-memory, resets on restart.
        self._pr_fix_attempts: set[tuple[str, str]] = set()
```

- [ ] **Step 4: Add `_pr_autofix_mode` and `request_pr_fix`**

Add these methods near `prs_for` (after line 1057). Imports: add `from server.sessions.pr_fix import build_pr_fix_prompt` to the module's imports (top of file) — match existing import style.

```python
    async def _pr_autofix_mode(self) -> str:
        """Current pr_autofix_mode from AppSettings; NULL/unknown → 'propose'."""
        from sqlalchemy import select
        from server.db.models import AppSettings
        async with db_session() as db:
            row = (
                await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))
            ).scalar_one_or_none()
            mode = (row.pr_autofix_mode if row else None) or "propose"
        return mode if mode in ("off", "propose", "auto") else "propose"

    async def request_pr_fix(self, session_id: str, pr_number: int) -> str:
        """Inject a branch-aware fix prompt for the session's PR #pr_number.

        Single source of truth for "restore this PR", shared by the auto path
        (_on_pr_change) and the manual propose endpoint. Resolves the (repo,
        branch) for the PR number from the session's authored branches, gates on
        an actionable (open/draft) state, and injects via send_prompt. Returns a
        status string; does not resume a driver and does not apply the loop guard.
        """
        rec = self._records.get(session_id)
        if rec is None or not rec.repo:
            return "no_session"
        info = None
        for br in getattr(rec, "authored_branches", None) or []:
            cached = self._pr_poller.cached(rec.repo, br)
            if cached is not None and cached.number == pr_number:
                info = cached
                break
        if info is None:
            return "no_pr"
        if info.state not in ("open", "draft"):
            return "not_actionable"
        prompt = build_pr_fix_prompt(rec.repo, info)
        return await self.send_prompt(session_id, prompt)
```

- [ ] **Step 5: Wire `_on_pr_change`**

Replace the body of `_on_pr_change` (lines 691-704) so recovery clears BOTH guards and a failure consults the mode. Keep the existing notification as the universal fallback.

```python
        key = (repo, branch)
        if new_checks != "failing":
            self._pushed_pr.discard(key)
            self._pr_fix_attempts.discard(key)  # recovery re-arms a future auto-fix
            return
        rec = next(
            (r for r in self._records.values() if r.repo == repo and r.branch == branch),
            None,
        )
        if rec is None:
            return

        mode = await self._pr_autofix_mode()
        # Auto: one attempt per failure event, only if a live driver exists.
        if mode == "auto" and key not in self._pr_fix_attempts:
            info = self.pr_for(repo, branch)
            if info is not None and session_id_has_live_driver := (rec.id in self._drivers):
                self._pr_fix_attempts.add(key)
                result = await self.request_pr_fix(rec.id, info.number)
                if result == "sent":
                    return  # injected; skip the redundant break notification
                # Could not inject (draining / not actionable) → fall through to notify.

        # Notify (off, propose, or auto-fallback). Debounced like before.
        if key in self._pushed_pr:
            return
        self._pushed_pr.add(key)
        await push_mod.notify(
            "ccswitch — PR broke", f"{rec.name}: {repo} CI failing", url=f"/session/{rec.id}"
        )
```

Note: the walrus `session_id_has_live_driver :=` is only to express "live driver present" inline; if the project's ruff config forbids unused-name walrus, replace with a plain `if info is not None and rec.id in self._drivers:`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_autofix.py -v`
Expected: PASS.

- [ ] **Step 7: Regression — existing PR-change tests still pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/ -k "pr_change or pr_poll or orchestrator" -v`
Expected: PASS (the notify-only behavior for `off`/`propose` matches the old single-path behavior).

- [ ] **Step 8: Lint**

Run: `ruff check server/sessions/orchestrator.py && mypy server/sessions/orchestrator.py`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_pr_autofix.py
git commit -m "feat(pr-fix): request_pr_fix + auto-mode loop guard in _on_pr_change

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Propose-tap endpoint `POST /api/sessions/{id}/fix-pr`

**Files:**
- Modify: `server/api/sessions.py` (add endpoint near the `/{session_id}/prompt` route at :621)
- Test: `server/tests/test_sessions_fix_pr.py`

**Interfaces:**
- Consumes: `orch.get_session`, `orch.ensure_driver(session_id, identity)`, `orch.request_pr_fix(session_id, pr_number)`, `record_audit`, `_record_to_dict`, `_resolve_repo` (all already in `server/api/sessions.py`).
- Produces: `POST /api/sessions/{session_id}/fix-pr` with body `{ "number": int }` → 200 with the session dict; 404 unknown session; 409 resume failure; 422 when the PR can't be fixed (not actionable / no match).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_sessions_fix_pr.py` (mirror auth/client setup from `server/tests/test_sessions*.py`):

```python
import pytest


@pytest.mark.asyncio
async def test_fix_pr_injects_and_returns_session(client, seed_failing_pr_session):
    sid = seed_failing_pr_session  # session with an actionable failing PR #7
    resp = await client.post(f"/api/sessions/{sid}/fix-pr", json={"number": 7})
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


@pytest.mark.asyncio
async def test_fix_pr_unknown_session_404(client):
    resp = await client.post("/api/sessions/nope/fix-pr", json={"number": 7})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fix_pr_not_actionable_422(client, seed_merged_pr_session):
    sid = seed_merged_pr_session
    resp = await client.post(f"/api/sessions/{sid}/fix-pr", json={"number": 7})
    assert resp.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_fix_pr.py -v`
Expected: FAIL — 404/405 (route not defined).

- [ ] **Step 3: Add the endpoint**

In `server/api/sessions.py`, add a request model near the other Pydantic bodies (where `SendPromptRequest` is defined):

```python
class FixPrRequest(BaseModel):
    number: int
```

Add the route right after `send_session_prompt` (after line 653):

```python
@router.post("/{session_id}/fix-pr")
async def fix_session_pr(
    session_id: str,
    body: FixPrRequest,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    """Manual 'Fix PR #N' tap (propose mode): resume the owning session if
    detached and inject the same branch-aware fix prompt the auto path uses.

    Routes through orch.request_pr_fix so the manual and autonomous fixes are
    one implementation. The auto-mode loop guard does NOT apply here — a human
    tap is always allowed."""
    orch = _get_orchestrator(request)
    record = await orch.get_session(session_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Session {session_id!r} not found"},
        )
    driver = await orch.ensure_driver(session_id, identity)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "resume_failed", "message": "Session could not be resumed."},
        )
    result = await orch.request_pr_fix(session_id, body.number)
    if result != "sent":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": result, "message": f"Cannot fix PR #{body.number}: {result}"},
        )
    await record_audit(db, "session.fix_pr", identity,
                       {"sessionId": session_id, "number": body.number})
    updated = await orch.get_session(session_id) or record
    return _record_to_dict(updated, repo=await _resolve_repo(updated))
```

If `BaseModel` is not already imported in `server/api/sessions.py`, add `from pydantic import BaseModel` (check existing imports first — `SendPromptRequest` implies it already is).

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/test_sessions_fix_pr.py -v`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `ruff check server/api/sessions.py && mypy server/api/sessions.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/api/sessions.py server/tests/test_sessions_fix_pr.py
git commit -m "feat(api): POST /sessions/{id}/fix-pr — manual PR restore (propose tap)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Settings UI — "PR auto-fix" dropdown

**Files:**
- Modify: `web/src/api/hooks.ts:106-127` (`AppSettings` interface)
- Modify: `web/src/components/settings/SettingsPanel.tsx`
- Test: `web/src/components/settings/__tests__/SettingsPanel.test.tsx` (create if absent; otherwise extend)

**Interfaces:**
- Consumes: `useSettings`, `useUpdateSettings` (already present).
- Produces: `AppSettings.prAutofixMode: string`; a labeled `<select id="settings-pr-autofix">` whose value is saved through `useUpdateSettings`.

- [ ] **Step 1: Write the failing test**

Create/extend `web/src/components/settings/__tests__/SettingsPanel.test.tsx` (mirror the render/query-client harness used by other component tests in `web/src/components/**/__tests__`):

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
// ...wrap in the project's QueryClientProvider test helper and mock useSettings
// to return { prAutofixMode: 'propose', ... } and useUpdateSettings to a spy.

describe('SettingsPanel PR auto-fix', () => {
  it('renders the PR auto-fix dropdown with the current value', () => {
    renderPanel({ prAutofixMode: 'auto' })
    const select = screen.getByLabelText(/PR auto-fix/i) as HTMLSelectElement
    expect(select.value).toBe('auto')
  })

  it('saves the chosen mode', () => {
    const mutate = vi.fn()
    renderPanel({ prAutofixMode: 'propose' }, mutate)
    fireEvent.change(screen.getByLabelText(/PR auto-fix/i), { target: { value: 'off' } })
    fireEvent.click(screen.getByRole('button', { name: /save/i }))
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ prAutofixMode: 'off' }),
      expect.anything(),
    )
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components/settings/__tests__/SettingsPanel.test.tsx`
Expected: FAIL — no element labeled "PR auto-fix".

- [ ] **Step 3: Extend the `AppSettings` interface**

In `web/src/api/hooks.ts`, add to `interface AppSettings` (after `defaultToolDetail`):

```ts
  prAutofixMode: string
```

- [ ] **Step 4: Add the dropdown + state + save**

In `web/src/components/settings/SettingsPanel.tsx`:

Add state (after the `toolDetail` state, line 41):
```tsx
  const [prAutofixMode, setPrAutofixMode] = useState(
    settings?.prAutofixMode ?? 'propose',
  )
```

Include it in `handleSave` (inside the `updateSettings.mutate({...})` object, after line 74):
```tsx
      prAutofixMode,
```

Add the dropdown markup. Place it after the "Default tool approvals" block (after line 140, before the "Transcript display" section):
```tsx
          {/* PR auto-fix */}
          <div className="space-y-1">
            <label
              htmlFor="settings-pr-autofix"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              PR auto-fix
            </label>
            <select
              id="settings-pr-autofix"
              value={prAutofixMode}
              onChange={(e) => setPrAutofixMode(e.target.value)}
              className={inputClass}
            >
              <option value="off">Off — notify only</option>
              <option value="propose">Propose — show a one-tap fix action</option>
              <option value="auto">Auto — fix failing PRs automatically</option>
            </select>
          </div>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd web && npx vitest run src/components/settings/__tests__/SettingsPanel.test.tsx`
Expected: PASS.

- [ ] **Step 6: Type + lint**

Run: `cd web && npx tsc -b --force && npx eslint src/components/settings/SettingsPanel.tsx src/api/hooks.ts`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add web/src/api/hooks.ts web/src/components/settings/SettingsPanel.tsx web/src/components/settings/__tests__/SettingsPanel.test.tsx
git commit -m "feat(web): PR auto-fix mode dropdown in settings

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: "Fix PR #N" action on the session card (propose mode)

**Files:**
- Modify: `web/src/api/hooks.ts` (add `useFixPr` mutation near `useSendPrompt` at :179-186)
- Modify: `web/src/components/SessionCard.tsx:240-244`
- Test: `web/src/components/__tests__/SessionCard.fixpr.test.tsx` (or extend the existing SessionCard test)

**Interfaces:**
- Consumes: `useSettings` (for `prAutofixMode`), `anyChecksFailing`/`isActionablePrState` from `@/session/prStatus`, `Session.prs`, `Session.id`.
- Produces: `useFixPr()` → `mutate({ id, number })` POSTing `/sessions/${id}/fix-pr`; a button rendered only when `prAutofixMode === 'propose'` AND the session has an actionable failing PR.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/__tests__/SessionCard.fixpr.test.tsx` (reuse the SessionCard render harness + a `useSettings` mock):

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
// mock useSettings → { prAutofixMode }, useFixPr → spy; build a session with
// prs: [{ number: 7, state: 'open', checks: 'failing', ... }]

describe('SessionCard fix-PR action', () => {
  it('shows "Fix PR #7" in propose mode when a PR is failing', () => {
    renderCard({ prAutofixMode: 'propose', prs: [failingPr(7)] })
    expect(screen.getByRole('button', { name: /fix pr #7/i })).toBeInTheDocument()
  })

  it('hides the action in off mode', () => {
    renderCard({ prAutofixMode: 'off', prs: [failingPr(7)] })
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('hides the action in auto mode (handled autonomously)', () => {
    renderCard({ prAutofixMode: 'auto', prs: [failingPr(7)] })
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('hides the action when no PR is failing', () => {
    renderCard({ prAutofixMode: 'propose', prs: [{ ...failingPr(7), checks: 'passing' }] })
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('hides the action on a terminal (merged) failing PR', () => {
    renderCard({ prAutofixMode: 'propose', prs: [{ ...failingPr(7), state: 'merged' }] })
    expect(screen.queryByRole('button', { name: /fix pr/i })).toBeNull()
  })

  it('posts the fix on click', () => {
    const mutate = vi.fn()
    renderCard({ prAutofixMode: 'propose', prs: [failingPr(7)] }, { fixMutate: mutate })
    screen.getByRole('button', { name: /fix pr #7/i }).click()
    expect(mutate).toHaveBeenCalledWith({ id: expect.any(String), number: 7 })
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd web && npx vitest run src/components/__tests__/SessionCard.fixpr.test.tsx`
Expected: FAIL — no "Fix PR" button.

- [ ] **Step 3: Add the `useFixPr` hook**

In `web/src/api/hooks.ts`, after `useSendPrompt` (line 186):

```ts
export function useFixPr() {
  const qc = useQueryClient()
  return useMutation<Session, ApiRequestError, { id: string; number: number }>({
    mutationFn: ({ id, number }) => api.post<Session>(`/sessions/${id}/fix-pr`, { number }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: QK.sessions }) },
  })
}
```

- [ ] **Step 4: Render the action in SessionCard**

In `web/src/components/SessionCard.tsx`:

Add imports (top of file, with the other `@/` imports):
```tsx
import { useSettings, useFixPr } from '@/api/hooks'
import { anyChecksFailing, isActionablePrState } from '@/session/prStatus'
import { Wrench } from 'lucide-react'
```

Inside the component body (near the top, with other hooks):
```tsx
  const { data: appSettings } = useSettings()
  const fixPr = useFixPr()
  // The specific failing-and-actionable PR to offer a one-tap fix for (lowest
  // number = oldest open PR). Only meaningful in 'propose' mode.
  const failingPr = (session.prs ?? [])
    .filter((p) => p.checks === 'failing' && isActionablePrState(p.state))
    .sort((a, b) => a.number - b.number)[0]
  const showFix = appSettings?.prAutofixMode === 'propose'
    && anyChecksFailing(session.prs) && !!failingPr
```

Render the button next to the PR list (after the `card-prs` span, inside the same flex row — after line 244):
```tsx
            {showFix && failingPr && (
              <button
                type="button"
                data-testid="card-fix-pr"
                onClick={(e) => { e.stopPropagation(); fixPr.mutate({ id: session.id, number: failingPr.number }) }}
                disabled={fixPr.isPending}
                aria-label={`Fix PR #${failingPr.number}`}
                className="hidden lg:inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50"
              >
                <Wrench size={11} aria-hidden />
                <span>Fix PR #{failingPr.number}</span>
              </button>
            )}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd web && npx vitest run src/components/__tests__/SessionCard.fixpr.test.tsx`
Expected: PASS.

- [ ] **Step 6: Full frontend gates**

Run: `cd web && npx vitest run && npx tsc -b --force && npx eslint src`
Expected: clean. (Run the FULL vitest suite — SessionCard is rendered widely; CI runs the full suite and a stray mock can break sibling tests.)

- [ ] **Step 7: Commit**

```bash
git add web/src/api/hooks.ts web/src/components/SessionCard.tsx web/src/components/__tests__/SessionCard.fixpr.test.tsx
git commit -m "feat(web): one-tap 'Fix PR #N' action on the card in propose mode

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: End-to-end verification + finish

**Files:** none (verification only)

- [ ] **Step 1: Full backend suite**

Run: `cd /mnt/ssk-ssd/tony/GitHub/.ccswitch/worktrees/cc-account-switcher/880d4c55 && env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=$PWD/src <main-checkout>/.venv/bin/python -m pytest server/tests/ -q`
Expected: PASS (known environmental failures `remote_mode_wiring`, `x11` may remain — confirm they predate this work).

- [ ] **Step 2: Backend lint gates**

Run: `ruff check server/ && mypy server/`
Expected: clean.

- [ ] **Step 3: Full frontend gates**

Run: `cd web && npx vitest run && npx tsc -b --force && npx eslint src`
Expected: clean.

- [ ] **Step 4: Manual sanity (optional, if a dev daemon is available)**

Set mode to `propose` in Settings; confirm a session with a failing PR shows "Fix PR #N" and tapping it injects the fix prompt. Switch to `auto`; confirm a fresh CI failure on a live session injects exactly once and a second failing poll does not re-inject.

- [ ] **Step 5: Open the PR**

Follow CLAUDE.md completion workflow: branch is already off `origin/master`; push and `gh pr create` (non-draft), then `gh pr merge <#> --squash --auto`. PR body ends with the Claude Code trailer. Reference the spec.

---

## Self-Review

**Spec coverage:**
- Global setting `off/propose/auto`, default `propose` → Task 1 (DB + API), Task 5 (UI). ✓
- Owning session, matched by `(repo, branch)` → Task 3 (`_on_pr_change` lookup, `request_pr_fix`). ✓
- Busy session queues behind current turn → reuses `send_prompt` → driver FIFO `_queue` (no new code; asserted indirectly by "sent" status). ✓
- Auto no-driver fallback → Task 3 `rec.id in self._drivers` gate + `test_auto_mode_no_live_driver_falls_back`. ✓
- Loop guard, one attempt per failure event, re-arm on recovery → Task 3 `_pr_fix_attempts` + `test_auto_mode_injects_once_per_failure_event`. ✓
- One shared action, two triggers → `request_pr_fix` called by both `_on_pr_change` (Task 3) and `/fix-pr` (Task 4). ✓
- Branch-aware fix prompt (checkout first) → Task 2 + `test_prompt_names_repo_pr_and_branch_checkout`. ✓
- Propose surface = actionable one-tap control gated on `anyChecksFailing` → Task 6. ✓
- Terminal PRs never acted on → Task 3 `state not in ("open","draft")` + Task 6 `isActionablePrState` + tests. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; test bodies are concrete. Fixture wiring in Tasks 3/4/5/6 references existing harness patterns by name rather than reproducing unrelated boilerplate — acceptable since each names the exact shape required.

**Type consistency:**
- `request_pr_fix(session_id, pr_number) -> str` — same signature in Task 3 (def), Task 4 (call), and tests. ✓
- Status strings `"sent"|"no_driver"|"draining"|"no_pr"|"not_actionable"|"no_session"` — produced in Task 3, consumed in Task 4 (`!= "sent"` → 422 with `result` code). ✓
- `prAutofixMode` wire field — backend `_to_dict`/`SettingsBody` (Task 1), frontend `AppSettings` interface (Task 5), card gate (Task 6). ✓
- `useFixPr().mutate({ id, number })` — defined Task 6, body `{ number }` matches `FixPrRequest` (Task 4). ✓
