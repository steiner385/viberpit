"""Fleet endpoints expose the registry and let an operator tune it."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _fake_request():
    """A stand-in for FastAPI's injected ``Request`` when a handler is called
    directly (not through a TestClient). ``request.app.state.sessions`` reads
    as absent, so ``drain_if_raised`` no-ops — these tests exercise the
    handler's own behavior, not the queue drain (covered separately by
    ``test_raising_a_host_cap_drains_the_session_queue``)."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


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


def test_max_sessions_null_is_ignored_when_not_explicitly_supplied():
    """Same call, but the omission is made explicit via an empty fields_set —
    the field is absent, not a caller-sent clear, so it stays untouched."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"maxSessions": None}, set()) == {}


def test_max_sessions_null_clears_the_cap_when_explicitly_supplied():
    """`maxSessions: null` is the only wire spelling for "uncapped" — a caller
    that explicitly sent it (tracked via fields_set, e.g. pydantic's
    ``model_fields_set``) must have it applied as a real clear, not dropped
    the way every other None is."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"maxSessions": None}, {"maxSessions"}) == {"max_sessions": None}


def test_other_fields_null_stays_not_supplied_even_when_explicitly_sent():
    """Only maxSessions has a NULL-means-clear wire contract — `enabled: null`
    explicitly sent must still be ignored, never write NULL onto a non-nullable
    column."""
    from server.api.fleet import sanitize_patch

    assert sanitize_patch({"enabled": None}, {"enabled"}) == {}


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
                          _fake_request(), _identity=_FakeIdentity())
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
        await create_host(body, _fake_request(), _identity=_FakeIdentity())
        return await create_host(body, _fake_request(), _identity=_FakeIdentity())

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
                          _fake_request(), _identity=_FakeIdentity())
        await delete_host("rocky", _fake_request(), _identity=_FakeIdentity())
        result = await list_hosts(_identity=_FakeIdentity())
        return result["hosts"]

    hosts = asyncio.run(run())
    assert hosts == []


def test_put_max_sessions_null_clears_an_existing_cap(tmp_path):
    """The web `FleetSection` "Max sessions" field clears to blank by sending
    `{"maxSessions": null}` (#capacity-governor-phase1 fix wave item 1) — that
    must set the cap back to NULL (uncapped), not 400 as "no writable fields"."""
    from server.db import init_db
    from server.api.fleet import HostPatch, create_host, update_host, HostCreate

    class _FakeIdentity:
        pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'clear.db'}")
        await create_host(HostCreate(name="rocky", endpoint="https://rocky:9443",
                                     maxSessions=4), _fake_request(), _identity=_FakeIdentity())
        return await update_host("rocky", HostPatch(maxSessions=None), _fake_request(),
                                 _identity=_FakeIdentity())

    wire = asyncio.run(run())
    assert wire["maxSessions"] is None


def test_put_empty_body_still_400s():
    """An omitted maxSessions (the field never appears in fields_set) must
    still count as no writable fields — only an explicit null clears it."""
    from fastapi import HTTPException

    from server.api.fleet import HostPatch, update_host

    class _FakeIdentity:
        pass

    try:
        asyncio.run(update_host("rocky", HostPatch(), _fake_request(), _identity=_FakeIdentity()))
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail["code"] == "no_writable_fields"


def test_delete_host_404_unknown(tmp_path):
    from fastapi import HTTPException

    from server.db import init_db
    from server.api.fleet import delete_host

    class _FakeIdentity:
        pass

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'del2.db'}")
        return await delete_host("ghost", _fake_request(), _identity=_FakeIdentity())

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


# --------------------------------------------------------------- queue drain


def test_raising_a_host_cap_drains_the_session_queue():
    """Physical ceiling = Σ host caps, so a host cap edit is a global-cap edit and must
    fill freed slots exactly as a settings PUT does (spec 2026-09-20 D3)."""
    from unittest.mock import AsyncMock, patch

    from server.tests.test_settings import IDENTITY_HEADERS, _client

    with _client() as client:
        r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                        json={"name": "rocky", "endpoint": "https://rocky:9443", "maxSessions": 4})
        assert r.status_code == 201, r.text
        with patch.object(client.app.state.sessions, "maybe_start_queued", new_callable=AsyncMock) as drain:
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"maxSessions": 8})
            assert drain.await_count == 1
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"maxSessions": 6})
            assert drain.await_count == 1, "lowering a cap frees nothing"
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"enabled": False})
            assert drain.await_count == 1
            client.put("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS, json={"enabled": True})
            assert drain.await_count == 2, "re-enabling a capped host restores its slots"


def test_deleting_an_uncapped_host_can_raise_the_effective_cap():
    """physical = None (rocky is uncapped) binds the DEFAULT floor of 5 even
    though dobby alone caps out at 10; deleting rocky lifts the ceiling to 10
    and must drain exactly like any other cap raise (F1)."""
    from unittest.mock import AsyncMock, patch

    from server.tests.test_settings import IDENTITY_HEADERS, _client

    with _client() as client:
        r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                        json={"name": "dobby", "endpoint": "https://dobby:9443", "maxSessions": 10})
        assert r.status_code == 201, r.text
        r = client.post("/api/fleet/hosts", headers=IDENTITY_HEADERS,
                        json={"name": "rocky", "endpoint": "https://rocky:9443"})
        assert r.status_code == 201, r.text

        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert gc["effective"] == 5, "rocky uncapped -> physical unbounded -> default floor binds"

        with patch.object(client.app.state.sessions, "maybe_start_queued", new_callable=AsyncMock) as drain:
            r = client.delete("/api/fleet/hosts/rocky", headers=IDENTITY_HEADERS)
            assert r.status_code == 204, r.text
            assert drain.await_count == 1

        gc = client.get("/api/settings", headers=IDENTITY_HEADERS).json()["effectiveDefaults"]["globalCap"]
        assert gc["effective"] == 10
