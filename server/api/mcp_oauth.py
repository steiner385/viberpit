"""One-tap MCP sign-in routes (MCP auth overhaul phase 4): ``start`` sends the browser to the
provider, ``callback`` takes it back. Every answer is a 302 to a relative ``/settings/mcp``
URL whose only parameters are the server name (quoted) or a closed-set error code: provider
text is never reflected. The flow lives in :mod:`server.mcp_health.one_tap`."""
from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.mcp_health import one_tap

router = APIRouter(prefix="/api/mcp/oauth", tags=["mcp"])

_SETTINGS = "/settings/mcp"


def _to_settings(param: str, value: str) -> RedirectResponse:
    return RedirectResponse(f"{_SETTINGS}?{param}={urllib.parse.quote(value, safe='')}",
                            status_code=302)


@router.get("/start")
async def oauth_start(server: str = "", identity: str = Depends(current_identity),
                      db=Depends(get_db)) -> RedirectResponse:  # noqa: ANN001
    try:
        url = await one_tap.start(identity, server)
    except one_tap.PasteBack as exc:
        return _to_settings("pasteBack", exc.server)
    except one_tap.SignInFailed as exc:
        return _to_settings("signInError", exc.code)
    # Audit the name only — never the authorize URL (it carries the state).
    await record_audit(db=db, type="library_mcp_auth_started", identity=identity,
                       detail={"name": server})
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def oauth_callback(request: Request, code: str | None = None, state: str | None = None,
                         error: str | None = None, identity: str = Depends(current_identity),
                         db=Depends(get_db)) -> RedirectResponse:  # noqa: ANN001
    try:
        name = await one_tap.complete(identity, state=state, code=code, error=error,
                                      orch_getter=lambda: request.app.state.sessions)
    except one_tap.PasteBack as exc:
        return _to_settings("pasteBack", exc.server)
    except one_tap.SignInFailed as exc:
        return _to_settings("signInError", exc.code)
    await record_audit(db=db, type="library_mcp_auth_completed", identity=identity,
                       detail={"name": name})
    return _to_settings("signedIn", name)
