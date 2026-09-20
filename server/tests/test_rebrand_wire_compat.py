"""Phase 2b of the rebrand (#1368): emit the new spelling, accept both.

Three browser-facing wire contracts flip their emitted spelling here — the session
cookie, the assistant attribution headers, and the normalized-export format id —
and each must keep reading what it wrote yesterday. The failure these pin is
asymmetric: getting the EMIT side wrong is loud (nothing works), getting the
ACCEPT side wrong is silent (the one deployment that flips the name logs every
open tab out, or an archive on someone's disk stops being recognisable as an
export). So every case below is an accept-side case.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from server.auth.middleware import (
    ASSISTANT_SESSION_HEADER,
    LEGACY_ASSISTANT_SESSION_HEADER,
    LEGACY_VIA_HEADER,
    VIA_HEADER,
    _COOKIE_NAME,
    _LEGACY_COOKIE_NAME,
    _resolve_or_create_app_session,
    _sign_session_id,
    request_attribution,
)
from server.db import db_session, init_db
from server.db.models import AppSession
from server.sessions.export import (
    LEGACY_NORMALIZED_EXPORT_FORMAT,
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


async def _seed_session() -> str:
    """One live AppSession for IDENTITY; returns its id."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session_id = str(uuid.uuid4())
    async with db_session() as db:
        db.add(AppSession(id=session_id, identity=IDENTITY, created_at=now,
                          expires_at=now + timedelta(hours=24), revoked_at=None))
        await db.commit()
    return session_id


def test_cookie_names_are_the_two_brand_spellings():
    assert _COOKIE_NAME == "viberpit_session"
    assert _LEGACY_COOKIE_NAME == "ccswitch_session"


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_name", ["viberpit_session", "ccswitch_session"])
async def test_a_request_carrying_only_one_spelling_resolves_its_session(cookie_name):
    """The exit criterion, both ways round: a browser holding ONLY the legacy
    cookie (it has not been back since the flip) and a browser holding only the
    new one must both land on the session row they already own — not on a freshly
    minted one, which is what "logged out" looks like from the user's side."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    seeded = await _seed_session()

    req = _Req({cookie_name: _sign_session_id(seeded)})
    async with db_session() as db:
        resolved = await _resolve_or_create_app_session(req, db, IDENTITY)
        await db.commit()

    assert resolved.id == seeded
    async with db_session() as db:
        rows = (await db.execute(select(AppSession))).scalars().all()
    assert len(rows) == 1, "resolving an existing cookie must not mint a row"


@pytest.mark.asyncio
async def test_the_new_cookie_wins_when_a_browser_carries_both():
    """Mid-migration a jar holds both: the legacy one until it ages out, the new
    one renewed on every response. They may point at different rows (the legacy
    cookie predates a session rotation), and the one still being renewed is the
    one that must win."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    stale = await _seed_session()
    current = await _seed_session()

    req = _Req({_LEGACY_COOKIE_NAME: _sign_session_id(stale),
                _COOKIE_NAME: _sign_session_id(current)})
    async with db_session() as db:
        resolved = await _resolve_or_create_app_session(req, db, IDENTITY)

    assert resolved.id == current


@pytest.mark.asyncio
async def test_a_legacy_cookie_for_another_identity_is_still_refused():
    """Reading both spellings widens the NAME, never the trust: a cookie is only
    honoured for the identity the header already verified."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")
    seeded = await _seed_session()

    req = _Req({_LEGACY_COOKIE_NAME: _sign_session_id(seeded)})
    async with db_session() as db:
        resolved = await _resolve_or_create_app_session(req, db, "mallory@example.com")
        await db.commit()

    assert resolved.id != seeded
    assert resolved.identity == "mallory@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie_name", ["viberpit_session", "ccswitch_session"])
async def test_logout_revokes_a_session_held_under_either_cookie(cookie_name, isolated_db):
    """Logout is the accept side's sharpest edge: a route that looked only for the
    new name would answer 204 to a legacy-cookie browser while leaving the row
    live — signed out in the UI, still signed in on the server."""
    from fastapi.testclient import TestClient

    from server.app import create_app

    seeded = await _seed_session()

    # The row is re-read INSIDE the client block: the app's lifespan disposes the
    # session factory on exit, so a read after `with` has no DB to talk to.
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        client.cookies.set(cookie_name, _sign_session_id(seeded))
        r = client.post("/api/auth/logout", headers={"Tailscale-User-Login": IDENTITY})
        assert r.status_code == 204
        # Both spellings are cleared — a jar mid-migration may hold either or both.
        cleared = {c.split("=")[0].strip() for c in r.headers.get_list("set-cookie")}
        assert {"viberpit_session", "ccswitch_session"} <= cleared

        async with db_session() as db:
            row = (await db.execute(
                select(AppSession).where(AppSession.id == seeded))).scalar_one()
        assert row.revoked_at is not None, "logout did not revoke the row it was handed"


# ---------------------------------------------------------------------------
# Attribution headers
# ---------------------------------------------------------------------------


def test_header_names_are_the_new_brand():
    assert VIA_HEADER == "X-ViberPit-Via"
    assert ASSISTANT_SESSION_HEADER == "X-ViberPit-Assistant-Session"


@pytest.mark.parametrize("via_header,session_header", [
    (VIA_HEADER, ASSISTANT_SESSION_HEADER),
    (LEGACY_VIA_HEADER, LEGACY_ASSISTANT_SESSION_HEADER),
])
def test_request_attribution_accepts_either_spelling(via_header, session_header):
    import httpx

    headers = httpx.Headers({via_header: "assistant", session_header: "sess-1"})
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
        "type": "ccswitchExportMeta",
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
