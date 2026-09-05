"""Every master-push workflow must get its own concurrency group, per commit.

GitHub allows at most ONE *pending* run per concurrency group: when a third run is
queued behind an in-flight one, the previously pending run is **cancelled**.
``cancel-in-progress: false`` does not prevent this — it only protects the run that
has already started. So a shared per-*ref* group on ``refs/heads/master`` silently
drops the middle commits of any merge burst: their runs are cancelled before a single
job starts, which means the sha carries **no check-run at all** for that workflow's
contexts.

That is not cosmetic. ``server/deploy/steps.py:ci_verdict`` treats a required context
with no run on the commit as not-green, so such a commit can never be deployed — the
banner sits on a verdict that will never resolve. ``constitution.yml`` already learned
this (its group falls back to ``github.sha`` on push); this test stops the sibling
workflows from drifting back.

The fix is always the same shape: make the group expression vary per commit on master
by referencing ``github.sha``.
"""
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _triggers(doc: dict) -> dict:
    """The workflow's `on:` block.

    YAML 1.1 resolves a bare ``on:`` key to the boolean ``True`` (the "Norway problem"
    in its other guise), so a plain ``doc["on"]`` misses it and this test would vacuously
    pass on every real workflow file.
    """
    raw = doc.get("on", doc.get(True))
    return raw if isinstance(raw, dict) else {}


def _pushes_to_master(doc: dict) -> bool:
    push = _triggers(doc).get("push")
    if not isinstance(push, dict):
        return False
    return "master" in (push.get("branches") or [])


def _workflow_files() -> list[Path]:
    return sorted(p for p in _WORKFLOWS.glob("*.yml"))


def test_workflow_dir_is_discoverable():
    # Guards the test itself: a moved/renamed directory must fail loudly rather than
    # silently parametrize over nothing.
    assert _workflow_files(), f"no workflows found under {_WORKFLOWS}"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_master_push_workflows_use_a_per_commit_concurrency_group(path: Path):
    doc = yaml.safe_load(path.read_text()) or {}
    if not _pushes_to_master(doc):
        pytest.skip("not triggered by a push to master")
    concurrency = doc.get("concurrency")
    if concurrency is None:
        return  # no group at all: every run is independent, which is the safe end state
    group = concurrency["group"] if isinstance(concurrency, dict) else concurrency
    assert "github.sha" in group, (
        f"{path.name}: concurrency group {group!r} is shared across master commits, so a "
        "merge burst cancels the middle commits' runs before they start and leaves those "
        "shas with no check-runs. Reference github.sha on master."
    )
