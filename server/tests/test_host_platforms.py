"""A runner advertises which host platforms it can run work for (`/info.platforms`)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


from server.ipc.daemon import create_daemon_app
from server.sessions.host_platforms import (
    PLATFORMS_INFO_KEY,
    PlatformProbe,
    build_platforms_info,
    configured_platforms,
    parse_platforms_info,
    platform_dispatchable,
)


def test_configured_platforms_normalizes_and_drops_unknown():
    assert configured_platforms("windows, ,WINDOWS,beos") == {"linux", "windows"}
    assert configured_platforms(None) == {"linux"}
    assert configured_platforms("") == {"linux"}


def test_parse_platforms_info_legacy_absent_is_linux():
    assert parse_platforms_info(None) == {"linux"}
    assert parse_platforms_info("x") == {"linux"}
    assert parse_platforms_info([]) == {"linux"}
    assert parse_platforms_info(["windows"]) == {"linux", "windows"}
    assert parse_platforms_info(["windows", "beos", 3]) == {"linux", "windows"}


def test_build_parse_round_trip():
    built = build_platforms_info(frozenset({"windows", "linux"}))
    assert built == ["linux", "windows"]
    assert parse_platforms_info(built) == {"linux", "windows"}


def test_platform_dispatchable():
    assert platform_dispatchable(frozenset({"linux"}), None)
    assert platform_dispatchable(frozenset({"linux"}), "linux")
    assert not platform_dispatchable(frozenset({"linux"}), "windows")
    assert platform_dispatchable(frozenset({"linux", "windows"}), "windows")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _probe(result: bool, clock=None):
    calls: list[str] = []

    async def check(platform: str) -> bool:
        calls.append(platform)
        return result

    return PlatformProbe(check=check, clock=clock or _Clock()), calls


@pytest.mark.asyncio
async def test_probe_drops_windows_when_check_fails():
    probe, _ = _probe(False)
    assert await probe.advertised(frozenset({"linux", "windows"})) == {"linux"}


@pytest.mark.asyncio
async def test_probe_keeps_windows_when_check_passes():
    probe, calls = _probe(True)
    assert await probe.advertised(frozenset({"linux", "windows"})) == {"linux", "windows"}
    assert calls == ["windows"]


@pytest.mark.asyncio
async def test_probe_caches_for_ttl():
    clock = _Clock()
    probe, calls = _probe(True, clock)
    cfg = frozenset({"linux", "windows"})
    await probe.advertised(cfg)
    await probe.advertised(cfg)
    assert calls == ["windows"]
    clock.now += 61
    await probe.advertised(cfg)
    assert calls == ["windows", "windows"]


@pytest.mark.asyncio
async def test_probe_never_checks_linux_only():
    probe, calls = _probe(True)
    assert await probe.advertised(frozenset({"linux"})) == {"linux"}
    assert calls == []


@pytest.mark.asyncio
async def test_probe_passes_macos_through_unchecked():
    probe, calls = _probe(False)
    assert await probe.advertised(frozenset({"linux", "macos"})) == {"linux", "macos"}
    assert calls == []


@pytest.mark.asyncio
async def test_probe_serialises_concurrent_stale_callers():
    calls = 0

    async def check(platform: str) -> bool:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return True

    probe = PlatformProbe(check=check, clock=_Clock())
    cfg = frozenset({"linux", "windows"})
    await asyncio.gather(*(probe.advertised(cfg) for _ in range(5)))
    assert calls == 1


def _info(monkeypatch: pytest.MonkeyPatch, platforms: str) -> dict:
    monkeypatch.setenv("VIBERPIT_HOST_PLATFORMS", platforms)
    app = create_daemon_app()
    with TestClient(app) as client:
        return client.get("/info").json()


def test_info_payload_carries_platforms(monkeypatch):
    body = _info(monkeypatch, "")
    assert body[PLATFORMS_INFO_KEY] == ["linux"]


@pytest.mark.parametrize("vm_up,expected", [(True, ["linux", "windows"]), (False, ["linux"])])
def test_info_advertises_windows_only_while_the_vm_answers(monkeypatch, vm_up, expected):
    async def fake_check(host: str) -> bool:
        assert host == "win11"
        return vm_up

    monkeypatch.setattr("server.sessions.host_platforms.ssh_vm_check", fake_check)
    assert _info(monkeypatch, "windows")[PLATFORMS_INFO_KEY] == expected


@pytest.mark.asyncio
async def test_ssh_vm_check_is_false_when_ssh_is_missing(monkeypatch):
    from server.sessions.host_platforms import ssh_vm_check

    monkeypatch.setenv("PATH", "/nonexistent")
    assert await ssh_vm_check("win11") is False
