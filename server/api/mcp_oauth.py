"""One-tap MCP sign-in routes (MCP auth overhaul phase 4): ``start`` sends the browser to the
provider, ``callback`` takes it back. Every answer is a 302 to a relative ``/settings/mcp``
URL whose only parameters are the server name (quoted) or a closed-set error code: provider
text is never reflected. The flow lives in :mod:`server.mcp_health.one_tap`."""
from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.library.names import valid_mcp_name
from server.mcp_health import one_tap

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/oauth", tags=["mcp"])

_SETTINGS = "/settings/mcp"


class CallbackAccessFilter(logging.Filter):
    """Redacts the query string of uvicorn access lines for the one-tap callback: it
    carries the provider's authorization code and state, which must never reach a log."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn's access record: args = (client, method, path_with_query, http_version, status)
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            path, sep, _query = str(args[2]).partition("?")
            if sep and path == one_tap.CALLBACK_PATH:
                record.args = (args[0], args[1], f"{path}?<redacted>", args[3], args[4])
        return True


def install_callback_access_filter() -> None:
    """Install :class:`CallbackAccessFilter` on ``uvicorn.access`` (idempotent)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, CallbackAccessFilter) for f in access.filters):
        access.addFilter(CallbackAccessFilter())


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
    except Exception as exc:  # never CancelledError (a BaseException)
        # An unexpected fault (e.g. the client cache write) still lands on a page: the
        # paste-back flow for a valid name. Only the exception type is logged.
        log.warning("mcp one-tap: start failed unexpectedly reason=%s", type(exc).__name__)
        if valid_mcp_name(server):
            return _to_settings("pasteBack", server)
        return _to_settings("signInError", "not_configured")
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
    except Exception as exc:  # never CancelledError (a BaseException)
        log.warning("mcp one-tap: callback failed unexpectedly reason=%s", type(exc).__name__)
        return _to_settings("signInError", "exchange_failed")
    await record_audit(db=db, type="library_mcp_auth_completed", identity=identity,
                       detail={"name": name})
    return _to_settings("signedIn", name)
