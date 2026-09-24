"""Credential-authority startup must validate its native boundary before bind."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.codex import auth_main
from server.codex.auth_authority import InstanceLock
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
    unit = (Path(__file__).resolve().parents[2] / "deploy/viberpit-codex-auth.service").read_text()
    assert "ProtectHome=yes" in unit
    assert "ProtectHome=read-only" not in unit
    assert "SupplementaryGroups=" not in unit
    assert (
        "BindReadOnlyPaths=/home/tony/GitHub/viberpit-deploy:"
        "/opt/viberpit-codex-auth/app"
    ) in unit
    assert (
        "BindReadOnlyPaths=/home/tony/GitHub/viberpit-deploy/.venv:"
        "/opt/viberpit-codex-auth/venv"
    ) in unit
    assert "WorkingDirectory=/opt/viberpit-codex-auth/app" in unit
    assert (
        "Environment=PYTHONPATH=/opt/viberpit-codex-auth/app:"
        "/opt/viberpit-codex-auth/app/src"
    ) in unit
    assert (
        "ExecStart=/usr/bin/env /opt/viberpit-codex-auth/venv/bin/python "
        "-m server.codex.auth_main"
    ) in unit


def test_systemd_start_limit_is_in_unit_section():
    unit = (Path(__file__).resolve().parents[2] / "deploy/viberpit-codex-auth.service").read_text()
    unit_section, service_section = unit.split("[Service]", maxsplit=1)
    assert "StartLimitBurst=5" in unit_section
    assert "StartLimitIntervalSec=120" in unit_section
    assert "StartLimitBurst=" not in service_section
    assert "StartLimitIntervalSec=" not in service_section


@pytest.mark.asyncio
async def test_cancellation_during_shutdown_still_releases_the_instance_lock(
    monkeypatch, tmp_path
):
    """#2048: a cancellation landing in server.aclose() must not skip
    authority.aclose() or lock.release() — a leaked authority.lock makes the
    next start die with exit 3."""
    closed: list[str] = []
    in_server_aclose = asyncio.Event()

    class FakeServer:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self, *args):
            return None

        async def aclose(self):
            in_server_aclose.set()
            await asyncio.sleep(3600)  # the second cancellation lands here

    class FakeAuthority:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            closed.append("authority")

    monkeypatch.setattr(
        auth_main.auth_endpoint,
        "listen_endpoint",
        lambda: SimpleNamespace(host="127.0.0.1", port=1),
    )
    monkeypatch.setattr(auth_main.auth_endpoint, "authority_tls_files", lambda: object())
    monkeypatch.setattr(auth_main.auth_endpoint, "server_ssl_context", lambda tls: None)
    monkeypatch.setattr(
        auth_main.auth_endpoint, "client_allowlist", lambda: frozenset({"codex-edge"})
    )
    monkeypatch.setattr(auth_main.auth_endpoint, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        auth_main.codex_process,
        "probe_availability",
        lambda **kwargs: _async_value(
            SimpleNamespace(available=True, binary_path="/bin/true", reason=None, detail="")
        ),
    )
    monkeypatch.setattr(auth_main, "CredentialStore", lambda state_dir: object())
    monkeypatch.setattr(auth_main, "CredentialAuthority", FakeAuthority)
    monkeypatch.setattr(auth_main, "AuthorityServer", FakeServer)

    task = asyncio.create_task(auth_main.run())
    for _ in range(50):
        if (tmp_path / "authority.lock").exists():
            break
        await asyncio.sleep(0)
    task.cancel()  # ends stop.wait(), entering the shutdown finally
    await asyncio.wait_for(in_server_aclose.wait(), 1.0)
    task.cancel()  # lands mid server.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert closed == ["authority"]
    # flock is per open file: a fresh lock in this process conflicts unless released.
    relock = InstanceLock(tmp_path / "authority.lock")
    relock.acquire()
    relock.release()
