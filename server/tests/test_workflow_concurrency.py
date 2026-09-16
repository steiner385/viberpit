"""A concurrency group must never cost a commit its check-runs.

Two shapes of the same mistake live here, both of which end with a commit whose
required contexts are not green for a reason that has nothing to do with the diff.

**Cancelled while pending (master).** GitHub allows at most ONE *pending* run per
concurrency group: when a third run is queued behind an in-flight one, the previously
pending run is **cancelled**. ``cancel-in-progress: false`` does not prevent this — it
only protects the run that has already started. So a shared per-*ref* group on
``refs/heads/master`` silently drops the middle commits of any merge burst: their runs
are cancelled before a single job starts, which means the sha carries **no check-run at
all** for that workflow's contexts.

**Cancelled mid-run (pull requests).** A group keyed on the pull request rather than on
the commit puts every event for that pull request in one bucket. That is harmless while
the only triggers are push-shaped — ``opened``/``synchronize``/``reopened`` each arrive
with a new head sha — but a workflow that also subscribes to ``edited``, ``labeled`` or
``ready_for_review`` gets a second event for a commit whose run is already executing.
``cancel-in-progress`` then kills it, and the half-finished run leaves a **CANCELLED**
check-run attached to that sha. GitHub's status rollup counts CANCELLED as a FAILURE,
so the pull request reads permanently red for a check that actually passed — while
branch protection, which reads only the latest run per context name, calls it CLEAN
(2026-09-16, ``constitution.yml`` and #1828).

Neither is cosmetic. ``server/deploy/steps.py:ci_verdict`` treats a required context
that is not green on the commit as not-deployable, so such a commit sits on a verdict
that will never resolve.

The fixes are the same shape twice: make the group vary per COMMIT — ``github.sha`` on
a master push, ``github.event.pull_request.head.sha`` on a pull request — and do not
cancel runs whose triggers can fire without a new commit.
"""
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# The pull_request activity types that always arrive with a new head sha. Anything
# outside this set can fire while a run for the CURRENT sha is still executing.
_PUSH_SHAPED_PR_TYPES = {"opened", "synchronize", "reopened"}


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


def _commitless_pr_types(doc: dict) -> set[str]:
    """PR activity types this workflow subscribes to that need no new commit."""
    triggers = _triggers(doc)
    if "pull_request" not in triggers:
        return set()
    pull_request = triggers.get("pull_request")
    # `pull_request:` with no body means the default types, all of them push-shaped.
    declared = pull_request.get("types") if isinstance(pull_request, dict) else None
    return set(declared or ()) - _PUSH_SHAPED_PR_TYPES


def _concurrency(doc: dict) -> tuple[str, object] | None:
    """(group expression, cancel-in-progress) or None when the workflow declares none."""
    concurrency = doc.get("concurrency")
    if concurrency is None:
        return None  # no group at all: every run is independent, the safe end state
    if not isinstance(concurrency, dict):
        return str(concurrency), False
    return str(concurrency["group"]), concurrency.get("cancel-in-progress", False)


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
    concurrency = _concurrency(doc)
    if concurrency is None:
        return
    group, _ = concurrency
    assert "github.sha" in group, (
        f"{path.name}: concurrency group {group!r} is shared across master commits, so a "
        "merge burst cancels the middle commits' runs before they start and leaves those "
        "shas with no check-runs. Reference github.sha on master."
    )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_commitless_pr_triggers_never_cancel_a_run_for_the_same_commit(path: Path):
    doc = yaml.safe_load(path.read_text()) or {}
    commitless = _commitless_pr_types(doc)
    if not commitless:
        pytest.skip("every pull_request trigger arrives with a new head sha")
    concurrency = _concurrency(doc)
    if concurrency is None:
        return
    group, cancel = concurrency
    types = ", ".join(sorted(commitless))
    assert "pull_request.head.sha" in group, (
        f"{path.name}: concurrency group {group!r} buckets every event for a pull request "
        f"together, but `{types}` can fire for a commit whose run is already in flight. "
        "Key the group on github.event.pull_request.head.sha so same-commit events share "
        "a bucket only with themselves."
    )
    assert cancel is False, (
        f"{path.name}: cancel-in-progress is {cancel!r} while `{types}` can fire without a "
        "new commit, so a second event kills the run for the SAME sha and leaves a "
        "CANCELLED check-run on it — which GitHub's status rollup reports as a FAILURE. "
        "Set cancel-in-progress: false."
    )
