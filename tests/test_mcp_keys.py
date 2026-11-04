import pytest

from viberpit import mcp_keys as k

VECTORS = [
    ("aws-oauth", {"type": "http", "url": "https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize"},
     "aws-oauth|c6c1c83162f8ce2d"),
    ("aws-oauth", {"type": "http", "url": "https://aws-mcp.us-east-1.api.aws/mcp"},
     "aws-oauth|751831b0c9c1bf84"),
    ("kindash-mcp", {"type": "http", "url": "https://qa.kindash.com/mcp"}, "kindash-mcp|ff3c9dc630867870"),
    ("plugin:railway:railway", {"type": "http", "url": "https://mcp.railway.com"},
     "plugin:railway:railway|b12ecd269c942ba3"),
]


@pytest.mark.parametrize("name,config,expected", VECTORS)
def test_cli_oauth_key_matches_binary_vectors(name, config, expected):
    assert k.cli_oauth_key(name, config) == expected


def test_cli_oauth_key_headers_default_and_order():
    url = "https://qa.kindash.com/mcp"
    base = k.cli_oauth_key("s", {"url": url, "type": "http"})
    assert base == k.cli_oauth_key("s", {"type": "http", "url": url, "headers": {}})
    assert k.cli_oauth_key("s", {"type": "http", "url": url, "headers": {"A": "b"}}) != base


def test_prm_url_for():
    assert k.prm_url_for("https://aws-mcp.us-east-1.api.aws/mcp?oauth=initialize") == \
        "https://aws-mcp.us-east-1.api.aws/.well-known/oauth-protected-resource/mcp"
    assert k.prm_url_for("https://mcp.railway.com") == "https://mcp.railway.com/.well-known/oauth-protected-resource"
    assert k.prm_url_for("https://mcp.railway.com/") == "https://mcp.railway.com/.well-known/oauth-protected-resource"
    assert k.prm_url_for("stdio-ish") is None
    assert k.prm_url_for("ftp://x/y") is None


def test_store_name():
    assert k.store_name("aws-oauth", None) == "aws-oauth"
    assert k.store_name("railway", "railway") == "plugin:railway:railway"
