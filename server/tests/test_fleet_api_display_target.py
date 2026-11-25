"""Fleet API: per-host display target / display ssh host / read-only bridge snapshot."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from server.api.fleet import (
    HostCreate, HostPatch, create_host, list_hosts, sanitize_patch, update_host,
)
from server.sessions.display_target import accepted_sources, parse_display_target

_REQ = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


class _Id:
    pass


@pytest.fixture(autouse=True)
def _db(isolated_db):
    return isolated_db


async def _mk(*names):
    for n in names:
        await create_host(HostCreate(name=n, endpoint=f"https://{n}:9443"),
                          request=_REQ, _identity=_Id())


async def _put(name, **kw):
    return await update_host(name, HostPatch(**kw), request=_REQ, _identity=_Id())


def test_put_display_target_roundtrips():
    asyncio.run(_test_put_display_target_roundtrips())


async def _test_put_display_target_roundtrips():
    await _mk("rocky", "dobby")
    w = await _put("rocky", displayTarget="host:dobby", displaySshHost="tony@dobby")
    assert w["displayTarget"] == "host:dobby"
    assert w["displaySshHost"] == "tony@dobby"
    assert w["displayBridge"] is None
    listed = {h["name"]: h for h in (await list_hosts(_identity=_Id()))["hosts"]}
    assert listed["rocky"]["displayTarget"] == "host:dobby"


def test_display_target_self_rejected_422():
    asyncio.run(_test_display_target_self_rejected_422())


async def _test_display_target_self_rejected_422():
    await _mk("rocky")
    with pytest.raises(HTTPException) as exc:
        await _put("rocky", displayTarget="host:rocky")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_display_target"


def test_display_target_unknown_host_rejected_422():
    asyncio.run(_test_display_target_unknown_host_rejected_422())


async def _test_display_target_unknown_host_rejected_422():
    await _mk("rocky")
    with pytest.raises(HTTPException) as exc:
        await _put("rocky", displayTarget="host:ghost")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_display_target"


@pytest.mark.parametrize("local", ["local", "", "  local "])
def test_display_target_local_normalizes_to_null(local):
    asyncio.run(_test_display_target_local_normalizes_to_null(local))


async def _test_display_target_local_normalizes_to_null(local):
    await _mk("rocky", "dobby")
    await _put("rocky", displayTarget="host:dobby")
    w = await _put("rocky", displayTarget=local)
    assert w["displayTarget"] is None


def test_display_target_windows_prefix_rejected_422():
    asyncio.run(_test_display_target_windows_prefix_rejected_422())


async def _test_display_target_windows_prefix_rejected_422():
    await _mk("rocky")
    with pytest.raises(HTTPException) as exc:
        await _put("rocky", displayTarget="windows:win11")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_display_target"


@pytest.mark.parametrize("bad", ["rocky; rm -rf /", "@rocky", "user@-rocky"])
def test_display_ssh_host_validated_with_SSH_HOST_RE(bad):
    with pytest.raises(HTTPException) as exc:
        sanitize_patch({"displaySshHost": bad})
    assert exc.value.detail["code"] == "invalid_display_ssh_host"
    assert sanitize_patch({"displaySshHost": "tony@dobby"}) == {"display_ssh_host": "tony@dobby"}


def test_display_bridge_is_read_only():
    assert sanitize_patch({"displayBridge": {"accepts": []}}) == {}


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("local", None), ("  local  ", None),
    ("host:dobby", "dobby"), ("host:win-1.lan", "win-1.lan"),
])
def test_parse_display_target_grammar(value, expected):
    assert parse_display_target(value) == expected


@pytest.mark.parametrize("bad", [
    "dobby", "host:", "host:-x", "host:a b", "host:a;b", "windows:win11", "Host:dobby", "host:a:b",
])
def test_parse_display_target_rejects(bad):
    with pytest.raises(ValueError):
        parse_display_target(bad)


def test_accepted_sources_parsing():
    s = SimpleNamespace(display_bridge_accept_from=" Rocky, dobby ,,")
    assert accepted_sources(s) == frozenset({"rocky", "dobby"})
    assert accepted_sources(SimpleNamespace(display_bridge_accept_from="")) == frozenset()
