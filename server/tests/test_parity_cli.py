import json
import subprocess
import pytest

from server.github.parity.__main__ import (
    fetch_oracle, render_report, HarnessError, compute_candidate_by_number,
)
from server.github.parity.harness import ParityReport, FieldMismatch


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status


def test_fetch_oracle_parses_json():
    def opener(url, timeout=0):
        assert url.endswith("/api/state")
        return _Resp(json.dumps({"repos": []}).encode())

    assert fetch_oracle("http://127.0.0.1:4400", opener=opener) == {"repos": []}


def test_fetch_oracle_raises_on_error():
    def opener(url, timeout=0):
        raise OSError("connection refused")

    with pytest.raises(HarnessError):
        fetch_oracle("http://127.0.0.1:4400", opener=opener)


def test_render_report_lists_mismatches():
    rep = ParityReport(
        matched=1,
        mismatches=[FieldMismatch(("o/r", 1), "state", "open", "merged")],
    )
    text = render_report(rep)
    assert "state" in text and "open" in text and "merged" in text


def test_compute_candidate_by_number_classifies_checks():
    def fake_gh(args):
        body = json.dumps({"data": {"repository": {"pullRequests": {"nodes": [
            {"number": 7, "url": "https://github.com/o/r/pull/7",
             "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}},
        ]}}}})
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

    cand = compute_candidate_by_number(["o/r"], run_gh=fake_gh)
    assert ("o/r", 7) in cand
    assert cand[("o/r", 7)].checks == "passing"
    assert cand[("o/r", 7)].url.endswith("/pull/7")


def test_compute_candidate_by_number_skips_repo_on_gh_error():
    def boom(args):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="gh fail")

    assert compute_candidate_by_number(["o/r"], run_gh=boom) == {}
