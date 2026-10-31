from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from server.github.aws_alarms import alarms_in_alarm


def test_alarms_in_alarm_empty_prefix_runs_nothing():
    def run(*a, **k):
        raise AssertionError("must not run")
    assert alarms_in_alarm("", "p", run=run) == []


def test_alarms_in_alarm_parses_metric_alarms():
    calls: list = []

    def run(argv, **k):
        calls.append((argv, k))
        out = {"MetricAlarms": [{"AlarmName": "kd-a", "StateReason": "r",
                                 "StateUpdatedTimestamp": "2026-10-31T01:00:00Z"}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
    got = alarms_in_alarm("kd-", "cairnea", run=run)
    assert got == [{"name": "kd-a", "reason": "r", "updated": "2026-10-31T01:00:00Z"}]
    argv, kw = calls[0]
    assert argv == ["aws", "cloudwatch", "describe-alarms", "--alarm-name-prefix", "kd-",
                    "--state-value", "ALARM", "--output", "json", "--profile", "cairnea",
                    "--region", "us-east-1"]
    assert kw["timeout"] == 30 and not kw.get("shell")


def test_alarms_in_alarm_cli_missing_returns_none():
    def missing(*a, **k):
        raise FileNotFoundError("aws")
    assert alarms_in_alarm("kd-", "", run=missing) is None

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("aws", 30)
    assert alarms_in_alarm("kd-", "", run=timeout) is None
    bad = lambda *a, **k: SimpleNamespace(returncode=0, stdout="nope", stderr="")  # noqa: E731
    assert alarms_in_alarm("kd-", "", run=bad) is None
    fail = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="x")  # noqa: E731
    assert alarms_in_alarm("kd-", "", run=fail) is None
