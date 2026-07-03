# PR-status Field Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three fields to `PullRequestInfo` — `mergeable`, `merge_state_status`, `auto_merge_enabled` — so PR Doctor and the Backlog Runner can read merge/conflict/auto-merge state without each editing `pr_status.py`.

**Architecture:** A single, pure, behavior-free change to `server/github/pr_status.py`: extend the GraphQL node selection, populate the new fields in `_to_info`, and round-trip them through `PullRequestInfo.to_dict`/`from_dict` (the daemon→edge RPC boundary). This plan **lands before** the PR Doctor and Backlog Runner plans; those plans only *read* these fields and must not touch the query/parse/struct.

**Tech Stack:** Python 3 · SQLAlchemy-adjacent dataclasses · GitHub GraphQL via `gh api graphql` · pytest.

## Global Constraints

- Source of truth: `docs/superpowers/specs/2026-06-30-pr-doctor-design.md` §0 (the shared extension) and §2.
- `mergeStateStatus` is **already** in `_PR_NODE_FIELDS` and read by `derive_open_stage` — the change is to also request `mergeable` + `autoMergeRequest`, and to *store* all three on the dataclass.
- New dataclass fields MUST be defaulted (backward compatible; `from_dict` uses `d.get(...)` so older RPC payloads missing the keys don't crash).
- `auto_merge_enabled` is derived: GitHub's `autoMergeRequest` is `null` unless auto-merge is armed → `auto_merge_enabled = pr.get("autoMergeRequest") is not None`.
- Behavior-free: no automation logic here; existing pr_status / poller / pipeline tests must stay green.
- Test env: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest …` (fresh worktree has no built env; use the main checkout's prebuilt venv).

---

## File Structure

- Modify: `server/github/pr_status.py` — `_PR_NODE_FIELDS` (query), `_to_info` (populate), `PullRequestInfo` (fields + `to_dict`/`from_dict`).
- Test: `server/tests/test_pr_status_merge_fields.py` (create).

---

### Task 1: Add mergeable / merge_state_status / auto_merge_enabled to PullRequestInfo

**Files:**
- Modify: `server/github/pr_status.py` (`PullRequestInfo` dataclass ~L97-118, `to_dict` ~L120-142, `from_dict` ~L144-168, `_PR_NODE_FIELDS` ~L206, `_to_info` ~L507-521)
- Test: `server/tests/test_pr_status_merge_fields.py` (create)

**Interfaces:**
- Produces: `PullRequestInfo.mergeable: str = "UNKNOWN"`, `.merge_state_status: str = ""`, `.auto_merge_enabled: bool = False`, all round-tripped through `to_dict`/`from_dict`; `build_pr_query` requests `mergeable` + `autoMergeRequest`; `_to_info` populates them from a GraphQL node.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_pr_status_merge_fields.py
"""mergeable / merge_state_status / auto_merge_enabled: query, parse, round-trip."""
from __future__ import annotations

from server.github.pr_status import (
    PullRequestInfo, _to_info, build_pr_query,
)


def test_query_requests_new_fields():
    q = build_pr_query([("owner/repo", "feat/x")])
    assert "mergeable" in q
    assert "autoMergeRequest" in q
    assert "mergeStateStatus" in q  # already present, still there


def test_to_info_populates_merge_fields():
    node = {
        "number": 5, "url": "u", "state": "OPEN", "title": "t",
        "isDraft": False, "headRefOid": "abc", "updatedAt": "",
        "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
        "autoMergeRequest": {"enabledAt": "2026-06-30T00:00:00Z"},
        "commits": {"nodes": []},
    }
    info = _to_info(node, {})
    assert info.mergeable == "CONFLICTING"
    assert info.merge_state_status == "DIRTY"
    assert info.auto_merge_enabled is True


def test_to_info_defaults_when_absent():
    node = {
        "number": 6, "url": "u", "state": "OPEN", "title": "t",
        "isDraft": False, "headRefOid": "", "updatedAt": "",
        "commits": {"nodes": []},
    }
    info = _to_info(node, {})
    assert info.mergeable == "UNKNOWN"
    assert info.merge_state_status == ""
    assert info.auto_merge_enabled is False  # autoMergeRequest absent → not armed


def test_round_trip_preserves_merge_fields():
    info = PullRequestInfo(
        number=5, url="u", state="open", checks="failing", title="t",
        mergeable="CONFLICTING", merge_state_status="DIRTY", auto_merge_enabled=True,
    )
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.mergeable == "CONFLICTING"
    assert back.merge_state_status == "DIRTY"
    assert back.auto_merge_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest server/tests/test_pr_status_merge_fields.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'mergeable'` (and missing query strings).

- [ ] **Step 3: Add the dataclass fields**

In `PullRequestInfo` (after `check_groups: tuple[CheckGroup, ...] = ()`, keeping all fields defaulted):

```python
    # GitHub merge state (consumed by PR Doctor / Backlog Runner). Defaulted +
    # round-tripped so older daemon→edge RPC payloads stay compatible.
    mergeable: str = "UNKNOWN"          # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str = ""        # BEHIND | BLOCKED | CLEAN | DIRTY | DRAFT | UNSTABLE | UNKNOWN
    auto_merge_enabled: bool = False    # autoMergeRequest != null
```

- [ ] **Step 4: Round-trip them in to_dict / from_dict**

In `to_dict`, add to the returned dict (before the closing brace):

```python
            "mergeable": self.mergeable,
            "merge_state_status": self.merge_state_status,
            "auto_merge_enabled": self.auto_merge_enabled,
```

In `from_dict`, add to the `cls(...)` call:

```python
            mergeable=d.get("mergeable", "UNKNOWN"),
            merge_state_status=d.get("merge_state_status", ""),
            auto_merge_enabled=d.get("auto_merge_enabled", False),
```

- [ ] **Step 5: Request the fields in the GraphQL query**

In `_PR_NODE_FIELDS` (L206-207), add `mergeable` and `autoMergeRequest { enabledAt }` to the selection. The existing string starts:
`"number url title state isDraft updatedAt mergedAt headRefOid mergeStateStatus\n"` — append `mergeable` to that scalar list and add an `autoMergeRequest { enabledAt }` selection. Example:

```python
_PR_NODE_FIELDS = (
    "number url title state isDraft updatedAt mergedAt headRefOid mergeStateStatus mergeable\n"
    "    autoMergeRequest { enabledAt }\n"
    # ... (the rest of the existing selection unchanged) ...
)
```
> Verify the exact existing lines and insert without disturbing the `commits`/`statusCheckRollup` selections that follow.

- [ ] **Step 6: Populate them in `_to_info`**

In `_to_info`, add to the `PullRequestInfo(...)` constructor call:

```python
        mergeable=pr.get("mergeable") or "UNKNOWN",
        merge_state_status=pr.get("mergeStateStatus") or "",
        auto_merge_enabled=pr.get("autoMergeRequest") is not None,
```

- [ ] **Step 7: Run the new test + the existing pr_status/poller/pipeline tests**

Run:
```
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 <main-checkout>/.venv/bin/python -m pytest \
  server/tests/test_pr_status_merge_fields.py server/tests/ -k "pr_status or pr_poller or pipeline or population" -q
```
Expected: PASS (new file green; no regressions in pr_status/poller/pipeline parsing — the new fields are additive + defaulted).

- [ ] **Step 8: Commit**

```bash
git add server/github/pr_status.py server/tests/test_pr_status_merge_fields.py
git commit -m "feat(pr-status): add mergeable/merge_state_status/auto_merge_enabled to PullRequestInfo

Shared precursor for PR Doctor (conflict/out-of-date detection) and the Backlog
Runner (auto-merge handoff). mergeStateStatus was already queried; this stores it
and adds mergeable + autoMergeRequest, round-tripped across the daemon->edge RPC.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** PR Doctor §0 (the three fields, query, parse, cross-RPC round-trip) — Task 1 covers all. ✓
- **Placeholders:** none — all code is concrete; Step 5 asks the implementer to verify exact existing lines before inserting (the only non-verbatim step, because the surrounding selection must not be disturbed).
- **Type consistency:** `mergeable`/`merge_state_status` are `str`, `auto_merge_enabled` is `bool`, consistent across dataclass, `to_dict`, `from_dict`, `_to_info`, and the tests.

## Downstream note

The PR Doctor and Backlog Runner plans depend on this. They must **only read** `info.mergeable` / `info.merge_state_status` / `info.auto_merge_enabled` — they must NOT re-edit `pr_status.py`'s query/parse/struct (that would re-introduce the merge-order coupling this plan exists to remove).
