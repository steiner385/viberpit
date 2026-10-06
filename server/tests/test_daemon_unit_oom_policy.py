# server/tests/test_daemon_unit_oom_policy.py
"""#2673: one session's OOM-killed tool must not take the whole daemon down.

systemd's default ``OOMPolicy=stop`` stops a unit when the kernel OOM-kills ANY process
in its cgroup — including on a host-wide (global) OOM. The daemon supervises every live
session, so on 2026-10-04 a single session's 5.9 GB ``node`` build got OOM-killed and
systemd then stopped the daemon, killing every other session with it. The killed
process's memory is already freed by then; stopping the siblings recovers nothing.
"""
from __future__ import annotations

import pytest

from server.deploy.steps import DEPLOY_DIR as DEPLOY, service_settings


@pytest.mark.parametrize("unit", ["viberpit-daemon.service", "viberpit-daemon-remote.service"])
def test_daemon_unit_survives_a_child_oom_kill(unit: str) -> None:
    settings = service_settings((DEPLOY / unit).read_text())
    assert settings.get("OOMPolicy") == ["continue"], (
        f"{unit} must set OOMPolicy=continue: the default (stop) turns one session's "
        "OOM-killed tool into a fleet-wide outage (#2673)"
    )
