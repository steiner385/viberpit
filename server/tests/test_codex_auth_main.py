"""Credential-authority startup must validate its native boundary before bind."""
from __future__ import annotations

from pathlib import Path

import pytest

from server.codex import auth_main
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason


@pytest.mark.asyncio
async def test_startup_refuses_an_unverified_codex_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_main.auth_endpoint, "listen_endpoint", lambda: object())
    monkeypatch.setattr(auth_main.auth_endpoint, "authority_tls_files", lambda: object())
    monkeypatch.setattr(
        auth_main.auth_endpoint, "client_allowlist", lambda: frozenset({"codex-edge"})
    )
    monkeypatch.setattr(auth_main.auth_endpoint, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        auth_main.codex_process,
        "probe_availability",
        lambda **kwargs: _async_value(
            CodexAvailability.unavailable(
                CodexUnavailableReason.UNSUPPORTED_VERSION, "schema mismatch"
            )
        ),
    )

    assert await auth_main.run() == 4
    assert not (tmp_path / "authority.lock").exists()


async def _async_value(value):
    return value


def test_systemd_sandbox_exposes_only_read_only_runtime_mounts_outside_home():
    unit = (Path(__file__).resolve().parents[2] / "deploy/ccswitch-codex-auth.service").read_text()
    assert "ProtectHome=yes" in unit
    assert "ProtectHome=read-only" not in unit
    assert "SupplementaryGroups=" not in unit
    assert (
        "BindReadOnlyPaths=/home/tony/GitHub/cc-account-switcher-deploy:"
        "/opt/ccswitch-codex-auth/app"
    ) in unit
    assert (
        "BindReadOnlyPaths=/home/tony/GitHub/cc-account-switcher-deploy/.venv:"
        "/opt/ccswitch-codex-auth/venv"
    ) in unit
    assert "WorkingDirectory=/opt/ccswitch-codex-auth/app" in unit
    assert (
        "Environment=PYTHONPATH=/opt/ccswitch-codex-auth/app:"
        "/opt/ccswitch-codex-auth/app/src"
    ) in unit
    assert (
        "ExecStart=/usr/bin/env /opt/ccswitch-codex-auth/venv/bin/python "
        "-m server.codex.auth_main"
    ) in unit


def test_systemd_start_limit_is_in_unit_section():
    unit = (Path(__file__).resolve().parents[2] / "deploy/ccswitch-codex-auth.service").read_text()
    unit_section, service_section = unit.split("[Service]", maxsplit=1)
    assert "StartLimitBurst=5" in unit_section
    assert "StartLimitIntervalSec=120" in unit_section
    assert "StartLimitBurst=" not in service_section
    assert "StartLimitIntervalSec=" not in service_section
