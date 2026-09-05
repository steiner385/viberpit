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


def test_systemd_sandbox_keeps_the_checkout_visible_read_only():
    unit = (Path(__file__).resolve().parents[2] / "deploy/ccswitch-codex-auth.service").read_text()
    assert "ProtectHome=read-only" in unit
    assert "ProtectHome=yes" not in unit
