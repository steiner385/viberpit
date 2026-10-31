"""CloudWatch alarm read for the CI/CD Doctor's systemic-red evidence. Shells to
the aws CLI with an argv list (no shell) and a timeout; every failure is None so
the caller treats "unknown" as "no evidence"."""
from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger(__name__)

_TIMEOUT_S = 30


def alarms_in_alarm(prefix: str, profile: str, *, run=subprocess.run) -> list[dict] | None:
    """``[{name, reason, updated}]`` for alarms in ALARM whose name starts with
    ``prefix``. ``[]`` (nothing run) for an empty prefix; None when the CLI is
    missing, fails, times out or prints bad JSON."""
    if not prefix:
        return []
    argv = ["aws", "cloudwatch", "describe-alarms", "--alarm-name-prefix", prefix,
            "--state-value", "ALARM", "--output", "json"]
    if profile:
        argv += ["--profile", profile]
    argv += ["--region", "us-east-1"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S)
        if proc.returncode != 0:
            log.warning("cicd.aws_alarms_failed: %s", (proc.stderr or "").strip()[:200])
            return None
        data = json.loads(proc.stdout)
        return [{"name": a.get("AlarmName", ""), "reason": a.get("StateReason", ""),
                 "updated": a.get("StateUpdatedTimestamp", "")}
                for a in data.get("MetricAlarms", [])]
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError, AttributeError):
        log.warning("cicd.aws_alarms_unavailable", exc_info=True)
        return None
