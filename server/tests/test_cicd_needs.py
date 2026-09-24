"""Workflow `needs:` folding — which check names are children of an aggregator.

The graduation verdict knew only branch-protection contexts, so on a repo that
gates merges through ONE aggregator (KinDash's `ci` -> `ci-gate`, reasonbridge's
`CI`) every child job read as "advisory" and was proposed for graduation, 49 at a
time. A job some other job `needs:` is already folded into that dependent's
result; its tier is decided there, never on its own.
"""
from __future__ import annotations

from server.automations.cicd.needs import is_folded, needed_job_names

CI = """
name: CI
on:
  pull_request:
  merge_group:
jobs:
  prepare:
    runs-on: ubuntu-latest
  static-checks:
    uses: ./.github/workflows/_static-checks.yml
  fix-patterns:
    name: "docs: fix-patterns README up to date (#7928)"
    runs-on: ubuntu-latest
  accessibility:
    uses: ./.github/workflows/_accessibility-tests.yml
  ci:
    name: ci
    needs: [prepare, static-checks, fix-patterns]
    runs-on: ubuntu-latest
"""

STATIC = """
name: _static-checks
on:
  workflow_call:
jobs:
  typecheck:
    name: "types: tsc"
    runs-on: ubuntu-latest
  server-tests:
    name: "test: server (${{ matrix.shard }}/3)"
    needs: typecheck
    runs-on: ubuntu-latest
"""

NIGHTLY = """
name: Nightly
on:
  schedule:
    - cron: "0 4 * * *"
jobs:
  full-ci:
    uses: ./.github/workflows/_full-ci.yml
  nightly:
    needs:
      - full-ci
    runs-on: ubuntu-latest
"""


def test_needed_job_names_are_display_names_across_all_workflows():
    needed = needed_job_names({"ci.yml": CI, "_static-checks.yml": STATIC,
                               "nightly.yml": NIGHTLY})
    # `needs:` references job ids; the check name GitHub reports is the job's
    # display `name:` when set, else the id.
    assert "docs: fix-patterns README up to date (#7928)" in needed
    assert "prepare" in needed and "static-checks" in needed
    assert "types: tsc" in needed          # needed inside the reusable workflow
    assert "full-ci" in needed
    # Roots (nothing needs them) are NOT folded: `ci`, `nightly`, `accessibility`.
    assert not {"ci", "nightly", "accessibility"} & set(needed)


def test_is_folded_matches_the_check_or_its_caller_prefix():
    needed = needed_job_names({"ci.yml": CI, "_static-checks.yml": STATIC,
                               "nightly.yml": NIGHTLY})
    assert is_folded("prepare", needed)
    # Reusable-workflow children report as "<caller job> / <callee job>": the
    # caller job being needed folds every child under it.
    assert is_folded("static-checks / types: tsc", needed)
    assert is_folded("static-checks / test: server (2/3)", needed)
    assert is_folded("full-ci / static-checks / test: server (3/3)", needed)
    # The aggregator itself and an un-needed advisory workflow stay candidates.
    assert not is_folded("ci", needed)
    assert not is_folded("nightly", needed)
    assert not is_folded("accessibility / a11y: axe (pr) (advisory)", needed)
    assert not is_folded("probe", needed)


def test_templated_job_names_match_their_expanded_check_names():
    """`test: server (${{ matrix.shard }}/3)` reports as `test: server (1/3)`."""
    needed = needed_job_names({"_static-checks.yml": STATIC})
    assert is_folded("test: server (1/3)", needed) is False  # not needed by anyone
    needed = needed_job_names({"w.yml": """
jobs:
  shard:
    name: "test: unit (${{ matrix.shard }}/8)"
    runs-on: ubuntu-latest
  gate:
    needs: shard
    runs-on: ubuntu-latest
"""})
    assert is_folded("test: unit (5/8)", needed)
    assert not is_folded("test: unit", needed)


def test_malformed_yaml_folds_nothing():
    assert needed_job_names({"bad.yml": "jobs: [", "list.yml": "- a\n- b"}) == frozenset()
    assert not is_folded("anything", frozenset())
