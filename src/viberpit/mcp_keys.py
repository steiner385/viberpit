"""The ``claude`` CLI's MCP OAuth key formula, reproduced (binary 2.1.293, function ``Ol``).

The CLI looks a server's OAuth bundle up in ``mcp-oauth`` under
``<serverName>|<sha256(json{type,url,headers})[:16]>``. A bundle filed under any other key
is invisible to it, so the shared store must be written at exactly this key. The hash
input is the raw configured URL (query included) with keys in the order ``type, url,
headers`` and compact separators; ``headers`` defaults to ``{}``. Pure functions, no IO.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse


def cli_oauth_key(server_name: str, config: dict) -> str:
    """The CLI's ``mcp-oauth`` key for a server. ``server_name`` is the configured name for
    user servers and ``plugin:<plugin>:<name>`` for plugin servers (see ``store_name``)."""
    payload = json.dumps({"type": config.get("type"), "url": config.get("url"),
                          "headers": config.get("headers") or {}}, separators=(",", ":"))
    return f"{server_name}|{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def prm_url_for(server_url: str) -> str | None:
    """RFC 9728 protected-resource-metadata URL for a server URL, or None if not http(s).

    ``origin + /.well-known/oauth-protected-resource + path``; an empty path or ``/`` adds
    nothing and the query is dropped."""
    parts = urllib.parse.urlsplit(server_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = "" if parts.path == "/" else parts.path
    return f"{parts.scheme}://{parts.netloc}/.well-known/oauth-protected-resource{path}"


def store_name(name: str, plugin_name: str | None) -> str:
    """The ``serverName`` a bundle carries: plain for user servers, namespaced for plugins."""
    return name if plugin_name is None else f"plugin:{plugin_name}:{name}"


DEFAULT_PORTS = {"http": 80, "https": 443}


def url_origin(url: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an http(s) URL, lower-cased with the default port filled
    in, or None for anything else. Two URLs share an origin iff these are equal."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return scheme, parts.hostname, port if port is not None else DEFAULT_PORTS[scheme]


def is_https_url(url: object) -> bool:
    """True for an ``https://`` URL with a host and no userinfo: the only shape an OAuth
    endpoint taken from remote metadata may have before ViberPit sends anything to it."""
    if not isinstance(url, str):
        return False
    origin = url_origin(url)
    return origin is not None and origin[0] == "https" and "@" not in urllib.parse.urlsplit(url).netloc
