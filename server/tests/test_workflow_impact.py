"""Tests for server/pipeline/workflow_impact.py — orchestration + cache (SPEC §4).

The orchestrator is injected with a blob fetcher and a ciGraph-meta lookup so
the diff logic is exercised without touching gh or SQLite.
"""
import pytest

from server.pipeline.workflow_impact import WorkflowImpactResolver

_MAIN_CI = """
name: CI
jobs:
  ci:
    needs: [build, test]
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 20
  test:
    runs-on: ubuntu-latest
"""

# head blob adds `lint` to the gate and bumps build's timeout 20 → 30.
_HEAD_CI = """
name: CI
jobs:
  ci:
    needs: [build, test, lint]
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 30
  test:
    runs-on: ubuntu-latest
  lint:
    runs-on: ubuntu-latest
"""

_OTHER_WF = "name: Nightly\njobs:\n  cron:\n    runs-on: ubuntu-latest\n"


class _FakeBlobs:
    """Serves .github/workflows/ listings and file contents keyed by ref."""

    def __init__(self, listing, files):
        self.listing = listing            # list[str] of workflow file names
        self.files = files                # {(name, ref): text}
        self.calls: list = []

    def list_workflow_files(self, repo, ref):
        self.calls.append(("list", repo, ref))
        return list(self.listing)

    def fetch_file(self, repo, path, ref=None):
        self.calls.append(("fetch", repo, path, ref))
        # None == the repo default branch; fixtures key the base blob as "main".
        return self.files.get((path, ref if ref is not None else "main"))


def _resolver(blobs, ci_graph_meta):
    return WorkflowImpactResolver(
        blobs=blobs,
        ci_graph_for=lambda repo: ci_graph_meta,
    )


def _blobs():
    return _FakeBlobs(
        listing=[".github/workflows/nightly.yml", ".github/workflows/ci.yml"],
        files={
            (".github/workflows/nightly.yml", "main"): _OTHER_WF,
            (".github/workflows/ci.yml", "main"): _MAIN_CI,
            (".github/workflows/ci.yml", "headsha1"): _HEAD_CI,
        },
    )


def test_impact_diffs_main_vs_head():
    r = _resolver(_blobs(), {"prefixes": ["ci", "build", "test"], "workflowName": "CI"})
    out = r.impact_for("o/r", "headsha1")
    assert out is not None
    assert "+ lint joins the merge_group gate" in out["summary"]
    assert "required-check set grows by 1: 3 → 4 checks" in out["summary"]
    assert "build timeout-minutes 20m → 30m" in out["summary"]


def test_impact_none_when_no_change():
    blobs = _FakeBlobs(
        listing=[".github/workflows/ci.yml"],
        files={
            (".github/workflows/ci.yml", "main"): _MAIN_CI,
            (".github/workflows/ci.yml", "headsha1"): _MAIN_CI,
        },
    )
    r = _resolver(blobs, {"prefixes": ["ci"], "workflowName": "CI"})
    assert r.impact_for("o/r", "headsha1") is None


def test_impact_none_when_no_ci_graph_meta():
    r = _resolver(_blobs(), None)
    assert r.impact_for("o/r", "headsha1") is None


def test_impact_none_when_workflow_file_unresolvable():
    # No listed file parses to name == "CI".
    blobs = _FakeBlobs(
        listing=[".github/workflows/nightly.yml"],
        files={(".github/workflows/nightly.yml", "main"): _OTHER_WF},
    )
    r = _resolver(blobs, {"prefixes": ["ci"], "workflowName": "CI"})
    assert r.impact_for("o/r", "headsha1") is None


def test_impact_none_when_head_fetch_fails():
    blobs = _FakeBlobs(
        listing=[".github/workflows/ci.yml"],
        files={(".github/workflows/ci.yml", "main"): _MAIN_CI},  # head missing
    )
    r = _resolver(blobs, {"prefixes": ["ci"], "workflowName": "CI"})
    assert r.impact_for("o/r", "headsha1") is None


def test_impact_none_when_fetcher_raises():
    class _Boom(_FakeBlobs):
        def list_workflow_files(self, repo, ref):
            raise RuntimeError("network")

    r = _resolver(_Boom([], {}), {"prefixes": ["ci"], "workflowName": "CI"})
    assert r.impact_for("o/r", "headsha1") is None


def test_workflow_path_cached_per_repo():
    blobs = _blobs()
    r = _resolver(blobs, {"prefixes": ["ci", "build", "test"], "workflowName": "CI"})
    r.impact_for("o/r", "headsha1")
    r.impact_for("o/r", "headsha2")  # different head; path resolution reused
    list_calls = [c for c in blobs.calls if c[0] == "list"]
    assert len(list_calls) == 1  # listing fetched once, then cached


def test_result_cached_per_repo_headsha():
    blobs = _blobs()
    r = _resolver(blobs, {"prefixes": ["ci", "build", "test"], "workflowName": "CI"})
    first = r.impact_for("o/r", "headsha1")
    blobs.calls.clear()
    second = r.impact_for("o/r", "headsha1")
    assert first == second
    assert blobs.calls == []  # served from cache, no re-fetch


def test_prune_evicts_absent_keys():
    blobs = _blobs()
    r = _resolver(blobs, {"prefixes": ["ci", "build", "test"], "workflowName": "CI"})
    r.impact_for("o/r", "headsha1")
    assert ("o/r", "headsha1") in r._cache
    r.prune({("o/r", "headsha2")})   # headsha1 no longer live
    assert ("o/r", "headsha1") not in r._cache


@pytest.mark.asyncio
async def test_impact_for_async_offloads_to_thread():
    blobs = _blobs()
    r = _resolver(blobs, {"prefixes": ["ci", "build", "test"], "workflowName": "CI"})
    out = await r.impact_for_async("o/r", "headsha1")
    assert out is not None
    assert "+ lint joins the merge_group gate" in out["summary"]
