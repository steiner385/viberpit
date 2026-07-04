"""Workflow-YAML waste detection."""
from __future__ import annotations

from server.automations.cicd.waste import workflow_waste_findings

WASTEFUL = """
name: CI
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e .
"""

CLEAN = """
name: CI
on:
  push:
    branches: [master]
  pull_request:
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e .
"""


def test_detects_all_three_waste_kinds():
    kinds = {f.kind for f in workflow_waste_findings("o/r", "ci.yml", WASTEFUL)}
    assert kinds == {"waste_double_run", "waste_no_concurrency", "waste_no_pip_cache"}


def test_clean_workflow_no_findings():
    assert workflow_waste_findings("o/r", "ci.yml", CLEAN) == []


def test_malformed_yaml_no_findings():
    assert workflow_waste_findings("o/r", "ci.yml", ": not yaml [") == []


def test_fingerprint_keys_on_path():
    (f,) = [f for f in workflow_waste_findings("o/r", "ci.yml", WASTEFUL)
            if f.kind == "waste_double_run"]
    assert f.fingerprint == "waste_double_run:o/r:ci.yml"
