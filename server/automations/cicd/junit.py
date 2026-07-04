"""JUnit XML parsing — pure, no I/O. Handles the pytest, vitest and Playwright
dialects (all nest <testcase> under <testsuite>, optionally under <testsuites>)."""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True)
class JUnitCase:
    classname: str
    name: str
    outcome: str  # passed | failed | error | skipped
    time_s: float
    message: str | None = None

    @property
    def test_id(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


_OUTCOME_TAGS = (("failure", "failed"), ("error", "error"), ("skipped", "skipped"))


def parse_junit_xml(data: bytes) -> list[JUnitCase]:
    """Parse one JUnit document into cases. Raises ValueError on malformed XML —
    the caller (Plan C ingest) decides how to degrade."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as e:
        raise ValueError(f"malformed junit xml: {e}") from e
    cases: list[JUnitCase] = []
    for tc in root.iter("testcase"):
        outcome, message = "passed", None
        for tag, out in _OUTCOME_TAGS:
            node = tc.find(tag)
            if node is not None:
                outcome = out
                message = ((node.get("message") or node.text or "").strip()[:500]) or None
                break
        try:
            time_s = float(tc.get("time") or 0.0)
        except ValueError:
            time_s = 0.0
        cases.append(JUnitCase(classname=tc.get("classname") or "",
                               name=tc.get("name") or "",
                               outcome=outcome, time_s=time_s, message=message))
    return cases
