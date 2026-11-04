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
import re
import time
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from . import creds, paths
from .mcp_keys import DEFAULT_PORTS, is_https_url, url_origin
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

_OAUTH_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_ERROR_CHARS = 500

FetchJson = Callable[[str], "dict | None"]
PostForm = Callable[[str, dict, dict], "tuple[int, bytes]"]


class McpGrantDead(Exception):
    """The token endpoint rejected the refresh token itself: the grant is gone.

    A sibling of :class:`McpRefreshTransient`, not a subclass, so one cannot swallow the
    other (the convention ``viberpit.oauth`` uses for ``AuthError`` / ``TransientError``)."""


class McpRefreshTransient(Exception):
    """The refresh failed for a reason that does not prove the grant dead. Retry later."""


# --- HTTP defaults (viberpit.oauth.http_request is the one outbound choke point) -----------

def default_fetch_json(url: str) -> dict | None:
    # Redirects are never followed here or in default_post_form: callers validate the URL
    # (origin, https) before the request, and a 3xx would bypass that.
    from . import oauth
    try:
        status, body = oauth.http_request("GET", url, headers={"Accept": "application/json"},
                                          timeout=_HTTP_TIMEOUT_S, max_retries=1, max_total_sec=15.0,
                                          follow_redirects=False)
    except oauth.TransientError:
        return None
    if status != 200:
        return None
    doc = json_or_none(body)
    return doc if isinstance(doc, dict) else None


def default_post_form(url: str, form: dict, headers: dict) -> tuple[int, bytes]:
    from . import oauth
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **headers}
    # Non-idempotent: a refresh token may be single-use, so a 5xx or a network error is
    # never resent (the origin may already have rotated it).
    return oauth.http_request("POST", url, headers=hdrs, data=urllib.parse.urlencode(form).encode(),
                              timeout=_HTTP_TIMEOUT_S, max_retries=2, max_total_sec=20.0,
                              idempotent=False, follow_redirects=False)


def json_or_none(body: bytes) -> object:
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def oauth_error_code(payload: object, secrets: Iterable[object] = ()) -> str | None:
    """The OAuth ``error`` code of an error response, safe to log and store, or None.

    Only a well-formed code (RFC 6749 §5.2 token charset) survives: a body field is never
    echoed verbatim. A hostile or buggy endpoint could echo a credential as the "code", so
    a code containing any of *secrets* is dropped too."""
    raw = payload.get("error") if isinstance(payload, dict) else None
    if not (isinstance(raw, str) and _OAUTH_CODE_RE.fullmatch(raw)):
        return None
    if any(isinstance(v, str) and v and v in raw for v in secrets):
        return None
    return raw


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


def expired_keys(store: dict, now_ms: int) -> frozenset[str]:
    """Store keys whose bundle has a positive ``expiresAt`` already in the past. A cleared
    (dead) bundle's ``expiresAt`` is 0, so it is never counted as expired."""
    out = set()
    for key, blob in store.items():
        if not isinstance(blob, dict):
            continue
        expiry = _expiry_ms(blob)
        if expiry is not None and 0 < expiry < now_ms:
            out.add(key)
    return frozenset(out)


def cleared(bundle: dict) -> dict:
    """The CLI's own cleared shape for a dead grant: no access or refresh token."""
    out = {k: v for k, v in bundle.items() if k != "refreshToken"}
    out["accessToken"] = ""
    out["expiresAt"] = 0
    return out


def cleared_keys(store: dict) -> frozenset[str]:
    """Store keys whose bundle is in the :func:`cleared` shape: no refresh token and an empty
    access token. A never-signed-in discovery stub has the same shape, so callers must not
    read this alone as "died"."""
    return frozenset(k for k, blob in store.items()
                     if isinstance(blob, dict) and not blob.get("refreshToken")
                     and not blob.get("accessToken"))


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


def metadata_urls(auth_url: str) -> list[str]:
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
    fetch = fetch_json or default_fetch_json
    explicit = _discovery_state(bundle).get("authServerMetadataUrl")
    explicit = explicit if isinstance(explicit, str) and explicit else None
    auth_url = _auth_server_url(bundle)
    cache_key = explicit or auth_url
    if cache_key in _metadata_cache:
        return _metadata_cache[cache_key]
    urls = ([explicit] if explicit else []) + metadata_urls(auth_url)
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
    """The token endpoint, or "" when there is none or it is not https: a refresh token is
    never sent to a ``file:``/``http:`` URL a metadata document named."""
    if metadata.get("token_endpoint"):
        url = str(metadata["token_endpoint"])
    else:
        origin = _origin(_auth_server_url(bundle))
        url = f"{origin}/token" if origin else ""
    return url if is_https_url(url) else ""


def token_endpoint(bundle: dict, *, fetch_json: FetchJson | None = None) -> str:
    """The token endpoint for *bundle*'s authorization server ("" if nothing to derive it from)."""
    return _endpoint_from(bundle, _auth_metadata(bundle, fetch_json))


# --- resource indicator (RFC 8707) ---------------------------------------------------------

_resource_cache: dict[str, str] = {}

def url_href(url: str) -> str:
    """*url* as the CLI serializes it (WHATWG ``new URL(url).href``) for the http(s) parts that
    differ from the raw string: lower-case scheme and host, no default port, and ``/`` for an
    empty path. Railway's metadata names the bare origin ``https://mcp.railway.com``; the CLI
    authorized with ``…com/`` and the token endpoint compares exactly (400 invalid_target)."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname or "@" in parts.netloc:
        return url
    try:
        port = parts.port
    except ValueError:
        return url
    netloc = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname  # IPv6 literal
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, parts.path or "/", parts.query, parts.fragment))


def resource_indicator(bundle: dict, *, fetch_json: FetchJson | None = None) -> str:
    """The ``resource`` to send, chosen as the CLI's MCP SDK does: the protected-resource
    metadata's ``resource`` when ``discoveryState.resourceMetadataUrl`` serves one, else the
    server URL minus its fragment, serialized as the URL's ``href`` (:func:`url_href`). The
    token endpoint rejects any other value: aws-oauth is configured as
    ``…/mcp?oauth=initialize`` but its metadata names ``…/mcp``."""
    server_url = bundle.get("serverUrl")
    fallback = url_href(urllib.parse.urldefrag(server_url)[0]) if isinstance(server_url, str) else ""
    prm_url = _discovery_state(bundle).get("resourceMetadataUrl")
    if not (isinstance(prm_url, str) and prm_url):
        return fallback
    if prm_url in _resource_cache:
        return _resource_cache[prm_url]
    try:
        doc = (fetch_json or default_fetch_json)(prm_url)
    except Exception:
        log.debug("mcp oauth resource metadata fetch failed url=%s", prm_url, exc_info=True)
        doc = None
    resource = doc.get("resource") if isinstance(doc, dict) else None
    if isinstance(resource, str) and resource:
        resource = url_href(resource)
        _resource_cache[prm_url] = resource
        return resource
    return fallback


# --- grant identity (RFC 9728) --------------------------------------------------------------

_identity_cache: dict[str, tuple[str, str]] = {}
# A failed lookup is not retried for IDENTITY_FAILURE_TTL_S (monotonic clock), so an offline
# or 404ing PRM endpoint costs one fetch per window, not one per tick.
IDENTITY_FAILURE_TTL_S = 300.0
_identity_failed_until: dict[str, float] = {}
_monotonic = time.monotonic


def grant_identity(prm_url: str, *, fetch_json: FetchJson | None = None) -> tuple[str, str] | None:
    """``(authorization_server, resource)`` named by the protected-resource metadata at
    *prm_url*, both serialized by :func:`url_href`; None when the fetch fails or either field
    is missing, or when ``resource`` is not on *prm_url*'s origin (RFC 9728 §3.3: a server
    must not claim another's grant). Two grants with an equal identity are interchangeable,
    so the re-key plan may move one to the other's key. A success is cached per *prm_url*; a
    failure is remembered separately for :data:`IDENTITY_FAILURE_TTL_S` (returning None
    without a fetch) and never shadows a success."""
    if prm_url in _identity_cache:
        return _identity_cache[prm_url]
    now = _monotonic()
    if _identity_failed_until.get(prm_url, 0.0) > now:
        return None
    identity = _fetch_identity(prm_url, fetch_json)
    if identity is None:
        _identity_failed_until[prm_url] = now + IDENTITY_FAILURE_TTL_S
        return None
    _identity_failed_until.pop(prm_url, None)
    _identity_cache[prm_url] = identity
    return identity


def _fetch_identity(prm_url: str, fetch_json: FetchJson | None) -> tuple[str, str] | None:
    try:
        doc = (fetch_json or default_fetch_json)(prm_url)
    except Exception:
        log.debug("mcp oauth grant identity fetch failed url=%s", prm_url, exc_info=True)
        return None
    return prm_identity(prm_url, doc)


def prm_identity(prm_url: str, doc: object) -> tuple[str, str] | None:
    """``(authorization_server, resource)`` of the protected-resource metadata *doc* served
    at *prm_url*, both :func:`url_href`-serialized; None when either field is missing or
    ``resource`` is not on *prm_url*'s origin. The pure half of :func:`grant_identity`."""
    if not isinstance(doc, dict):
        return None
    servers = doc.get("authorization_servers")
    resource = doc.get("resource")
    auth_server = servers[0] if isinstance(servers, list) and servers else None
    if not (isinstance(auth_server, str) and auth_server and isinstance(resource, str) and resource):
        return None
    identity = (url_href(auth_server), url_href(resource))
    origin = url_origin(identity[1])
    if origin is None or origin != url_origin(prm_url):
        # RFC 9728 §3.3: a document naming another origin's resource is forged or misrouted.
        log.debug("mcp oauth grant identity resource origin mismatch url=%s", prm_url)
        return None
    return identity


_SECRET_METHODS = ("client_secret_basic", "client_secret_post")


def client_auth(client_id: str, secret: str | None, metadata: dict,
                method: str | None = None) -> tuple[dict, dict]:
    """``(extra form fields, headers)`` authenticating a confidential client at the token
    endpoint. *method* is the registration's ``token_endpoint_auth_method`` when known;
    otherwise ``client_secret_basic`` unless the metadata lists only ``client_secret_post``.
    A public client (no secret) sends nothing extra."""
    if not secret:
        return {}, {}
    if method not in _SECRET_METHODS:
        methods = metadata.get("token_endpoint_auth_methods_supported")
        if not isinstance(methods, list) or "client_secret_basic" in methods:
            method = "client_secret_basic"
        elif "client_secret_post" in methods:
            method = "client_secret_post"
        else:
            return {}, {}
    if method == "client_secret_post":
        return {"client_secret": secret}, {}
    pair = f"{urllib.parse.quote(client_id, safe='')}:{urllib.parse.quote(secret, safe='')}"
    return {}, {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}


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
    secret = bundle.get("clientSecret")
    extra, headers = client_auth(str(client_id or ""), secret if isinstance(secret, str) else None,
                                 metadata)
    form.update(extra)
    try:
        status, body = (post_form or default_post_form)(url, form, headers)
    except Exception as exc:
        raise McpRefreshTransient(f"token request failed: {type(exc).__name__}") from exc
    payload = json_or_none(body)
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
    code = oauth_error_code(payload, (refresh_token, bundle.get("clientSecret")))
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


def _publish_all(names: list[str], *, drop: frozenset[str] = frozenset()) -> int:
    written = 0
    for name in names:
        try:
            written += bool(creds.sync_cfg_mcp_oauth(paths.account_cfg_dir(name), drop=drop))
        except Exception:
            log.debug("mcp oauth publish failed account=%s", name, exc_info=True)
    return written


def _refresh_due(summary: dict, post_form: PostForm | None, fetch_json: FetchJson | None,
                 now_ms: int | None, skip_keys: frozenset[str] = frozenset()) -> bool:
    store = creds.read_shared_mcp()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    changed = False
    for key in due_keys(store, now):
        if key in skip_keys or _backoff_until_ms.get(key, 0) > now:
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
            summary["outcomes"].append({"key": key, "label": label, "outcome": "transient",
                                        "error": str(exc)[:MAX_ERROR_CHARS]})
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
        summary["outcomes"].append({"key": key, "label": label, "outcome": outcome, "error": None})
        if outcome == "refreshed":
            log.info("mcp oauth refreshed server=%s key=%s expires=%s", label, key, _expiry_label(replacement))
        else:
            log.info("mcp oauth grant dead, cleared server=%s key=%s (sign in again)", label, key)
    return changed


def run_once(*, accounts_list: list[str] | None = None, post_form: PostForm | None = None,
             fetch_json: FetchJson | None = None, now_ms: int | None = None,
             drop_keys: frozenset[str] = frozenset(), skip_keys: frozenset[str] = frozenset()) -> dict:
    """One refresher tick. Never raises.

    1. Capture + publish every Anthropic account dir (strips any refresh-token copy), purging
       *drop_keys* (the keys the edge's grant reconcile just deleted or moved) from each.
    2. Refresh each due store bundle (except *skip_keys*: superseded grants) outside the
       store lock; write back compare-and-swap.
    3. If the store changed, publish again."""
    summary: dict = {"refreshed": [], "dead": [], "transient": [], "published": 0,
                     "outcomes": []}
    if accounts_list is None:
        try:
            from . import accounts
            names = accounts._anthropic_account_names()
        except Exception:
            log.debug("mcp oauth refresher: listing accounts failed", exc_info=True)
            names = []
    else:
        names = list(accounts_list)
    summary["published"] += _publish_all(names, drop=drop_keys)
    try:
        changed = _refresh_due(summary, post_form, fetch_json, now_ms, skip_keys)
    except Exception:
        log.debug("mcp oauth refresher: tick failed", exc_info=True)
        changed = False
    if changed:
        summary["published"] += _publish_all(names, drop=drop_keys)
    return summary
