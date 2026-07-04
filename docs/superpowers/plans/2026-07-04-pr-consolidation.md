# PR Consolidation for PR Doctor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let PR Doctor consolidate several selected open PRs into one PR — a human-triggered surface that spawns one account-rotated `claude` session to merge them and close the originals — plus a recommender that clusters open PRs by title-scope / all-green CI.

**Architecture:** A pure engine (`consolidation_engine.py`) derives candidate metadata and suggests clusters; a prompt builder seeds the session; the orchestrator enumerates candidates from the population cache and spawns one de-duped session via the existing `spawn_session` path; a provider maps outcome statuses to user messages; two dedicated pr-doctor API routes back a `ConsolidatePanel` inside the existing `AutomationCard`.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy (backend) · React + Vite + TypeScript + Tailwind + sonner (frontend) · pytest / vitest.

**Design doc:** `docs/superpowers/specs/2026-07-04-pr-consolidation-design.md`

## Global Constraints

- **Never automatic.** Consolidation only ever runs on an explicit human trigger — no `auto`-mode path. The recommender suggests; it never acts.
- **Per-repo only.** Candidates and clusters are grouped by repo; a consolidation is always within one repo.
- **Git norms:** feature work branches off `origin/master` (never local `master`); commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Backend gates (run locally before commit):** `ruff check`, `mypy`, `pytest server/tests/`. From a fresh worktree use the system interpreter with `PYTHONPATH=.:src CCSWITCH_DEV=1` and pin the auth allowlist per-test (see below).
- **Web gates (run in `web/`):** `vitest run`, `tsc --noEmit`, `eslint`.
- **Auth in tests:** API tests 401 unless the test identity is allow-listed. Run API tests with `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 pytest …` (empty allowlist needs `CCSWITCH_DEV=1`).
- **No new NOT NULL columns.** This feature adds no DB columns; it reuses `Session.origin`/`origin_ref`.
- **Single source of truth:** route new work through existing helpers (`spawn_session`, `_pr_doctor_scope`, `_pr_population`, the sonner `toast`, `QK`) — do not add parallel copies.

---

### Task 1: Consolidation engine (pure)

**Files:**
- Create: `server/automations/consolidation_engine.py`
- Test: `server/tests/test_consolidation_engine.py`

**Interfaces:**
- Produces:
  - `derive_scope(title: str) -> str`
  - `@dataclass(frozen=True) ConsolidationCandidate(number:int, title:str, url:str, checks:str, mergeable:str, updated_at:str, scope:str)` with `.to_dict() -> dict`
  - `@dataclass(frozen=True) Cluster(numbers:tuple[int,...], rationale:str, risk:str)` with `.to_dict() -> dict`
  - `suggest_clusters(candidates: list[ConsolidationCandidate]) -> list[Cluster]`
  - `validate_selection(candidates: list[ConsolidationCandidate]) -> str | None`

> **Learning-mode note (execution only):** `suggest_clusters` is the user's contribution point. At execution time, present the failing tests below and let the user write the body before falling back to the reference implementation. The tests are the contract either way.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_consolidation_engine.py
"""Pure consolidation engine: scope derivation, clustering, selection guard."""
from __future__ import annotations

from server.automations.consolidation_engine import (
    ConsolidationCandidate, Cluster, derive_scope, suggest_clusters, validate_selection,
)


def _c(number, *, title="fix(deploy): x", checks="passing", mergeable="MERGEABLE"):
    return ConsolidationCandidate(
        number=number, title=title, url=f"https://x/pull/{number}",
        checks=checks, mergeable=mergeable, updated_at="2026-07-04T00:00:00Z",
        scope=derive_scope(title))


def test_derive_scope_reads_conventional_commit_scope():
    assert derive_scope("fix(deploy): mark phase done") == "deploy"
    assert derive_scope("feat(web)!: breaking") == "web"
    assert derive_scope("chore: no scope") == ""
    assert derive_scope("") == ""


def test_suggest_clusters_groups_same_scope_all_green_low_risk():
    cands = [_c(1), _c(2), _c(3, title="feat(web): y")]
    clusters = suggest_clusters(cands)
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl.numbers == (1, 2)
    assert cl.risk == "low"
    assert "deploy" in cl.rationale and "all green" in cl.rationale


def test_suggest_clusters_flags_risky_when_a_member_is_red_or_conflicting():
    cands = [_c(1), _c(2, checks="failing")]
    cl = suggest_clusters(cands)[0]
    assert cl.risk == "risky"
    assert "all green" not in cl.rationale


def test_suggest_clusters_excludes_singletons_and_sorts_largest_first():
    cands = [_c(1), _c(2), _c(3), _c(10, title="feat(web): a"), _c(11, title="feat(web): b")]
    clusters = suggest_clusters(cands)
    assert [len(c.numbers) for c in clusters] == [3, 2]  # deploy(3) before web(2)


def test_validate_selection_requires_at_least_two_distinct():
    assert validate_selection([_c(1)]) == "too_few"
    assert validate_selection([_c(1), _c(1)]) == "too_few"
    assert validate_selection([_c(1), _c(2)]) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_consolidation_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: server.automations.consolidation_engine`.

- [ ] **Step 3: Write the implementation**

```python
# server/automations/consolidation_engine.py
"""Pure consolidation engine: derive candidate metadata and suggest low-risk
clusters of open PRs to combine into one. No I/O — trivially testable."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Conventional-commit scope: `type(scope): …` or `type(scope)!: …`
_SCOPE_RE = re.compile(r"^\s*[a-zA-Z]+\(([^)]+)\)!?:")


def derive_scope(title: str) -> str:
    """Conventional-commit scope from a PR title, lowercased ('' if none)."""
    m = _SCOPE_RE.match(title or "")
    return m.group(1).strip().lower() if m else ""


@dataclass(frozen=True)
class ConsolidationCandidate:
    number: int
    title: str
    url: str
    checks: str          # "passing" | "failing" | "pending" | "none"
    mergeable: str       # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
    updated_at: str
    scope: str           # derive_scope(title)

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title, "url": self.url,
                "checks": self.checks, "mergeable": self.mergeable,
                "updated_at": self.updated_at, "scope": self.scope}


@dataclass(frozen=True)
class Cluster:
    numbers: tuple[int, ...]   # sorted, len >= 2
    rationale: str
    risk: str                  # "low" | "risky"

    def to_dict(self) -> dict:
        return {"numbers": list(self.numbers), "rationale": self.rationale,
                "risk": self.risk}


def _is_risky(c: ConsolidationCandidate) -> bool:
    return c.checks == "failing" or c.mergeable == "CONFLICTING"


def suggest_clusters(candidates: list[ConsolidationCandidate]) -> list[Cluster]:
    """Group candidates sharing a non-empty conventional-commit scope into
    clusters of >=2. A cluster is 'risky' if any member has failing CI or a
    conflict, else 'low'. Largest clusters first.

    [USER CONTRIBUTION POINT] The weighting/threshold here defines the feature's
    taste — v1 keys off scope + all-green; enrich with files/author in v1.5."""
    by_scope: dict[str, list[ConsolidationCandidate]] = {}
    for c in candidates:
        if c.scope:
            by_scope.setdefault(c.scope, []).append(c)
    clusters: list[Cluster] = []
    for scope, members in by_scope.items():
        if len(members) < 2:
            continue
        nums = tuple(sorted(m.number for m in members))
        risky = any(_is_risky(m) for m in members)
        rationale = (f"{len(members)} PRs share scope `{scope}`"
                     + ("" if risky else ", all green"))
        clusters.append(Cluster(numbers=nums, rationale=rationale,
                                risk="risky" if risky else "low"))
    clusters.sort(key=lambda cl: (-len(cl.numbers), cl.numbers))
    return clusters


def validate_selection(candidates: list[ConsolidationCandidate]) -> str | None:
    """Return an error status ('too_few') or None if consolidatable. Distinct
    numbers only; the caller pre-filters to open/non-draft PRs."""
    distinct = {c.number for c in candidates}
    if len(distinct) < 2:
        return "too_few"
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_consolidation_engine.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + type-check**

Run: `ruff check server/automations/consolidation_engine.py && mypy server/automations/consolidation_engine.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add server/automations/consolidation_engine.py server/tests/test_consolidation_engine.py
git commit -m "feat(consolidation): pure engine — scope derivation, clustering, selection guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Consolidation prompt builder

**Files:**
- Modify: `server/sessions/pr_fix.py` (append a new function)
- Test: `server/tests/test_consolidation_prompt.py`

**Interfaces:**
- Consumes: `PullRequestInfo` (already imported in `pr_fix.py`).
- Produces: `build_consolidation_prompt(repo: str, base: str, prs: list[PullRequestInfo]) -> str`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_consolidation_prompt.py
from server.github.pr_status import PullRequestInfo
from server.sessions.pr_fix import build_consolidation_prompt


def _pr(n):
    return PullRequestInfo(number=n, url=f"https://x/pull/{n}", state="open",
                           checks="passing", title=f"fix(deploy): item {n}")


def test_consolidation_prompt_names_every_pr_and_closes_and_supersedes():
    prompt = build_consolidation_prompt("o/r", "master", [_pr(11), _pr(12), _pr(13)])
    for n in (11, 12, 13):
        assert f"#{n}" in prompt
        assert f"Closes #{n}" in prompt
    assert "origin/master" in prompt          # branches off the base
    assert "Superseded by" in prompt          # closes originals with a linking comment
    assert "o/r" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_consolidation_prompt.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_consolidation_prompt'`.

- [ ] **Step 3: Append the implementation to `server/sessions/pr_fix.py`**

```python
def build_consolidation_prompt(repo: str, base: str, prs: list[PullRequestInfo]) -> str:
    """A system-originated prompt instructing a fresh session to consolidate
    several open PRs into ONE new PR (saving CI). Pure (no I/O).

    Mirrors build_pr_fix_prompt/build_conflict_fix_prompt: explicit, branch-aware,
    and self-contained because no human will attach to steer the session."""
    nums = ", ".join(f"#{p.number}" for p in prs)
    listing = "\n".join(f"  - #{p.number} \"{p.title}\" ({p.url})" for p in prs)
    closes = " ".join(f"Closes #{p.number}" for p in prs)
    branch = f"consolidate/{'-'.join(str(p.number) for p in prs)}"
    return (
        f"Consolidate {len(prs)} open pull requests in {repo} into ONE new PR to "
        f"save CI. The PRs to combine ({nums}):\n{listing}\n\n"
        f"Steps:\n"
        f"1. `git fetch origin`, then create a fresh branch off the base: "
        f"`git checkout -b {branch} origin/{base}`.\n"
        f"2. Bring each PR's changes onto this branch — `gh pr checkout <n>` to "
        f"inspect, then cherry-pick or merge its commits. Resolve conflicts by "
        f"reconciling BOTH sides (never blindly discard). Run the tests/lint/build "
        f"after each so you know which PR introduced any breakage.\n"
        f"3. Push the branch and open ONE consolidated PR whose body summarizes the "
        f"combined change and lists every original with a closing keyword: {closes}\n"
        f"4. Close each original PR with a linking comment — for each number run "
        f"`gh pr close <n> --comment \"Superseded by the consolidated PR.\"`.\n"
        f"5. Follow the project completion workflow: the branch is already off "
        f"origin/{base}; open the PR non-draft and arm auto-merge "
        f"(`gh pr merge --squash --auto`).\n"
        f"6. Report the consolidated PR number and which originals you closed."
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_consolidation_prompt.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + type-check + commit**

```bash
ruff check server/sessions/pr_fix.py && mypy server/sessions/pr_fix.py
git add server/sessions/pr_fix.py server/tests/test_consolidation_prompt.py
git commit -m "feat(consolidation): branch-aware consolidation prompt builder

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Orchestrator — candidate enumeration + consolidation spawn

**Files:**
- Modify: `server/sessions/orchestrator.py` (add two methods near `_pr_doctor_session_fix`, ~line 1900)
- Test: `server/tests/test_pr_consolidation.py`

**Interfaces:**
- Consumes: `self._pr_population.all()` / `.cached(repo)` → `{repo: [PullRequestInfo]}`; `self._pr_doctor_scope() -> set[str]|None`; `self._pr_doctor_ctx.spawn_session(...)`; `gitrepos.ensure_clone`; `get_settings().repo_root`; `AllAccountsBusy`; module-level `db_session`, `select`, `asyncio`, `gitrepos`. From Tasks 1–2: `ConsolidationCandidate`, `validate_selection`, `derive_scope`, `build_consolidation_prompt`.
- Produces:
  - `async consolidation_candidates(self, repo: str | None = None) -> dict` → `{"repos": {repo: {"candidates": [dict], "clusters": [dict]}}}`
  - `async _pr_doctor_consolidate(self, repo: str, numbers: list[int]) -> tuple[str, str | None]` → status ∈ `{spawned, duplicate, too_few, not_actionable, clone_failed, ceiling, spawn_failed}`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_pr_consolidation.py
"""Orchestrator consolidation: candidate enumeration + de-duped session spawn.
Uses the __new__ + injected-fakes harness from test_pr_doctor_step.py."""
from __future__ import annotations

import types
import pytest

from server.github.pr_status import PullRequestInfo


def _pr(n, *, title="fix(deploy): x", state="open", is_draft=False,
        checks="passing", mergeable="MERGEABLE"):
    return PullRequestInfo(number=n, url=f"https://x/pull/{n}", state=state,
                           checks=checks, title=title, is_draft=is_draft,
                           mergeable=mergeable, head_ref=f"feat/{n}")


class _Population:
    def __init__(self, prs, repo="o/r"):
        self._prs, self._repo = prs, repo

    def all(self):
        return {self._repo: list(self._prs)}

    def cached(self, repo):
        return list(self._prs) if repo == self._repo else ()


async def _init_db(tmp_path):
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    await registry.ensure_rows()


def _build_orch(prs):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {}
    orch._drivers = {}
    orch._pr_population = _Population(prs)          # type: ignore[assignment]
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    return orch


@pytest.mark.asyncio
async def test_candidates_groups_open_prs_and_suggests_clusters(tmp_path):
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2), _pr(9, state="draft", is_draft=True)])
    out = await orch.consolidation_candidates()
    repo = out["repos"]["o/r"]
    nums = {c["number"] for c in repo["candidates"]}
    assert nums == {1, 2}                            # draft excluded
    assert repo["clusters"][0]["numbers"] == [1, 2]  # same scope → cluster


@pytest.mark.asyncio
async def test_consolidate_spawns_one_deduped_session(tmp_path, monkeypatch):
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))

    spawned = {}

    async def fake_spawn(**kw):
        spawned.update(kw)
        return types.SimpleNamespace(id="new-sess")

    orch._pr_doctor_ctx.spawn_session = fake_spawn  # type: ignore[assignment]
    status, sid = await orch._pr_doctor_consolidate("o/r", [2, 1])
    assert status == "spawned" and sid == "new-sess"
    assert spawned["dedupe_key"] == "consolidate:o/r#1+2"   # sorted, namespaced
    assert "Closes #1" in spawned["initial_prompt"]
    assert "Closes #2" in spawned["initial_prompt"]


@pytest.mark.asyncio
async def test_consolidate_too_few_and_not_actionable(tmp_path, monkeypatch):
    from ccswitch import gitrepos
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))
    status, _ = await orch._pr_doctor_consolidate("o/r", [1])
    assert status == "too_few"
    status2, _ = await orch._pr_doctor_consolidate("o/r", [1, 999])
    assert status2 == "not_actionable"               # #999 absent from cache


@pytest.mark.asyncio
async def test_consolidate_duplicate_when_live_session_exists(tmp_path, monkeypatch):
    from ccswitch import gitrepos
    from server.db import db_session
    from server.db.models import Session
    await _init_db(tmp_path)
    orch = _build_orch([_pr(1), _pr(2)])
    monkeypatch.setattr(gitrepos, "ensure_clone", lambda root, repo, url: str(tmp_path))
    async with db_session() as db:
        db.add(Session(id="existing", name="c", account_name="a", status="idle",
                       origin="pr-doctor", origin_ref="consolidate:o/r#1+2"))
    status, sid = await orch._pr_doctor_consolidate("o/r", [1, 2])
    assert status == "duplicate" and sid == "existing"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_pr_consolidation.py -v`
Expected: FAIL — `AttributeError: … has no attribute 'consolidation_candidates'`.

- [ ] **Step 3: Add both methods to `server/sessions/orchestrator.py`** (place right after `_pr_doctor_session_fix`, before `_pr_doctor_mechanical`)

```python
    async def consolidation_candidates(self, repo: str | None = None) -> dict:
        """Open, non-draft PRs per repo (from the population cache, within
        pr-doctor scope) as ConsolidationCandidate dicts + suggested clusters.
        Read-only; the picker calls this. Broader than _pr_doctor_scope_pairs
        (every open PR, not just session-authored ones)."""
        from server.automations.consolidation_engine import (
            ConsolidationCandidate, derive_scope, suggest_clusters)
        scope = await self._pr_doctor_scope()          # set[str] | None (None = all)
        out: dict[str, dict] = {}
        for r, prs in self._pr_population.all().items():
            if scope is not None and r not in scope:
                continue
            if repo is not None and r != repo:
                continue
            cands = [
                ConsolidationCandidate(
                    number=p.number, title=p.title, url=p.url, checks=p.checks,
                    mergeable=p.mergeable, updated_at=p.updated_at,
                    scope=derive_scope(p.title))
                for p in prs if p.state == "open" and not p.is_draft
            ]
            if not cands:
                continue
            out[r] = {"candidates": [c.to_dict() for c in cands],
                      "clusters": [cl.to_dict() for cl in suggest_clusters(cands)]}
        return {"repos": out}

    async def _pr_doctor_consolidate(
        self, repo: str, numbers: list[int]
    ) -> tuple[str, str | None]:
        """Spawn ONE session to consolidate the given PRs. Validates the
        selection, resolves each PR from the population cache, seeds the
        consolidation prompt, and de-dups on the sorted number set. Mirrors
        _pr_doctor_session_fix's clone/spawn/ceiling handling. Returns
        (status, session_id)."""
        from server.automations.consolidation_engine import (
            ConsolidationCandidate, validate_selection)
        from server.sessions.pr_fix import build_consolidation_prompt
        from server.config import get_settings

        want = sorted({int(n) for n in numbers})
        by_number = {p.number: p for p in self._pr_population.cached(repo)}
        prs = [by_number[n] for n in want
               if n in by_number and by_number[n].state == "open"
               and not by_number[n].is_draft]
        if len(prs) != len(want):
            return "not_actionable", None
        cands = [ConsolidationCandidate(
            number=p.number, title=p.title, url=p.url, checks=p.checks,
            mergeable=p.mergeable, updated_at=p.updated_at, scope="") for p in prs]
        err = validate_selection(cands)
        if err is not None:
            return err, None                      # "too_few"

        target = f"consolidate:{repo}#{'+'.join(str(n) for n in want)}"
        # Pre-check the dedupe: a live consolidation session for this exact set →
        # duplicate (skip a needless clone + second spawn).
        async with db_session() as db:
            existing = (await db.execute(select(DbSession).where(
                DbSession.origin == "pr-doctor",
                DbSession.origin_ref == target,
                DbSession.status != "ended"))).scalars().first()
        if existing is not None:
            return "duplicate", existing.id

        prompt = build_consolidation_prompt(repo, "master", prs)
        url = f"https://github.com/{repo}.git"
        try:
            cwd = await asyncio.to_thread(
                gitrepos.ensure_clone, get_settings().repo_root, repo, url)
        except (gitrepos.CloneError, gitrepos.GhError) as exc:
            await self._pr_doctor_ctx.emit(
                "error", f"clone failed for {target}: {exc}",
                kind="spawn_failed", target=target)
            return "clone_failed", None
        try:
            rec = await self._pr_doctor_ctx.spawn_session(
                origin_ref=target, dedupe_key=target, repo=repo,
                account=None, cwd=cwd,
                name=f"pr-doctor consolidate {repo} ({len(prs)} PRs)",
                initial_prompt=prompt)
        except AllAccountsBusy:
            await self._pr_doctor_ctx.emit(
                "warn", f"consolidate deferred for {target}: all accounts at/above usage ceiling",
                kind="spawn_failed", target=target)
            return "ceiling", None
        except Exception as exc:  # noqa: BLE001
            await self._pr_doctor_ctx.emit(
                "error", f"consolidate spawn failed for {target}: {exc}",
                kind="spawn_failed", target=target)
            return "spawn_failed", None
        sid = getattr(rec, "id", None)
        await self._pr_doctor_ctx.emit(
            "info", f"consolidation spawned for {target}",
            kind="attempted", target=target, session_id=sid)
        return "spawned", sid
```

> **Note:** `DbSession` is confirmed as the alias for the `Session` DB model in `orchestrator.py:40` (`from server.db.models import INACTIVE_SESSION_STATUSES, Session as DbSession`). `select`, `db_session`, `asyncio`, `gitrepos`, and `AllAccountsBusy` are already module-level imports (used by `_pr_doctor_session_fix`), so no new imports are needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_pr_consolidation.py -v`
Expected: PASS (4 tests). If the `DbSession` alias is wrong, the failure is an `ImportError`/`NameError` — fix the alias per the note and rerun.

- [ ] **Step 5: Lint + type-check + commit**

```bash
ruff check server/sessions/orchestrator.py && mypy server/sessions/orchestrator.py
git add server/sessions/orchestrator.py server/tests/test_pr_consolidation.py
git commit -m "feat(consolidation): orchestrator candidate enumeration + de-duped spawn

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Provider mapping + public orchestrator wrapper

**Files:**
- Modify: `server/automations/action_providers.py` (extend `PrDoctorActionProvider`)
- Modify: `server/sessions/orchestrator.py` (add `automation_consolidate` public wrapper)
- Test: `server/tests/test_consolidation_provider.py`

**Interfaces:**
- Consumes: `orch._pr_doctor_consolidate(repo, numbers)` (Task 3); `TriggerResult` from `server.automations.base`.
- Produces:
  - `PrDoctorActionProvider.consolidate(self, repo: str, numbers: list[int]) -> TriggerResult`
  - `SessionOrchestrator.automation_consolidate(self, repo: str, numbers: list[int]) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_consolidation_provider.py
import pytest
from server.automations.action_providers import PrDoctorActionProvider


class _Orch:
    def __init__(self, status):
        self._status = status

    async def _pr_doctor_consolidate(self, repo, numbers):
        return self._status, ("sid" if self._status == "spawned" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,level", [
    ("spawned", "ok"), ("duplicate", "warn"), ("too_few", "warn"),
    ("not_actionable", "warn"), ("ceiling", "warn"),
    ("clone_failed", "error"), ("spawn_failed", "error"),
])
async def test_consolidate_maps_status_to_level(status, level):
    provider = PrDoctorActionProvider(_Orch(status))
    result = await provider.consolidate("o/r", [1, 2])
    assert result.status == status
    assert result.level == level
    assert result.message                      # non-empty human message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_consolidation_provider.py -v`
Expected: FAIL — `AttributeError: 'PrDoctorActionProvider' object has no attribute 'consolidate'`.

- [ ] **Step 3a: Add the mapping + method to `server/automations/action_providers.py`** (below the existing `_PR_STATUS_MAP` / `PrDoctorActionProvider`)

```python
_CONSOLIDATE_STATUS_MAP = {
    "spawned": ("ok", "Spawned consolidation session"),
    "duplicate": ("warn", "Already consolidating this set"),
    "too_few": ("warn", "Select at least 2 PRs"),
    "not_actionable": ("warn", "One or more PRs are no longer open"),
    "ceiling": ("warn", "All accounts at/above usage ceiling — try later"),
    "clone_failed": ("error", "Clone failed — see the automation feed"),
    "spawn_failed": ("error", "Spawn failed — see the automation feed"),
}


def _consolidate_result(status: str) -> TriggerResult:
    level, message = _CONSOLIDATE_STATUS_MAP.get(
        status, ("error", "Consolidation failed — see the feed"))
    return TriggerResult(status=status, level=level, message=message)
```

Add this method to `PrDoctorActionProvider`:

```python
    async def consolidate(self, repo: str, numbers: list[int]) -> TriggerResult:
        status, _sid = await self._orch._pr_doctor_consolidate(repo, numbers)
        return _consolidate_result(status)
```

- [ ] **Step 3b: Add the public wrapper to `server/sessions/orchestrator.py`** (next to `automation_trigger`, ~line 2777)

```python
    async def automation_consolidate(self, repo: str, numbers: list[int]) -> dict:
        """Public seam for the consolidate API route — delegates to the
        pr-doctor provider so the outcome mapping stays in one place."""
        provider = self._action_providers.get("pr-doctor")
        if provider is None:
            return {"status": "no_provider", "level": "error",
                    "message": "no action provider"}
        return (await provider.consolidate(repo, numbers)).to_dict()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env PYTHONPATH=.:src CCSWITCH_DEV=1 pytest server/tests/test_consolidation_provider.py -v`
Expected: PASS (7 params).

- [ ] **Step 5: Lint + type-check + commit**

```bash
ruff check server/automations/action_providers.py server/sessions/orchestrator.py
mypy server/automations/action_providers.py server/sessions/orchestrator.py
git add server/automations/action_providers.py server/sessions/orchestrator.py server/tests/test_consolidation_provider.py
git commit -m "feat(consolidation): provider status mapping + automation_consolidate wrapper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: API routes

**Files:**
- Modify: `server/api/automations.py` (add a body model + two routes)
- Test: `server/tests/test_pr_consolidation_api.py`

**Interfaces:**
- Consumes: `orch.consolidation_candidates()` (Task 3), `orch.automation_consolidate(repo, numbers)` (Task 4).
- Produces:
  - `GET  /api/automations/pr-doctor/consolidation` → `{"repos": {...}}`
  - `POST /api/automations/pr-doctor/consolidate` — body `{repo: str, numbers: list[int]}` → `TriggerResult` dict

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_consolidation_api.py
"""API surface for consolidation. Uses the app factory + a stub orchestrator on
app.state.sessions, mirroring test_pr_doctor_actions_api.py."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from server.app import create_app


class _StubOrch:
    async def consolidation_candidates(self, repo=None):
        return {"repos": {"o/r": {"candidates": [{"number": 1}], "clusters": []}}}

    async def automation_consolidate(self, repo, numbers):
        return {"status": "spawned", "level": "ok", "message": "Spawned consolidation session"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    app = create_app()
    app.state.sessions = _StubOrch()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_get_consolidation_returns_repos(client):
    async with client as c:
        r = await c.get("/api/automations/pr-doctor/consolidation")
    assert r.status_code == 200
    assert "o/r" in r.json()["repos"]


@pytest.mark.asyncio
async def test_post_consolidate_returns_trigger_result(client):
    async with client as c:
        r = await c.post("/api/automations/pr-doctor/consolidate",
                         json={"repo": "o/r", "numbers": [1, 2]})
    assert r.status_code == 200
    assert r.json()["status"] == "spawned"


@pytest.mark.asyncio
async def test_post_consolidate_422_on_malformed_body(client):
    async with client as c:
        r = await c.post("/api/automations/pr-doctor/consolidate", json={"repo": "o/r"})
    assert r.status_code == 422       # numbers missing → pydantic rejects
```

> **Auth note:** if the app factory enforces the tailnet allowlist even under `CCSWITCH_DEV=1`, follow `test_pr_doctor_actions_api.py`'s exact fixture (it already solves auth for these routes) rather than reinventing it — copy its client/identity setup.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src pytest server/tests/test_pr_consolidation_api.py -v`
Expected: FAIL — 404 on both routes (not yet registered).

- [ ] **Step 3: Add the body model + routes to `server/api/automations.py`**

Add near `TriggerBody`:

```python
class ConsolidateBody(BaseModel):
    repo: str
    numbers: list[int]
```

Add the routes (after the `trigger_automation_action` route):

```python
@router.get("/pr-doctor/consolidation")
async def pr_doctor_consolidation(request: Request,
                                  _identity: str = Depends(current_identity)) -> dict:
    """Open PRs (per repo) + suggested consolidation clusters for the picker."""
    _automation_or_404("pr-doctor")
    orch = request.app.state.sessions
    return await orch.consolidation_candidates()


@router.post("/pr-doctor/consolidate")
async def pr_doctor_consolidate(body: ConsolidateBody, request: Request,
                                _identity: str = Depends(current_identity)) -> dict:
    """Spawn one session to consolidate the selected PRs. Semantic rejections
    (<2 PRs, closed PR) come back as graceful TriggerResults, not 4xx — only a
    malformed body is a 422 (pydantic)."""
    _automation_or_404("pr-doctor")
    orch = request.app.state.sessions
    return await orch.automation_consolidate(body.repo, body.numbers)
```

> **Route ordering:** register these BEFORE the generic `/{automation_id}/…` catch-alls if FastAPI would otherwise match `pr-doctor` as an `automation_id` path param. In FastAPI, a literal path (`/pr-doctor/consolidation`) and a parametrized one (`/{automation_id}/actionable`) don't collide because the trailing segments differ — but `/pr-doctor/consolidate` vs `/{automation_id}/trigger` also differ. No reordering needed; verify with the test.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src pytest server/tests/test_pr_consolidation_api.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Full backend gate + commit**

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src pytest server/tests/ -q
ruff check server/ && mypy server/api/automations.py
git add server/api/automations.py server/tests/test_pr_consolidation_api.py
git commit -m "feat(consolidation): pr-doctor consolidation API routes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Frontend API client + hooks

**Files:**
- Modify: `web/src/api/automations.ts` (types + fetchers)
- Modify: `web/src/api/hooks.ts` (QK entry + two hooks)

**Interfaces:**
- Consumes: `api.get`/`api.post` (client prepends `/api`); existing `TriggerResult` type; `QK`, `useQuery`, `useMutation`, `useQueryClient`, `ApiRequestError`.
- Produces: `fetchConsolidation`, `postConsolidate`, `useConsolidation`, `useConsolidate`, plus `ConsolidationCandidate`/`ConsolidationCluster`/`ConsolidationView` types.

- [ ] **Step 1: Add types + fetchers to `web/src/api/automations.ts`** (append after `postAutomationTrigger`)

```ts
// --- PR-doctor consolidation -------------------------------------------------

export interface ConsolidationCandidate {
  number: number
  title: string
  url: string
  checks: string
  mergeable: string
  updated_at: string
  scope: string
}

export interface ConsolidationCluster {
  numbers: number[]
  rationale: string
  risk: 'low' | 'risky'
}

export interface ConsolidationView {
  repos: Record<string, { candidates: ConsolidationCandidate[]; clusters: ConsolidationCluster[] }>
}

export function fetchConsolidation(): Promise<ConsolidationView> {
  return api.get<ConsolidationView>('/automations/pr-doctor/consolidation')
}

export function postConsolidate(
  body: { repo: string; numbers: number[] },
): Promise<TriggerResult> {
  return api.post<TriggerResult>('/automations/pr-doctor/consolidate', body)
}
```

- [ ] **Step 2: Add the QK entry + hooks to `web/src/api/hooks.ts`**

Add to the imports from `./automations`: `fetchConsolidation, postConsolidate, type ConsolidationView`.

Add to the `QK` object (near `automationActionable`):

```ts
  consolidation: ['automation-consolidation'] as const,
```

Add the hooks (near `useAutomationTrigger`):

```ts
export function useConsolidation(enabled = true) {
  return useQuery<ConsolidationView>({
    queryKey: QK.consolidation,
    queryFn: fetchConsolidation,
    enabled,
    staleTime: 10_000,
  })
}

export function useConsolidate() {
  const qc = useQueryClient()
  return useMutation<TriggerResult, ApiRequestError, { repo: string; numbers: number[] }>({
    mutationFn: (body) => postConsolidate(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.consolidation })
      qc.invalidateQueries({ queryKey: QK.automationActionable('pr-doctor') })
      qc.invalidateQueries({ queryKey: QK.automationSessions('pr-doctor') })
    },
  })
}
```

> `TriggerResult` is already imported in `hooks.ts` (used by `useAutomationTrigger`). If not, add it to the `./automations` import list.

- [ ] **Step 3: Type-check**

Run (in `web/`): `npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add web/src/api/automations.ts web/src/api/hooks.ts
git commit -m "feat(consolidation): frontend API client + query hooks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: ConsolidatePanel component + mount

**Files:**
- Create: `web/src/components/ConsolidatePanel.tsx`
- Modify: `web/src/components/AutomationCard.tsx` (render the panel for pr-doctor, ~line 375)
- Test: `web/src/components/ConsolidatePanel.test.tsx`

**Interfaces:**
- Consumes: `useConsolidation`, `useConsolidate` (Task 6); `toast` from `sonner`; `ConsolidationCandidate`/`ConsolidationCluster` types.
- Produces: `export function ConsolidatePanel({ enabled }: { enabled: boolean })`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/ConsolidatePanel.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ConsolidatePanel } from './ConsolidatePanel'

const mutate = vi.fn()
vi.mock('@/api/hooks', () => ({
  useConsolidation: () => ({
    data: { repos: { 'o/r': {
      candidates: [
        { number: 1, title: 'fix(deploy): a', url: 'u1', checks: 'passing', mergeable: 'MERGEABLE', updated_at: '', scope: 'deploy' },
        { number: 2, title: 'fix(deploy): b', url: 'u2', checks: 'passing', mergeable: 'MERGEABLE', updated_at: '', scope: 'deploy' },
      ],
      clusters: [{ numbers: [1, 2], rationale: '2 PRs share scope `deploy`, all green', risk: 'low' }],
    } } },
    isLoading: false,
  }),
  useConsolidate: () => ({ mutateAsync: mutate, isPending: false }),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))

describe('ConsolidatePanel', () => {
  beforeEach(() => { mutate.mockReset(); mutate.mockResolvedValue({ status: 'spawned', level: 'ok', message: 'ok' }) })

  it('renders candidate rows and a cluster chip', () => {
    render(<ConsolidatePanel enabled />)
    expect(screen.getByText(/fix\(deploy\): a/)).toBeInTheDocument()
    expect(screen.getByText(/2 PRs share scope/)).toBeInTheDocument()
  })

  it('button is disabled until at least 2 are selected, then posts {repo, numbers}', async () => {
    render(<ConsolidatePanel enabled />)
    const btn = screen.getByRole('button', { name: /consolidate/i })
    expect(btn).toBeDisabled()                              // nothing selected
    fireEvent.click(screen.getByLabelText('select PR 1'))
    fireEvent.click(screen.getByLabelText('select PR 2'))
    expect(btn).toBeEnabled()
    fireEvent.click(btn)
    await waitFor(() => expect(mutate).toHaveBeenCalledWith({ repo: 'o/r', numbers: [1, 2] }))
  })

  it('a cluster chip selects its numbers', () => {
    render(<ConsolidatePanel enabled />)
    fireEvent.click(screen.getByText(/2 PRs share scope/))
    expect(screen.getByLabelText('select PR 1')).toBeChecked()
    expect(screen.getByLabelText('select PR 2')).toBeChecked()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `web/`): `npx vitest run src/components/ConsolidatePanel.test.tsx`
Expected: FAIL — cannot resolve `./ConsolidatePanel`.

- [ ] **Step 3: Write `web/src/components/ConsolidatePanel.tsx`**

```tsx
import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useConsolidation, useConsolidate } from '@/api/hooks'
import type { ConsolidationCandidate, ConsolidationCluster } from '@/api/automations'

function ciDot(checks: string): string {
  if (checks === 'passing') return 'bg-green-500'
  if (checks === 'failing') return 'bg-red-500'
  return 'bg-amber-500'
}

export function ConsolidatePanel({ enabled }: { enabled: boolean }) {
  const { data } = useConsolidation(enabled)
  const consolidate = useConsolidate()
  const [selected, setSelected] = useState<Record<number, boolean>>({})

  // v1: one repo group at a time. Pick the first repo with candidates; a repo
  // selector is a later enhancement. Selection is scoped to this repo so a
  // cross-repo consolidation can never be assembled.
  const [repo, group] = useMemo(() => {
    const repos = Object.entries(data?.repos ?? {})
    return repos.length ? repos[0] : ['', { candidates: [], clusters: [] }]
  }, [data]) as [string, { candidates: ConsolidationCandidate[]; clusters: ConsolidationCluster[] }]

  const chosen = group.candidates.filter((c) => selected[c.number]).map((c) => c.number)
  const canConsolidate = chosen.length >= 2 && !consolidate.isPending

  function toggle(n: number) {
    setSelected((s) => ({ ...s, [n]: !s[n] }))
  }

  function selectCluster(cluster: ConsolidationCluster) {
    setSelected((s) => {
      const next = { ...s }
      cluster.numbers.forEach((n) => { next[n] = true })
      return next
    })
  }

  async function run() {
    const r = await consolidate.mutateAsync({ repo, numbers: chosen })
    if (r.level === 'ok') { toast.success(r.message); setSelected({}) }
    else if (r.level === 'warn') toast.warning(r.message)
    else toast.error(r.message)
  }

  if (!group.candidates.length) return null

  return (
    <div className="mt-3 border-t border-neutral-800 pt-3">
      <div className="mb-2 text-xs font-medium text-neutral-400">
        Consolidate PRs · {repo}
      </div>

      {group.clusters.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {group.clusters.map((cl) => (
            <button
              key={cl.numbers.join('-')}
              type="button"
              onClick={() => selectCluster(cl)}
              className={`rounded-full px-3 py-1 text-xs ${
                cl.risk === 'risky'
                  ? 'bg-red-950 text-red-300 hover:bg-red-900'
                  : 'bg-neutral-800 text-neutral-200 hover:bg-neutral-700'
              }`}
            >
              Consolidate these {cl.numbers.length} · {cl.rationale}
            </button>
          ))}
        </div>
      )}

      <ul className="mb-3 space-y-1">
        {group.candidates.map((c) => (
          <li key={c.number} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              aria-label={`select PR ${c.number}`}
              checked={!!selected[c.number]}
              onChange={() => toggle(c.number)}
            />
            <span className={`inline-block h-2 w-2 rounded-full ${ciDot(c.checks)}`} />
            <a href={c.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
              #{c.number} {c.title}
            </a>
            {c.mergeable === 'CONFLICTING' && (
              <span className="rounded bg-red-950 px-1 text-xs text-red-300">conflict</span>
            )}
          </li>
        ))}
      </ul>

      <button
        type="button"
        onClick={run}
        disabled={!canConsolidate}
        className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-40"
      >
        {consolidate.isPending ? 'Consolidating…' : `Consolidate ${chosen.length} PRs`}
      </button>
    </div>
  )
}
```

> Match the card's existing Tailwind palette during implementation — the class names above are representative; align them with `AutomationCard.tsx`'s actual tokens. The behavior (aria-labels, disabled logic, post shape) must stay as tested.

- [ ] **Step 4: Mount it in `web/src/components/AutomationCard.tsx`**

Add the import near the top:

```tsx
import { ConsolidatePanel } from './ConsolidatePanel'
```

Render it right after `<AutomationActions … />` (~line 375), gated on pr-doctor:

```tsx
          <AutomationActions automationId={a.id} enabled={open} />
          {a.id === 'pr-doctor' && <ConsolidatePanel enabled={open} />}
```

- [ ] **Step 5: Run the component test + full web gate**

Run (in `web/`):
```bash
npx vitest run src/components/ConsolidatePanel.test.tsx
npx vitest run
npx tsc --noEmit
npx eslint src/components/ConsolidatePanel.tsx src/components/AutomationCard.tsx
```
Expected: all pass/clean. (The full `vitest run` confirms the ~16 `@/api/hooks` mock lists in other suites still pass — `ConsolidatePanel` lives inside `AutomationCard`, not `SessionList`, so no mock-list churn is expected, but the full run proves it.)

- [ ] **Step 6: Commit**

```bash
git add web/src/components/ConsolidatePanel.tsx web/src/components/ConsolidatePanel.test.tsx web/src/components/AutomationCard.tsx
git commit -m "feat(consolidation): ConsolidatePanel — cluster chips + multi-select in PR Doctor card

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Manual multi-select → Task 7 checkbox list. ✅
- Recommended clusters → Task 1 `suggest_clusters` + Task 7 chips. ✅
- Session-driven consolidation, branch off master, close originals with "Superseded by" → Task 2 prompt + Task 3 spawn. ✅
- `Closes #N` per original → Task 2 prompt. ✅
- Never automatic → no auto path anywhere; only the POST route triggers it (Task 5). ✅
- Per-repo grouping / no cross-repo → Task 3 enumeration + Task 7 single-repo scoping. ✅
- Dedicated routes (not the generic trigger) → Task 5. ✅
- Candidates from `_pr_population.all()` → Task 3. ✅
- Guardrails (≥2, open/non-draft) → Task 1 `validate_selection` + Task 3 filter. ✅
- Dedupe on the sorted set → Task 3 `target` key + pre-check. ✅
- Status→message feedback → Task 4 map + Task 7 toast. ✅
- v1 recommender uses only already-fetched signals; files/author/labels deferred → Task 1 uses title-scope + checks only. ✅
- Testing across engine/prompt/orchestrator/provider/API/frontend → Tasks 1–7 each ship tests. ✅

**Placeholder scan:** No TBD/TODO; every code step is complete. The one flagged `[USER CONTRIBUTION POINT]` in Task 1 ships with a full reference implementation and a complete test suite, so the plan is executable as written even if the user declines to hand-write it.

**Type consistency:** `ConsolidationCandidate`/`Cluster` fields identical across Tasks 1/3/6/7. `_pr_doctor_consolidate` returns `(status, session_id)`; provider unpacks `status, _sid`; statuses in `_CONSOLIDATE_STATUS_MAP` (Task 4) exactly match those `_pr_doctor_consolidate` returns (Task 3): `spawned/duplicate/too_few/not_actionable/ceiling/clone_failed/spawn_failed`. Dedupe key string `consolidate:{repo}#{n+n}` identical in Task 3 code and Task 3 test. Frontend post body `{repo, numbers}` matches `ConsolidateBody` (Task 5) and the Task 7 test assertion.

**Open verification flagged inline for the implementer:** the API test's auth fixture (Task 5 note) says "copy the sibling `test_pr_doctor_actions_api.py` fixture" rather than guessing at the allowlist setup. (The `DbSession` alias was resolved during planning — confirmed at `orchestrator.py:40`.)
