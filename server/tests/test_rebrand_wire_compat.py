"""Browser-facing wire contracts of the rebrand (#1368, #1378).

Phase 2b flipped the emitted spelling of the session cookie, the assistant
attribution headers, and the normalized-export format id. Phase 7 (#1378) then
retired the cookie and header accept side once no pre-flip session was left alive:
both now read the new spelling only. The export format id is different — an export
already on someone's disk can never be rewritten — so it keeps accepting both
forever, and its cases below stay accept-side cases.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from viberpit import brand

from server.auth.middleware import (
    ASSISTANT_SESSION_HEADER,
    VIA_HEADER,
    _COOKIE_NAME,
    _resolve_or_create_app_session,
    _sign_session_id,
    request_attribution,
)
from server.db import db_session, init_db
from server.db.models import AppSession
from server.sessions.export import (
    LEGACY_NORMALIZED_EXPORT_FORMAT,
    LEGACY_NORMALIZED_EXPORT_META_TYPE,
    NORMALIZED_EXPORT_FORMAT,
    NORMALIZED_EXPORT_META_TYPE,
    is_normalized_export_header,
    normalized_ndjson,
)

IDENTITY = "alice@example.com"


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------


class _Req:
    """The one attribute `_resolve_or_create_app_session` reads off a Request."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}


async def _seed_session(created_ago: timedelta = timedelta(0)) -> str:
    """One live AppSession for IDENTITY, created *created_ago* in the past; returns its id."""
    now = datetime.now(timezone.utc).replace(tzinfo=None) - created_ago
    session_id = str(uuid.uuid4())
    async with db_session() as db:
        db.add(AppSession(id=session_id, identity=IDENTITY, created_at=now,
                          expires_at=now + timedelta(hours=24), revoked_at=None))
        await db.commit()
    return session_id


def test_cookie_name_is_the_new_brand():
    assert _COOKIE_NAME == "viberpit_session"


@pytest.mark.asyncio
async def test_a_request_carrying_the_cookie_resolves_its_session():
    """A browser holding the cookie lands on the session row it already owns — not
    on a freshly minted one, which is what "logged out" looks like from its side."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    seeded = await _seed_session()

    req = _Req({_COOKIE_NAME: _sign_session_id(seeded)})
    async with db_session() as db:
        resolved = await _resolve_or_create_app_session(req, db, IDENTITY)
        await db.commit()

    assert resolved.id == seeded
    async with db_session() as db:
        rows = (await db.execute(select(AppSession))).scalars().all()
    assert len(rows) == 1, "resolving an existing cookie must not mint a row"


@pytest.mark.asyncio
async def test_the_retired_legacy_cookie_no_longer_resolves_a_session():
    """#1378 retired the pre-rebrand cookie name. Even a validly signed value under
    it is no longer read: the request is treated as cookie-less, so it falls back to
    the identity's NEWEST live row — not the older row the old cookie names, which
    is where the Phase 2b dual-accept would have landed it."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    named_by_legacy_cookie = await _seed_session(created_ago=timedelta(hours=1))
    newest = await _seed_session()

    req = _Req({f"{brand.LEGACY_SLUG}_session": _sign_session_id(named_by_legacy_cookie)})
    async with db_session() as db:
        resolved = await _resolve_or_create_app_session(req, db, IDENTITY)
        await db.commit()

    assert resolved.id == newest


@pytest.mark.asyncio
async def test_logout_revokes_the_session_held_under_the_cookie(isolated_db):
    """A logout that answered 204 while leaving the row live would be signed out in
    the UI and still signed in on the server."""
    from fastapi.testclient import TestClient

    from server.app import create_app

    seeded = await _seed_session()

    # The row is re-read INSIDE the client block: the app's lifespan disposes the
    # session factory on exit, so a read after `with` has no DB to talk to. It also
    # runs on the client's portal loop: the app's engine pool belongs to that loop,
    # and a checkout from the test's own loop that has to wait for a connection
    # raises "Queue ... is bound to a different event loop".
    async def _read_row():
        async with db_session() as db:
            return (await db.execute(
                select(AppSession).where(AppSession.id == seeded))).scalar_one()

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        client.cookies.set(_COOKIE_NAME, _sign_session_id(seeded))
        r = client.post("/api/auth/logout", headers={"Tailscale-User-Login": IDENTITY})
        assert r.status_code == 204
        cleared = {c.split("=")[0].strip() for c in r.headers.get_list("set-cookie")}
        assert _COOKIE_NAME in cleared

        row = client.portal.call(_read_row)
        assert row.revoked_at is not None, "logout did not revoke the row it was handed"


# ---------------------------------------------------------------------------
# Attribution headers
# ---------------------------------------------------------------------------


def test_header_names_are_the_new_brand():
    assert VIA_HEADER == "X-ViberPit-Via"
    assert ASSISTANT_SESSION_HEADER == "X-ViberPit-Assistant-Session"


def test_request_attribution_reads_the_new_spelling():
    import httpx

    headers = httpx.Headers({VIA_HEADER: "assistant", ASSISTANT_SESSION_HEADER: "sess-1"})
    assert request_attribution(headers) == ("assistant", "sess-1")


def test_request_attribution_is_none_when_unattributed():
    import httpx

    assert request_attribution(httpx.Headers({})) == (None, None)


def test_the_assistant_client_emits_the_new_spelling():
    """The emitter and the acceptor share one set of names, so this is really a
    check that `client.py` did not keep a hand-spelled literal of its own."""
    from server.assistant.client import AssistantApi

    api = AssistantApi(base_url="http://127.0.0.1:8787", token="ccas1:x", session_id="s1")
    assert api._client.headers[VIA_HEADER] == "assistant"
    assert api._client.headers[ASSISTANT_SESSION_HEADER] == "s1"
    assert request_attribution(api._client.headers) == ("assistant", "s1")


# ---------------------------------------------------------------------------
# Export format id
# ---------------------------------------------------------------------------


def test_export_writes_the_new_format_id():
    import json

    header = json.loads(normalized_ndjson(
        [], session_id="s1", engine="codex").splitlines()[0])
    assert header["type"] == NORMALIZED_EXPORT_META_TYPE == "viberpitExportMeta"
    assert header["format"] == NORMALIZED_EXPORT_FORMAT == "viberpit.normalized-events/v1"
    assert is_normalized_export_header(header)


def test_an_export_written_before_the_flip_is_still_recognised():
    """The exit criterion for the export half. This literal is a FILE ON DISK from
    before this PR — it can never be rewritten, so the reader keeps taking it."""
    assert LEGACY_NORMALIZED_EXPORT_FORMAT == "ccswitch.normalized-events/v1"
    assert is_normalized_export_header({
        "type": LEGACY_NORMALIZED_EXPORT_META_TYPE,
        "format": LEGACY_NORMALIZED_EXPORT_FORMAT,
        "engine": "codex",
        "sessionId": "s1",
    })


def test_a_future_version_still_reads_as_an_export_header():
    """A `/v2` file is a version mismatch, not a stranger: the caller has to be
    able to say "newer export than this reader" rather than "not an export"."""
    assert is_normalized_export_header(
        {"type": NORMALIZED_EXPORT_META_TYPE, "format": "viberpit.normalized-events/v2"})


@pytest.mark.parametrize("record", [
    None, "viberpitExportMeta", [], {}, {"type": "user", "text": "hi"},
    {"type": "sessionMeta", "model": "opus"},
])
def test_non_headers_are_not_mistaken_for_one(record):
    assert not is_normalized_export_header(record)
