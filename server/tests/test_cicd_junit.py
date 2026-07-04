"""JUnit XML parsing — pytest/vitest/Playwright dialects, pure."""
from __future__ import annotations

import pytest

from server.automations.cicd.junit import parse_junit_xml

PYTEST_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_a" name="test_ok" time="0.01"/>
<testcase classname="tests.test_a" name="test_bad" time="0.50">
  <failure message="assert 1 == 2">traceback...</failure></testcase>
<testcase classname="tests.test_a" name="test_skip" time="0">
  <skipped message="no reason"/></testcase>
</testsuite></testsuites>"""

FLAT_XML = b"""<testsuite tests="1">
<testcase classname="" name="dashboard renders" time="2.3">
  <error message="boom"/></testcase></testsuite>"""


def test_parses_outcomes_and_ids():
    cases = parse_junit_xml(PYTEST_XML)
    assert [(c.test_id, c.outcome) for c in cases] == [
        ("tests.test_a::test_ok", "passed"),
        ("tests.test_a::test_bad", "failed"),
        ("tests.test_a::test_skip", "skipped"),
    ]
    assert cases[1].message == "assert 1 == 2"
    assert cases[1].time_s == 0.5


def test_flat_suite_and_error_outcome():
    (c,) = parse_junit_xml(FLAT_XML)
    assert c.test_id == "dashboard renders" and c.outcome == "error"


def test_malformed_raises():
    with pytest.raises(ValueError):
        parse_junit_xml(b"<not-closed")
