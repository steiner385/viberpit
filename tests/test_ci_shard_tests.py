"""`scripts/ci_shard_tests.py` chooses which test FILES each CI shard runs.

That makes it the one piece of the split that can fail *silently green*: a file
dropped from every shard simply never runs, and `lint-and-test` still reports
success. Nothing downstream would notice — which is precisely the failure mode the
sharding was meant not to introduce. These tests pin the invariant that makes the
split safe (the shards partition the suite exactly, once each) and the error paths
that must exit non-zero rather than print an empty list.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Must match `matrix: shard: [...]` and the `--total` argument in ci.yml; the
# drift test below is what keeps that true.
TOTAL = 4


def _load_module():
    path = _REPO_ROOT / "scripts" / "ci_shard_tests.py"
    spec = importlib.util.spec_from_file_location("ci_shard_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _shard_files(capsys, shard: int, total: int = TOTAL) -> list[str]:
    assert mod.main(["--shard", str(shard), "--total", str(total)]) == 0
    out = capsys.readouterr().out.strip()
    return out.split(" ") if out else []


def _all_test_files() -> set[str]:
    return {
        str(p.relative_to(_REPO_ROOT))
        for p in mod._discover(_REPO_ROOT / "server" / "tests")
    }


def test_shards_partition_the_suite_exactly(capsys):
    """Every file runs in exactly one shard: no file dropped, no file run twice.

    A dropped file is a silent loss of coverage; a duplicated one wastes the wall
    time the split exists to buy back.
    """
    selected: list[str] = []
    for shard in range(1, TOTAL + 1):
        selected.extend(_shard_files(capsys, shard))

    assert len(selected) == len(set(selected)), "a file was assigned to two shards"
    assert set(selected) == _all_test_files()


def test_every_shard_gets_work(capsys):
    """An empty shard would spin up a runner (and a Postgres service) for nothing,
    and signals the bucketing has degenerated at this `--total`."""
    for shard in range(1, TOTAL + 1):
        assert _shard_files(capsys, shard), f"shard {shard}/{TOTAL} is empty"


def test_workflow_matrix_matches_the_shard_count():
    """`matrix: shard` and `--total` must agree.

    They are two hand-written numbers in the same file, and disagreeing is the
    silent-green case: a matrix of [1..4] against `--total 5` runs four fifths of
    the suite and reports success on the missing fifth.
    """
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    match = re.search(r"^\s*shard: \[([0-9, ]+)\]\s*$", ci, re.M)
    assert match, "no `shard: [...]` matrix found in ci.yml"
    assert [int(n) for n in match.group(1).split(",")] == list(range(1, TOTAL + 1))

    assert f"--total {TOTAL}" in ci, f"ci.yml does not pass --total {TOTAL}"


def test_bucket_depends_only_on_the_path():
    """Path-hash bucketing means a file's shard is a function of its own path, so
    adding or deleting a test file never reshuffles the others onto new shards."""
    path = Path("server/tests/test_example.py")
    assert mod._bucket(path, TOTAL) == mod._bucket(Path(str(path)), TOTAL)
    assert 0 <= mod._bucket(path, TOTAL) < TOTAL


def test_discover_finds_test_files_recursively(tmp_path):
    """Discovery must match what `pytest server/tests/` collects — `test_*.py`,
    at any depth — or the shards and the unsharded suite disagree about scope."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "test_a.py").touch()
    (tmp_path / "sub" / "test_b.py").touch()
    (tmp_path / "helpers.py").touch()
    (tmp_path / "conftest.py").touch()

    assert {p.name for p in mod._discover(tmp_path)} == {"test_a.py", "test_b.py"}


@pytest.mark.parametrize("shard", [0, -1, TOTAL + 1])
def test_out_of_range_shard_is_an_error(shard):
    assert mod.main(["--shard", str(shard), "--total", str(TOTAL)]) == 1


def test_missing_root_is_an_error():
    assert mod.main(["--root", "server/tests/nope", "--shard", "1", "--total", "1"]) == 1


def test_empty_selection_is_an_error():
    """An empty shard must exit non-zero rather than print nothing: an empty
    argument list makes pytest fall back to `testpaths` and run the ENTIRE suite,
    turning a bucketing bug into four full-suite runs that all look correct."""
    total = 5000  # more buckets than files, so some bucket is necessarily empty
    used = {
        mod._bucket(p.relative_to(_REPO_ROOT), total)
        for p in mod._discover(_REPO_ROOT / "server" / "tests")
    }
    empty = next(i for i in range(total) if i not in used)
    assert mod.main(["--shard", str(empty + 1), "--total", str(total)]) == 1
