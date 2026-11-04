"""The pure re-key plan and grant identity (MCP auth overhaul phase 2, task 3)."""
import copy

import pytest

from viberpit import mcp_oauth_refresh as mor
from viberpit.mcp_keys import cli_oauth_key, prm_url_for
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
    yield
    mor._identity_cache.clear()


def _grant(name, url, *, prm=AWS_PRM, access="t", refresh="r", expires=100):
    b = {"serverName": name, "serverUrl": url, "clientId": "cid", "accessToken": access,
         "expiresAt": expires, "discoveryState": {"authorizationServerUrl": "https://as.example/",
                                                  "resourceMetadataUrl": prm}}
    if refresh is not None:
        b["refreshToken"] = refresh
    return b


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
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL)}
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
    store = {AWS_OLD_KEY: _grant("other-name", AWS_OLD_URL)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], _Identity({}))
    assert plan.moved == {}
    assert AWS_NEW_KEY not in plan.store
    assert AWS_OLD_KEY in plan.store  # unconfigured name with a token is kept


def test_same_name_different_identity_drops_not_moves():
    other_prm = "https://elsewhere.example/.well-known/oauth-protected-resource/mcp"
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL, prm=other_prm)}
    ident = _Identity({AWS_PRM: AWS_ID, other_prm: ("https://other-as/", "https://elsewhere.example/mcp")})
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)], ident)
    assert plan.moved == {}
    assert plan.dropped == frozenset({AWS_OLD_KEY})
    assert plan.store == {}


def test_rename_follows_identity():
    store = {AWS_OLD_KEY: _grant("aws-oauth", AWS_OLD_URL)}
    new_key = cli_oauth_key("aws2", {"type": "http", "url": AWS_NEW_URL})
    plan = plan_rekey(store, [WantedServer(new_key, "aws2", AWS_NEW_URL)], _Identity({AWS_PRM: AWS_ID}))
    assert plan.moved == {AWS_OLD_KEY: new_key}
    assert plan.store[new_key]["serverName"] == "aws2"
    assert plan.store[new_key]["serverUrl"] == AWS_NEW_URL
    assert AWS_OLD_KEY not in plan.store


def test_latest_expiry_donor_wins():
    k1, k2 = "aws-old-a|0000000000000001", "aws-old-b|0000000000000002"
    store = {k1: _grant("aws-old-a", AWS_OLD_URL, expires=5),
             k2: _grant("aws-old-b", AWS_OLD_URL, expires=9)}
    plan = plan_rekey(store, [WantedServer(AWS_NEW_KEY, "aws-oauth", AWS_NEW_URL)],
                      _Identity({AWS_PRM: AWS_ID}))
    assert plan.moved == {k2: AWS_NEW_KEY}
    assert k1 in plan.store


def test_drops_empty_unwanted_bundles():
    store = {
        "linear|aaaaaaaaaaaaaaaa": {"serverName": "linear", "serverUrl": "https://mcp.linear.app/mcp",
                                    "accessToken": "", "expiresAt": 0},
        "notion|bbbbbbbbbbbbbbbb": {"serverName": "notion", "serverUrl": "https://mcp.notion.com/mcp",
                                    "accessToken": "", "expiresAt": 0, "clientId": "cid"},
        "plugin:stripe:stripe|cccccccccccccccc": {"serverName": "plugin:stripe:stripe",
                                                  "serverUrl": "https://mcp.stripe.com",
                                                  "accessToken": ""},
    }
    ident = _Identity({})
    plan = plan_rekey(store, [], ident)
    assert plan.dropped == frozenset(store)
    assert plan.store == {}
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

    other = "https://x.example/.well-known/oauth-protected-resource"
    assert mor.grant_identity(other, fetch_json=boom) is None
    assert other not in mor._identity_cache
    assert mor.grant_identity(other, fetch_json=fetch) is not None  # failure was not cached


@pytest.mark.parametrize("doc", [None, {}, {"resource": "https://r/"},
                                 {"authorization_servers": ["https://a/"]},
                                 {"resource": "https://r/", "authorization_servers": []},
                                 {"resource": 3, "authorization_servers": ["https://a/"]}])
def test_grant_identity_missing_fields_is_none(doc):
    assert mor.grant_identity("https://p/", fetch_json=lambda _u: doc) is None
    assert "https://p/" not in mor._identity_cache
