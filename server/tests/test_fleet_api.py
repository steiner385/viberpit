"""Fleet endpoints expose the registry and let an operator tune it."""
from __future__ import annotations

import asyncio


def test_host_to_wire_shape():
    from datetime import datetime

    from server.api.fleet import host_to_wire
    from server.db.models import Host

    h = Host(name="rocky", endpoint="https://rocky:9443", enabled=True, weight=100,
             max_sessions=4, healthy=True, load_avg=1.25, mem_available_mb=2048,
             active_sessions=2, sha="abc1234", protocol=1,
             last_probe_at=datetime(2026, 8, 21, 12, 0, 0), last_error=None)
    w = host_to_wire(h)
    assert w["name"] == "rocky"
    assert w["maxSessions"] == 4
    assert w["memAvailableMb"] == 2048
    assert w["healthy"] is True
    assert isinstance(w["lastProbeAt"], str), "datetime must serialize for JSON"


def test_unprobed_host_reports_null_not_false():
    """healthy=None means 'never probed'. Collapsing it to false in the wire
    format would tell the operator a host is DOWN when it is merely unknown."""
    from server.api.fleet import host_to_wire
    from server.db.models import Host

    w = host_to_wire(Host(name="new", endpoint="unix:///a"))
    assert w["healthy"] is None
    assert w["lastProbeAt"] is None


def test_patch_rejects_unknown_fields():
    """Only operator-tunable fields may be written; probe-owned state must never
    be settable through the API, or the UI could lie to the ranker."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"enabled": False, "weight": 50, "maxSessions": 3}) == {
        "enabled": False, "weight": 50, "max_sessions": 3}
    assert sanitize_patch({"healthy": True, "loadAvg": 0.0, "sha": "x"}) == {}


def test_weight_is_clamped_to_a_sane_range():
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"weight": -5})["weight"] == 1
    assert sanitize_patch({"weight": 10_000})["weight"] == 1000


def test_none_values_are_ignored_not_written():
    """A PATCH omitting a field must leave it alone, not null it out."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"enabled": None, "weight": None, "maxSessions": None}) == {}


def test_create_host_rejects_blank_name():
    from pydantic import ValidationError
    from server.api.fleet import HostCreate

    try:
        HostCreate(name="  ", endpoint="https://rocky:9443")
        assert False, "expected ValidationError"
    except ValidationError:
        pass


def test_create_host_rejects_blank_endpoint():
    from pydantic import ValidationError
    from server.api.fleet import HostCreate

    try:
        HostCreate(name="rocky", endpoint="")
        assert False, "expected ValidationError"
    except ValidationError:
        pass


def test_create_and_list_host(tmp_path):

    from server.db import init_db
    from server.api.fleet import create_host, list_hosts, HostCreate

    class _FakeIdentity:
        pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ch.db'}")
        await create_host(HostCreate(name="rocky", endpoint="https://rocky:9443"),
                          _identity=_FakeIdentity())
        result = await list_hosts(_identity=_FakeIdentity())
        return result["hosts"]

    hosts = asyncio.run(run())
    assert len(hosts) == 1
    assert hosts[0]["name"] == "rocky"
    assert hosts[0]["endpoint"] == "https://rocky:9443"
    assert hosts[0]["healthy"] is None


def test_create_host_409_on_duplicate(tmp_path):
    from fastapi import HTTPException

    from server.db import init_db
    from server.api.fleet import create_host, HostCreate

    class _FakeIdentity:
        pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'dup.db'}")
        body = HostCreate(name="rocky", endpoint="https://rocky:9443")
        await create_host(body, _identity=_FakeIdentity())
        return await create_host(body, _identity=_FakeIdentity())

    try:
        asyncio.run(run())
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 409


def test_delete_host(tmp_path):

    from server.db import init_db
    from server.api.fleet import create_host, delete_host, list_hosts, HostCreate

    class _FakeIdentity:
        pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'del.db'}")
        await create_host(HostCreate(name="rocky", endpoint="https://rocky:9443"),
                          _identity=_FakeIdentity())
        await delete_host("rocky", _identity=_FakeIdentity())
        result = await list_hosts(_identity=_FakeIdentity())
        return result["hosts"]

    hosts = asyncio.run(run())
    assert hosts == []


def test_delete_host_404_unknown(tmp_path):
    from fastapi import HTTPException

    from server.db import init_db
    from server.api.fleet import delete_host

    class _FakeIdentity:
        pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'del2.db'}")
        return await delete_host("ghost", _identity=_FakeIdentity())

    try:
        asyncio.run(run())
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


# --------------------------------------------------------------- engine wire


_CODEX_UP = {
    "claude_code": {"available": True},
    "codex": {"available": True, "cliVersion": "codex-cli 0.153.2",
              "contractSha256": "moved"},
}


def test_host_wire_lists_every_known_engine_even_for_a_legacy_runner():
    """A missing row would read as "the page failed", not as "an old runner"."""
    from server.api.fleet import host_to_wire
    from server.db.models import Host

    engines = {e["engine"]: e for e in host_to_wire(
        Host(name="rocky", endpoint="unix:///a"))["engines"]}
    assert set(engines) == {"claude_code", "codex"}
    assert engines["codex"]["available"] is False
    assert engines["codex"]["reason"] == "engine_not_advertised"
    assert engines["claude_code"]["dispatchable"] is True


def test_host_wire_separates_what_the_runner_said_from_what_the_edge_would_do():
    """A runner on a contract this edge is not pinned to is available, not dispatchable.

    Collapsing the two would hide which side of a mixed-version fleet to upgrade.
    """
    from server.api.fleet import host_to_wire
    from server.db.models import Host

    engines = {e["engine"]: e for e in host_to_wire(
        Host(name="rocky", endpoint="unix:///a", engines=_CODEX_UP))["engines"]}
    assert engines["codex"]["available"] is True
    assert engines["codex"]["cliVersion"] == "codex-cli 0.153.2"
    assert engines["codex"]["dispatchable"] is False
    assert engines["codex"]["dispatchReason"] == "contract_hash_mismatch"


def test_engine_state_is_not_operator_writable():
    """Probe-owned state must never be settable, or the UI could lie to the ranker."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"engines": {"codex": {"available": True}}}) == {}
