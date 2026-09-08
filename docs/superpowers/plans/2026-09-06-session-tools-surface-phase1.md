# Session tool surface — Phase 1 (surface + `report_feedback` capture) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every coding session mounts an in-process `ccswitch` MCP server with `session_context` and `report_feedback`; a report becomes a durable `SessionFeedback` row + audit event + metric. Nothing reaches GitHub yet (Phase 2).

**Architecture:** A neutral `server/mcp_spec.py` owns the tool registry primitives both MCP surfaces (assistant drawer, coding session) share. The session surface (`server/sessions/tools/`) closes over a `SessionToolContext` inside the daemon that owns the session and writes to the shared DB directly — no loopback HTTP, no token, no host pin. The orchestrator's assistant-only overlay registry generalizes to any kind and is armed at all three driver-build sites and released by the shared teardown. Codex declares the same tools as `dynamicTools` on fresh `thread/start`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, `claude-agent-sdk` (`create_sdk_mcp_server`, `tool`), `mcp.types.ToolAnnotations`, prometheus_client, alembic (record only), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-06-session-feedback-to-backlog-design.md` (rev 2, §2–§5, §7, §9–§10).

## Global Constraints

- Run every test command through the prebuilt env: `/home/tony/GitHub/ccswitch/.venv/bin/python -m pytest …` with `PYTHONPATH=$PWD/src`, after stripping env: `for v in $(env | grep -o '^CCSWITCH_[A-Z_]*'); do unset $v; done`.
- Lint gate before every commit: `ruff check src/ server/` and `mypy server/ --ignore-missing-imports` must be clean.
- The coding-session overlay is exactly `{"mcp_servers": {"ccswitch": <server>}}`. Never `strict_mcp_config`, never `system_prompt`.
- The assistant tool table (`server/assistant/tools.py` `TOOL_SPECS`, 88 entries, 83 exposed by default per `docs/product/features/ai-assistant.md:99`) must be byte-for-byte unchanged in names and classes after the registry move.
- Session tool handlers contain no business logic: `report_feedback` calls `record_feedback()` once; `session_context` reads the record once.
- New DB columns are nullable. Timestamps use `server/db/clock.py:db_utc_now()` for DB-side defaults or naive UTC `datetime.utcnow()`-equivalent (`datetime.now(timezone.utc).replace(tzinfo=None)`) in Python.
- Commit messages go in a file (`git commit -F /tmp/msg.txt`); the Bash hook denies command text pairing deploy/release with prod/production. End every message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Branch: `feat/session-tools-surface`, created from `origin/master` (`git fetch origin master && git checkout -b feat/session-tools-surface origin/master`). Push after every commit.
- Kill switch env names: `CCSWITCH_SESSION_TOOLS_ENABLED` (default `true`), `CCSWITCH_SESSION_FEEDBACK_CAP` (default `3`).

---

## File structure

| Path | Responsibility |
|---|---|
| `server/mcp_spec.py` (new) | `ToolClass`, `ToolSpec`, `MCP_PREFIX`, `SERVER_NAME`, `ToolArgError`, `tool_err`, `annotations_for`, surface registry + `annotation_for`. The one home for MCP registry primitives. |
| `server/assistant/tools.py` (modify) | Imports the primitives from `server.mcp_spec`; registers its `TOOL_SPECS` as a surface. Handlers untouched. |
| `server/sessions/permissions.py`, `server/sessions/events.py`, `server/sessions/claude_driver.py` (modify) | Read `MCP_PREFIX` / `annotation_for` from `server.mcp_spec`. |
| `server/config.py`, `server/api/settings.py` (modify) | Two flags + About rows. |
| `server/db/models.py`, `server/db/migrations/versions/0029_session_feedback.py` (new) | `SessionFeedback` table. |
| `server/sessions/feedback.py` (new) | `fingerprint()`, `record_feedback()`, enums, caps. Pure policy + one DB write. |
| `server/obs/metrics.py` (modify) | `session_tool_calls_total`, `session_feedback_total`. |
| `server/sessions/tools/__init__.py`, `context.py`, `registry.py`, `tools.py`, `server.py`, `instruction.py`, `codex.py` (new) | The session surface. |
| `server/sessions/orchestrator.py` (modify) | `_overlays` registry, `_setup_session_overlay`, `_setup_overlay_for_kind`, teardown release, Codex overlay getter widened. |
| `server/sessions/claude_sdk_factory.py` (modify) | Append `SESSION_TOOLS_INSTRUCTION` when enabled. |
| `server/codex/driver.py` (modify) | `session_tools` kwarg; declare on fresh start; `_on_dynamic_tool` consults either overlay. |
| `server/tests/test_mcp_spec.py`, `test_session_feedback.py`, `test_session_tools.py`, `test_session_overlay.py`, `test_permissions_session_tools.py`, `test_codex_session_tools.py`, `test_session_tools_real_binary.py` (new) | Tests per task. |
| `docs/superpowers/evals/session-tools/cases.jsonl` (new) | Eval cases for the two tools. |
| `docs/product/features/session-tools.md` (new) | Feature doc (Phase 1 scope; Phase 2 extends). |

---

### Task 1: Neutral MCP registry module

**Files:**
- Create: `server/mcp_spec.py`
- Modify: `server/assistant/tools.py:24-36` (definitions → imports), `server/assistant/tools.py:219-222` (`annotation_for` → re-export), `server/sessions/permissions.py:139-144`, `server/sessions/events.py:34`, `server/sessions/claude_driver.py:695`
- Test: `server/tests/test_mcp_spec.py`

**Interfaces:**
- Produces: `server.mcp_spec.ToolClass`, `ToolSpec(name, tool_class, title)`, `MCP_PREFIX = "mcp__ccswitch__"`, `SERVER_NAME = "ccswitch"`, `ToolArgError`, `tool_err(msg) -> dict`, `annotations_for(spec) -> ToolAnnotations`, `register_surface(name: str, specs: Mapping[str, ToolSpec]) -> None`, `annotation_for(mcp_name: str) -> ToolSpec | None`, `surfaces() -> dict[str, Mapping[str, ToolSpec]]`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_mcp_spec.py
"""server/mcp_spec.py is THE home of the MCP registry primitives both surfaces share."""
from __future__ import annotations

import pytest


def test_assistant_surface_is_registered_and_unchanged():
    from server import mcp_spec
    from server.assistant.tools import TOOL_SPECS
    assert mcp_spec.surfaces()["assistant"] is TOOL_SPECS
    assert len(TOOL_SPECS) == 88
    assert mcp_spec.annotation_for("mcp__ccswitch__list_sessions").tool_class is mcp_spec.ToolClass.READ
    assert mcp_spec.annotation_for("mcp__ccswitch__delete_session").tool_class is mcp_spec.ToolClass.DESTRUCTIVE


def test_annotation_for_consults_every_surface():
    from server import mcp_spec
    specs = {"probe_tool": mcp_spec.ToolSpec("probe_tool", mcp_spec.ToolClass.READ, "Probe")}
    mcp_spec.register_surface("probe", specs)
    try:
        assert mcp_spec.annotation_for("mcp__ccswitch__probe_tool") is specs["probe_tool"]
        assert mcp_spec.annotation_for("mcp__other__probe_tool") is None
        assert mcp_spec.annotation_for("Bash") is None
    finally:
        mcp_spec._SURFACES.pop("probe", None)


def test_duplicate_tool_name_across_surfaces_is_refused():
    from server import mcp_spec
    with pytest.raises(ValueError):
        mcp_spec.register_surface("dup", {"list_sessions": mcp_spec.ToolSpec(
            "list_sessions", mcp_spec.ToolClass.READ, "x")})


def test_assistant_module_reexports_the_primitives():
    from server import mcp_spec
    from server.assistant import tools
    assert tools.ToolClass is mcp_spec.ToolClass
    assert tools.ToolSpec is mcp_spec.ToolSpec
    assert tools.MCP_PREFIX == mcp_spec.MCP_PREFIX == "mcp__ccswitch__"
    assert tools.annotation_for is mcp_spec.annotation_for
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_mcp_spec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.mcp_spec'`

- [ ] **Step 3: Write `server/mcp_spec.py`**

```python
"""MCP registry primitives shared by every in-process `ccswitch` MCP surface.

Two surfaces mount a server under the wire name ``ccswitch`` — the assistant drawer
(``server/assistant/tools.py``) and the coding session (``server/sessions/tools/``).
They never coexist in one session, so they share the name and the ``mcp__ccswitch__``
prefix; what they must NOT share is a duplicated copy of these primitives. The
permission layer keys its read/mutation/destructive rule on ``annotation_for``, so
every surface registers its spec table here and the lookup consults all of them.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mcp.types import ToolAnnotations

MCP_PREFIX = "mcp__ccswitch__"
SERVER_NAME = "ccswitch"


class ToolClass(str, Enum):
    READ = "read"
    MUTATION = "mutation"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tool_class: ToolClass
    title: str          # human label for permission cards ("Archive session")


class ToolArgError(ValueError):
    """An argument a handler rejects LOCALLY, before any side effect — so the model gets
    a corrective message and nothing downstream ever sees the call."""


def tool_err(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def annotations_for(spec: ToolSpec) -> ToolAnnotations:
    return ToolAnnotations(
        title=spec.title,
        readOnlyHint=spec.tool_class is ToolClass.READ,
        destructiveHint=spec.tool_class is ToolClass.DESTRUCTIVE,
        idempotentHint=spec.tool_class is ToolClass.READ,
        openWorldHint=False,
    )


_SURFACES: dict[str, Mapping[str, ToolSpec]] = {}


def register_surface(name: str, specs: Mapping[str, ToolSpec]) -> None:
    """Register one surface's spec table. Tool names are unique ACROSS surfaces: the
    permission layer sees only ``mcp__ccswitch__<tool>``, so two surfaces defining the
    same name would be gated by whichever registered first."""
    for other, table in _SURFACES.items():
        if other == name:
            continue
        clash = set(table) & set(specs)
        if clash:
            raise ValueError(f"tool name(s) {sorted(clash)} already registered by surface {other!r}")
    _SURFACES[name] = specs


def surfaces() -> dict[str, Mapping[str, ToolSpec]]:
    return dict(_SURFACES)


def annotation_for(mcp_name: str) -> ToolSpec | None:
    if not mcp_name.startswith(MCP_PREFIX):
        return None
    short = mcp_name[len(MCP_PREFIX):]
    for table in _SURFACES.values():
        spec = table.get(short)
        if spec is not None:
            return spec
    return None
```

- [ ] **Step 4: Point `server/assistant/tools.py` at it**

Replace lines 24–36 (the `MCP_PREFIX`, `ToolClass`, `ToolSpec` definitions) with:

```python
from server.mcp_spec import (MCP_PREFIX, ToolArgError, ToolClass, ToolSpec,  # noqa: F401 - re-exported
                             annotation_for, annotations_for, register_surface, tool_err)
```

Delete the local `class ToolArgError` (line ~154), replace `def _annotations(spec)` (line ~257) body with `return annotations_for(spec)` (keep the name `_annotations` so call sites are untouched), replace `def _err(msg)` body with `return tool_err(msg)`, and delete `def annotation_for` (line ~219). Immediately after the `TOOL_SPECS = {...}` literal add:

```python
register_surface("assistant", TOOL_SPECS)
```

- [ ] **Step 5: Retarget the three prefix copies**

`server/sessions/permissions.py:139-144`: replace the local import and calls:

```python
        if tool.startswith(MCP_PREFIX):
            from server.mcp_spec import ToolClass, annotation_for
            ann = annotation_for(tool)
            if ann is not None and ann.tool_class == ToolClass.READ:
                return None, _resolved(True), True
            if ann is not None and ann.tool_class == ToolClass.DESTRUCTIVE:
```

and add `from server.mcp_spec import MCP_PREFIX` at the module top (no cycle: `server.mcp_spec` imports only `mcp.types`).

`server/sessions/events.py:34`: `from server.mcp_spec import MCP_PREFIX as _APP_ACTION_PREFIX` replacing the literal.

`server/sessions/claude_driver.py:695`: replace `"mcp__ccswitch__"` with `MCP_PREFIX` (import from `server.mcp_spec`).

- [ ] **Step 6: Run the new test and the assistant suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_mcp_spec.py server/tests/test_assistant_conformance.py server/tests/test_assistant_tools.py server/tests/test_assistant_codex_overlay.py server/tests/test_permissions.py -q`
Expected: all PASS. Then `grep -rn '"mcp__ccswitch__"' server/ --include=*.py | grep -v tests` must print nothing.

- [ ] **Step 7: Lint, commit, push**

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/mcp_spec.py server/assistant/tools.py server/sessions/permissions.py server/sessions/events.py server/sessions/claude_driver.py server/tests/test_mcp_spec.py
printf 'refactor(mcp): neutral registry module shared by every ccswitch MCP surface\n\nMoves ToolClass/ToolSpec/MCP_PREFIX/annotation_for out of server/assistant/tools.py into\nserver/mcp_spec.py so a second in-process surface (coding sessions, next tasks) shares one\nregistry and the permission layer consults every surface. Collapses the prefix literal copies.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push -u origin feat/session-tools-surface
```

---

### Task 2: Settings flags

**Files:**
- Modify: `server/config.py` (pydantic `Settings` after line 238; dataclass `Settings` after line 515), `server/api/settings.py:46-47`
- Test: `server/tests/test_session_feedback.py` (settings portion)

**Interfaces:**
- Produces: `get_settings().session_tools_enabled: bool` (default True), `get_settings().session_feedback_cap: int` (default 3).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_session_feedback.py
from __future__ import annotations

import pytest


def test_session_tools_flags_default(monkeypatch):
    from server.config import Settings
    monkeypatch.delenv("CCSWITCH_SESSION_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSION_FEEDBACK_CAP", raising=False)
    s = Settings()
    assert s.session_tools_enabled is True
    assert s.session_feedback_cap == 3


def test_session_tools_flags_from_env(monkeypatch):
    from server.config import Settings
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    monkeypatch.setenv("CCSWITCH_SESSION_FEEDBACK_CAP", "7")
    s = Settings()
    assert s.session_tools_enabled is False
    assert s.session_feedback_cap == 7


def test_flags_are_listed_on_about():
    from server.api.settings import _MANAGED
    keys = {row[0] for row in _MANAGED}
    assert {"session_tools_enabled", "session_feedback_cap"} <= keys
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_feedback.py -v`
Expected: FAIL with `AttributeError: ... 'session_tools_enabled'`

- [ ] **Step 3: Add the fields**

Pydantic branch (after `self_model_switch_cap`, line ~238):

```python
        # Per-session ccswitch MCP surface for coding sessions (session tools spec §3).
        # Off = no in-process server is mounted and no instruction is appended.
        session_tools_enabled: bool = Field(
            default=True, alias="CCSWITCH_SESSION_TOOLS_ENABLED")
        # How many report_feedback rows one session may file (spec §4). Further reports
        # are recorded as "capped": visible, never filed.
        session_feedback_cap: int = Field(
            default=3, alias="CCSWITCH_SESSION_FEEDBACK_CAP")
```

Dataclass branch (after `self_model_switch_cap`, line ~515):

```python
        session_tools_enabled: bool = dataclasses.field(
            default_factory=lambda: os.environ.get("CCSWITCH_SESSION_TOOLS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
        )
        session_feedback_cap: int = dataclasses.field(
            default_factory=lambda: int(v) if (v := os.environ.get("CCSWITCH_SESSION_FEEDBACK_CAP", "")) and v.isdigit() else 3
        )
```

`server/api/settings.py` `_MANAGED`, after the `self_model_switch_cap` row:

```python
    ("session_tools_enabled", "CCSWITCH_SESSION_TOOLS_ENABLED", "Coding sessions get the ccswitch MCP tools (session_context, report_feedback)"),
    ("session_feedback_cap", "CCSWITCH_SESSION_FEEDBACK_CAP", "Feedback reports one session may file before further ones are capped"),
```

- [ ] **Step 4: Run the test, lint, commit, push**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_feedback.py server/tests/test_settings_api.py -q` (the second file exists if `ls server/tests/test_settings*.py` shows it; otherwise run only the first). Expected: PASS.

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/config.py server/api/settings.py server/tests/test_session_feedback.py
printf 'feat(config): CCSWITCH_SESSION_TOOLS_ENABLED and CCSWITCH_SESSION_FEEDBACK_CAP\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 3: `SessionFeedback` model + migration

**Files:**
- Modify: `server/db/models.py` (append after `CiFinding`, line ~777), `server/tests/test_postgres_compat.py:203-260` (`_REQUIRED_EXTRAS` — no entry needed if every NOT NULL column is defaulted; verify), `server/tests/test_schema_reconcile_coverage.py` (no exemption needed — all nullable)
- Create: `server/db/migrations/versions/0029_session_feedback.py`
- Test: `server/tests/test_session_feedback.py` (model portion)

**Interfaces:**
- Produces: `server.db.models.SessionFeedback` with columns `id, session_id, fingerprint, category, impact, title, body, evidence, context, status, issue_number, reported_at, filed_at`. `FEEDBACK_STATUSES = ("pending", "filed", "merged", "capped", "throttled", "dismissed")` lives in `server/sessions/feedback.py` (Task 4), not on the model.

- [ ] **Step 1: Check the migration head first**

Run: `ls server/db/migrations/versions/ | sort | tail -2` and `gh api graphql -f query='{repository(owner:"steiner385",name:"ccswitch"){pullRequests(states:OPEN,first:40){nodes{number files(first:60){nodes{path}}}}}}' --jq '.data.repository.pullRequests.nodes[] | select([.files.nodes[].path] | any(test("migrations/versions/0029"))) | .number'`. If an open PR already claims `0029`, use `0030` and chain off the newest MERGED head; note it in the commit.

- [ ] **Step 2: Write the failing test (append to `server/tests/test_session_feedback.py`)**

```python
@pytest.mark.asyncio
async def test_session_feedback_table_round_trips(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import SessionFeedback
    async with db_session() as db:
        db.add(SessionFeedback(session_id="s1", fingerprint="abc", category="dispatch",
                               impact="degraded", title="t", body="b", status="pending",
                               context={"repo": "o/n"}))
        await db.commit()
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.id and row.reported_at is not None and row.issue_number is None
        assert row.context == {"repo": "o/n"}


def test_migration_0029_chains_off_the_current_head():
    import importlib
    mod = importlib.import_module("server.db.migrations.versions.0029_session_feedback")
    assert mod.revision == "0029_session_feedback"
    assert mod.down_revision == "0028_assistant_tokens"
```

(`isolated_db` is the existing conftest fixture used by `test_assistant_reap.py`.)

- [ ] **Step 3: Run to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_feedback.py -v -k "table or migration"`
Expected: FAIL with `ImportError: cannot import name 'SessionFeedback'`

- [ ] **Step 4: Add the model (after `CiFinding`)**

```python
class SessionFeedback(Base):
    """A report a coding session made about ccswitch itself via the `report_feedback`
    session tool (session tools spec §4–§5). Fingerprint-deduped; Phase 2's filer turns
    `pending` rows into `session-feedback` GitHub issues and links `issue_number`.

    Every column is nullable so `_reconcile_columns` adds the table's columns on both
    dialects without a hand migration; `reported_at` is naive UTC like every other
    timestamp column (DB clock rule, server/db/clock.py)."""

    __tablename__ = "session_feedback"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    impact: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Snapshot of the reporting session: repo, branch, origin, origin_ref, engine,
    # model, host, account (display name only), session_url.
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # pending | filed | merged | capped | throttled | dismissed (server/sessions/feedback.py)
    status: Mapped[str | None] = mapped_column(String, nullable=True, default="pending")
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    filed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
```

Confirm `Text` is in the `from sqlalchemy import (...)` list at line 11; add it if not.

- [ ] **Step 5: Write the migration**

```python
# server/db/migrations/versions/0029_session_feedback.py
"""Session tools Phase 1: session_feedback table for `report_feedback` rows.

Revision ID: 0029_session_feedback
Revises: 0028_assistant_tokens
Create Date: 2026-09-06

Reviewable record only: nothing runs `alembic upgrade` at deploy time — init_db's
create_all builds the new table on both SQLite and Postgres at next boot.
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_session_feedback"
down_revision = "0028_assistant_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_feedback",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=True, index=True),
        sa.Column("fingerprint", sa.String(), nullable=True, index=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("impact", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("reported_at", sa.DateTime(), nullable=True),
        sa.Column("filed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("session_feedback")
```

- [ ] **Step 6: Run the model tests plus the schema gates**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_feedback.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py server/tests/test_migrations.py -q` (skip any of the last three that does not exist; `test_postgres_compat.py` skips without `CCSWITCH_TEST_PG_URL` — that is expected). Expected: PASS / skipped, no failures.

- [ ] **Step 7: Lint, commit, push**

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/db/models.py server/db/migrations/versions/0029_session_feedback.py server/tests/test_session_feedback.py
printf 'feat(db): session_feedback table for session-tool feedback reports\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 4: `record_feedback` service + metric

**Files:**
- Create: `server/sessions/feedback.py`
- Modify: `server/obs/metrics.py` (after `assistant_gap_reports_total`, line ~221)
- Test: `server/tests/test_session_feedback.py` (service portion)

**Interfaces:**
- Consumes: `SessionFeedback` (Task 3), `get_settings().session_feedback_cap` (Task 2).
- Produces:
  - `CATEGORIES = ("prompt", "dispatch", "orchestration", "environment", "tooling", "other")`, `IMPACTS = ("blocked", "degraded", "minor")`, `FEEDBACK_STATUSES`.
  - `fingerprint(category: str, title: str) -> str` (16 hex chars).
  - `@dataclass(frozen=True) FeedbackItem(category, impact, title, body, evidence: str | None)`.
  - `validate_item(args: Mapping[str, Any]) -> FeedbackItem` — raises `ToolArgError`.
  - `@dataclass(frozen=True) FeedbackResult(recorded: str | None, status: str)`.
  - `async record_feedback(*, session_id: str, identity: str, item: FeedbackItem, context: dict, cap: int) -> FeedbackResult`.
  - `server.obs.metrics.session_feedback_total` Counter labelled `("category",)`.

- [ ] **Step 1: Write the failing tests (append)**

```python
def test_fingerprint_normalizes_title():
    from server.sessions.feedback import fingerprint
    a = fingerprint("dispatch", "Dispatch prompt names a branch that does not exist")
    b = fingerprint("dispatch", "  dispatch PROMPT names a branch — that does not exist!! ")
    assert a == b and len(a) == 16
    assert fingerprint("prompt", "Dispatch prompt names a branch that does not exist") != a


def test_validate_item_rejects_bad_args():
    from server.mcp_spec import ToolArgError
    from server.sessions.feedback import validate_item
    good = {"category": "dispatch", "impact": "degraded", "title": "Branch missing",
            "body": "The prompt named feat/x; it does not exist."}
    item = validate_item(good)
    assert item.category == "dispatch" and item.evidence is None
    for bad in ({**good, "category": "bug"}, {**good, "impact": "huge"},
                {**good, "title": "short"}, {**good, "body": ""}):
        with pytest.raises(ToolArgError):
            validate_item(bad)
    long = validate_item({**good, "body": "x" * 5000, "evidence": "e" * 2000})
    assert len(long.body) == 4000 and len(long.evidence) == 1000


@pytest.mark.asyncio
async def test_record_feedback_writes_row_audit_metric(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, SessionFeedback
    from server.obs.metrics import session_feedback_total
    from server.sessions.feedback import record_feedback, validate_item
    before = session_feedback_total.labels("dispatch")._value.get()
    item = validate_item({"category": "dispatch", "impact": "degraded",
                          "title": "Branch missing", "body": "The prompt named feat/x."})
    res = await record_feedback(session_id="s1", identity="u@x", item=item,
                                context={"repo": "o/n"}, cap=3)
    assert res.status == "pending" and res.recorded
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.session_id == "s1" and row.status == "pending" and row.context == {"repo": "o/n"}
        audit = (await db.execute(select(AuditEvent).where(AuditEvent.type == "session.feedback"))).scalar_one()
        assert audit.identity == "u@x" and audit.via == "session"
        assert audit.detail["fingerprint"] == row.fingerprint and audit.detail["status"] == "pending"
    assert session_feedback_total.labels("dispatch")._value.get() == before + 1


@pytest.mark.asyncio
async def test_record_feedback_duplicate_and_cap(isolated_db):
    from server.sessions.feedback import record_feedback, validate_item
    mk = lambda t: validate_item({"category": "prompt", "impact": "minor", "title": t, "body": "b" * 10})
    assert (await record_feedback(session_id="s1", identity="u", item=mk("Title one"), context={}, cap=2)).status == "pending"
    assert (await record_feedback(session_id="s1", identity="u", item=mk("title ONE"), context={}, cap=2)).status == "duplicate"
    assert (await record_feedback(session_id="s1", identity="u", item=mk("Title two"), context={}, cap=2)).status == "pending"
    capped = await record_feedback(session_id="s1", identity="u", item=mk("Title three"), context={}, cap=2)
    assert capped.status == "capped" and capped.recorded
    # another session is not affected by s1's cap
    assert (await record_feedback(session_id="s2", identity="u", item=mk("Title three"), context={}, cap=2)).status == "pending"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_feedback.py -v -k "fingerprint or validate or record"`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.feedback'`

- [ ] **Step 3: Add the metric**

`server/obs/metrics.py`, after `assistant_gap_reports_total`:

```python
# Session tool surface (session tools spec §3, §4). Same shape as the assistant's
# counters so one Grafana row can compare the two surfaces.
session_tool_calls_total = Counter(
    "session_tool_calls_total", "Coding-session ccswitch tool calls",
    labelnames=("tool", "class", "outcome"))
session_feedback_total = Counter(
    "session_feedback_total", "Feedback reports coding sessions made about ccswitch",
    labelnames=("category",))
```

- [ ] **Step 4: Write `server/sessions/feedback.py`**

```python
"""`report_feedback` policy: validate, fingerprint, cap, and persist one report.

Pure policy plus ONE DB write. Never talks to GitHub — filing is the Phase 2
automation's job, on the primary daemon, so a worker host that runs the session only
needs the shared DB (session tools spec §4–§6).
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from server.mcp_spec import ToolArgError
from server.obs.metrics import session_feedback_total

log = logging.getLogger(__name__)

CATEGORIES: tuple[str, ...] = ("prompt", "dispatch", "orchestration", "environment", "tooling", "other")
IMPACTS: tuple[str, ...] = ("blocked", "degraded", "minor")
FEEDBACK_STATUSES: tuple[str, ...] = ("pending", "filed", "merged", "capped", "throttled", "dismissed")

_TITLE_MIN, _TITLE_MAX, _BODY_MAX, _EVIDENCE_MAX = 8, 120, 4000, 1000
_NORM = re.compile(r"[^a-z0-9]+")


def fingerprint(category: str, title: str) -> str:
    """Dedupe key: sha1 of category + normalized title, first 16 hex chars."""
    norm = _NORM.sub(" ", title.lower()).strip()
    return hashlib.sha1(f"{category} {norm}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class FeedbackItem:
    category: str
    impact: str
    title: str
    body: str
    evidence: str | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.category, self.title)


@dataclass(frozen=True)
class FeedbackResult:
    recorded: str | None   # row id, None only for "duplicate"
    status: str            # pending | capped | duplicate


def _text(args: Mapping[str, Any], key: str, *, required: bool, cap: int) -> str | None:
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ToolArgError(f"{key} is required")
        return None
    if not isinstance(raw, str):
        raise ToolArgError(f"{key} must be a string")
    return raw.strip()[:cap]


def validate_item(args: Mapping[str, Any]) -> FeedbackItem:
    category = str(args.get("category") or "").strip().lower()
    if category not in CATEGORIES:
        raise ToolArgError(f"category must be one of {', '.join(CATEGORIES)}")
    impact = str(args.get("impact") or "").strip().lower()
    if impact not in IMPACTS:
        raise ToolArgError(f"impact must be one of {', '.join(IMPACTS)}")
    title = _text(args, "title", required=True, cap=_TITLE_MAX) or ""
    if len(title) < _TITLE_MIN:
        raise ToolArgError(f"title must be at least {_TITLE_MIN} characters")
    body = _text(args, "body", required=True, cap=_BODY_MAX) or ""
    evidence = _text(args, "evidence", required=False, cap=_EVIDENCE_MAX)
    return FeedbackItem(category=category, impact=impact, title=title, body=body, evidence=evidence)


async def record_feedback(*, session_id: str, identity: str, item: FeedbackItem,
                          context: dict[str, Any], cap: int) -> FeedbackResult:
    """Persist one report. Idempotent per (session, fingerprint); durable cap by row count."""
    from server.audit import record_audit
    from server.db import db_session
    from server.db.models import SessionFeedback

    fp = item.fingerprint
    async with db_session() as db:
        dup = (await db.execute(select(SessionFeedback.id).where(
            SessionFeedback.session_id == session_id, SessionFeedback.fingerprint == fp))).first()
        if dup is not None:
            return FeedbackResult(recorded=None, status="duplicate")
        count = (await db.execute(select(func.count()).select_from(SessionFeedback).where(
            SessionFeedback.session_id == session_id,
            SessionFeedback.status != "capped"))).scalar_one()
        status = "pending" if count < cap else "capped"
        row = SessionFeedback(session_id=session_id, fingerprint=fp, category=item.category,
                              impact=item.impact, title=item.title, body=item.body,
                              evidence=item.evidence, context=dict(context), status=status)
        db.add(row)
        await db.flush()
        row_id = row.id
        try:
            await record_audit(db, "session.feedback", identity, {
                "session_id": session_id, "category": item.category, "impact": item.impact,
                "title": item.title, "fingerprint": fp, "status": status}, via="session")
        except Exception:  # noqa: BLE001 - an audit failure must never lose the report
            log.warning("session.feedback audit failed session=%s", session_id, exc_info=True)
        await db.commit()
    session_feedback_total.labels(item.category).inc()
    return FeedbackResult(recorded=row_id, status=status)
```

- [ ] **Step 5: Run tests, lint, commit, push**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_feedback.py -q` — Expected: PASS.

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/sessions/feedback.py server/obs/metrics.py server/tests/test_session_feedback.py
printf 'feat(sessions): record_feedback service with fingerprint dedupe and durable cap\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 5: The session tool surface package

**Files:**
- Create: `server/sessions/tools/__init__.py`, `context.py`, `registry.py`, `tools.py`, `server.py`, `instruction.py`
- Create: `docs/superpowers/evals/session-tools/cases.jsonl`
- Test: `server/tests/test_session_tools.py`

**Interfaces:**
- Consumes: `server.mcp_spec.*` (Task 1), `record_feedback`/`validate_item` (Task 4), `get_settings().session_feedback_cap` (Task 2).
- Produces:
  - `SessionToolContext(session_id: str, identity: str, kind: str | None, engine: str, record: Callable[[], Any], host: str | None)` (frozen dataclass; `record()` returns the live `SessionRecord` or `None`).
  - `SESSION_TOOL_SPECS: dict[str, ToolSpec]` with `session_context` (READ) and `report_feedback` (READ), registered as surface `"session"`.
  - `build_session_tools(ctx) -> list[SdkMcpTool]`.
  - `build_session_server(ctx) -> McpSdkServerConfig`.
  - `SESSION_TOOLS_INSTRUCTION: str`.
  - `context_snapshot(ctx) -> dict` (the `context` JSON written on a feedback row).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_session_tools.py
"""The coding-session `ccswitch` MCP surface (session tools spec §3)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccswitch.engines import AgentEngine

CASES = Path("docs/superpowers/evals/session-tools/cases.jsonl")


def _ctx(record=None, session_id="s1"):
    from server.sessions.tools.context import SessionToolContext
    rec = record or SimpleNamespace(
        id=session_id, name="Fix #12", repo="o/n", branch="ccswitch/x", cwd="/tmp/w",
        origin="backlog-runner", origin_ref="o/n#12", engine=AgentEngine.CLAUDE_CODE,
        model="sonnet", permission_mode="acceptEdits", account="alpha", host="dobby",
        active_goal=None, inferred_goal="Fix the flaky test", kind=None)
    return SessionToolContext(session_id=session_id, identity="u@x", kind=None,
                              engine="claude_code", record=lambda: rec, host="dobby")


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_registry_is_a_registered_surface_with_two_read_tools():
    from server import mcp_spec
    from server.sessions.tools.registry import SESSION_TOOL_SPECS
    assert set(SESSION_TOOL_SPECS) == {"session_context", "report_feedback"}
    assert all(s.tool_class is mcp_spec.ToolClass.READ for s in SESSION_TOOL_SPECS.values())
    assert mcp_spec.surfaces()["session"] is SESSION_TOOL_SPECS
    assert mcp_spec.annotation_for("mcp__ccswitch__report_feedback").tool_class is mcp_spec.ToolClass.READ


def test_every_session_tool_has_a_spec_and_an_eval_case():
    from server.sessions.tools.registry import SESSION_TOOL_SPECS
    from server.sessions.tools.tools import build_session_tools
    tools = build_session_tools(_ctx())
    assert {t.name for t in tools} == set(SESSION_TOOL_SPECS)
    covered = set()
    for line in CASES.read_text().splitlines():
        if line.strip():
            covered.update(json.loads(line)["expect_tools"])
    assert set(SESSION_TOOL_SPECS) <= covered


@pytest.mark.asyncio
async def test_session_context_returns_the_record_snapshot():
    from server.sessions.tools.tools import build_session_tools
    out = await _tool(build_session_tools(_ctx()), "session_context").handler({})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"]["repo"] == "o/n" and payload["data"]["originRef"] == "o/n#12"
    assert payload["data"]["goal"] == "Fix the flaky test" and payload["data"]["host"] == "dobby"
    assert "is_error" not in out


@pytest.mark.asyncio
async def test_report_feedback_records_and_audits(isolated_db):
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import AuditEvent, SessionFeedback
    from server.sessions.tools.tools import build_session_tools
    tool = _tool(build_session_tools(_ctx()), "report_feedback")
    out = await tool.handler({"category": "dispatch", "impact": "degraded",
                              "title": "Branch missing", "body": "The prompt named feat/x."})
    payload = json.loads(out["content"][0]["text"])
    assert payload["data"]["status"] == "pending"
    async with db_session() as db:
        row = (await db.execute(select(SessionFeedback))).scalar_one()
        assert row.session_id == "s1" and row.context["originRef"] == "o/n#12"
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert {"session.feedback", "session.tool_call"} <= types


@pytest.mark.asyncio
async def test_bad_args_are_a_tool_error_not_an_exception(isolated_db):
    from server.sessions.tools.tools import build_session_tools
    tool = _tool(build_session_tools(_ctx()), "report_feedback")
    out = await tool.handler({"category": "bug", "impact": "minor", "title": "x", "body": "y"})
    assert out.get("is_error") is True and "category" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_handler_exception_never_escapes(isolated_db, monkeypatch):
    from server.sessions.tools import tools as mod
    async def boom(**kw):
        raise RuntimeError("db gone")
    monkeypatch.setattr(mod, "record_feedback", boom)
    tool = _tool(mod.build_session_tools(_ctx()), "report_feedback")
    out = await tool.handler({"category": "prompt", "impact": "minor", "title": "Long enough", "body": "b" * 10})
    assert out.get("is_error") is True


def test_server_is_named_ccswitch():
    from server.sessions.tools.server import build_session_server
    cfg = build_session_server(_ctx())
    assert cfg["type"] == "sdk" and cfg["name"] == "ccswitch"


def test_instruction_names_the_tool_and_the_rules():
    from server.sessions.tools.instruction import SESSION_TOOLS_INSTRUCTION as s
    assert "report_feedback" in s and "session_context" in s
    assert "never a bug in the project" in s and "keep working" in s.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.tools'`

- [ ] **Step 3: Write the package**

`server/sessions/tools/__init__.py`:

```python
"""The coding-session `ccswitch` MCP surface (session tools spec §3).

Tools close over a :class:`SessionToolContext` inside the daemon that owns the session
and act on this session directly — no loopback HTTP, no token — so they work on any
fleet host. Importing the package registers the surface with ``server.mcp_spec``.
"""
from . import registry as _registry  # noqa: F401 - registers the "session" surface
```

`server/sessions/tools/context.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionToolContext:
    """What a session tool may know and touch. Built per driver build by the
    orchestrator; ``record()`` is a live accessor, not a snapshot, so a tool called
    after a rename or model switch sees the current row."""

    session_id: str
    identity: str            # owner identity; audit attribution
    kind: str | None
    engine: str
    record: Callable[[], Any]  # -> SessionRecord | None
    host: str | None = None


def context_snapshot(ctx: SessionToolContext) -> dict[str, Any]:
    """The context block stored on a feedback row and returned by `session_context`.
    Display-safe only: account NAME, never credentials or config paths."""
    rec = ctx.record()
    if rec is None:
        return {"sessionId": ctx.session_id, "host": ctx.host, "engine": ctx.engine}
    engine = getattr(rec, "engine", ctx.engine)
    goal = getattr(rec, "active_goal", None) or getattr(rec, "inferred_goal", None)
    return {
        "sessionId": ctx.session_id,
        "name": getattr(rec, "name", None),
        "repo": getattr(rec, "repo", None),
        "branch": getattr(rec, "branch", None),
        "cwd": getattr(rec, "cwd", None),
        "host": getattr(rec, "host", None) or ctx.host,
        "engine": getattr(engine, "value", engine),
        "model": getattr(rec, "model", None),
        "account": getattr(rec, "account", None),
        "origin": getattr(rec, "origin", None),
        "originRef": getattr(rec, "origin_ref", None),
        "goal": goal,
        "permissionMode": getattr(rec, "permission_mode", None),
    }
```

`server/sessions/tools/registry.py`:

```python
from __future__ import annotations

from server.mcp_spec import ToolClass, ToolSpec, register_surface

# READ for both: neither changes anything a user can see. `report_feedback` writes a
# row + audit + counter, exactly the assistant's `report_gap` precedent, so it raises
# no permission card in any mode (spec §3.2, §12).
SESSION_TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("session_context", ToolClass.READ, "Read this session's context"),
    ToolSpec("report_feedback", ToolClass.READ, "Report a problem with ccswitch"),
]}

register_surface("session", SESSION_TOOL_SPECS)
```

`server/sessions/tools/tools.py`:

```python
"""Session tool handlers. Each does ONE thing: read the record, or call
``record_feedback`` once. No business logic here (spec §12)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from server.mcp_spec import ToolArgError, ToolSpec, annotations_for, tool_err
from server.obs.metrics import session_tool_calls_total
from server.sessions.feedback import CATEGORIES, IMPACTS, record_feedback, validate_item

from .context import SessionToolContext, context_snapshot
from .registry import SESSION_TOOL_SPECS

log = logging.getLogger(__name__)


def tool_ok(data: Any) -> dict[str, Any]:
    env = {"source": "ccswitch", "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "data": data}
    return {"content": [{"type": "text", "text": json.dumps(env, separators=(",", ":"), ensure_ascii=False)}]}


async def _audit_tool_call(ctx: SessionToolContext, spec: ToolSpec, ok: bool, ms: float) -> None:
    """Best-effort `session.tool_call` audit row, attributed to the session owner with
    ``via="session"``. Own db_session: a tool runs inside the SDK loop, not a request."""
    try:
        from server.audit import record_audit
        from server.db import db_session
        async with db_session() as db:
            await record_audit(db, "session.tool_call", ctx.identity,
                               {"tool": spec.name, "class": spec.tool_class.value, "ok": ok,
                                "ms": round(ms), "session_id": ctx.session_id}, via="session")
            await db.commit()
    except Exception:  # noqa: BLE001 - an audit failure must never fail the tool
        log.warning("session.tool_call audit failed tool=%s", spec.name, exc_info=True)


def build_session_tools(ctx: SessionToolContext) -> list[SdkMcpTool[Any]]:
    def guarded(fn: Any) -> Any:
        spec = SESSION_TOOL_SPECS[fn.__name__]   # build-time KeyError for a spec-less handler

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            t0 = time.monotonic()
            ok = True
            try:
                return tool_ok(await fn(args or {}))
            except ToolArgError as e:
                ok = False
                return tool_err(str(e))
            except Exception as e:  # noqa: BLE001 - never let a tool crash the SDK loop
                ok = False
                log.warning("session tool %s failed", spec.name, exc_info=True)
                return tool_err(f"ccswitch tool error: {e.__class__.__name__}")
            finally:
                session_tool_calls_total.labels(spec.name, spec.tool_class.value,
                                                "ok" if ok else "error").inc()
                await _audit_tool_call(ctx, spec, ok, (time.monotonic() - t0) * 1000)
        return run

    def spec_tool(name: str, description: str, schema: dict[str, Any]) -> Any:
        return tool(name, description, schema, annotations=annotations_for(SESSION_TOOL_SPECS[name]))

    @spec_tool("session_context",
               "What ccswitch knows about THIS session: id, name, repo, branch, cwd, host, "
               "engine, model, account, the automation that dispatched it (origin) and its "
               "reference (originRef, e.g. owner/repo#123), the effective goal, and the "
               "permission mode. Call it when you need to know how you were dispatched.",
               {"type": "object", "properties": {}, "required": []})
    @guarded
    async def session_context(args: dict[str, Any]) -> Any:
        return context_snapshot(ctx)

    @spec_tool("report_feedback",
               "Report a problem with ccswitch, the harness that dispatched and supervises "
               "this session — never a bug in the project you are working on or a choice the "
               "user made. Call it when ccswitch's instructions contradict each other or the "
               "task, the dispatch prompt was wrong or ambiguous about the repo, branch or "
               "issue, ccswitch interrupted or switched model or account at a bad moment, or a "
               "worktree, credential, tool or marker it provides is broken. One call per "
               "distinct problem, at most three per session. It records the report for the "
               "ccswitch backlog and returns immediately — keep working. "
               f"category: one of {', '.join(CATEGORIES)}. impact: one of {', '.join(IMPACTS)}. "
               "title: 8-120 chars. body: what happened, what you expected, what ccswitch "
               "should change (quote the exact text when the problem is an instruction). "
               "evidence (optional): the offending prompt excerpt, command, or error.",
               {"type": "object",
                "properties": {"category": {"type": "string", "enum": list(CATEGORIES)},
                               "impact": {"type": "string", "enum": list(IMPACTS)},
                               "title": {"type": "string"}, "body": {"type": "string"},
                               "evidence": {"type": "string"}},
                "required": ["category", "impact", "title", "body"]})
    @guarded
    async def report_feedback(args: dict[str, Any]) -> Any:
        from server.config import get_settings
        item = validate_item(args)
        res = await record_feedback(session_id=ctx.session_id, identity=ctx.identity, item=item,
                                    context=context_snapshot(ctx),
                                    cap=get_settings().session_feedback_cap)
        return {"recorded": res.recorded, "status": res.status}

    return [session_context, report_feedback]
```

`server/sessions/tools/server.py`:

```python
"""Build the in-process `ccswitch` MCP server for one CODING session."""
from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from server.mcp_spec import SERVER_NAME
from server.version import running_git_sha

from .context import SessionToolContext
from .tools import build_session_tools


def build_session_server(ctx: SessionToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(name=SERVER_NAME, version=running_git_sha()[:12] or "dev",
                                 tools=build_session_tools(ctx))
```

`server/sessions/tools/instruction.py`:

```python
"""The system-prompt paragraph that tells a coding session its ccswitch tools exist.
Detail lives in the tool descriptions, which the model reads at call time."""
from __future__ import annotations

SESSION_TOOLS_INSTRUCTION = (
    "ccswitch is the harness that dispatched and supervises this session; it exposes two "
    "MCP tools under the `ccswitch` server. `session_context` tells you how you were "
    "dispatched (repo, branch, originating automation and issue, goal). `report_feedback` "
    "reports a problem with ccswitch ITSELF — contradictory or wrong instructions, an "
    "ambiguous dispatch prompt, an interrupt or model/account switch at a bad moment, a "
    "broken worktree, credential or tool it provides — never a bug in the project you are "
    "working on or a choice the user made. One report per distinct problem, at most three "
    "per session. A report never waits for a reply: keep working."
)
```

- [ ] **Step 4: Write the eval cases**

`docs/superpowers/evals/session-tools/cases.jsonl`:

```json
{"id": "context-before-branching", "prompt": "Before you start, tell me which issue and branch this session was dispatched for.", "expect_tools": ["session_context"], "forbid_tools": ["report_feedback"], "rubric": "Calls session_context and reports repo, branch and originRef from its data; does not guess."}
{"id": "contradictory-instruction", "prompt": "[system-prompt append contains: 'Always open a PR.' and 'Never open a PR for this task.'] Implement the fix.", "expect_tools": ["report_feedback"], "forbid_tools": [], "rubric": "Calls report_feedback once with category=prompt, quotes both lines in body or evidence, then continues working under a stated assumption; does not stop to wait."}
{"id": "project-bug-is-not-feedback", "prompt": "The project's test suite has a flaky test in tests/test_io.py. Fix it.", "expect_tools": [], "forbid_tools": ["report_feedback"], "rubric": "Fixes the project bug; never reports it via report_feedback."}
```

- [ ] **Step 5: Run tests, lint, commit, push**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_tools.py server/tests/test_mcp_spec.py -q` — Expected: PASS.

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/sessions/tools docs/superpowers/evals/session-tools server/tests/test_session_tools.py
printf 'feat(sessions): coding-session ccswitch MCP surface with session_context and report_feedback\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 6: Mount the surface on every Claude coding session

**Files:**
- Modify: `server/sessions/orchestrator.py` — `__init__` (line ~717 `_assistant_overlays` → `_overlays`), `register_assistant_overlay` (1193) → `register_overlay`, new `_setup_session_overlay` + `_setup_overlay_for_kind` next to `_setup_assistant_overlay` (1206), `_session_factory` (1277), `_codex_assistant_overlay_for_session` (1338), spawn site (2956-2959, 3087), `_teardown_driver_state` (3229), reassign pop (3421), `ensure_driver` (10357-10364, 10475), `_reassign_locked` (12767-12770)
- Modify: `server/sessions/claude_sdk_factory.py:12-23` (`_build_append_string`)
- Modify: every test referencing `_assistant_overlays` / `register_assistant_overlay` (`grep -rln "_assistant_overlays\|register_assistant_overlay" server/tests/`)
- Test: `server/tests/test_session_overlay.py`

**Interfaces:**
- Consumes: `build_session_server`, `SessionToolContext`, `SESSION_TOOLS_INSTRUCTION` (Task 5), `get_settings().session_tools_enabled` (Task 2).
- Produces: `SessionOrchestrator._overlays: dict[str, Any]`, `register_overlay(session_id, overlay)`, `async _setup_session_overlay(session_id, *, identity, engine) -> None`, `async _setup_overlay_for_kind(session_id, *, kind, identity, model, engine) -> AssistantApi | None` (returns the assistant api for assistant kind, `None` otherwise).

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_session_overlay.py
"""A coding session's ccswitch MCP overlay is armed at every driver build and released
by the shared teardown (session tools spec §3.3; fix-pattern
docs/superpowers/fix-patterns/2026-09-04-per-process-state-armed-only-on-the-create-path.md)."""
from __future__ import annotations

import pytest

from server.sessions.types import ASSISTANT_KIND
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.test_assistant_reap import _boot_orch, _capturing_factory, _teardown


@pytest.mark.asyncio
async def test_spawn_mounts_only_mcp_servers(isolated_db, tmp_path, monkeypatch):
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients, overlays=overlays))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        ov = overlays[-1]
        assert ov is not None and set(ov) == {"mcp_servers"}, ov
        assert set(ov["mcp_servers"]) == {"ccswitch"}
        assert "strict_mcp_config" not in ov and "system_prompt" not in ov
        assert s.id in orch._overlays
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_flag_off_mounts_nothing(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    from server.config import get_settings
    get_settings.cache_clear()
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients, overlays=overlays))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        assert overlays[-1] is None and s.id not in orch._overlays
    finally:
        await _teardown(orch, patches)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_assistant_kind_still_gets_the_assistant_overlay(isolated_db, tmp_path, monkeypatch):
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients, overlays=overlays))
    try:
        await orch.create_session(name="A", cwd=str(tmp_path), identity="u@x",
                                  kind=ASSISTANT_KIND, owner_identity="u@x")
        ov = overlays[-1]
        assert ov["strict_mcp_config"] is True and ov["tools"] == []
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_close_releases_the_overlay(isolated_db, tmp_path, monkeypatch):
    clients: list[FakeSDKClient] = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        assert s.id in orch._overlays
        await orch.close_session(s.id)
        assert s.id not in orch._overlays
    finally:
        await _teardown(orch, patches)


@pytest.mark.asyncio
async def test_resume_rebuilds_the_overlay(isolated_db, tmp_path, monkeypatch):
    """Mirror of test_resume_after_reap_rebuilds_the_assistant_overlay for a coding
    session: a detach + ensure_driver must hand the SDK factory a fresh overlay."""
    clients: list[FakeSDKClient] = []
    overlays: list = []
    orch, patches = await _boot_orch(tmp_path, monkeypatch, _capturing_factory(clients, overlays=overlays))
    try:
        s = await orch.create_session(name="Code", cwd=str(tmp_path), identity="u@x")
        first = orch._overlays[s.id]
        driver = await orch._teardown_driver_state(s.id)
        await driver.aclose()
        assert s.id not in orch._overlays
        rec = orch._records[s.id]
        rec.engine_session_id = rec.engine_session_id or "claude-sid"
        assert await orch.ensure_driver(s.id, "u@x") is not None
        assert overlays[-1] is not None and overlays[-1] is not first
        assert set(overlays[-1]) == {"mcp_servers"}
    finally:
        await _teardown(orch, patches)


def test_append_string_carries_the_instruction_when_enabled(monkeypatch):
    from server.config import get_settings
    from server.sessions.claude_sdk_factory import _build_append_string
    from server.sessions.tools.instruction import SESSION_TOOLS_INSTRUCTION
    monkeypatch.delenv("CCSWITCH_SESSION_TOOLS_ENABLED", raising=False)
    get_settings.cache_clear()
    assert SESSION_TOOLS_INSTRUCTION in _build_append_string()
    monkeypatch.setenv("CCSWITCH_SESSION_TOOLS_ENABLED", "false")
    get_settings.cache_clear()
    assert SESSION_TOOLS_INSTRUCTION not in _build_append_string()
    get_settings.cache_clear()
```

If `get_settings` has no `cache_clear` (check `grep -n "lru_cache\|def get_settings" server/config.py`), use whatever reset helper the existing settings tests use (`grep -rn "get_settings" server/tests/test_settings*.py server/tests/conftest.py | head`).

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_overlay.py -v`
Expected: FAIL (`AttributeError: 'SessionOrchestrator' object has no attribute '_overlays'` and the spawn overlay is `None`).

- [ ] **Step 3: Generalize the registry**

In `server/sessions/orchestrator.py`:

1. Line ~717: rename `self._assistant_overlays: dict[str, dict] = {}` to `self._overlays: dict[str, Any] = {}` and reword the comment: "session_id → options overlay for ANY kind: the assistant's full overlay (dict for Claude, CodexAssistantOverlay for Codex) or a coding session's `{"mcp_servers": …}` / CodexSessionTools. In-memory only …".
2. Rename `register_assistant_overlay` → `register_overlay` (same body, using `_overlays`); update its docstring's first line to "Register the options overlay a session spawns with."
3. `sed -i 's/_assistant_overlays/_overlays/g; s/register_assistant_overlay/register_overlay/g' server/sessions/orchestrator.py` then review the diff — every remaining reference is now the generalized registry.
4. Add, directly after `_setup_assistant_overlay`:

```python
    async def _setup_session_overlay(self, session_id: str, *, identity: str,
                                     engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> None:
        """Arm a CODING session's `ccswitch` MCP surface (session tools spec §3.3).

        The overlay is exactly ``{"mcp_servers": {"ccswitch": server}}``: never
        ``strict_mcp_config`` (it would drop the repo's own .mcp.json servers) and never
        ``system_prompt`` (the factory's append carries the marker contracts). Rebuilt
        at every driver build like the assistant's — the server closes over this
        orchestrator's live record accessor, and ``_teardown_driver_state`` drops it.
        """
        from server.config import get_settings
        from server.sessions.tools.context import SessionToolContext
        if not get_settings().session_tools_enabled:
            self._overlays.pop(session_id, None)
            return
        selected = parse_agent_engine(engine, legacy_default=True)
        ctx = SessionToolContext(
            session_id=session_id, identity=identity, kind=None, engine=selected.value,
            record=lambda: self._records.get(session_id), host=self_host_name())
        if selected is AgentEngine.CODEX:
            from server.sessions.tools.codex import CodexSessionTools
            self.register_overlay(session_id, CodexSessionTools.build(ctx))
            return
        from server.sessions.tools.server import build_session_server
        self.register_overlay(session_id, {"mcp_servers": {"ccswitch": build_session_server(ctx)}})

    async def _setup_overlay_for_kind(self, session_id: str, *, kind: str | None, identity: str,
                                      model: str | None,
                                      engine: AgentEngine | str = AgentEngine.CLAUDE_CODE):
        """THE overlay arming call every driver-build site makes (spawn, ensure_driver,
        _reassign_locked). Returns the AssistantApi for an assistant kind, else None."""
        if is_assistant_kind(kind):
            return await self._setup_assistant_overlay(
                session_id, identity=identity, model=model, engine=engine)
        await self._setup_session_overlay(session_id, identity=identity, engine=engine)
        return None
```

`self_host_name` — check the import already used in this module (`grep -n "self_host_name" server/sessions/orchestrator.py | head -2`); import from `server.hostname` if absent. `CodexSessionTools` is created in Task 7; until then make the Codex branch `return` after popping any overlay (leave a `# Task 7 wires CodexSessionTools here` comment ONLY if Task 7 is not in the same PR — it is, so implement Task 7 before merging; for this task's tests the Claude path suffices and the Codex import may temporarily fail only when a Codex session is created).

5. Replace the three call sites:

Spawn (2956-2959):
```python
            assistant_api = await self._setup_overlay_for_kind(
                session_id, kind=kind, identity=identity, model=model, engine=selected_engine)
```
`ensure_driver` `_build` (10357-10364):
```python
                assistant_api = await self._setup_overlay_for_kind(
                    session_id, kind=getattr(rec, "kind", None), identity=identity,
                    model=rec.model, engine=resume_engine)
                if assistant_api is not None:
                    built_assistant_apis.append(assistant_api)
```
`_reassign_locked` (12767-12770):
```python
            assistant_api = await self._setup_overlay_for_kind(
                record.id, kind=getattr(record, "kind", None), identity=identity,
                model=record.model, engine=record.engine)
```
Delete the now-redundant `if is_assistant_kind(...)` wrappers at each site.

6. `_teardown_driver_state` (3229): add `self._overlays.pop(session_id, None)` right after `self._handled_model_requests.pop(session_id, None)`. Then delete the now-duplicate pops at the spawn-rollback (3087), reap (3421) and ensure_driver-failure (10475) sites ONLY where `_teardown_driver_state` is already called on that path (spawn rollback calls it: delete; reap at 3421 — read the surrounding code: if the reap calls `_teardown_driver_state`, delete the explicit pop, otherwise keep it; ensure_driver failure path at 10475 does not call teardown: keep it).

7. `_codex_assistant_overlay_for_session` (1338): keep the name and body (it type-checks for `CodexAssistantOverlay`); Task 7 adds the sibling getter.

8. `_session_factory` (1277): unchanged apart from the rename (a coding overlay is a dict, so the existing `isinstance(overlay, dict)` passes it through).

- [ ] **Step 4: Append the instruction**

`server/sessions/claude_sdk_factory.py` `_build_append_string`:

```python
def _build_append_string() -> str:
    from server.config import get_settings
    from server.sessions.tools.instruction import SESSION_TOOLS_INSTRUCTION
    cfg = get_settings()
    parts = [marker_instructions(self_model_switch=cfg.self_model_switch_enabled)]
    if cfg.session_tools_enabled:
        parts.append(SESSION_TOOLS_INSTRUCTION)
    return "\n\n".join(parts)
```

Keep the docstring; add one sentence: "Also appends the session-tools paragraph when the surface is mounted (session tools spec §3.3), so a session is never told about tools it does not have."

- [ ] **Step 5: Update tests that used the old names**

`grep -rln "_assistant_overlays\|register_assistant_overlay" server/tests/` → `sed -i 's/_assistant_overlays/_overlays/g; s/register_assistant_overlay/register_overlay/g'` on each. `test_all_three_driver_teardowns_route_through_one_helper` in `test_assistant_reap.py` reads source with `inspect.getsource` — re-run it and adjust its expectations if it counted overlay pops.

- [ ] **Step 6: Run the affected suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_session_overlay.py server/tests/test_assistant_reap.py server/tests/test_reassign_assistant_overlay.py server/tests/test_assistant_orchestrator.py server/tests/test_orchestrator.py server/tests/test_ensure_driver*.py -q --timeout=300`
Expected: PASS. Then `grep -rn "_assistant_overlays\|register_assistant_overlay" server/ docs/superpowers/fix-patterns/` must show only the historical fix-pattern doc (leave it; it is a dated record).

- [ ] **Step 7: Lint, commit, push**

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/sessions/orchestrator.py server/sessions/claude_sdk_factory.py server/tests/
printf 'feat(sessions): mount the ccswitch MCP surface on every coding session\n\nGeneralizes the assistant-only overlay registry to any kind, armed at all three driver-build\nsites and released by the shared teardown; appends the session-tools instruction.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 7: Codex — fresh-start declaration + resume spike

**Files:**
- Create: `server/sessions/tools/codex.py`, `docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md`
- Modify: `server/codex/driver.py:292-338` (constructor), `:484-560` (`start`), `:1159-1176` (`_on_dynamic_tool`), `:2010-2045` (factory); `server/sessions/orchestrator.py` (`_codex_session_tools_for_session` getter next to `_codex_assistant_overlay_for_session`, factory wiring at 1320)
- Test: `server/tests/test_codex_session_tools.py`

**Interfaces:**
- Consumes: `build_session_tools`, `SessionToolContext` (Task 5); `server.assistant.options_codex.input_schema_for`, `_as_result`, `tool_error`; `MCP_PREFIX`.
- Produces: `CodexSessionTools` (frozen dataclass: `dynamic_tools: tuple[FunctionDynamicToolSpec, ...]`, `handlers: Mapping[str, ToolHandler]`; classmethod `build(ctx)`; methods `declares(tool)`, `mcp_name(tool)`, `async run_tool(tool, arguments)`); `CodexAgentDriver(..., session_tools: CodexSessionTools | None = None)`; factory kwarg `session_tools_for_session: Callable[[str], CodexSessionTools | None] | None`.

- [ ] **Step 1: Spike the real binary (throwaway; result is a doc)**

If `codex` is installed (`which codex`), start an app-server thread with one `dynamicTools` entry, call `thread/resume` with a `dynamicTools` array on the same thread, and record whether it is accepted, ignored, or rejected; also list the app-server's methods for any `tools`-related method. Write findings to `docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md` (what was tried, exact request/response, conclusion). If `codex` is absent on this host, write that down and keep the resume gap as designed. Do not keep spike code.

- [ ] **Step 2: Write the failing tests**

```python
# server/tests/test_codex_session_tools.py
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from server.sessions.tools.context import SessionToolContext


def _ctx():
    rec = SimpleNamespace(id="s1", name="n", repo="o/n", branch="b", cwd="/w", origin=None,
                          origin_ref=None, engine="codex", model=None, permission_mode="acceptEdits",
                          account="a", host="h", active_goal=None, inferred_goal=None, kind=None)
    return SessionToolContext(session_id="s1", identity="u", kind=None, engine="codex",
                              record=lambda: rec, host="h")


def test_build_declares_both_tools_with_object_schemas():
    from server.sessions.tools.codex import CodexSessionTools
    st = CodexSessionTools.build(_ctx())
    assert {s.name for s in st.dynamic_tools} == {"session_context", "report_feedback"}
    for spec in st.dynamic_tools:
        assert spec.input_schema["type"] == "object"
    assert st.declares("report_feedback") and not st.declares("Bash")
    assert st.mcp_name("report_feedback") == "mcp__ccswitch__report_feedback"


@pytest.mark.asyncio
async def test_run_tool_translates_result_and_never_raises():
    from server.sessions.tools.codex import CodexSessionTools
    st = CodexSessionTools.build(_ctx())
    res = await st.run_tool("session_context", {})
    assert res.success and json.loads(res.content_items[0].text)["data"]["repo"] == "o/n"
    bad = await st.run_tool("nope", {})
    assert not bad.success


@pytest.mark.asyncio
async def test_schemas_match_the_claude_server(isolated_db):
    """Anti-drift: the Codex declaration must equal what the Claude MCP server publishes."""
    from mcp.types import ListToolsRequest
    from server.sessions.tools.codex import CodexSessionTools
    from server.sessions.tools.server import build_session_server
    ctx = _ctx()
    instance = build_session_server(ctx)["instance"]
    handler = instance.request_handlers[ListToolsRequest]
    published = {t.name: t.inputSchema for t in (await handler(ListToolsRequest(method="tools/list"))).root.tools}
    declared = {s.name: s.input_schema for s in CodexSessionTools.build(ctx).dynamic_tools}
    assert declared == published
```

Copy the exact `ListToolsRequest` invocation shape from `server/tests/test_assistant_codex_overlay.py:86-100` if it differs from the above.

- [ ] **Step 3: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_codex_session_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.sessions.tools.codex'`

- [ ] **Step 4: Write `server/sessions/tools/codex.py`**

```python
"""Codex twin of the coding-session tool surface: the same handlers declared as
`dynamicTools` on `thread/start` (session tools spec §3.4).

The pinned `thread/resume` has no `dynamicTools`, so a RESUMED Codex coding session has
no session tools until the contract gains them — see the 2026-09-06 spike doc. A coding
session must keep its thread (its context is the work), so unlike the assistant we do
not restart it to keep the tools.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from server.assistant.options_codex import ToolHandler, _as_result, input_schema_for, tool_error
from server.codex import schema
from server.mcp_spec import MCP_PREFIX

from .context import SessionToolContext
from .tools import build_session_tools


@dataclass(frozen=True)
class CodexSessionTools:
    dynamic_tools: tuple[schema.FunctionDynamicToolSpec, ...]
    handlers: Mapping[str, ToolHandler]

    @classmethod
    def build(cls, ctx: SessionToolContext) -> "CodexSessionTools":
        tools = build_session_tools(ctx)
        specs = tuple(schema.FunctionDynamicToolSpec(
            name=t.name, description=t.description, input_schema=input_schema_for(t))
            for t in tools)
        return cls(dynamic_tools=specs, handlers={t.name: t.handler for t in tools})

    def declares(self, tool: str) -> bool:
        return tool in self.handlers

    def mcp_name(self, tool: str) -> str:
        return MCP_PREFIX + tool

    async def run_tool(self, tool: str, arguments: Any) -> schema.DynamicToolCallResponse:
        handler = self.handlers.get(tool)
        if handler is None:
            return tool_error(f"ccswitch tool error: no tool named {tool!r}")
        args = dict(arguments) if isinstance(arguments, Mapping) else {}
        try:
            return _as_result(await handler(args))
        except Exception as exc:  # noqa: BLE001 - a tool must never fail the turn
            return tool_error(f"ccswitch tool error: {exc.__class__.__name__}")
```

Check `FunctionDynamicToolSpec`'s constructor field names at `server/codex/schema.py:2026` and match them exactly.

- [ ] **Step 5: Thread it through the driver**

`server/codex/driver.py` constructor: add `session_tools: CodexSessionTools | None = None` after `assistant_overlay`, store `self._session_tools = session_tools` (import under `TYPE_CHECKING` from `server.sessions.tools.codex`).

`start()`: in the `else:` (fresh, non-assistant) branch add `dynamic_tools=(self._session_tools.dynamic_tools if self._session_tools is not None else ())` to `ThreadStartParams(...)`; when `self.resume` and `self._session_tools is not None`, emit `log_event(logger, logging.INFO, "codex.driver.session_tools_unavailable_on_resume", session=self.session_id)`. In `register_thread(...)`: `dynamic_tools=None if (overlay is None and self._session_tools is None) else self._on_dynamic_tool`.

`_on_dynamic_tool`: replace `overlay = self._assistant` with `overlay = self._assistant or self._session_tools` (the rest of the function only uses `declares`, `mcp_name`, `run_tool`, all of which `CodexSessionTools` provides; the `overlay is None` fence keeps its meaning).

Factory (`CodexAgentDriverFactory.__init__`): add `session_tools_for_session: Callable[[str], "CodexSessionTools | None"] | None = None`; in `create`, resolve it like the assistant overlay and pass `session_tools=...`.

Orchestrator: add next to `_codex_assistant_overlay_for_session`:

```python
    def _codex_session_tools_for_session(self, session_id: str):
        from server.sessions.tools.codex import CodexSessionTools
        overlay = self._overlays.get(session_id)
        return overlay if isinstance(overlay, CodexSessionTools) else None
```

and pass it as the new factory argument at line ~1320.

- [ ] **Step 6: Run tests, lint, commit, push**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_codex_session_tools.py server/tests/codex/ server/tests/test_assistant_codex_overlay.py server/tests/test_session_overlay.py -q --timeout=300` — Expected: PASS.

```bash
ruff check src/ server/ && mypy server/ --ignore-missing-imports
git add server/sessions/tools/codex.py server/codex/driver.py server/sessions/orchestrator.py server/tests/test_codex_session_tools.py docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md
printf 'feat(codex): declare the session tools as dynamicTools on fresh thread/start\n\nResumed threads keep their context and go without session tools until the contract\ngains them (spike doc records what the real binary does).\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

---

### Task 8: Permission behaviour pin + gated real-binary tests + docs + full gate + PR

**Files:**
- Create: `server/tests/test_permissions_session_tools.py`, `server/tests/test_session_tools_real_binary.py`, `docs/product/features/session-tools.md`
- Modify: `.agents/instructions/core.md` (one paragraph after "Self-directed model switching"), then `agentctl render`

- [ ] **Step 1: Permission pin test**

```python
# server/tests/test_permissions_session_tools.py
"""report_feedback / session_context resolve silently in EVERY mode — they are READ-class
on the shared registry (session tools spec §3.2)."""
from __future__ import annotations

import pytest

import server.sessions.tools  # noqa: F401 - registers the surface
from server.sessions.permissions import PermissionRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "default", "acceptEdits", "full-auto"])
@pytest.mark.parametrize("tool", ["mcp__ccswitch__report_feedback", "mcp__ccswitch__session_context"])
async def test_session_tools_never_raise_a_card(mode, tool):
    reg = PermissionRegistry()
    pid, fut, _ = await reg.request("s1", "t1", tool, {"category": "prompt"}, mode=mode,
                                    denylist=(), allowlist=True, identity="u")
    assert pid is None and fut.done() and fut.result() is True
```

Match `PermissionRegistry()`'s constructor and `request`'s kwargs to `server/tests/test_permissions.py`.

- [ ] **Step 2: Gated real-binary test**

```python
# server/tests/test_session_tools_real_binary.py
"""Constitution III: verify against the real `claude` binary. Gated on
CCSWITCH_TEST_CLAUDE_MCP=1 (needs the system claude + a logged-in account)."""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CCSWITCH_TEST_CLAUDE_MCP") != "1",
                                reason="set CCSWITCH_TEST_CLAUDE_MCP=1 to run against the real claude binary")


@pytest.mark.asyncio
async def test_project_mcp_server_and_ccswitch_server_coexist(tmp_path):
    """(a) a coding session with a project .mcp.json sees BOTH that server and
    mcp__ccswitch__session_context; (b) calling report_feedback in `default` mode
    produces no permission prompt."""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ClaudeAgentOptions, ToolUseBlock
    from server.sessions.claude_bin import cli_path_kwargs
    from server.sessions.tools.context import SessionToolContext
    from server.sessions.tools.server import build_session_server
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"echo": {
        "command": "python3", "args": ["-c", "import sys; sys.exit(0)"]}}}))
    ctx = SessionToolContext(session_id="real", identity="test", kind=None, engine="claude_code",
                             record=lambda: None, host="local")
    prompts: list[str] = []
    async def can_use_tool(name, inp, ctx_=None):
        from claude_agent_sdk import PermissionResultAllow
        prompts.append(name)
        return PermissionResultAllow()
    opts = ClaudeAgentOptions(
        cwd=str(tmp_path), permission_mode="default", can_use_tool=can_use_tool,
        settings=json.dumps({"enableAllProjectMcpServers": True}),
        mcp_servers={"ccswitch": build_session_server(ctx)}, max_turns=2,
        **cli_path_kwargs())
    used: list[str] = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("Call the ccswitch session_context tool, then call report_feedback with "
                           "category=other impact=minor title='Real-binary smoke check' body='ignore'. "
                           "Reply with the word done.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                used += [b.name for b in msg.content if isinstance(b, ToolUseBlock)]
    assert "mcp__ccswitch__session_context" in used
    assert "mcp__ccswitch__report_feedback" in used
    assert not [p for p in prompts if p.startswith("mcp__ccswitch__")], prompts
```

Run it once locally with the flag set (`CCSWITCH_TEST_CLAUDE_MCP=1 …`) and paste the pass/fail into the PR body. It is skipped in CI.

- [ ] **Step 3: Feature doc + core.md paragraph**

`docs/product/features/session-tools.md`: title, what the surface is (2 tools, READ-class, in-process closure, any host), the kill switch, the feedback row lifecycle (`pending`/`capped`/`duplicate` in Phase 1; Phase 2 adds filing), the Codex resume gap, and a pointer to the spec. Keep it under 60 lines.

`.agents/instructions/core.md`, after the "Self-directed model switching" section:

```markdown
## Session tool surface

Every coding session mounts an in-process `ccswitch` MCP server (`server/sessions/tools/`)
with `session_context` and `report_feedback`, both READ-class (no permission card). Tools
close over `SessionToolContext` in the daemon that owns the session — never loopback HTTP,
never a token — so they work on any fleet host. The coding overlay is exactly
`{"mcp_servers": …}`: never add `strict_mcp_config` (drops the repo's `.mcp.json` servers)
or `system_prompt` (erases the marker contracts). Registry primitives live in
`server/mcp_spec.py`; add a tool by adding a `ToolSpec` + handler + eval case. Kill switch
`CCSWITCH_SESSION_TOOLS_ENABLED`. Feedback rows (`session_feedback`) are filed to GitHub by
the Phase 2 automation. Spec: `docs/superpowers/specs/2026-09-06-session-feedback-to-backlog-design.md`.
```

Then run `agentctl render` (see `grep -rn "agentctl" .agents/ scripts/ Makefile 2>/dev/null | head -3` for the exact invocation) and check `AGENTS.md` stays under the 320-line cap.

- [ ] **Step 4: Full gate**

```bash
for v in $(env | grep -o '^CCSWITCH_[A-Z_]*'); do unset $v; done
ruff check src/ server/ && mypy server/ --ignore-missing-imports
PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/ -q
PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/ -n auto --dist loadfile --timeout=300 -q 2>&1 | tee /tmp/session-tools-server-suite.log | tail -5
```

All four must be green (a lone `test_lifespan_shutdown_bounded_*` failure on this box is the known worktree-janitor artefact; everything else must pass).

- [ ] **Step 5: Commit, PR, ready, auto-merge**

```bash
git add server/tests/test_permissions_session_tools.py server/tests/test_session_tools_real_binary.py docs/product/features/session-tools.md .agents/instructions/core.md AGENTS.md
printf 'test(sessions): permission pin, gated real-binary check, session-tools feature doc\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/msg.txt
git commit -F /tmp/msg.txt && git push
```

Write `/tmp/pr-body.md` (summary of the surface, the two guardrails, the Codex gap + spike result, the real-binary test result, `Refs #<issue>` if one exists, and the `🤖 Generated with [Claude Code](https://claude.com/claude-code)` trailer), then:

```bash
gh pr create --title "feat(sessions): per-session ccswitch MCP surface with session_context and report_feedback" --body-file /tmp/pr-body.md --base master
gh pr merge <#> --squash --auto
```

---

## Self-review

**Spec coverage (Phase 1 slice):** §2 reuse (Task 1, 6); §3.1 package + neutral module (Task 1, 5); §3.2 tools (Task 5); §3.3 mounting + guardrails + instruction + flag (Task 2, 6); §3.4 Codex (Task 7); §3.5 real-binary tests (Task 8); §4 contract + service (Task 4, 5); §5 persistence (Task 3); §7 safety — no token, fail-open, kill switch (Task 5, 6), caps (Task 4); §9 config (Task 2); §10 tests (each task). Deferred to Phase 2 by design: §6 filing, §8 API/UI/assistant tool, `session_url` in the snapshot (needs the edge's public URL, which only the filer knows).

**Type consistency:** `SessionToolContext(session_id, identity, kind, engine, record, host)` is used identically in Tasks 5–8; `record_feedback(session_id=, identity=, item=, context=, cap=)` in Tasks 4–5; `FeedbackResult(recorded, status)`; `CodexSessionTools.build(ctx)` in Tasks 6–7; `_overlays` / `register_overlay` / `_setup_overlay_for_kind` in Tasks 6–7.

**Placeholders:** none. The one conditional ("if `get_settings` has no `cache_clear`") names the exact grep to resolve it.
