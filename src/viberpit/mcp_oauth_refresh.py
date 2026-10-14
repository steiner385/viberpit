"""The single MCP OAuth refresher (spec 2026-10-13-mcp-oauth-single-refresher-design).

``store/mcp.shared.json`` is the only holder of MCP refresh tokens; account dirs hold the
public view (no refresh token), so their CLIs never refresh. This module refreshes each
store bundle shortly before it expires, writes the result back compare-and-swap, and
publishes the public view to every Anthropic account dir. It runs on the one refresh
authority (``accounts.is_refresh_authority``) from an edge lifespan loop.

Pure core: HTTP (``post_form`` / ``fetch_json``) and the clock are injectable. Token
values are never logged; logs carry the server name, key, expiry and presence only.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone

from . import creds, paths
from .mcp_names import short_name

log = logging.getLogger(__name__)

LEAD_S = 600.0
_HTTP_TIMEOUT_S = 10
# After a transient failure a key waits this long before its next attempt, so an endpoint
# that keeps answering e.g. 400 invalid_client is not POSTed every tick forever.
TRANSIENT_BACKOFF_S = 300.0
_backoff_until_ms: dict[str, float] = {}

# The CLI's own dead-grant codes, plus the standard one. Anything else is transient: an
# unrecognized 400 (invalid_target, invalid_request) is a protocol mismatch, and a DCR
# client error (invalid_client) cannot be fixed by clearing the token.
DEAD_GRANT_ERRORS = frozenset({"invalid_grant", "invalid_refresh_token", "expired_refresh_token",
                               "token_expired"})

FetchJson = Callable[[str], "dict | None"]
PostForm = Callable[[str, dict, dict], "tuple[int, bytes]"]


class McpGrantDead(Exception):
    """The token endpoint rejected the refresh token itself: the grant is gone.

    A sibling of :class:`McpRefreshTransient`, not a subclass, so one cannot swallow the
    other (the convention ``viberpit.oauth`` uses for ``AuthError`` / ``TransientError``)."""


class McpRefreshTransient(Exception):
    """The refresh failed for a reason that does not prove the grant dead. Retry later."""


# --- HTTP defaults (viberpit.oauth.http_request is the one outbound choke point) -----------

def _default_fetch_json(url: str) -> dict | None:
    from . import oauth
    try:
        status, body = oauth.http_request("GET", url, headers={"Accept": "application/json"},
                                          timeout=_HTTP_TIMEOUT_S, max_retries=1, max_total_sec=15.0)
    except oauth.TransientError:
        return None
    if status != 200:
        return None
    doc = _json_or_none(body)
    return doc if isinstance(doc, dict) else None


def _default_post_form(url: str, form: dict, headers: dict) -> tuple[int, bytes]:
    from . import oauth
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **headers}
    # Non-idempotent: a refresh token may be single-use, so a 5xx or a network error is
    # never resent (the origin may already have rotated it).
    return oauth.http_request("POST", url, headers=hdrs, data=urllib.parse.urlencode(form).encode(),
                              timeout=_HTTP_TIMEOUT_S, max_retries=2, max_total_sec=20.0,
                              idempotent=False)


def _json_or_none(body: bytes) -> object:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


# --- selection --------------------------------------------------------------------------

def _expiry_ms(blob: dict) -> float | None:
    value = blob.get("expiresAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def due_keys(store: dict, now_ms: int, lead_s: float = LEAD_S) -> list[str]:
    """Store keys whose bundle has a refresh token and expires within *lead_s* seconds."""
    out = []
    for key, blob in store.items():
        if not (isinstance(blob, dict) and blob.get("refreshToken")):
            continue
        expiry = _expiry_ms(blob)
        if expiry is not None and expiry - now_ms < lead_s * 1000:
            out.append(key)
    return out


def cleared(bundle: dict) -> dict:
    """The CLI's own cleared shape for a dead grant: no access or refresh token."""
    out = {k: v for k, v in bundle.items() if k != "refreshToken"}
    out["accessToken"] = ""
    out["expiresAt"] = 0
    return out


# --- token endpoint discovery -----------------------------------------------------------

_metadata_cache: dict[str, dict] = {}


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _discovery_state(bundle: dict) -> dict:
    ds = bundle.get("discoveryState")
    return ds if isinstance(ds, dict) else {}


def _auth_server_url(bundle: dict) -> str:
    for candidate in (_discovery_state(bundle).get("authorizationServerUrl"), bundle.get("issuer")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return _origin(str(bundle.get("serverUrl") or ""))


def _metadata_urls(auth_url: str) -> list[str]:
    """RFC 8414 path insertion, then OIDC path insertion, then OIDC path append."""
    origin = _origin(auth_url)
    if not origin:
        return []
    path = urllib.parse.urlsplit(auth_url).path.rstrip("/")
    urls = [f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{auth_url.rstrip('/')}/.well-known/openid-configuration"]
    return list(dict.fromkeys(urls))


def _auth_metadata(bundle: dict, fetch_json: FetchJson | None) -> dict:
    """The authorization server's metadata, memoized per auth server in-process; ``{}`` when
    no document with a ``token_endpoint`` was found (not memoized: discovery retries)."""
    fetch = fetch_json or _default_fetch_json
    explicit = _discovery_state(bundle).get("authServerMetadataUrl")
    explicit = explicit if isinstance(explicit, str) and explicit else None
    auth_url = _auth_server_url(bundle)
    cache_key = explicit or auth_url
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]
    urls = ([explicit] if explicit else []) + _metadata_urls(auth_url)
    for url in dict.fromkeys(urls):
        try:
            doc = fetch(url)
        except Exception:
            log.debug("mcp oauth discovery fetch failed url=%s", url, exc_info=True)
            continue
        if isinstance(doc, dict) and isinstance(doc.get("token_endpoint"), str) and doc["token_endpoint"]:
            _metadata_cache[cache_key] = doc
            return doc
    return {}


def _endpoint_from(bundle: dict, metadata: dict) -> str:
    if metadata.get("token_endpoint"):
        return str(metadata["token_endpoint"])
    origin = _origin(_auth_server_url(bundle))
    return f"{origin}/token" if origin else ""


def token_endpoint(bundle: dict, *, fetch_json: FetchJson | None = None) -> str:
    """The token endpoint for *bundle*'s authorization server ("" if nothing to derive it from)."""
    return _endpoint_from(bundle, _auth_metadata(bundle, fetch_json))


# --- resource indicator (RFC 8707) ---------------------------------------------------------

_resource_cache: dict[str, str] = {}


def resource_indicator(bundle: dict, *, fetch_json: FetchJson | None = None) -> str:
    """The ``resource`` to send, chosen as the CLI's MCP SDK does: the protected-resource
    metadata's ``resource`` when ``discoveryState.resourceMetadataUrl`` serves one, else the
    server URL minus its fragment. The token endpoint rejects any other value: aws-oauth is
    configured as ``…/mcp?oauth=initialize`` but its metadata names ``…/mcp``."""
    server_url = bundle.get("serverUrl")
    fallback = urllib.parse.urldefrag(server_url)[0] if isinstance(server_url, str) else ""
    prm_url = _discovery_state(bundle).get("resourceMetadataUrl")
    if not (isinstance(prm_url, str) and prm_url):
        return fallback
    if prm_url in _resource_cache:
        return _resource_cache[prm_url]
    try:
        doc = (fetch_json or _default_fetch_json)(prm_url)
    except Exception:
        log.debug("mcp oauth resource metadata fetch failed url=%s", prm_url, exc_info=True)
        doc = None
    resource = doc.get("resource") if isinstance(doc, dict) else None
    if isinstance(resource, str) and resource:
        _resource_cache[prm_url] = resource
        return resource
    return fallback


# --- the grant ----------------------------------------------------------------------------

def refresh_bundle(bundle: dict, *, post_form: PostForm | None = None,
                   fetch_json: FetchJson | None = None, now_ms: int | None = None) -> dict:
    """Run the ``refresh_token`` grant for *bundle*; return the refreshed bundle.

    Raises :class:`McpGrantDead` only for a 400/401 whose ``error`` is a dead-grant code,
    and :class:`McpRefreshTransient` for everything else."""
    refresh_token = bundle.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise McpRefreshTransient("bundle has no refresh token")
    metadata = _auth_metadata(bundle, fetch_json)
    url = _endpoint_from(bundle, metadata)
    if not url:
        raise McpRefreshTransient("no token endpoint")
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    client_id = bundle.get("clientId")
    if isinstance(client_id, str) and client_id:
        form["client_id"] = client_id
    resource = resource_indicator(bundle, fetch_json=fetch_json)
    if resource:
        form["resource"] = resource
    headers: dict = {}
    secret = bundle.get("clientSecret")
    if isinstance(secret, str) and secret:
        methods = metadata.get("token_endpoint_auth_methods_supported")
        if not isinstance(methods, list) or "client_secret_basic" in methods:
            pair = (f"{urllib.parse.quote(str(client_id or ''), safe='')}:"
                    f"{urllib.parse.quote(secret, safe='')}")
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        elif "client_secret_post" in methods:
            form["client_secret"] = secret
    try:
        status, body = (post_form or _default_post_form)(url, form, headers)
    except Exception as exc:
        raise McpRefreshTransient(f"token request failed: {type(exc).__name__}") from exc
    payload = _json_or_none(body)
    payload = payload if isinstance(payload, dict) else {}
    access_token = payload.get("access_token")
    if status == 200 and isinstance(access_token, str) and access_token:
        new = dict(bundle)
        new["accessToken"] = access_token
        rotated = payload.get("refresh_token")
        new["refreshToken"] = rotated if isinstance(rotated, str) and rotated else refresh_token
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and math.isfinite(expires_in):
            now = int(time.time() * 1000) if now_ms is None else now_ms
            new["expiresAt"] = now + int(expires_in * 1000)
        else:
            new.pop("expiresAt", None)
        if isinstance(payload.get("scope"), str) and payload["scope"]:
            new["scope"] = payload["scope"]
        return new
    code = payload.get("error") if isinstance(payload.get("error"), str) else None
    # Case-insensitive: AWS sign-in answers in upper case (INVALID_GRANT, INVALID_TARGET).
    if status in (400, 401) and code is not None and code.lower() in DEAD_GRANT_ERRORS:
        raise McpGrantDead(f"token endpoint returned {status} {code}")
    raise McpRefreshTransient(f"token endpoint returned {status}" + (f" {code}" if code else ""))


# --- the tick -----------------------------------------------------------------------------

def _server_label(key: str, bundle: dict) -> str:
    name = bundle.get("serverName")
    return short_name(name) if isinstance(name, str) and name else key


def _expiry_label(bundle: dict) -> str:
    expiry = _expiry_ms(bundle)
    if expiry is None:
        return "none"
    return datetime.fromtimestamp(expiry / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _swap_if_unchanged(key: str, used_refresh_token: str, replacement: dict) -> bool:
    """Write *replacement* under the store lock iff the key still holds the refresh token
    we used. A login or capture that landed meanwhile wins."""
    swapped = False

    def swap(store: dict) -> dict:
        nonlocal swapped
        current = store.get(key)
        if isinstance(current, dict) and current.get("refreshToken") == used_refresh_token:
            store[key] = replacement
            swapped = True
        return store

    creds.update_shared_mcp(swap)
    return swapped


def _publish_all(names: list[str]) -> int:
    written = 0
    for name in names:
        try:
            written += bool(creds.sync_cfg_mcp_oauth(paths.account_cfg_dir(name)))
        except Exception:
            log.debug("mcp oauth publish failed account=%s", name, exc_info=True)
    return written


def _refresh_due(summary: dict, post_form: PostForm | None, fetch_json: FetchJson | None,
                 now_ms: int | None) -> bool:
    store = creds.read_shared_mcp()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    changed = False
    for key in due_keys(store, now):
        if _backoff_until_ms.get(key, 0) > now:
            continue
        old = store[key]
        label = _server_label(key, old)
        used = old["refreshToken"]
        try:
            replacement = refresh_bundle(old, post_form=post_form, fetch_json=fetch_json, now_ms=now_ms)
            outcome = "refreshed"
            _backoff_until_ms.pop(key, None)
        except McpGrantDead as exc:
            replacement, outcome = cleared(old), "dead"
            log.debug("mcp oauth grant dead server=%s key=%s: %s", label, key, exc)
        except Exception as exc:
            _backoff_until_ms[key] = now + TRANSIENT_BACKOFF_S * 1000
            summary["transient"].append(label)
            # INFO, not DEBUG: an unrefreshed grant silently expires, so the status and error
            # code (never a token) must be visible. Backoff caps this at one line per 5 min.
            log.info("mcp oauth refresh not done, will retry server=%s key=%s: %s", label, key, exc)
            continue
        try:
            swapped = _swap_if_unchanged(key, used, replacement)
        except Exception:
            log.debug("mcp oauth store write failed server=%s key=%s", label, key, exc_info=True)
            continue
        if not swapped:
            log.debug("mcp oauth store changed during refresh server=%s key=%s; kept it", label, key)
            continue
        changed = True
        summary[outcome].append(label)
        if outcome == "refreshed":
            log.info("mcp oauth refreshed server=%s key=%s expires=%s", label, key, _expiry_label(replacement))
        else:
            log.info("mcp oauth grant dead, cleared server=%s key=%s (sign in again)", label, key)
    return changed


def run_once(*, accounts_list: list[str] | None = None, post_form: PostForm | None = None,
             fetch_json: FetchJson | None = None, now_ms: int | None = None) -> dict:
    """One refresher tick. Never raises.

    1. Capture + publish every Anthropic account dir (strips any refresh-token copy).
    2. Refresh each due store bundle outside the store lock; write back compare-and-swap.
    3. If the store changed, publish again."""
    summary: dict = {"refreshed": [], "dead": [], "transient": [], "published": 0}
    if accounts_list is None:
        try:
            from . import accounts
            names = accounts._anthropic_account_names()
        except Exception:
            log.debug("mcp oauth refresher: listing accounts failed", exc_info=True)
            names = []
    else:
        names = list(accounts_list)
    summary["published"] += _publish_all(names)
    try:
        changed = _refresh_due(summary, post_form, fetch_json, now_ms)
    except Exception:
        log.debug("mcp oauth refresher: tick failed", exc_info=True)
        changed = False
    if changed:
        summary["published"] += _publish_all(names)
    return summary
