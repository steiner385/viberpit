"""HTTP client for claude.ai's sessions API, as the Claude Code CLI itself calls it.

Verified against the live API with claude 2.1.331/2.1.332 (plan
2026-11-25-remote-sessions-reverse, "Verified"):

* ``GET /v1/sessions`` — ``{data, first_id, last_id, has_more}``, 200 per page by default;
  ``limit`` and ``after_id`` (a session id) page it.
* ``GET /v1/sessions/{id}/events`` — the same envelope over the session's stream-json
  events, oldest first. Each event's cursor id is its ``uuid``: ``after_id=<uuid>``
  returns the events after it, ``limit`` pages, ``has_more`` says whether more follow.
  The ``/events/stream`` SSE endpoint answers 404 for a Remote Control (``bridge``)
  session, so new events are polled with ``after_id``.
* ``POST /v1/sessions/{id}/events`` with ``{"events": [<stream-json event>]}`` — a
  ``user`` event reaches the CLI as a prompt; a ``control_response`` answers its
  ``can_use_tool`` ``control_request`` (the CLI then cancels its own pending ask).

Every request carries the account's FULL-scope claude.ai access token and its org UUID.
The token is a secret: it lives only in :class:`Credentials` (excluded from ``repr``) and
in the outgoing header, and is never logged or put in an exception message.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, TypeGuard

import httpx

from server.sessions import account_auth

log = logging.getLogger(__name__)

API_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
#: The beta the CLI sends for the sessions API (Remote Control / "bring your own compute").
SESSIONS_BETA = "ccr-byoc-2025-07-29"
REQUEST_TIMEOUT_S = 20.0
#: Pages of the account's session list read per poll. The list is not ordered by
#: activity, so a live session can sit behind archived ones; two default pages (400) is
#: well past the probe's count.
MAX_LIST_PAGES = 2
#: Pages of one session's events read per fetch (a long session's first fetch). Each page
#: is the API's default size; the rest arrives on the next poll.
MAX_EVENT_PAGES = 50

# UpstreamError kinds.
KIND_TRANSPORT = "transport"      # no answer: DNS, connect, timeout
KIND_AUTH = "auth"                # 401 / 403: the login was refused
KIND_NOT_FOUND = "not_found"      # 404: no such session (or not this org's)
KIND_RATE_LIMITED = "rate_limited"  # 429
KIND_HTTP = "http"                # any other non-2xx status
KIND_BAD_SHAPE = "bad_shape"      # 2xx with a body that is not the verified shape


#: A sessions-API session id. Checked before it is put in a request path, so no caller
#: input can reach another endpoint (``../``) — the ids come from the listing anyway.
SESSION_ID_RE = re.compile(r"^session_[A-Za-z0-9]{1,128}$")


def valid_session_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and SESSION_ID_RE.match(value) is not None


class UpstreamError(Exception):
    """The sessions API did not answer as verified. ``kind`` is one of the ``KIND_*``
    constants; ``status`` the HTTP status when there was one. The message never carries
    the token or a response body."""

    def __init__(self, kind: str, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(f"sessions API {kind}" + (f" (HTTP {status})" if status else ""))
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        #: The transport exception's type name, for a transport failure.
        self.detail: str | None = None


@dataclass(frozen=True)
class Credentials:
    """An account's claude.ai access token and org UUID. ``token`` is a secret and is
    left out of ``repr`` so a logged object cannot leak it."""
    token: str = field(repr=False)
    org_uuid: str


def credentials_for(account: str) -> Credentials | None:
    """*account*'s sessions-API credentials, or None when it has no usable claude.ai
    login here. Reuses the Remote Control launch credential (``account_auth``): the
    refresh authority only, a Claude account, its browser-login slot ``ok`` and its org
    known; the slot's current full-scope ACCESS token (never refreshed here — the
    authority's sweep keeps it fresh — and never the refresh token). Blocking IO: async
    callers wrap it in ``account_store_io``."""
    if not account_auth.remote_control_slot_ready(account):
        return None
    token = account_auth.slot_access_token(account)
    org = account_auth.organization_uuid(account)
    if not token or not org:
        return None
    return Credentials(token=token, org_uuid=org)


@dataclass
class Page:
    """One page of a list: its items, the last item's cursor id, and whether more
    follow."""
    data: list[dict]
    last_id: str | None
    has_more: bool


def _headers(creds: Credentials) -> dict[str, str]:
    return {"Authorization": f"Bearer {creds.token}",
            "anthropic-version": API_VERSION,
            "anthropic-beta": SESSIONS_BETA,
            "x-organization-uuid": creds.org_uuid}


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


def _page(body: Any) -> Page:
    """The verified ``{data, last_id, has_more}`` envelope, or ``bad_shape``. Items that
    are not objects are dropped rather than failing the page."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise UpstreamError(KIND_BAD_SHAPE)
    last = body.get("last_id")
    return Page(data=[d for d in body["data"] if isinstance(d, dict)],
                last_id=last if isinstance(last, str) and last else None,
                has_more=body.get("has_more") is True)


class SessionsApiClient:
    """Async client for the endpoints above. *transport* lets tests substitute an
    ``httpx.MockTransport``."""

    def __init__(self, *, base_url: str = API_BASE,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = REQUEST_TIMEOUT_S) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport,
                                       timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, creds: Credentials, *,
                       params: dict | None = None, json: Any = None) -> Any:
        try:
            resp = await self._http.request(method, path, headers=_headers(creds),
                                            params=params, json=json)
        except httpx.HTTPError as exc:
            # Only the exception's type travels on: no URL, header or body.
            err = UpstreamError(KIND_TRANSPORT)
            err.detail = type(exc).__name__
            raise err from None
        status = resp.status_code
        if status in (401, 403):
            raise UpstreamError(KIND_AUTH, status)
        if status == 404:
            raise UpstreamError(KIND_NOT_FOUND, status)
        if status == 429:
            raise UpstreamError(KIND_RATE_LIMITED, status, _retry_after(resp))
        if not 200 <= status < 300:
            raise UpstreamError(KIND_HTTP, status, _retry_after(resp))
        try:
            return resp.json()
        except ValueError:
            raise UpstreamError(KIND_BAD_SHAPE, status) from None

    async def list_sessions(self, creds: Credentials) -> list[dict]:
        """Every session on the account's first ``MAX_LIST_PAGES`` pages (raw API
        objects). A later page that fails keeps what the earlier ones returned."""
        out: list[dict] = []
        after: str | None = None
        for i in range(MAX_LIST_PAGES):
            params = {"after_id": after} if after else None
            try:
                page = _page(await self._request("GET", "/v1/sessions", creds, params=params))
            except UpstreamError:
                if i == 0:
                    raise
                log.info("remote sessions: list page %d failed; keeping the first %d",
                         i + 1, len(out))
                break
            out.extend(page.data)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
        return out

    async def fetch_events(self, creds: Credentials, session_id: str, *,
                           after_id: str | None = None) -> Page:
        """The session's events after *after_id* (from the start when None), oldest
        first, across up to ``MAX_EVENT_PAGES`` pages. ``last_id`` is the cursor to
        pass next time (None when nothing new arrived); ``has_more`` is True when the
        page cap stopped the read early."""
        if not valid_session_id(session_id):
            raise UpstreamError(KIND_NOT_FOUND)
        events: list[dict] = []
        cursor = after_id
        has_more = False
        for _ in range(MAX_EVENT_PAGES):
            params = {"after_id": cursor} if cursor else None
            page = _page(await self._request("GET", f"/v1/sessions/{session_id}/events",
                                             creds, params=params))
            events.extend(page.data)
            if page.last_id is not None:
                cursor = page.last_id
            has_more = page.has_more and page.last_id is not None
            if not has_more:
                break
        return Page(data=events, last_id=cursor if cursor != after_id else None,
                    has_more=has_more)

    async def post_events(self, creds: Credentials, session_id: str,
                          events: list[dict]) -> None:
        """POST *events* into the session. 2xx is success; anything else raises."""
        if not valid_session_id(session_id):
            raise UpstreamError(KIND_NOT_FOUND)
        await self._request("POST", f"/v1/sessions/{session_id}/events", creds,
                            json={"events": events})
