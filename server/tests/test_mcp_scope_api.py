"""/api/library/mcp/scope rules API: defaults, per-repo rules, validation, kill switch, prune on delete."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from server.mcp_scope import store
from server.tests.test_sessions_actions_api import _ENV, _H, _audit_events, _make_client

_KEYS = "server.api.library.configured_server_keys"
_REPOS = "server.api.library.known_repo_slugs"
BASE = "/api/library/mcp/scope"
PLUGIN = "plugin:Notion:notion"


def _env(**extra):
    return patch.dict(os.environ, {**_ENV, **extra})


def _ctx(keys=("gh", PLUGIN), repos=("acme/widgets",)):
    return _env(), patch(_KEYS, return_value=list(keys)), patch(_REPOS, AsyncMock(return_value=set(repos)))


def test_get_merges_defaults_for_unset_keys():
    client, _ = _make_client()
    e, k, r = _ctx()
    with e, k, r, client:
        client.portal.call(store.set_default, "gh", False)
        client.portal.call(store.set_repo_rule, "Acme/Widgets", "gh", True)
        body = client.get(BASE, headers=_H).json()
    assert body["servers"] == {"gh": {"defaultEnabled": False}, PLUGIN: {"defaultEnabled": True}}
    assert body["rules"] == [{"repo": "acme/widgets", "server": "gh", "enabled": True}]


def test_put_default_round_trips_plugin_key_and_audits_server_only():
    client, _ = _make_client()
    e, k, r = _ctx()
    with e, k, r, client:
        resp = client.put(f"{BASE}/plugin%3ANotion%3Anotion", json={"defaultEnabled": False}, headers=_H)
        assert resp.status_code == 204
        assert client.get(BASE, headers=_H).json()["servers"][PLUGIN] == {"defaultEnabled": False}
        events = client.portal.call(_audit_events)
    saved = [e for e in events if e[0] == "library_mcp_scope_saved"]
    assert [x[2] for x in saved] == [{"server": PLUGIN}]


def test_put_rule_round_trips_normalises_case_and_audits():
    client, _ = _make_client()
    e, k, r = _ctx()
    with e, k, r, client:
        resp = client.put(f"{BASE}/gh/repos/Acme/Widgets", json={"enabled": False}, headers=_H)
        assert resp.status_code == 204
        rules = client.get(BASE, headers=_H).json()["rules"]
        events = client.portal.call(_audit_events)
    assert rules == [{"repo": "acme/widgets", "server": "gh", "enabled": False}]
    assert [x[2] for x in events if x[0] == "library_mcp_scope_saved"] == [
        {"server": "gh", "repo": "acme/widgets"}]


def test_delete_rule_then_404_when_absent():
    client, _ = _make_client()
    e, k, r = _ctx()
    with e, k, r, client:
        client.put(f"{BASE}/gh/repos/acme/widgets", json={"enabled": True}, headers=_H)
        assert client.delete(f"{BASE}/gh/repos/acme/widgets", headers=_H).status_code == 204
        assert client.delete(f"{BASE}/gh/repos/acme/widgets", headers=_H).status_code == 404


def test_unknown_server_400_and_unknown_repo_400():
    client, _ = _make_client()
    e, k, r = _ctx()
    with e, k, r, client:
        a = client.put(f"{BASE}/nope", json={"defaultEnabled": True}, headers=_H)
        b = client.put(f"{BASE}/nope/repos/acme/widgets", json={"enabled": True}, headers=_H)
        c = client.put(f"{BASE}/gh/repos/acme/other", json={"enabled": True}, headers=_H)
        d = client.delete(f"{BASE}/gh/repos/acme/other", headers=_H)
    assert a.status_code == b.status_code == 400
    assert a.json()["detail"] == {"error": "unknown_server"}
    assert c.status_code == d.status_code == 400
    assert c.json()["detail"] == {"error": "unknown_repo"}


@pytest.mark.parametrize("path", [
    "gh/repos/../widgets", "gh/repos/./widgets", "gh/repos/acme/..", "gh/repos/acme/.",
    "gh/repos/a%2Fb/widgets", "gh/repos//widgets", "gh/repos/ac%20me/widgets", "gh/repos/acme/wid%00gets",
])
def test_traversal_shaped_segments_never_reach_store_or_known_repos(path):
    client, _ = _make_client()
    boom = AsyncMock(side_effect=AssertionError("reached"))
    e, k, _r = _ctx()
    with e, k, patch(_REPOS, boom), client, \
            patch.object(store, "set_repo_rule", boom), patch.object(store, "delete_repo_rule", boom):
        for method, kw in (("put", {"json": {"enabled": True}}), ("delete", {})):
            resp = getattr(client, method)(f"{BASE}/{path}", headers=_H, **kw)
            assert resp.status_code in (400, 404, 405, 307), (path, resp.status_code)
    boom.assert_not_called()


def test_dot_segments_rejected_by_validator_directly():
    # An HTTP client may normalise `..` away before the request; hit the validator with the raw segments.
    import asyncio
    from fastapi import HTTPException
    from server.api import library
    boom = AsyncMock(side_effect=AssertionError("reached"))
    with patch(_REPOS, boom):
        for owner, name in (("..", "w"), (".", "w"), ("a", ".."), ("a", "."), ("a/b", "w"), ("", "w")):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(library._scope_repo_or_400(None, owner, name))
            assert ei.value.status_code == 400
    boom.assert_not_called()


def test_kill_switch_404_on_every_route():
    client, _ = _make_client()
    e, k, r = _ctx()
    with _env(VIBERPIT_MCP_SCOPE_ENABLED="false"), k, r, client:
        resps = [
            client.get(BASE, headers=_H),
            client.put(f"{BASE}/gh", json={"defaultEnabled": True}, headers=_H),
            client.put(f"{BASE}/gh/repos/acme/widgets", json={"enabled": True}, headers=_H),
            client.delete(f"{BASE}/gh/repos/acme/widgets", headers=_H),
        ]
    assert all(x.status_code == 404 and x.json() == {"error": "mcp_scope_disabled"} for x in resps)


def test_deleting_a_server_prunes_its_scope_rows():
    client, _ = _make_client()
    e, k, r = _ctx()
    with e, k, r, client, patch("server.api.library.mutate.delete_mcp_server", return_value=True):
        client.put(f"{BASE}/gh", json={"defaultEnabled": False}, headers=_H)
        client.put(f"{BASE}/gh/repos/acme/widgets", json={"enabled": True}, headers=_H)
        assert client.delete("/api/library/mcp/gh", headers=_H).status_code == 204
        assert client.portal.call(store.all_rules) == []
        assert client.portal.call(store.server_defaults) == {}


def test_known_repo_slugs_lowercases_and_shares_last_good_cache():
    import asyncio
    from types import SimpleNamespace
    from viberpit.gitrepos import GhError
    from server.api.repos import known_repo_slugs
    state = SimpleNamespace()
    with patch("viberpit.gitrepos.list_accessible_repos",
               return_value=[{"nameWithOwner": "Acme/Widgets"}, {}]):
        assert asyncio.run(known_repo_slugs(state)) == {"acme/widgets"}
    with patch("viberpit.gitrepos.list_accessible_repos", side_effect=GhError("rate limited")):
        assert asyncio.run(known_repo_slugs(state)) == {"acme/widgets"}
