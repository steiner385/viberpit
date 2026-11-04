"""ViberPit's own MCP OAuth client (spec 2026-11-04-mcp-auth-overhaul-design §5, phase 4).

Signs ViberPit in to a remote MCP server the way the CLI's MCP SDK does, so the bundle it
produces is one the CLI and :mod:`viberpit.mcp_oauth_refresh` already understand:

1. :func:`discover`: the protected-resource metadata (RFC 9728), origin-bound as the
   refresher's :func:`~viberpit.mcp_oauth_refresh.grant_identity` is, then the
   authorization server's metadata (RFC 8414, then OIDC).
2. :func:`ensure_client`: a cached Dynamic Client Registration (RFC 7591) per
   authorization server, in ``store/mcp.clients.json``.
3. :func:`authorize_url` with PKCE (:func:`pkce_pair`) and :func:`new_state`.
4. :func:`exchange_code`: the ``authorization_code`` grant, returning the store bundle.

Library code only: HTTP is injectable and nothing here logs a code, verifier or token.
Exception text carries an HTTP status and a sanitised OAuth error code, never a body.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import math
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import creds
from .mcp_keys import is_https_url, prm_url_for, url_origin
from .mcp_oauth_refresh import (FetchJson, PostForm, client_auth, default_fetch_json,
                                default_post_form, json_or_none, metadata_urls, oauth_error_code,
                                prm_identity, url_href)

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 10
_HTTP_TIMEOUT_S = 10
CLIENT_NAME = "ViberPit"
OFFLINE_ACCESS = "offline_access"

Probe = Callable[[str], "dict[str, str]"]
PostJson = Callable[[str, dict], "tuple[int, bytes]"]


class McpOAuthClientError(Exception):
    """Base of this module's errors. The message is safe to log and show (no secrets);
    ``code`` is the sanitised OAuth ``error`` code of the response, when there was one."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class DiscoveryFailed(McpOAuthClientError):
    """No usable, origin-bound protected-resource or authorization-server metadata."""


class RegistrationUnsupported(McpOAuthClientError):
    """The authorization server offers no ``registration_endpoint`` (no DCR)."""


class RegistrationFailed(McpOAuthClientError):
    """Dynamic Client Registration was refused or returned no ``client_id``."""


class ExchangeFailed(McpOAuthClientError):
    """The ``authorization_code`` grant did not return an access token."""


@dataclasses.dataclass(frozen=True)
class Discovery:
    server_url: str
    prm_url: str
    resource: str  # url_href of the PRM ``resource``: the RFC 8707 indicator to send
    authorization_server: str  # url_href of the PRM's first authorization server
    as_metadata: dict
    as_metadata_url: str
    scopes_supported: tuple[str, ...]  # from the PRM


@dataclasses.dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str | None
    redirect_uri: str
    token_endpoint_auth_method: str | None = None  # as the registration echoed it


# --- HTTP defaults ------------------------------------------------------------------------

def _default_probe(url: str) -> dict[str, str]:
    """Response headers of an unauthenticated GET to *url*, for any status. ``http_request``
    returns no headers, so this one read uses urllib directly with the same User-Agent and
    the same no-redirect opener; it never reads the body. A 3xx yields the 3xx's headers."""
    from . import oauth
    req = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/json, text/event-stream", "User-Agent": oauth.USER_AGENT})
    try:
        with oauth.no_redirect_opener().open(req, timeout=_PROBE_TIMEOUT_S) as resp:
            return dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return dict(exc.headers.items()) if exc.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {}


def _default_post_json(url: str, body: dict) -> tuple[int, bytes]:
    from . import oauth
    return oauth.http_request("POST", url, headers={"Content-Type": "application/json",
                                                    "Accept": "application/json"},
                              data=json.dumps(body).encode(), timeout=_HTTP_TIMEOUT_S,
                              max_retries=1, max_total_sec=20.0, idempotent=False,
                              follow_redirects=False)


# --- discovery ----------------------------------------------------------------------------

_RESOURCE_METADATA_RE = re.compile(r'resource_metadata\s*=\s*"([^"]+)"', re.IGNORECASE)


def _header_prm_url(server_url: str, probe: Probe) -> str | None:
    try:
        headers = probe(server_url)
    except Exception:
        log.debug("mcp oauth client: probe failed url=%s", server_url, exc_info=True)
        return None
    for name, value in (headers or {}).items():
        if name.lower() == "www-authenticate" and isinstance(value, str):
            match = _RESOURCE_METADATA_RE.search(value)
            if not match:
                continue
            url = urllib.parse.urljoin(server_url, match.group(1))
            # Never fetch a URL the server names on another origin (or a file:/http: one):
            # that is an SSRF and endless-body vector. Path-insert and root still follow.
            if url_origin(url) == url_origin(server_url):
                return url
            log.debug("mcp oauth client: off-origin resource_metadata ignored server=%s", server_url)
    return None


def _prm_candidates(server_url: str, probe: Probe) -> list[str]:
    parts = urllib.parse.urlsplit(server_url)
    root = prm_url_for(f"{parts.scheme}://{parts.netloc}")
    urls = [_header_prm_url(server_url, probe), prm_url_for(server_url), root]
    return list(dict.fromkeys(u for u in urls if u))


def _fetch(fetch: FetchJson, url: str) -> dict | None:
    try:
        doc = fetch(url)
    except Exception:
        log.debug("mcp oauth client: metadata fetch failed url=%s", url, exc_info=True)
        return None
    return doc if isinstance(doc, dict) else None


def _has_prm_fields(doc: dict) -> bool:
    servers = doc.get("authorization_servers")
    return (isinstance(doc.get("resource"), str) and bool(doc["resource"])
            and isinstance(servers, list) and bool(servers) and isinstance(servers[0], str)
            and bool(servers[0]))


def _as_metadata(authorization_server: str, fetch: FetchJson) -> tuple[str, dict]:
    """The authorization server's metadata URL and document. Every endpoint ViberPit will
    send a user or a credential to is checked here, once: https only, and the document's
    ``issuer`` must be the authorization server itself (RFC 8414 §3.3)."""
    if not is_https_url(authorization_server):
        raise DiscoveryFailed("authorization server is not https")
    for url in metadata_urls(authorization_server):
        doc = _fetch(fetch, url)
        if not (doc and all(isinstance(doc.get(k), str) and doc[k]
                            for k in ("authorization_endpoint", "token_endpoint"))):
            continue
        issuer = doc.get("issuer")
        if not (isinstance(issuer, str) and url_href(issuer) == authorization_server):
            raise DiscoveryFailed("authorization server metadata names another issuer")
        endpoints = ["authorization_endpoint", "token_endpoint"]
        if doc.get("registration_endpoint") is not None:
            endpoints.append("registration_endpoint")
        if not all(is_https_url(doc[k]) for k in endpoints):
            raise DiscoveryFailed("authorization server endpoint is not https")
        return url, doc
    raise DiscoveryFailed("no authorization server metadata")


def discover(server_url: str, *, fetch_json: FetchJson | None = None,
             probe: Probe | None = None) -> Discovery:
    """Discover *server_url*'s protected-resource and authorization-server metadata.

    The PRM is looked for at the ``WWW-Authenticate`` ``resource_metadata`` URL of an
    unauthenticated request, then the path-inserted well-known URL, then the origin root.
    The first document with ``resource`` + ``authorization_servers`` wins, and its
    ``resource`` must be on both its own origin and *server_url*'s (Phase 2's binding)."""
    fetch = fetch_json or default_fetch_json
    server_origin = url_origin(server_url)
    if server_origin is None:
        raise DiscoveryFailed("server URL is not http(s)")
    for prm_url in _prm_candidates(server_url, probe or _default_probe):
        doc = _fetch(fetch, prm_url)
        if doc is None or not _has_prm_fields(doc):
            continue
        identity = prm_identity(prm_url, doc)
        if identity is None or url_origin(identity[1]) != server_origin:
            raise DiscoveryFailed("protected-resource metadata names another origin")
        authorization_server, resource = identity
        as_url, as_doc = _as_metadata(authorization_server, fetch)
        scopes = doc.get("scopes_supported")
        return Discovery(
            server_url=server_url, prm_url=prm_url, resource=resource,
            authorization_server=authorization_server, as_metadata=as_doc,
            as_metadata_url=as_url,
            scopes_supported=tuple(s for s in scopes if isinstance(s, str) and s)
            if isinstance(scopes, list) else ())
    raise DiscoveryFailed("no protected-resource metadata")


def choose_scope(discovery: Discovery) -> str:
    """The PRM's ``scopes_supported``, plus ``offline_access`` when the authorization server
    supports it (so the grant carries a refresh token); "" when neither names a scope."""
    scopes = list(discovery.scopes_supported)
    as_scopes = discovery.as_metadata.get("scopes_supported")
    if OFFLINE_ACCESS not in scopes and isinstance(as_scopes, list) and OFFLINE_ACCESS in as_scopes:
        scopes.append(OFFLINE_ACCESS)
    return " ".join(scopes)


# --- PKCE / state -------------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    """``(verifier, challenge)``: a 64-char url-safe verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)  # 48 bytes -> exactly 64 base64url chars
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_state() -> str:
    return secrets.token_urlsafe(32)


# --- client registration ------------------------------------------------------------------

def _cache_key(issuer: str) -> str:
    return url_href(issuer)


def ensure_client(discovery: Discovery, redirect_uri: str, *, post_json: PostJson | None = None,
                  cache_path: Path | None = None) -> Client:
    """The registered client for *discovery*'s authorization server: the cached one when it
    was registered for *redirect_uri*, else a fresh Dynamic Client Registration."""
    key = _cache_key(discovery.authorization_server)
    entry = creds.read_mcp_clients(cache_path).get(key)
    if (isinstance(entry, dict) and entry.get("redirect_uri") == redirect_uri
            and isinstance(entry.get("client_id"), str) and entry["client_id"]):
        secret = entry.get("client_secret")
        method = entry.get("token_endpoint_auth_method")
        return Client(entry["client_id"], secret if isinstance(secret, str) and secret else None,
                      redirect_uri, method if isinstance(method, str) and method else None)
    endpoint = discovery.as_metadata.get("registration_endpoint")
    if not (isinstance(endpoint, str) and endpoint):
        raise RegistrationUnsupported("authorization server offers no dynamic client registration")
    body = {"client_name": CLIENT_NAME, "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none"}
    try:
        status, raw = (post_json or _default_post_json)(endpoint, body)
    except Exception as exc:
        raise RegistrationFailed(f"registration request failed: {type(exc).__name__}") from None
    payload = json_or_none(raw)
    payload = payload if isinstance(payload, dict) else {}
    client_id = payload.get("client_id")
    if not (200 <= status < 300 and isinstance(client_id, str) and client_id):
        code = oauth_error_code(payload, (payload.get("client_secret"),))
        raise RegistrationFailed(f"registration endpoint returned {status}"
                                 + (f" {code}" if code else " without a client_id"
                                    if 200 <= status < 300 else ""), code=code)
    secret = payload.get("client_secret")
    secret = secret if isinstance(secret, str) and secret else None
    record: dict = {"client_id": client_id, "redirect_uri": redirect_uri,
                    "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if secret:
        record["client_secret"] = secret
    method = payload.get("token_endpoint_auth_method")
    method = method if isinstance(method, str) and method else None
    if method:
        record["token_endpoint_auth_method"] = method

    def store(cache: dict) -> dict:
        cache[key] = record
        return cache

    creds.update_mcp_clients(store, cache_path)
    log.info("mcp oauth client registered issuer=%s", key)
    return Client(client_id, secret, redirect_uri, method)


def forget_client(issuer: str, *, cache_path: Path | None = None) -> None:
    """Drop the cached registration for *issuer* (e.g. after ``invalid_client``)."""
    key = _cache_key(issuer)

    def drop(cache: dict) -> dict:
        cache.pop(key, None)
        return cache

    creds.update_mcp_clients(drop, cache_path)


# --- authorize + exchange -----------------------------------------------------------------

def authorize_url(discovery: Discovery, client: Client, *, state: str, code_challenge: str,
                  scope: str) -> str:
    """The authorization-endpoint URL for an S256 PKCE authorization-code request."""
    params = {"response_type": "code", "client_id": client.client_id,
              "redirect_uri": client.redirect_uri, "code_challenge": code_challenge,
              "code_challenge_method": "S256", "state": state, "resource": discovery.resource}
    if scope:
        params["scope"] = scope
    parts = urllib.parse.urlsplit(str(discovery.as_metadata["authorization_endpoint"]))
    query = "&".join(q for q in (parts.query, urllib.parse.urlencode(params)) if q)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def exchange_code(discovery: Discovery, client: Client, *, code: str, verifier: str,
                  server_name: str, now_ms: int, post_form: PostForm | None = None) -> dict:
    """Run the ``authorization_code`` grant; return the store bundle (CLI-compatible)."""
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": client.redirect_uri, "client_id": client.client_id,
            "code_verifier": verifier, "resource": discovery.resource}
    extra, headers = client_auth(client.client_id, client.client_secret, discovery.as_metadata,
                                 client.token_endpoint_auth_method)
    form.update(extra)
    try:
        status, raw = (post_form or default_post_form)(
            str(discovery.as_metadata["token_endpoint"]), form, headers)
    except Exception as exc:
        raise ExchangeFailed(f"token request failed: {type(exc).__name__}") from None
    payload = json_or_none(raw)
    payload = payload if isinstance(payload, dict) else {}
    access_token = payload.get("access_token")
    if not (status == 200 and isinstance(access_token, str) and access_token):
        error = oauth_error_code(payload, (code, verifier, client.client_secret,
                                           payload.get("access_token"),
                                           payload.get("refresh_token")))
        raise ExchangeFailed(f"token endpoint returned {status}"
                             + (f" {error}" if error else " without an access token"
                                if status == 200 else ""), code=error)
    bundle: dict = {"serverName": server_name, "serverUrl": discovery.server_url,
                    "accessToken": access_token}
    refresh_token = payload.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        bundle["refreshToken"] = refresh_token
    expires_in = payload.get("expires_in")
    if (isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
            and math.isfinite(expires_in) and expires_in >= 0):
        bundle["expiresAt"] = now_ms + int(expires_in * 1000)
    scope = payload.get("scope")
    if isinstance(scope, str) and scope:
        bundle["scope"] = scope
    bundle["clientId"] = client.client_id
    if client.client_secret:
        bundle["clientSecret"] = client.client_secret
    bundle["redirectUri"] = client.redirect_uri
    bundle["discoveryState"] = {"authorizationServerUrl": discovery.authorization_server,
                                "resourceMetadataUrl": discovery.prm_url,
                                "authServerMetadataUrl": discovery.as_metadata_url,
                                "oauthMetadataFound": True}
    return bundle
