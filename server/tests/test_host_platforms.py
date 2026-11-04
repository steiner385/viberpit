"""A runner advertises which host platforms it can run work for (`/info.platforms`)."""
from __future__ import annotations

import asyncio
import time

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


class _Check:
    """A check the test releases by hand, so refresh timing is observable."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0
        self.gate = asyncio.Event()

    async def __call__(self, platform: str) -> bool:
        assert platform == "windows"
        self.calls += 1
        await self.gate.wait()
        return self.result


async def _settle(probe: PlatformProbe) -> None:
    if probe._refresh is not None:
        await probe._refresh


WIN = frozenset({"linux", "windows"})


@pytest.mark.asyncio
async def test_advertised_never_waits_on_the_check():
    check = _Check()
    probe = PlatformProbe(check=check, clock=_Clock())
    # The check is blocked forever; advertised() must still answer at once.
    got = await asyncio.wait_for(probe.advertised(WIN), timeout=1)
    assert got == {"linux"}  # no verdict yet: windows is not advertised
    await asyncio.sleep(0)
    assert check.calls == 1
    check.gate.set()
    await probe.aclose()


@pytest.mark.asyncio
async def test_verdict_flips_after_the_refresh_completes():
    check = _Check(True)
    probe = PlatformProbe(check=check, clock=_Clock())
    assert await probe.advertised(WIN) == {"linux"}
    check.gate.set()
    await _settle(probe)
    assert await probe.advertised(WIN) == WIN


@pytest.mark.asyncio
async def test_failed_check_keeps_windows_out():
    check = _Check(False)
    check.gate.set()
    probe = PlatformProbe(check=check, clock=_Clock())
    await probe.advertised(WIN)
    await _settle(probe)
    assert await probe.advertised(WIN) == {"linux"}


@pytest.mark.asyncio
async def test_a_raising_check_counts_as_down():
    async def boom(platform: str) -> bool:
        raise RuntimeError("ssh exploded")

    probe = PlatformProbe(check=boom, clock=_Clock())
    await probe.advertised(WIN)
    await _settle(probe)
    assert await probe.advertised(WIN) == {"linux"}


@pytest.mark.asyncio
async def test_at_most_one_refresh_in_flight():
    check = _Check()
    probe = PlatformProbe(check=check, clock=_Clock())
    for _ in range(5):
        await probe.advertised(WIN)
        await asyncio.sleep(0)
    assert check.calls == 1
    check.gate.set()
    await probe.aclose()


@pytest.mark.asyncio
async def test_cache_holds_for_ttl_then_refreshes_in_background():
    clock = _Clock()
    check = _Check(True)
    check.gate.set()
    probe = PlatformProbe(check=check, clock=clock)
    await probe.advertised(WIN)
    await _settle(probe)
    await probe.advertised(WIN)
    assert check.calls == 1  # fresh: no new check
    clock.now += 61
    # Stale: the old verdict is still served immediately while a refresh starts.
    assert await probe.advertised(WIN) == WIN
    await _settle(probe)
    assert check.calls == 2


@pytest.mark.asyncio
async def test_probe_never_checks_linux_only():
    check = _Check()
    probe = PlatformProbe(check=check, clock=_Clock())
    assert await probe.advertised(frozenset({"linux"})) == {"linux"}
    await asyncio.sleep(0)
    assert check.calls == 0


@pytest.mark.asyncio
async def test_probe_passes_macos_through_unchecked():
    check = _Check(False)
    probe = PlatformProbe(check=check, clock=_Clock())
    assert await probe.advertised(frozenset({"linux", "macos"})) == {"linux", "macos"}
    await asyncio.sleep(0)
    assert check.calls == 0


@pytest.mark.asyncio
async def test_aclose_reaps_an_in_flight_refresh():
    check = _Check()
    probe = PlatformProbe(check=check, clock=_Clock())
    await probe.advertised(WIN)
    await asyncio.sleep(0)
    task = probe._refresh
    await probe.aclose()
    assert task is not None and task.done()


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
    monkeypatch.setenv("VIBERPIT_HOST_PLATFORMS", "windows")
    with TestClient(create_daemon_app()) as client:
        # First poll answers at once without windows; the background check then lands.
        assert client.get("/info").json()[PLATFORMS_INFO_KEY] == ["linux"]
        deadline = time.monotonic() + 5
        body = client.get("/info").json()
        while body[PLATFORMS_INFO_KEY] != expected and time.monotonic() < deadline:
            time.sleep(0.02)
            body = client.get("/info").json()
        assert body[PLATFORMS_INFO_KEY] == expected


@pytest.mark.asyncio
async def test_ssh_vm_check_is_false_when_ssh_is_missing(monkeypatch):
    from server.sessions.host_platforms import ssh_vm_check

    monkeypatch.setenv("PATH", "/nonexistent")
    assert await ssh_vm_check("win11") is False
