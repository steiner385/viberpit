# Child-Session Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Claude coding session's model spawn child ViberPit sessions through a `dispatch_session` tool, link them to the parent, notify the parent when a child finishes or hands back, and show lineage on the board.

**Architecture:** A new MUTATION session tool calls one orchestrator method, `dispatch_child_session`, which wraps the existing `create_session` and stamps a nullable `parent_session_id`. `_persist_status` (the driver status callback) notifies the parent by resuming it and sending a `<child-session-notification>` prompt once per stop-transition, guarded by a nullable `parent_notified_status` column. Pure policy (limits, mode narrowing, notice text) lives in `server/sessions/child_sessions.py`. The web derives children from the sessions list and renders lineage rows and a "child of" pill.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / `claude_agent_sdk` in-process MCP tools · React + TypeScript + vitest · pytest.

**Spec:** `docs/superpowers/specs/2026-09-11-child-session-dispatch-design.md`

## Global Constraints

- Branch `feat/child-session-dispatch` (off `origin/master` at 184cc843; spec committed at 46554682). Never commit to `master`.
- Python tests: `PYTEST <paths>` means
  `env -u CCSWITCH_SESSIOND_TLS_KEY -u CCSWITCH_SESSIOND_CLIENT_ALLOWLIST -u CCSWITCH_SESSIOND_TLS_CERT -u CCSWITCH_SESSIOND_ENDPOINT -u CCSWITCH_SESSIOND_TLS_CA -u CCSWITCH_DAEMON_ROLE -u CCSWITCH_TAILNET_ALLOWLIST -u VIBERPIT_SESSIOND_TLS_KEY -u VIBERPIT_SESSIOND_CLIENT_ALLOWLIST -u VIBERPIT_SESSIOND_TLS_CERT -u VIBERPIT_SESSIOND_ENDPOINT -u VIBERPIT_SESSIOND_TLS_CA -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST CCSWITCH_DEV=1 /home/tony/GitHub/viberpit-deploy/.venv/bin/python -m pytest <paths> -q -p no:cacheprovider`.
  Lint/type: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python -m ruff check src/ server/` and `… -m mypy server/ --ignore-missing-imports` (0 errors; `note:` lines are fine; mypy covers `server/tests/fakes` too).
- Web: `web/node_modules` is a symlink (already linked). Always `(cd web && …)` subshells. Gates: `npm run typecheck`, `npm run lint` (`--max-warnings 0`), FULL `npm run test`.
- Env knobs (exact): `CCSWITCH_CHILD_SESSIONS_ENABLED` (bool, default true), `CCSWITCH_CHILD_SESSION_CAP` (int, default 3), `CCSWITCH_CHILD_SESSION_MAX_DEPTH` (int, default 2). Every `Settings` field exists in BOTH branches of `server/config.py` (pydantic `Field(alias=…)` and the dataclass fallback).
- Wire keys camelCase (`parentSessionId`); Python snake_case. New DB columns are NULLABLE (reconcile-safe). Never `func.now()`.
- Mode width order (single source in `server/sessions/permissions.py`): `plan` < `interactive` < `auto` < `full-auto`; unknown ranks as `full-auto`.
- `CHILD_NOTIFY_STATUSES = frozenset({"complete", "done", "waiting_question", "ended"})`.
- Tool name `dispatch_session`, `ToolClass.MUTATION`, title `"Start a child session"`; mounted iff `signalling_tools and child_sessions`.
- Commit each task with a message file (`git commit -F /tmp/msg.txt`) ending `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Keep deploy/release/rollout/promote away from prod/production in command text (Bash hook).
- Do not touch `server/codex/`. No user-facing dispatch button (spec non-goal).

---

## File structure

| File | Responsibility |
|---|---|
| `server/config.py` | three knobs, both branches |
| `server/sessions/permissions.py` | `MODE_WIDTH`, `narrowest()` |
| `server/sessions/child_sessions.py` (new) | `ChildSessionLimit`, `check_limits`, `CHILD_NOTIFY_STATUSES`, `build_child_notice`, `truncate` |
| `server/db/models.py`, `server/db/migrations/versions/0038_child_sessions.py` | `parent_session_id`, `parent_notified_status` |
| `server/sessions/types.py`, `server/api/sessions.py` | record fields + both serializers |
| `server/sessions/orchestrator.py` | `dispatch_child_session`, `children_of`, `_notify_parent`, `_persist_status` hook, ctx wiring |
| `server/sessions/tools/{registry,tools,context,instruction}.py`, `server/sessions/claude_sdk_factory.py` | the tool, mount predicate, context accessors, blurb |
| `docs/superpowers/evals/session-tools/cases.jsonl`, `docs/product/features/{session-tools,child-sessions}.md` | eval case + docs |
| `web/src/api/types.ts`, `web/src/components/{ChildSessionRows,SessionCard,SessionList}.tsx` | lineage UI |

---

### Task 1: Config knobs, mode width, and the pure `child_sessions` module

**Files:**
- Modify: `server/config.py` (pydantic branch after `session_feedback_cap` ~line 256; dataclass branch after its `session_feedback_cap` ~line 552)
- Modify: `server/sessions/permissions.py` (module level, after `_SDK_PASSTHROUGH_MODES`)
- Create: `server/sessions/child_sessions.py`
- Test: `server/tests/test_child_sessions.py`

**Interfaces:**
- Produces:
  ```python
  get_settings().child_sessions_enabled: bool; .child_session_cap: int; .child_session_max_depth: int
  permissions.MODE_WIDTH: dict[str, int]; permissions.narrowest(a: str | None, b: str) -> str
  child_sessions.ChildSessionLimit(Exception)  # str(exc) is the model-readable reason
  child_sessions.check_limits(*, enabled: bool, parent_depth: int, live_children: int, cap: int, max_depth: int) -> None
  child_sessions.CHILD_NOTIFY_STATUSES: frozenset[str]
  child_sessions.LIVE_CHILD_EXCLUDED: frozenset[str] = {"complete", "done", "ended", "archived"}
  child_sessions.truncate(text: str, limit: int = 800) -> str
  child_sessions.build_child_notice(*, session_id, name, status, last_message, hand_back_reason=None, pr_resolution=None) -> str
  ```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_child_sessions.py`:

```python
"""Pure policy for child sessions (spec 2026-09-11-child-session-dispatch §4.3, §4.4)."""
import os
from unittest.mock import patch

import pytest

from server.sessions import child_sessions as cs
from server.sessions.permissions import MODE_WIDTH, narrowest


def test_mode_width_orders_plan_interactive_auto_full_auto():
    assert MODE_WIDTH["plan"] < MODE_WIDTH["interactive"] < MODE_WIDTH["auto"] < MODE_WIDTH["full-auto"]


@pytest.mark.parametrize("requested,parent,expected", [
    (None, "auto", "auto"),
    ("interactive", "auto", "interactive"),
    ("full-auto", "auto", "auto"),
    ("auto", "auto", "auto"),
    ("bogus", "interactive", "interactive"),   # unknown ranks as full-auto → parent wins
    (None, "full-auto", "full-auto"),
])
def test_narrowest_never_widens_past_the_parent(requested, parent, expected):
    assert narrowest(requested, parent) == expected


def test_limits_allow_within_cap_and_depth():
    cs.check_limits(enabled=True, parent_depth=0, live_children=2, cap=3, max_depth=2)
    cs.check_limits(enabled=True, parent_depth=1, live_children=0, cap=3, max_depth=2)


def test_limits_refuse_at_cap():
    with pytest.raises(cs.ChildSessionLimit) as exc:
        cs.check_limits(enabled=True, parent_depth=0, live_children=3, cap=3, max_depth=2)
    assert "3 live children" in str(exc.value) and "cap 3" in str(exc.value)


def test_limits_refuse_beyond_depth():
    with pytest.raises(cs.ChildSessionLimit) as exc:
        cs.check_limits(enabled=True, parent_depth=2, live_children=0, cap=3, max_depth=2)
    assert "depth 2" in str(exc.value)


def test_limits_refuse_when_disabled():
    with pytest.raises(cs.ChildSessionLimit) as exc:
        cs.check_limits(enabled=False, parent_depth=0, live_children=0, cap=3, max_depth=2)
    assert "disabled" in str(exc.value)


def test_notify_statuses_are_the_four_stops():
    assert cs.CHILD_NOTIFY_STATUSES == frozenset({"complete", "done", "waiting_question", "ended"})
    assert "busy" not in cs.CHILD_NOTIFY_STATUSES and "in_progress" not in cs.CHILD_NOTIFY_STATUSES


def test_truncate_keeps_short_text_and_marks_long_text():
    assert cs.truncate("hi") == "hi"
    long = "x" * 1000
    out = cs.truncate(long)
    assert len(out) <= 800 + 1 and out.endswith("…") and out.startswith("x" * 700)


def test_notice_carries_all_fields_and_omits_absent_ones():
    text = cs.build_child_notice(session_id="c1", name="Fix tests", status="waiting_question",
                                 last_message="Which DB?", hand_back_reason="need a decision",
                                 pr_resolution=None)
    assert text.startswith("<child-session-notification>")
    assert "<session-id>c1</session-id>" in text
    assert "<name>Fix tests</name>" in text
    assert "<status>waiting_question</status>" in text
    assert "<hand-back-reason>need a decision</hand-back-reason>" in text
    assert "<pr-resolution>" not in text
    assert "<summary>Which DB?</summary>" in text
    assert "<url>/sessions/c1</url>" in text
    assert text.rstrip().endswith("</child-session-notification>")


def test_notice_truncates_the_summary_and_escapes_nothing_else():
    text = cs.build_child_notice(session_id="c1", name="n", status="complete",
                                 last_message="y" * 2000, pr_resolution="not-needed")
    assert "<pr-resolution>not-needed</pr-resolution>" in text
    assert "y" * 800 in text and "y" * 801 not in text


def test_settings_knobs_read_env_in_both_branches():
    from server.config import get_settings
    with patch.dict(os.environ, {"CCSWITCH_CHILD_SESSIONS_ENABLED": "false",
                                 "CCSWITCH_CHILD_SESSION_CAP": "5",
                                 "CCSWITCH_CHILD_SESSION_MAX_DEPTH": "1"}):
        get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
        s = get_settings()
        assert s.child_sessions_enabled is False
        assert s.child_session_cap == 5 and s.child_session_max_depth == 1
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
```

If `get_settings` is not cached (no `cache_clear`), the test still passes because it reads env at construction; check how `server/tests/test_config*.py` reset settings and copy that idiom if one exists (`grep -n "cache_clear\|_settings = None\|reset_settings" server/tests/test_config*.py server/config.py`).

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST server/tests/test_child_sessions.py`
Expected: FAIL — `ImportError: cannot import name 'MODE_WIDTH'` / no module `child_sessions`.

- [ ] **Step 3: Config knobs**

`server/config.py`, pydantic branch, right after `session_feedback_cap`:

```python
        # Child sessions (spec 2026-09-11-child-session-dispatch §4.7): a Claude session
        # may spawn linked child sessions through the `dispatch_session` tool. Off = the
        # tool is not mounted and the orchestrator refuses a dispatch.
        child_sessions_enabled: bool = Field(
            default=True, alias="CCSWITCH_CHILD_SESSIONS_ENABLED")
        # Live (not complete/done/ended/archived) children one parent may have at once.
        child_session_cap: int = Field(default=3, alias="CCSWITCH_CHILD_SESSION_CAP")
        # Deepest nesting: 1 = children only, 2 = grandchildren, …
        child_session_max_depth: int = Field(default=2, alias="CCSWITCH_CHILD_SESSION_MAX_DEPTH")
```

Dataclass branch, right after its `session_feedback_cap`:

```python
        child_sessions_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_CHILD_SESSIONS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        )
        child_session_cap: int = dataclasses.field(
            default_factory=lambda: int(v) if (v := os.environ.get("CCSWITCH_CHILD_SESSION_CAP", "")) and v.isdigit() else 3
        )
        child_session_max_depth: int = dataclasses.field(
            default_factory=lambda: int(v) if (v := os.environ.get("CCSWITCH_CHILD_SESSION_MAX_DEPTH", "")) and v.isdigit() else 2
        )
```

- [ ] **Step 4: Mode width in `permissions.py`**

After `_SDK_PASSTHROUGH_MODES`:

```python
#: How much a ccswitch permission mode lets a session do without asking — the ONE
#: ordering for "is mode A wider than mode B". A child session may never run wider
#: than its parent (child-session spec §4.3); an unknown mode ranks widest so it can
#: never be picked as the "narrower" of two.
MODE_WIDTH: dict[str, int] = {"plan": 0, "interactive": 1, "auto": 2, "full-auto": 3}


def narrowest(requested: str | None, parent: str) -> str:
    """The narrower of a requested mode and the parent's mode; the parent's when
    nothing was requested or the request is unknown."""
    if not requested:
        return parent
    widest = max(MODE_WIDTH.values()) + 1
    return requested if MODE_WIDTH.get(requested, widest) <= MODE_WIDTH.get(parent, widest) else parent
```

- [ ] **Step 5: The pure module**

Create `server/sessions/child_sessions.py`:

```python
"""Pure policy for child sessions — limits, the parent notice, nothing stateful.

Spec: docs/superpowers/specs/2026-09-11-child-session-dispatch-design.md §4.3, §4.4.
The orchestrator owns the state (records, drivers); this module owns the decisions so
they are unit-testable without one.
"""
from __future__ import annotations

#: A child's stop transitions the parent is told about (§4.4). Busy/idle transitions
#: are not stops and never notify.
CHILD_NOTIFY_STATUSES: frozenset[str] = frozenset({"complete", "done", "waiting_question", "ended"})

#: A child in one of these no longer counts against the live-children cap.
LIVE_CHILD_EXCLUDED: frozenset[str] = frozenset({"complete", "done", "ended", "archived"})

SUMMARY_LIMIT = 800


class ChildSessionLimit(Exception):
    """A dispatch the policy refuses; ``str(exc)`` is the model-readable reason."""


def check_limits(*, enabled: bool, parent_depth: int, live_children: int, cap: int,
                 max_depth: int) -> None:
    if not enabled:
        raise ChildSessionLimit("child sessions are disabled on this host "
                                "(CCSWITCH_CHILD_SESSIONS_ENABLED=false)")
    if parent_depth + 1 > max_depth:
        raise ChildSessionLimit(f"children may not spawn grandchildren beyond depth {max_depth}; "
                                "do this work yourself or hand it back")
    if live_children >= cap:
        raise ChildSessionLimit(f"this session already has {live_children} live children "
                                f"(cap {cap}); wait for one to finish before dispatching another")


def truncate(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def build_child_notice(*, session_id: str, name: str, status: str, last_message: str,
                       hand_back_reason: str | None = None,
                       pr_resolution: str | None = None) -> str:
    """The prompt a parent receives when a child stops (§4.4) — the same shape as the
    CLI's own <task-notification>, so the parent's model reads it the way it already
    reads a finished background task."""
    lines = ["<child-session-notification>",
             f"<session-id>{session_id}</session-id>",
             f"<name>{name}</name>",
             f"<status>{status}</status>"]
    if hand_back_reason:
        lines.append(f"<hand-back-reason>{hand_back_reason}</hand-back-reason>")
    if pr_resolution:
        lines.append(f"<pr-resolution>{pr_resolution}</pr-resolution>")
    lines.append(f"<summary>{truncate(last_message)}</summary>")
    lines.append(f"<url>/sessions/{session_id}</url>")
    lines.append("</child-session-notification>")
    return "\n".join(lines)
```

- [ ] **Step 6: Run to verify they pass**

Run: `PYTEST server/tests/test_child_sessions.py server/tests/test_config*.py` and ruff + mypy on the three files.
Expected: PASS, clean.

- [ ] **Step 7: Commit**

```bash
git add server/config.py server/sessions/permissions.py server/sessions/child_sessions.py server/tests/test_child_sessions.py
printf '%s\n' 'feat(sessions): child-session knobs, mode width ordering, pure limits + parent notice' '' 'Refs spec docs/superpowers/specs/2026-09-11-child-session-dispatch-design.md §4.3, §4.4, §4.7.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 2: Data model — `parent_session_id`, `parent_notified_status`, both serializers, migration

**Files:**
- Modify: `server/db/models.py` (`Session`, after `assisted_by` ~line 233)
- Create: `server/db/migrations/versions/0038_child_sessions.py` (chain off `0037_triage_undo_snapshot`)
- Modify: `server/sessions/types.py` (`SessionRecord` fields near `origin_ref` ~line 61; `to_dict` ~line 193; `from_dict`)
- Modify: `server/api/sessions.py` (`_record_to_dict`, next to `"originRef"`)
- Test: `server/tests/test_child_sessions_wire.py`

**Interfaces:**
- Produces: `SessionRecord.parent_session_id: str | None = None`, `SessionRecord.parent_notified_status: str | None = None`; `to_dict()["parent_session_id"]`, `to_dict()["parent_notified_status"]` (RPC copy, snake_case like `origin_ref` there); `_record_to_dict(rec)["parentSessionId"]`; DB columns of the same names.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_child_sessions_wire.py`:

```python
"""parent_session_id rides both serializers and the DB row (spec §4.1)."""
import types

from server.api.sessions import _record_to_dict
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**over):
    base = dict(
        id="c1", name="child", account="a", engine=AgentEngine.CLAUDE_CODE,
        engine_session_id=None, reasoning_effort=None, repo=None, status="idle",
        name_is_auto=False, detached=False, adopted_in_place=False, sub_agents=None,
        created_at=None, last_turn_at=None, account_pinned=False, model=None,
        active_goal=None, usage_unverified=False, updated_at_iso=None, rev_at=None, cwd=None,
        parent_session_id="p1",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_http_serializer_emits_parent_session_id():
    assert _record_to_dict(_rec())["parentSessionId"] == "p1"
    assert _record_to_dict(_rec(parent_session_id=None))["parentSessionId"] is None


def test_record_roundtrips_parent_fields_over_rpc():
    rec = SessionRecord(id="c1", name="child", account="a", cwd="/tmp/x", status="idle",
                        parent_session_id="p1", parent_notified_status="done")
    d = rec.to_dict()
    assert d["parent_session_id"] == "p1" and d["parent_notified_status"] == "done"
    back = SessionRecord.from_dict(d)
    assert back.parent_session_id == "p1" and back.parent_notified_status == "done"


def test_db_session_has_nullable_parent_columns():
    from server.db.models import Session
    for col in ("parent_session_id", "parent_notified_status"):
        c = Session.__table__.c[col]
        assert c.nullable is True
```

Adjust the `SessionRecord(...)` constructor call to the dataclass's real required fields (read `server/sessions/types.py:46-70`; keep only what is required plus the two new fields).

- [ ] **Step 2: Run to verify it fails**

Run: `PYTEST server/tests/test_child_sessions_wire.py`
Expected: FAIL — `KeyError: 'parentSessionId'` / `TypeError: unexpected keyword 'parent_session_id'`.

- [ ] **Step 3: Model columns + migration**

`server/db/models.py`, after `assisted_by`:

```python
    # Lineage (child-session spec §4.1): the session whose `dispatch_session` tool call
    # created this one. NULL = a root session (every human-created one, every row that
    # predates the feature). Nullable so init_db's reconcile adds it on both dialects.
    parent_session_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # The child status most recently reported to the parent as a
    # <child-session-notification> — the idempotency guard for that notice across
    # repeated status persists and daemon restarts. NULL = never reported.
    parent_notified_status: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
```

Create `server/db/migrations/versions/0038_child_sessions.py` mirroring `0037_triage_undo_snapshot.py`'s header (copy its imports and the `revision`/`down_revision` idiom; `down_revision` = 0037's `revision` string):

```python
"""child sessions: parent_session_id, parent_notified_status on sessions

Revision ID: 0038_child_sessions
Revises: <0037 revision id>
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_child_sessions"
down_revision = "<0037 revision id>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("parent_session_id", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("parent_notified_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "parent_notified_status")
    op.drop_column("sessions", "parent_session_id")
```

Check the table name used by 0037 (`sessions` vs `session`) and match it.

- [ ] **Step 4: Record + serializers**

`server/sessions/types.py` `SessionRecord`, after `assisted_by`:

```python
    parent_session_id: str | None = None      # dispatching parent session (child-session spec §4.1)
    parent_notified_status: str | None = None # last child status reported to the parent
```

`to_dict`: add `"parent_session_id": self.parent_session_id, "parent_notified_status": self.parent_notified_status,` beside `"origin_ref"`. `from_dict`: read both with `.get(...)`. Wherever the orchestrator builds a `SessionRecord` from a DB row (`grep -n "SessionRecord(" server/sessions/orchestrator.py`), and wherever it copies a record to a DB row on create, thread both fields the same way `assisted_by` is threaded (grep `assisted_by=` in orchestrator.py and mirror each site).

`server/api/sessions.py` `_record_to_dict`: after `"originRef": …` add `"parentSessionId": getattr(record, "parent_session_id", None),`.

- [ ] **Step 5: Run to verify it passes**

Run: `PYTEST server/tests/test_child_sessions_wire.py server/tests/test_sessions_api_summary.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py -q` (the last two are skipped/green without `CCSWITCH_TEST_PG_URL`; nullable columns need no `_REQUIRED_EXTRAS` entry). Then ruff + mypy.
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/db/models.py server/db/migrations/versions/0038_child_sessions.py server/sessions/types.py server/api/sessions.py server/sessions/orchestrator.py server/tests/test_child_sessions_wire.py
printf '%s\n' 'feat(sessions): parent_session_id / parent_notified_status on the session row and both serializers' '' 'Refs spec §4.1.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 3: Orchestrator — `dispatch_child_session`, `children_of`, parent notification

**Files:**
- Modify: `server/sessions/orchestrator.py` (new methods next to `create_session`; hook in `_persist_status` after the DB status write and before `if status == "complete": live_driver = …`)
- Test: `server/tests/test_child_session_dispatch.py`

**Interfaces:**
- Consumes: Task 1 (`check_limits`, `narrowest`, `build_child_notice`, `CHILD_NOTIFY_STATUSES`, `LIVE_CHILD_EXCLUDED`, settings), Task 2 (fields).
- Produces:
  ```python
  SessionOrchestrator.children_of(parent_id: str) -> list[SessionRecord]   # creation order, from self._records
  SessionOrchestrator.child_depth(session_id: str) -> int                  # 0 for a root
  async SessionOrchestrator.dispatch_child_session(parent_id: str, *, name: str, prompt: str, model: str | None, permission_mode: str | None, identity: str) -> dict  # {"id","name","url","status"}
  async SessionOrchestrator._notify_parent(parent_id: str, child, status: str, hand_back_reason: str | None) -> None
  ```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_child_session_dispatch.py`, reusing the working orchestrator harness:

```python
"""dispatch_child_session + parent notification (spec §4.3, §4.4)."""
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from server.sessions.child_sessions import ChildSessionLimit
from server.tests.test_assistant_orchestrator import _make_orch, _stop_orch


def _run(coro):
    return asyncio.run(coro)


def test_dispatch_creates_a_linked_child_with_narrowed_mode(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'a.db'}", tmp_path=tmp_path)
        try:
            parent = await orch.create_session(name="Parent", cwd=str(tmp_path), identity="u@x",
                                               permission_mode="auto")
            out = await orch.dispatch_child_session(parent.id, name="Child A", prompt="do X",
                                                    model=None, permission_mode="full-auto",
                                                    identity="u@x")
            child = orch._records[out["id"]]
            assert out["name"] == "Child A" and out["url"] == f"/sessions/{child.id}"
            assert child.parent_session_id == parent.id
            assert child.permission_mode == "auto"          # never wider than the parent
            assert child.name_is_auto is False
            assert orch.child_depth(child.id) == 1 and orch.child_depth(parent.id) == 0
            assert [c.id for c in orch.children_of(parent.id)] == [child.id]
            from sqlalchemy import select
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                row = (await db.execute(select(DbSession).where(DbSession.id == child.id))).scalar_one()
                assert row.parent_session_id == parent.id
        finally:
            await _stop_orch(orch)
    _run(run())


def test_dispatch_enforces_cap_and_depth_and_switch(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'b.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            with patch.dict(os.environ, {"CCSWITCH_CHILD_SESSION_CAP": "1",
                                         "CCSWITCH_CHILD_SESSION_MAX_DEPTH": "1"}):
                c1 = await orch.dispatch_child_session(parent.id, name="c1", prompt="x",
                                                       model=None, permission_mode=None, identity="u@x")
                with pytest.raises(ChildSessionLimit, match="cap 1"):
                    await orch.dispatch_child_session(parent.id, name="c2", prompt="x",
                                                      model=None, permission_mode=None, identity="u@x")
                with pytest.raises(ChildSessionLimit, match="depth 1"):
                    await orch.dispatch_child_session(c1["id"], name="g", prompt="x",
                                                      model=None, permission_mode=None, identity="u@x")
            with patch.dict(os.environ, {"CCSWITCH_CHILD_SESSIONS_ENABLED": "false"}):
                with pytest.raises(ChildSessionLimit, match="disabled"):
                    await orch.dispatch_child_session(parent.id, name="c3", prompt="x",
                                                      model=None, permission_mode=None, identity="u@x")
        finally:
            await _stop_orch(orch)
    _run(run())


def test_child_stop_notifies_parent_once_per_transition(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            out = await orch.dispatch_child_session(parent.id, name="c", prompt="x", model=None,
                                                    permission_mode=None, identity="u@x")
            sent: list[tuple[str, str]] = []

            async def fake_send(sid, text, *a, **k):
                sent.append((sid, text)); return "sent"
            orch.send_prompt = fake_send                      # type: ignore[method-assign]
            orch.ensure_driver = AsyncMock(return_value=object())   # type: ignore[method-assign]
            with patch("server.sessions.orchestrator.last_assistant_text", return_value="all done"):
                await orch._persist_status(out["id"], "done")
                await orch._persist_status(out["id"], "done")      # same status → no second notice
                await orch._persist_status(out["id"], "busy")
                await orch._persist_status(out["id"], "waiting_question")
            for _ in range(20):                                   # notices are spawned tasks
                await asyncio.sleep(0.01)
            assert [s for s, _ in sent] == [parent.id, parent.id]
            assert "<status>done</status>" in sent[0][1] and "<summary>all done</summary>" in sent[0][1]
            assert "<status>waiting_question</status>" in sent[1][1]
            assert orch._records[out["id"]].parent_notified_status == "waiting_question"
        finally:
            await _stop_orch(orch)
    _run(run())


def test_notice_is_skipped_for_an_archived_parent(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}", tmp_path=tmp_path,
                                max_active_sessions=20)
        try:
            parent = await orch.create_session(name="P", cwd=str(tmp_path), identity="u@x")
            out = await orch.dispatch_child_session(parent.id, name="c", prompt="x", model=None,
                                                    permission_mode=None, identity="u@x")
            orch._records[parent.id].status = "archived"
            orch.send_prompt = AsyncMock()                     # type: ignore[method-assign]
            with patch("server.sessions.orchestrator.last_assistant_text", return_value=""):
                await orch._persist_status(out["id"], "complete")
            for _ in range(20):
                await asyncio.sleep(0.01)
            orch.send_prompt.assert_not_awaited()
            assert orch._records[out["id"]].parent_notified_status == "complete"
        finally:
            await _stop_orch(orch)
    _run(run())
```

Notes for the implementer: `_persist_status` may read `driver.last_hand_back_reason` — the fake driver may lack it; use `getattr`. If `_persist_status` needs the child to have a live driver for `waiting_question`, adapt the sequence to statuses the harness supports and keep the assertion "two notices for done→busy→waiting_question, none for the repeated done". If importing `_make_orch` from the test module is blocked by conftest, move `_make_orch`/`_stop_orch`/`_seed_account`/`_fake_usage` into `server/tests/fakes/orch_harness.py` and import them from there in BOTH files (delete the originals — no duplicate).

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST server/tests/test_child_session_dispatch.py`
Expected: FAIL — `AttributeError: 'SessionOrchestrator' object has no attribute 'dispatch_child_session'`.

- [ ] **Step 3: Implement**

In `server/sessions/orchestrator.py`, module-level imports: `from server.sessions.background_tasks import last_assistant_text` (check whether already imported; if the module imports `background_tasks` as a module, use that path and patch target accordingly in the tests) and `from server.sessions import child_sessions as _cs`.

New methods next to `create_session`:

```python
    # ── Child sessions (spec 2026-09-11-child-session-dispatch) ────────────────────
    def children_of(self, parent_id: str) -> list:
        """Records whose parent is *parent_id*, in creation order."""
        kids = [r for r in self._records.values()
                if getattr(r, "parent_session_id", None) == parent_id]
        kids.sort(key=lambda r: (getattr(r, "created_at", None) or 0, r.id))
        return kids

    def child_depth(self, session_id: str) -> int:
        """0 for a root; 1 + parent's depth otherwise (a missing parent counts as 1)."""
        depth, seen, cur = 0, set(), self._records.get(session_id)
        while cur is not None and getattr(cur, "parent_session_id", None) and cur.id not in seen:
            seen.add(cur.id)
            depth += 1
            cur = self._records.get(cur.parent_session_id)
        return depth

    async def dispatch_child_session(self, parent_id: str, *, name: str, prompt: str,
                                     model: str | None, permission_mode: str | None,
                                     identity: str) -> dict:
        """Create a CHILD session of *parent_id* (spec §4.3): same repo, fresh worktree,
        same owner, mode never wider than the parent's. Raises ChildSessionLimit for the
        cap/depth/kill-switch refusals (the tool reports str(exc) to the model)."""
        from server.sessions.permissions import narrowest
        parent = self._records.get(parent_id)
        if parent is None:
            raise KeyError(f"No session with id {parent_id!r}")
        cfg = get_settings()
        live = [c for c in self.children_of(parent_id)
                if getattr(c, "status", None) not in _cs.LIVE_CHILD_EXCLUDED]
        _cs.check_limits(enabled=cfg.child_sessions_enabled,
                         parent_depth=self.child_depth(parent_id), live_children=len(live),
                         cap=cfg.child_session_cap, max_depth=cfg.child_session_max_depth)
        mode = narrowest(permission_mode, getattr(parent, "permission_mode", None) or "interactive")
        rec = await self.create_session(
            name=name, initial_prompt=prompt, repo=getattr(parent, "repo", None),
            cwd=None if getattr(parent, "repo", None) else getattr(parent, "cwd", None),
            identity=identity, permission_mode=mode, name_is_auto=False,
            model=model or getattr(parent, "model", None),
            engine=getattr(parent, "engine", AgentEngine.CLAUDE_CODE))
        rec.parent_session_id = parent_id
        async with db_session() as db:
            from sqlalchemy import update as sa_update
            await db.execute(sa_update(DbSession).where(DbSession.id == rec.id)
                             .values(parent_session_id=parent_id))
            await record_audit(db, "session.child_dispatch", identity,
                               {"parentSessionId": parent_id, "childSessionId": rec.id, "name": name},
                               via="session")
        log.info("session.child_dispatch parent=%s child=%s mode=%s", parent_id, rec.id, mode)
        return {"id": rec.id, "name": rec.name, "url": f"/sessions/{rec.id}", "status": rec.status}
```

(`cwd=None if repo else parent.cwd` keeps the fake-SDK harness — which creates sessions by `cwd` with no repo — working, and gives a real repo-backed parent a fresh worktree via `repo=`. Confirm `db_session`, `DbSession`, `record_audit`, `get_settings`, `AgentEngine` are already imported in the module; add any missing import at module level.)

In `_persist_status`, right after the DB row's status write (find the `await db.execute(sa_update(DbSession)...status=...)` / equivalent that persists `status` for `rec`) and BEFORE the `if status == "complete": live_driver = self._drivers.pop(...)` block:

```python
        # Child sessions (spec §4.4): tell the parent about a stop transition, once per
        # transition. Spawned, never awaited — this runs inside the child's status callback.
        parent_id = getattr(rec, "parent_session_id", None) if rec is not None else None
        if parent_id and status in _cs.CHILD_NOTIFY_STATUSES \
                and getattr(rec, "parent_notified_status", None) != status:
            rec.parent_notified_status = status
            async with db_session() as db:
                from sqlalchemy import update as sa_update
                await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                                 .values(parent_notified_status=status))
            drv = self._drivers.get(session_id)
            reason = getattr(drv, "last_hand_back_reason", None) if status == "waiting_question" else None
            self._spawn(self._notify_parent(parent_id, rec, status, reason), name="child-notify")
        elif parent_id and status not in _cs.CHILD_NOTIFY_STATUSES \
                and getattr(rec, "parent_notified_status", None) is not None:
            rec.parent_notified_status = None       # a new run resets the guard (done→busy→done notifies twice)
```

`_notify_parent`:

```python
    async def _notify_parent(self, parent_id: str, child, status: str,
                             hand_back_reason: str | None) -> None:
        """Deliver a <child-session-notification> to the parent as a prompt (spec §4.4).
        Resumes a detached parent first; a bare send_prompt drops the text without a
        driver. Best-effort: logs, never raises."""
        try:
            parent = self._records.get(parent_id)
            if parent is None or getattr(parent, "status", None) in ("archived", "ended"):
                log.info("child_notify.skip parent=%s child=%s parent_status=%s",
                         parent_id, child.id, getattr(parent, "status", None))
                return
            path = await self.transcript_path(child.id)
            last = last_assistant_text(path) if path else ""
            text = _cs.build_child_notice(
                session_id=child.id, name=getattr(child, "name", child.id), status=status,
                last_message=last, hand_back_reason=hand_back_reason,
                pr_resolution=getattr(child, "pr_resolution", None))
            drv = await self.ensure_driver(parent_id, getattr(parent, "owner_identity", None)
                                           or getattr(child, "identity", None) or SYSTEM_IDENTITY)
            if drv is None:
                log.warning("child_notify.no_driver parent=%s child=%s", parent_id, child.id)
                return
            await self.send_prompt(parent_id, text)
            log.info("child_notify.sent parent=%s child=%s status=%s", parent_id, child.id, status)
        except Exception:  # noqa: BLE001 — a notice failure must never fail the child's status persist
            log.warning("child_notify.failed parent=%s child=%s", parent_id, child.id, exc_info=True)
```

Check `transcript_path`'s real signature (`grep -n "async def transcript_path" server/sessions/orchestrator.py`) and `ensure_driver(session_id, identity)`; pick the identity the record stores (`grep -n "identity" server/sessions/types.py` — use the field that holds the owner, else `SYSTEM_IDENTITY`).

The `parent_notified_status` reset branch must NOT write the DB on every busy tick: only write when it changes from non-None to None (the code above does exactly that: it only runs when the stored value is not None). Persist that reset with the same `sa_update` (add it) so the guard survives a restart consistently.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTEST server/tests/test_child_session_dispatch.py server/tests/test_assistant_orchestrator.py server/tests/test_orchestrator_facade.py -q` then ruff + mypy.
Expected: PASS, clean. (`test_orchestrator_facade`'s completeness test is untouched: `session_actions` does not call the new method.)

- [ ] **Step 5: Commit**

```bash
git add server/sessions/orchestrator.py server/tests/test_child_session_dispatch.py server/tests/fakes/orch_harness.py server/tests/test_assistant_orchestrator.py
printf '%s\n' 'feat(sessions): dispatch_child_session + parent notification on child stop transitions' '' 'Refs spec §4.3, §4.4.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

(Only add `orch_harness.py` / the assistant test if you moved the harness.)

---

### Task 4: The `dispatch_session` tool — spec, mount predicate, handler, context, instruction, eval

**Files:**
- Modify: `server/sessions/tools/registry.py` (`SESSION_TOOL_SPECS`, `mounted_signal_names`/`mounted_names`)
- Modify: `server/sessions/tools/context.py` (`SessionToolContext` fields; `context_snapshot`)
- Modify: `server/sessions/tools/tools.py` (`mounted_tool_names`, new handler)
- Modify: `server/sessions/tools/instruction.py` (`_BLURBS`, `_session_tools_instruction`, `session_instructions` signature)
- Modify: `server/sessions/claude_sdk_factory.py` (~line 33, pass `child_sessions=`)
- Modify: `server/sessions/orchestrator.py` (~line 1464 `SessionToolContext(...)`: pass `child_sessions`, `dispatch_child`, `children`)
- Modify: `docs/superpowers/evals/session-tools/cases.jsonl` (append one case)
- Test: `server/tests/test_session_tools.py` (existing expectations), `server/tests/test_session_tools_instruction.py` (existing), new `server/tests/test_dispatch_session_tool.py`

**Interfaces:**
- Consumes: `SessionOrchestrator.dispatch_child_session`, `children_of` (Task 3).
- Produces:
  ```python
  SessionToolContext.child_sessions: bool = False
  SessionToolContext.dispatch_child: Callable[..., Awaitable[dict]] | None = None   # kwargs: name, prompt, model, permission_mode
  SessionToolContext.children: Callable[[], list] | None = None                     # -> records
  registry.mounted_names(*, signalling_tools, self_model_switch, child_sessions=False)
  instruction.session_instructions(*, session_tools, signalling_tools, self_model_switch, child_sessions=False)
  context_snapshot(ctx)["parentSessionId"], ["children"]: [{"id","name","status"}]
  ```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_dispatch_session_tool.py`:

```python
"""The dispatch_session tool (spec §4.2, §4.5)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import mcp_spec
from server.sessions.tools.context import SessionToolContext, context_snapshot
from server.sessions.tools.registry import SESSION_TOOL_SPECS, mounted_names
from server.sessions.tools.tools import build_session_tools


def _rec(**over):
    base = dict(id="p1", name="Parent", repo="o/n", branch="b", cwd="/tmp/w", origin=None,
                origin_ref=None, engine="claude_code", model="sonnet", permission_mode="auto",
                account="alpha", host="dobby", active_goal=None, inferred_goal=None, kind=None,
                parent_session_id=None)
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(*, child_sessions=True, dispatch=None, children=None, record=None):
    rec = record or _rec()
    return SessionToolContext(session_id="p1", identity="u@x", kind=None, engine="claude_code",
                              record=lambda: rec, host="dobby", signalling_tools=True,
                              child_sessions=child_sessions, dispatch_child=dispatch,
                              children=children or (lambda: []))


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def _payload(result):
    return json.loads(result["content"][0]["text"])


def test_spec_is_a_mutation_and_mount_is_gated():
    assert SESSION_TOOL_SPECS["dispatch_session"].tool_class is mcp_spec.ToolClass.MUTATION
    assert "dispatch_session" in mounted_names(signalling_tools=True, self_model_switch=False, child_sessions=True)
    assert "dispatch_session" not in mounted_names(signalling_tools=True, self_model_switch=False, child_sessions=False)
    assert "dispatch_session" not in mounted_names(signalling_tools=False, self_model_switch=False, child_sessions=True)
    names = {t.name for t in build_session_tools(_ctx(child_sessions=False))}
    assert "dispatch_session" not in names
    names = {t.name for t in build_session_tools(_ctx())}
    assert "dispatch_session" in names


@pytest.mark.asyncio
async def test_handler_calls_dispatch_child_once_with_its_args():
    dispatch = AsyncMock(return_value={"id": "c1", "name": "Child", "url": "/sessions/c1", "status": "idle"})
    tools = build_session_tools(_ctx(dispatch=dispatch))
    out = await _tool(tools, "dispatch_session").handler(
        {"name": "Child", "prompt": "Do X", "model": "sonnet", "permission_mode": "interactive"})
    dispatch.assert_awaited_once_with(name="Child", prompt="Do X", model="sonnet",
                                      permission_mode="interactive")
    assert _payload(out)["data"]["id"] == "c1"


@pytest.mark.asyncio
async def test_handler_rejects_bad_args_before_dispatch():
    dispatch = AsyncMock()
    tools = build_session_tools(_ctx(dispatch=dispatch))
    t = _tool(tools, "dispatch_session")
    for bad in ({"name": "ab", "prompt": "x"}, {"name": "ok name", "prompt": "  "},
                {"name": "ok name", "prompt": "x", "model": "gpt-9"},
                {"name": "ok name", "prompt": "x", "permission_mode": "root"}):
        out = await t.handler(bad)
        assert out.get("is_error") is True
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_limit_refusal_is_reported_as_the_tool_result():
    from server.sessions.child_sessions import ChildSessionLimit
    dispatch = AsyncMock(side_effect=ChildSessionLimit("this session already has 3 live children (cap 3)"))
    tools = build_session_tools(_ctx(dispatch=dispatch))
    out = await _tool(tools, "dispatch_session").handler({"name": "Child", "prompt": "x"})
    assert out.get("is_error") is True and "cap 3" in out["content"][0]["text"]


def test_session_context_lists_children_and_parent():
    kids = [SimpleNamespace(id="c1", name="Child", status="idle")]
    snap = context_snapshot(_ctx(children=lambda: kids, record=_rec(parent_session_id="root")))
    assert snap["parentSessionId"] == "root"
    assert snap["children"] == [{"id": "c1", "name": "Child", "status": "idle"}]


def test_instruction_names_the_tool_only_when_mounted():
    from server.sessions.tools.instruction import session_instructions
    on = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False, child_sessions=True)
    off = session_instructions(session_tools=True, signalling_tools=True, self_model_switch=False, child_sessions=False)
    assert "dispatch_session" in on and "dispatch_session" not in off
```

How a tool's handler is reached on an `SdkMcpTool` (attribute name `handler` vs `fn`): copy whatever `server/tests/test_session_tools.py` uses (`grep -n "\.handler(\|\.fn(" server/tests/test_session_tools.py`).

- [ ] **Step 2: Run to verify they fail**

Run: `PYTEST server/tests/test_dispatch_session_tool.py`
Expected: FAIL — `KeyError: 'dispatch_session'` / unexpected kwarg `child_sessions`.

- [ ] **Step 3: Registry + context**

`registry.py` `SESSION_TOOL_SPECS`: add

```python
    # MUTATION: it creates a session the user sees on the board. Gated on its
    # annotation by the permission layer — a card in interactive mode, auto-allowed in
    # full-auto — exactly like the assistant drawer's create_session.
    ToolSpec("dispatch_session", ToolClass.MUTATION, "Start a child session"),
```

`mounted_names(*, signalling_tools, self_model_switch, child_sessions=False)`: return
`READ_TOOL_NAMES + mounted_signal_names(...) + (("dispatch_session",) if signalling_tools and child_sessions else ())`. Update its docstring: the tool rides only on the durable surface (Claude), and only with the kill switch on.

`context.py` `SessionToolContext`: add

```python
    #: Child sessions are enabled on this host (``CCSWITCH_CHILD_SESSIONS_ENABLED``) —
    #: the mount predicate for `dispatch_session` alongside `signalling_tools`.
    child_sessions: bool = False
    #: Bound by the orchestrator to ``dispatch_child_session(parent_id=…, identity=…)``;
    #: the handler's ONE call. None when the surface is off.
    dispatch_child: Callable[..., Awaitable[Any]] | None = None
    #: Live accessor for this session's children (records), for `session_context`.
    children: Callable[[], list[Any]] | None = None
```

(`from collections.abc import Awaitable, Callable` — check the file's existing imports.) `context_snapshot`: add `"parentSessionId": getattr(rec, "parent_session_id", None)` and
`"children": [{"id": c.id, "name": getattr(c, "name", None), "status": getattr(c, "status", None)} for c in (ctx.children() if ctx.children else [])]`.

- [ ] **Step 4: Handler**

`tools.py`: `mounted_tool_names(ctx)` passes `child_sessions=ctx.child_sessions`. Add the handler after `request_model` (inside `build_session_tools`), and add `"dispatch_session"` to whatever list/loop selects handlers by `mounted_tool_names` (read how the SIGNAL handlers are conditionally included and mirror it):

```python
    @spec_tool("dispatch_session",
               "Start a CHILD session of this one: a separate session on the same repo, in "
               "its own worktree, owned by the same user, that the user can open, steer, "
               "approve and stop like any session. Use it for a unit of work the user must "
               "be able to interact with independently — a sub-task that will need its own "
               "decisions, review or a long runtime; keep using the Agent tool for quick, "
               "self-contained research. `name` (3-60 chars): the board title. `prompt`: the "
               "child's COMPLETE brief — it does not see this conversation. `model` "
               f"(optional, one of {', '.join(MODEL_TIERS)}): default is this session's model. "
               "`permission_mode` (optional, one of plan, interactive, auto, full-auto): never "
               "wider than this session's. Returns the child's id and URL immediately; you "
               "receive a <child-session-notification> prompt when the child completes, hands "
               "back or ends, carrying its last message. Call session_context to list your "
               "children and their status. Live children are capped and grandchildren are "
               "limited; a refusal comes back as the tool result.",
               {"type": "object",
                "properties": {"name": {"type": "string"}, "prompt": {"type": "string"},
                               "model": {"type": "string"}, "permission_mode": {"type": "string"}},
                "required": ["name", "prompt"]})
    @guarded
    async def dispatch_session(args: dict[str, Any]) -> Any:
        name = str(args.get("name") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        model = args.get("model")
        mode = args.get("permission_mode")
        if not (3 <= len(name) <= 60):
            raise ToolArgError("name must be 3-60 characters")
        if not prompt:
            raise ToolArgError("prompt must not be empty — it is the child's whole brief")
        if model is not None and model not in MODEL_TIERS:
            raise ToolArgError(f"model must be one of {', '.join(MODEL_TIERS)}")
        if mode is not None and mode not in MODE_WIDTH:
            raise ToolArgError("permission_mode must be one of plan, interactive, auto, full-auto")
        if ctx.dispatch_child is None:
            raise ToolArgError("child sessions are not available in this session")
        try:
            return await ctx.dispatch_child(name=name, prompt=prompt, model=model, permission_mode=mode)
        except ChildSessionLimit as e:
            raise ToolArgError(str(e)) from e
```

Imports: `from server.sessions.permissions import MODE_WIDTH`, `from server.sessions.child_sessions import ChildSessionLimit` (check `MODEL_TIERS` is the tier alias list already imported).

- [ ] **Step 5: Instruction + factory + orchestrator wiring**

`instruction.py`: `_BLURBS["dispatch_session"] = "you need a linked child session the user can steer independently."`; `_session_tools_instruction(*, signalling_tools, self_model_switch, child_sessions)` and `session_instructions(..., child_sessions: bool = False)` thread the flag into `mounted_names`. `claude_sdk_factory.py` ~line 33: add `child_sessions=cfg.child_sessions_enabled`. Codex's call site (`server/codex/driver.py` calls `session_instructions`) keeps the default `False` — do not touch `server/codex/`.

`orchestrator.py` ~line 1464 `SessionToolContext(...)`: add

```python
            child_sessions=bool(caps is not None and caps.durable_tool_surface
                                and get_settings().child_sessions_enabled),
            dispatch_child=functools.partial(self.dispatch_child_session, session_id, identity=identity),
            children=lambda: self.children_of(session_id),
```

(`import functools` if missing; `dispatch_child_session(parent_id, *, name, prompt, model, permission_mode, identity)` — the partial binds `parent_id` positionally and `identity` by keyword, so the handler's four kwargs complete the call.)

Eval case, append to `docs/superpowers/evals/session-tools/cases.jsonl` (one line, valid JSON):

```json
{"id": "dispatch-a-steerable-subtask", "prompt": "Split this feature: the migration is straightforward, but the API redesign needs the user to weigh in on naming as it goes. Get both moving.", "expect_tools": ["dispatch_session"], "forbid_tools": ["report_feedback"], "rubric": "Dispatches the API redesign as a child session with a complete brief (dispatch_session) because the user must steer it; does the migration itself or via the Agent tool; does not dispatch trivial research."}
```

- [ ] **Step 6: Run to verify they pass**

Run: `PYTEST server/tests/test_dispatch_session_tool.py server/tests/test_session_tools.py server/tests/test_session_tools_instruction.py server/tests/test_session_signal_tools.py server/tests/test_assistant_permissions.py -q`, then ruff + mypy.
Expected: PASS. If `test_every_session_tool_has_a_spec_and_an_eval_case` asserts an exact READ-only name set for the default ctx, it still holds (the default ctx has `child_sessions=False`).

- [ ] **Step 7: Commit**

```bash
git add server/sessions/tools/ server/sessions/claude_sdk_factory.py server/sessions/orchestrator.py docs/superpowers/evals/session-tools/cases.jsonl server/tests/test_dispatch_session_tool.py
printf '%s\n' 'feat(sessions): dispatch_session tool — spawn a linked child session from a Claude session' '' 'MUTATION tool on the durable surface, gated by CCSWITCH_CHILD_SESSIONS_ENABLED; session_context lists children. Refs spec §4.2, §4.5.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 5: Web lineage — `parentSessionId`, child rows on the parent card, "child of" pill

**Files:**
- Modify: `web/src/api/types.ts` (`Session`, after `originRef`)
- Create: `web/src/components/ChildSessionRows.tsx`
- Modify: `web/src/components/SessionCard.tsx` (props `childSessions?: Session[]`, `parentName?: string | null`; pill next to the provenance chips ~line 288-305; rows after `SubAgentRows` ~line 724; a "N children" trailing count like the agents count ~line 318)
- Modify: `web/src/components/SessionList.tsx` (`renderCard` ~line 145: compute `childSessions` and `parentName` from `sessions`)
- Test: `web/src/components/__tests__/ChildSessionRows.test.tsx`, `web/src/components/SessionCard.children.test.tsx`

**Interfaces:**
- Produces: `Session.parentSessionId?: string | null`; `ChildSessionRows({ children: Session[] })`; `SessionCard` props `childSessions?: Session[]`, `parentName?: string | null`.

- [ ] **Step 1: Write the failing tests**

`web/src/components/__tests__/ChildSessionRows.test.tsx`:

```tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const real = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...real, useNavigate: () => navigate }
})

import { ChildSessionRows } from '../ChildSessionRows'
import type { Session } from '@/api/types'

const child = { id: 'c1', name: 'API redesign', status: 'waiting_question', accountName: 'a', engine: 'claude_code' } as unknown as Session

describe('ChildSessionRows', () => {
  it('renders one row per child with its status and navigates to the child', () => {
    render(<MemoryRouter><ChildSessionRows children={[child]} /></MemoryRouter>)
    const row = screen.getByRole('button', { name: /Open child session API redesign/ })
    expect(row).toHaveTextContent('API redesign')
    fireEvent.click(row)
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining('/sessions/c1'))
  })
})
```

`web/src/components/SessionCard.children.test.tsx` — copy the render harness from `SessionCard.summary.test.tsx` (its mocks and a minimal `session` fixture), then:

```tsx
it('shows a "child of" pill linking to the parent and lists children when expanded', () => {
  // render with session.parentSessionId = 'p1', parentName = 'Parent', childSessions = [child]
  expect(screen.getByText(/child of Parent/)).toBeInTheDocument()
  expect(screen.getByText(/1 child/)).toBeInTheDocument()
  // expand (the same chevron/toggle the agents rows use), then:
  expect(screen.getByRole('button', { name: /Open child session/ })).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `(cd web && npx vitest run src/components/__tests__/ChildSessionRows.test.tsx src/components/SessionCard.children.test.tsx)`
Expected: FAIL — module not found / text not found.

- [ ] **Step 3: Implement**

`types.ts` `Session`, after `originRef`:

```ts
  /** The session whose dispatch_session call created this one; null/absent for a root. */
  parentSessionId?: string | null
```

`ChildSessionRows.tsx` (mirror `SubAgentRows.tsx`'s markup and classes; status dot from the session's triage tier is overkill — use a small map):

```tsx
import { useLocation, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { listScopeForPath, sessionPath } from '@/session/sessionNavState'
import type { Session } from '@/api/types'

const DOT: Record<string, string> = {
  in_progress: 'bg-blue-500 animate-pulse', busy: 'bg-blue-500 animate-pulse',
  waiting_question: 'bg-amber-500', waiting_input: 'bg-amber-500', ready_review: 'bg-amber-500',
  complete: 'bg-emerald-500', done: 'bg-emerald-500', needs_attention: 'bg-red-500',
  ended: 'bg-gray-400', archived: 'bg-gray-400', queued: 'bg-gray-400', idle: 'bg-gray-400',
}

/** Rows for a parent card's child sessions (child-session spec §4.6): each is a real
 *  session, so a row navigates to it rather than to a sub-agent drill-down. */
export function ChildSessionRows({ children }: { children: Session[] }) {
  const navigate = useNavigate()
  const scope = listScopeForPath(useLocation().pathname)
  return (
    <ul className="flex flex-col py-1" aria-label="Child sessions">
      {children.map((c) => (
        <li key={c.id}>
          <button type="button" onClick={() => navigate(sessionPath(c.id, scope))}
            aria-label={`Open child session ${c.name}`}
            className={cn('flex w-full items-center gap-2 py-1.5 pl-3 pr-2 text-left text-xs min-h-[36px] rounded',
              'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/40',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500')}>
            <span aria-hidden className={cn('h-2 w-2 flex-shrink-0 rounded-full', DOT[c.status] ?? 'bg-gray-400')} />
            <span className="font-medium text-gray-800 dark:text-gray-200 truncate">{c.name}</span>
            <span className="ml-auto text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{c.status}</span>
          </button>
        </li>
      ))}
    </ul>
  )
}
```

`SessionCard.tsx`: add props `childSessions?: Session[]` and `parentName?: string | null`; `const hasChildren = (childSessions?.length ?? 0) > 0`; the expand toggle that exists for sub-agents must also enable when `hasChildren` (read how `expanded`/the chevron is gated on `hasSubAgents` and OR in `hasChildren`); in the trailing slot next to the agents count render `{hasChildren && !expanded && <span …>{n} {n === 1 ? 'child' : 'children'}</span>}`; beside the provenance chips render, when `session.parentSessionId`:

```tsx
<button type="button" onClick={(e) => { e.stopPropagation(); onNavigate?.(session.parentSessionId!) }}
  className="flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-sky-100 dark:bg-sky-900/40 text-sky-700 dark:text-sky-300 hover:underline"
  title="Open the session that dispatched this one">
  child of {parentName ?? session.parentSessionId.slice(0, 8)}
</button>
```

(Use whichever navigation callback the card already has for opening a session by id — `onNavigate`/`onOpen`; read their signatures.) After `SubAgentRows`: `{hasChildren && expanded && <ChildSessionRows children={childSessions!} />}`.

`SessionList.tsx` `renderCard`: before the JSX,

```tsx
const childSessions = sessions.filter((s) => s.parentSessionId === session.id)
const parentName = session.parentSessionId ? (sessions.find((s) => s.id === session.parentSessionId)?.name ?? null) : null
```

and pass `childSessions={childSessions} parentName={parentName}`. (`sessions` is the list prop at line 18. If `SessionList` receives an already-filtered list, children outside the current filter simply do not appear as rows — acceptable; the count uses the same array.)

- [ ] **Step 4: Run to verify they pass**

Run: `(cd web && npx vitest run src/components/ && npm run typecheck && npm run lint)`
Expected: PASS, clean. Then the FULL `(cd web && npm run test 2>&1 | tail -8)` — fix any `SessionCard` test whose fixture now needs the new optional props (they are optional, so none should break).

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/components/ChildSessionRows.tsx web/src/components/SessionCard.tsx web/src/components/SessionList.tsx web/src/components/__tests__/ChildSessionRows.test.tsx web/src/components/SessionCard.children.test.tsx
printf '%s\n' 'feat(web): session lineage — child rows on the parent card and a "child of" pill' '' 'Refs spec §4.6.' '' 'Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>' > /tmp/msg.txt && git commit -q -F /tmp/msg.txt
```

---

### Task 6: Docs, gates, PR

**Files:**
- Create: `docs/product/features/child-sessions.md`
- Modify: `docs/product/features/session-tools.md` (tool list: add `dispatch_session` with its class and gate; kill switch section: mention `CCSWITCH_CHILD_SESSIONS_ENABLED`)
- Modify: `.agents/instructions/core.md` § "Session tool surface": change "drops all six tools" to name the seventh and its own switch — ONE sentence (the file is at the AGENTS.md render cap: run `agentctl render` and `agentctl check` afterwards and commit the regenerated `AGENTS.md`; see memory `core-md-at-agents-render-cap`).

- [ ] **Step 1: Docs**

`child-sessions.md` (≤ 60 lines): what a child session is, when the model should use it vs the Agent tool, the notification shape, lineage on the board, the three knobs, the audit event `session.child_dispatch`, non-goals (no UI button yet, no cascade, Claude only), and a pointer to the spec.

- [ ] **Step 2: Gates**

Server: ruff, mypy, `PYTEST tests/`, then the full server suite in the background with the test-slot acquire in the same shell (CLAUDE.md snippet), tee'd to `/tmp/child-sessions-gate.log`. Known host-environmental failures on dobby (see memory): `test_model_switch_restart::default_alias`, `test_orchestrator_lazy_resume::existing_driver`, `test_browser_open::no_x11`, `test_session_id_persistence::persists_engine_session_id`, plus the `-n auto` reauth flake — anything else is real. Web: typecheck, lint, full test, build.

- [ ] **Step 3: PR**

Check in-flight PRs touching `tools/registry.py|tools/tools.py|orchestrator.py|SessionCard.tsx` (GraphQL query from CLAUDE.md). Push `feat/child-session-dispatch`, `gh pr create --base master --body-file /tmp/pr-body.md` (body: the summary above, the knobs, "Refs #1553", the eval case, verification; end with the Claude Code trailer), then `gh pr merge <#> --squash --auto`; confirm non-draft and auto-merge armed.

---

## Self-review

**Spec coverage.** §4.1 → Task 2. §4.2 (tool, mount, blurb, eval) → Task 4. §4.3 (limits, narrowest, create, stamp, audit, return) → Tasks 1+3. §4.4 (notify statuses, guard, notice, resume-then-send, skip archived) → Tasks 1+3. §4.5 (`session_context` children/parent) → Task 4. §4.6 (types, rows, pill, list derivation) → Task 5. §4.7 (knobs, docs) → Tasks 1+6. §5 error table: limit → tool result (T4 `except ChildSessionLimit`), create failures → `guarded` generic tool error (acceptable: message is the exception class; the spec's `session.child_dispatch_failed` audit is NOT implemented — Ruling for the executor: skip it, the `session.tool_call` audit already records `ok=false` for the call), archived parent skip + guard written (T3), send failure logged (T3), queued child status returned (T3). §6 security: identity from ctx (T4 partial binds it), mode narrowing (T3), MUTATION card (T4). §7 tests: each task; eval case (T4). §8 rollout: knob default on.

**Placeholders.** None. The 0037 revision id and one table name are read from the file, with the exact grep given.

**Type consistency.** `dispatch_child_session(parent_id, *, name, prompt, model, permission_mode, identity)` (T3) ↔ `functools.partial(self.dispatch_child_session, session_id, identity=identity)` (T4) ↔ handler kwargs `name, prompt, model, permission_mode` (T4 test asserts exactly these). `mounted_names(..., child_sessions=False)` (T4) ↔ `session_instructions(..., child_sessions=False)` ↔ factory passes `cfg.child_sessions_enabled`. `LIVE_CHILD_EXCLUDED`/`CHILD_NOTIFY_STATUSES` names match between T1 and T3. Wire `parentSessionId` (T2) ↔ `Session.parentSessionId` (T5).
