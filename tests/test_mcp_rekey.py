"""The pure re-key plan and grant identity (MCP auth overhaul phase 2, task 3)."""
import copy
import logging

import pytest

from viberpit import mcp_oauth_refresh as mor
from viberpit.mcp_keys import cli_oauth_key, prm_url_for, url_origin
from viberpit.mcp_rekey import RekeyPlan, WantedServer, plan_rekey

AWS_OLD_URL = "https://aws-mcp.us-east-1.api.aws/mcp"
AWS_NEW_URL = "https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize"
AWS_OLD_KEY = "aws-oauth|751831b0c9c1bf84"
AWS_NEW_KEY = "aws-oauth|c6c1c83162f8ce2d"
AWS_PRM = "https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp"
AWS_ID = ("https://signin.aws.amazon.com/", "https://aws-mcp.us-east-1.api.aws/mcp")


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    mor._identity_cache.clear()
    mor._identity_failed_until.clear()
    yield
    mor._identity_cache.clear()
    mor._identity_failed_until.clear()


def _grant(name, url, *, prm=AWS_PRM, access="t", refresh="r", expires=100):
    b = {"serverName": name, "serverUrl": url, "clientId": "cid", "accessToken": access,
         "expiresAt": expires, "discoveryState": {"authorizationServerUrl": "https://as.example/",
                                                  "resourceMetadataUrl": prm}}
    if refresh is not None:
        b["refreshToken"] = refresh
    if prm is None:
        del b["discoveryState"]["resourceMetadataUrl"]
    return b


def _discovery(name, url):
    """The token-less bundle the CLI writes under a server's key when it first connects."""
    return {"serverName": name, "serverUrl": url, "accessToken": "", "expiresAt": 0}


class _Identity:
    """Fake ``identity_of``: a dict lookup that records every call."""

    def __init__(self, table):
        self.table = table
        self.calls: list[str] = []

    def __call__(self, prm_url):
        self.calls.append(prm_url)
        return self.table.get(prm_url)


def test_cli_key_vectors_match_constants():
    assert cli_oauth_key("aws-oauth", {"type": "http", "url": AWS_OLD_URL}) == AWS_OLD_KEY
    assert cli_oauth_key("aws-oauth", {"type": "http", "url": AWS_NEW_URL}) == AWS_NEW_KEY
    assert prm_url_for(AWS_NEW_URL) == AWS_PRM


def test_moves_grant_to_new_key_after_url_edit():
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL),
             AWS_NEW_KEY: _discovery("aws-oauth", AWS_NEW_URL)}
    before = copy.deepcopy(store)
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)],
                      _Identity({AWS_PRM: AWS_ID}))
    assert isinstance(plan, RekeyPlan)
    assert plan.moved == {AWS_OLD_KEY: AWS_NEW_KEY}
    assert plan.dropped == frozenset()
    assert AWS_OLD_KEY not in plan.store
    moved = plan.store[AWS_NEW_KEY]
    assert moved["serverUrl"] == AWS_NEW_URL
    assert moved["serverName"] == "aws-oauth"
    assert moved["refreshToken"] == "r" and moved["clientId"] == "cid"
    assert store == before  # input untouched


def test_unknown_identity_never_moves():
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL),
             AWS_NEW_KEY: _discovery("aws-oauth", AWS_NEW_URL)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], _Identity({}))
    assert plan.moved == {}
    assert plan.dropped == frozenset()
    assert plan.store == store  # superseded but token-holding, identity unknown: kept


def test_wanted_key_absent_from_store_never_moves(caplog):
    """Key-drift guard: until the CLI writes a discovery bundle at the wanted key, our computed
    key is unproven, so the grant stays put and no identity is fetched."""
    caplog.set_level(logging.DEBUG, logger="viberpit.mcp_rekey")
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL)}
    ident = _Identity({AWS_PRM: AWS_ID})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.moved == {} and plan.dropped == frozenset()
    assert plan.store == store
    assert ident.calls == []
    [line] = [r for r in caplog.records if "not yet in store" in r.getMessage()]
    assert line.levelno == logging.DEBUG and AWS_NEW_KEY in line.getMessage()


def test_superseded_token_bundle_without_prm_kept():
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, prm=None)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)],
                      _Identity({AWS_PRM: AWS_ID}))
    assert plan.dropped == frozenset() and AWS_OLD_KEY in plan.store


@pytest.mark.parametrize("refresh,dropped", [("r", False), (None, True)])
def test_superseded_bundle_identity_differs_drops_only_without_refresh(refresh, dropped):
    other_prm = "https://elsewhere.example/.well-known/oauth-protected-resource/mcp"
    store = {AWS_OLD_KEY: _grant("aws-oauth", "https://elsewhere.example/mcp", prm=other_prm,
                                 refresh=refresh)}
    ident = _Identity({AWS_PRM: AWS_ID, other_prm: ("https://other-as/", "https://elsewhere.example/mcp")})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.moved == {}
    assert plan.dropped == (frozenset({AWS_OLD_KEY}) if dropped else frozenset())
    assert (AWS_OLD_KEY in plan.store) is not dropped


@pytest.mark.parametrize("refresh,dropped", [("r", False), (None, True)])
def test_superseded_bundle_wanted_usable_drops_only_without_refresh(refresh, dropped):
    """A refresh token is never deleted, even once the wanted key holds a live grant; an
    access-token-only superseded copy still goes."""
    store = {AWS_NEW_KEY: _grant("aws-oauth", AWS_NEW_URL),
             AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, refresh=refresh)}
    ident = _Identity({})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.dropped == (frozenset({AWS_OLD_KEY}) if dropped else frozenset())
    assert (AWS_OLD_KEY in plan.store) is not dropped
    assert plan.moved == {} and ident.calls == []


def test_superseded_tokenless_bundle_dropped():
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, access="", refresh=None)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], _Identity({}))
    assert plan.dropped == frozenset({AWS_OLD_KEY})


def test_cross_origin_donor_never_moves():
    evil_prm = "https://evil.example/.well-known/oauth-protected-resource/mcp"
    store = {"stray|eeeeeeeeeeeeeeee": _grant("stray", "https://evil.example/mcp", prm=evil_prm)}
    ident = _Identity({AWS_PRM: AWS_ID, evil_prm: AWS_ID})  # forged equal identity
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.moved == {}
    assert AWS_NEW_KEY not in plan.store


def test_same_name_donor_preferred_on_identity_tie():
    other = "aws-other|0000000000000003"
    store = {other: _grant("aws-other", AWS_OLD_URL, expires=99),
             AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, expires=1),
             AWS_NEW_KEY: _discovery("aws-oauth", AWS_NEW_URL)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)],
                      _Identity({AWS_PRM: AWS_ID}))
    assert plan.moved == {AWS_OLD_KEY: AWS_NEW_KEY}
    assert other in plan.store


def test_same_name_different_identity_never_moves_and_keeps_refresh_token():
    other_prm = "https://elsewhere.example/.well-known/oauth-protected-resource/mcp"
    discovery = _discovery("aws-oauth", AWS_NEW_URL)
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, prm=other_prm),
             AWS_NEW_KEY: discovery}
    ident = _Identity({AWS_PRM: AWS_ID, other_prm: ("https://other-as/", "https://elsewhere.example/mcp")})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.moved == {}
    assert plan.dropped == frozenset()  # refresh-bearing: never deleted
    assert plan.store == store


def test_same_name_different_identity_drops_access_only_copy():
    other_prm = "https://elsewhere.example/.well-known/oauth-protected-resource/mcp"
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, prm=other_prm, refresh=None)}
    ident = _Identity({AWS_PRM: AWS_ID, other_prm: ("https://other-as/", "https://elsewhere.example/mcp")})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.moved == {}
    assert plan.dropped == frozenset({AWS_OLD_KEY})
    assert plan.store == {}


def test_rename_follows_identity():
    new_key = cli_oauth_key("aws2", {"type": "http", "url": AWS_NEW_URL})
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL), new_key: _discovery("aws2", AWS_NEW_URL)}
    plan = plan_rekey(store, [WantedServer(new_key, "aws2", AWS_NEW_URL)], _Identity({AWS_PRM: AWS_ID}))
    assert plan.moved == {AWS_OLD_KEY: new_key}
    assert plan.store[new_key]["serverName"] == "aws2"
    assert plan.store[new_key]["serverUrl"] == AWS_NEW_URL
    assert AWS_OLD_KEY not in plan.store


def test_latest_expiry_donor_wins():
    k1, k2 = "aws-old-a|0000000000000001", "aws-old-b|0000000000000002"
    store = {k1: _grant("aws-old-a", AWS_OLD_URL, expires=5),
             k2: _grant("aws-old-b", AWS_OLD_URL, expires=9),
             AWS_NEW_KEY: _discovery("aws-oauth", AWS_NEW_URL)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)],
                      _Identity({AWS_PRM: AWS_ID}))
    assert plan.moved == {k2: AWS_NEW_KEY}
    assert k1 in plan.store


def test_drops_empty_bundles_only_of_disabled_plugins_or_configured_names():
    """An empty unwanted bundle goes only when provably dead: its plugin is not enabled, or
    its name is a configured server's (superseded). A plain unconfigured name may be a repo
    ``.mcp.json`` server, so it is kept."""
    dead = {
        "plugin:linear:linear|aaaaaaaaaaaaaaaa": {"serverName": "plugin:linear:linear",
                                                  "serverUrl": "https://mcp.linear.app/mcp",
                                                  "accessToken": "", "expiresAt": 0},
        "plugin:stripe:stripe|cccccccccccccccc": {"serverName": "plugin:stripe:stripe",
                                                  "serverUrl": "https://mcp.stripe.com",
                                                  "accessToken": ""},
        AWS_OLD_KEY: _discovery("aws-oauth", AWS_OLD_URL),
    }
    kept = {
        "projsrv|bbbbbbbbbbbbbbbb": {"serverName": "projsrv", "serverUrl": "https://proj.example/mcp",
                                     "accessToken": "", "expiresAt": 0, "clientId": "cid"},
        "plugin:Notion:notion|dddddddddddddddd": {"serverName": "plugin:Notion:notion",
                                                  "serverUrl": "https://mcp.notion.com/mcp",
                                                  "accessToken": ""},
    }
    ident = _Identity({})
    plan = plan_rekey({**dead, **kept}, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident,
                      enabled_plugins=frozenset({"Notion"}))
    assert plan.dropped == frozenset(dead)
    assert plan.store == kept
    assert ident.calls == []


def test_unconfigured_bundle_with_token_kept():
    store = {"repo-srv|dddddddddddddddd": _grant("repo-srv", "https://repo.example/mcp", refresh=None)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], _Identity({}))
    assert plan.store == store
    assert plan.dropped == frozenset() and plan.moved == {}


def test_wanted_live_key_untouched_and_no_identity_calls():
    store = {AWS_NEW_KEY: _grant("aws-oauth", AWS_NEW_URL),
             "repo-srv|dddddddddddddddd": _grant("repo-srv", "https://repo.example/mcp")}
    ident = _Identity({AWS_PRM: AWS_ID})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert ident.calls == []
    assert plan.store == store
    assert plan.moved == {} and plan.dropped == frozenset()


def test_grant_identity_normalises_and_caches():
    calls = []

    def fetch(url):
        calls.append(url)
        return {"resource": "https://mcp.railway.com",
                "authorization_servers": ["https://backboard.railway.com"]}

    prm = "https://mcp.railway.com/.well-known/oauth-protected-resource"
    assert mor.grant_identity(prm, fetch_json=fetch) == ("https://backboard.railway.com/",
                                                         "https://mcp.railway.com/")
    assert mor.grant_identity(prm, fetch_json=fetch) == ("https://backboard.railway.com/",
                                                         "https://mcp.railway.com/")
    assert len(calls) == 1

    def boom(url):
        calls.append(url)
        raise OSError("down")

    assert mor.grant_identity(prm, fetch_json=boom) == ("https://backboard.railway.com/",
                                                        "https://mcp.railway.com/")  # cached
    other = "https://x.example/.well-known/oauth-protected-resource"
    assert mor.grant_identity(other, fetch_json=boom) is None
    assert other not in mor._identity_cache
    mor._identity_failed_until.clear()  # the failure window is covered by its own test
    doc = {"resource": "https://x.example", "authorization_servers": ["https://as.example"]}
    assert mor.grant_identity(other, fetch_json=lambda _u: doc) == ("https://as.example/",
                                                                   "https://x.example/")


@pytest.mark.parametrize("doc", [None, {}, {"resource": "https://r/"},
                                 {"authorization_servers": ["https://a/"]},
                                 {"resource": "https://r/", "authorization_servers": []},
                                 {"resource": 3, "authorization_servers": ["https://a/"]}])
def test_grant_identity_missing_fields_is_none(doc):
    assert mor.grant_identity("https://p/", fetch_json=lambda _u: doc) is None
    assert "https://p/" not in mor._identity_cache


def test_grant_identity_rejects_cross_origin_resource():
    prm = "https://a.example/.well-known/oauth-protected-resource"
    doc = {"resource": "https://b.example/", "authorization_servers": ["https://as.example/"]}
    assert mor.grant_identity(prm, fetch_json=lambda _u: doc) is None
    assert prm not in mor._identity_cache


def test_url_origin():
    assert url_origin("HTTPS://Mcp.Railway.com") == ("https", "mcp.railway.com", 443)
    assert url_origin("http://h:8080/x?q") == ("http", "h", 8080)
    assert url_origin("https://h:443/") == url_origin("https://h")
    assert url_origin("ftp://h/") is None
    assert url_origin("not a url") is None


def test_grant_identity_failure_not_retried_within_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mor, "_monotonic", lambda: now[0])
    calls = []

    def boom(url):
        calls.append(url)
        raise OSError("down")

    doc = {"resource": "https://x.example", "authorization_servers": ["https://as.example"]}

    def ok(url):
        calls.append(url)
        return doc

    prm = "https://x.example/.well-known/oauth-protected-resource"
    assert mor.grant_identity(prm, fetch_json=boom) is None
    now[0] += mor.IDENTITY_FAILURE_TTL_S - 1
    assert mor.grant_identity(prm, fetch_json=ok) is None  # still in the failure window
    assert len(calls) == 1
    now[0] += 2
    assert mor.grant_identity(prm, fetch_json=ok) == ("https://as.example/", "https://x.example/")
    assert len(calls) == 2
    assert prm not in mor._identity_failed_until  # a success clears the failure entry
