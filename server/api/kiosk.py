"""Kiosk exchange: a display token in, a read-only display session out (#1408).

``POST /api/kiosk/session`` redeems a ``scope="display"`` token (minted by a full
identity at ``POST /api/assistant/tokens``) for an HttpOnly display cookie. The token
arrives in a form BODY — never a query string — and the answer is a 303 to a fixed
page, so it never re-enters a URL, an access log or a Referer. The route authenticates
by that body alone (the middleware exempts it from the identity requirement, not from
the Origin guard). The cookie session's life is clamped to the token's own expiry, and
every request it makes re-checks the token row, so revoking the token ends the display
on its next poll (``server/auth/middleware.py:resolve_display_session``).
"""
from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from server.audit import record_audit
from server.auth.middleware import (DISPLAY_COOKIE_NAME, KIOSK_PAGE_PATH,
                                    _sign_session_id, _validate_user_token)
from server.db import get_db
from server.db.models import AppSession, AssistantToken

router = APIRouter(prefix="/api/kiosk", tags=["kiosk"])
# `GET /kiosk` lives outside the `/api/kiosk` prefix: it is the page a display link opens.
page_router = APIRouter(tags=["kiosk"])

_DISPLAY_SESSION_TTL = timedelta(hours=24)
# The kiosk page every display lands on; `kiosk=1` keeps the client's layout guard on
# until the client derives its mode from the scope.
FIRST_PAGE = "/pipeline?kiosk=1"
_MAX_BODY = 4096


def _refuse(code: str, message: str, http: int) -> JSONResponse:
    return JSONResponse(status_code=http, content={"error": {"code": code, "message": message}})


@router.post("/session")
async def exchange(request: Request, db=Depends(get_db)):
    body = await request.body()
    token = (parse_qs(body[:_MAX_BODY].decode("utf-8", "replace")).get("token") or [""])[0]
    claims = await _validate_user_token(token, display=True) if token else None
    if claims is None:
        await record_audit(db, "kiosk.exchange.deny", "display:unknown", {})
        await db.commit()
        return _refuse("DISPLAY_TOKEN_INVALID",
                       "this display link has expired or been revoked", 401)
    row = (await db.execute(
        select(AssistantToken).where(AssistantToken.id == claims.token_id)
    )).scalar_one()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    token_expires = row.expires_at
    if token_expires.tzinfo is not None:
        token_expires = token_expires.astimezone(timezone.utc).replace(tzinfo=None)
    expires_at = min(now + _DISPLAY_SESSION_TTL, token_expires)
    session = AppSession(id=str(uuid.uuid4()), identity=claims.identity, created_at=now,
                         expires_at=expires_at, revoked_at=None, scope="display",
                         token_id=row.id)
    db.add(session)
    await record_audit(db, "kiosk.exchange", claims.identity,
                       {"tokenId": row.id, "label": row.label, "sessionId": session.id})
    await db.commit()
    response = RedirectResponse(FIRST_PAGE, status_code=303)
    response.set_cookie(
        DISPLAY_COOKIE_NAME, _sign_session_id(session.id), httponly=True, samesite="strict",
        secure=os.environ.get("VIBERPIT_DEV_INSECURE_COOKIE") != "1",
        max_age=int((expires_at - now).total_seconds()))
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# --- The exchange page (#3543) -----------------------------------------------------
# A device with no tailnet identity cannot load the SPA shell (the middleware demands
# an identity for every static asset), so the link's landing page is NOT the SPA: a
# tiny self-contained document the server renders, with one inline script and no
# bundle. The token is read from the URL by that script and never reflected into the
# HTML. The script strips it from the address bar first, then POSTs it to the exchange
# above and follows the 303 to the wall — by which point the display cookie admits the
# SPA shell like any other display request.

_COPY = {
    "incomplete": ("This display link is incomplete",
                   "Open the full link copied from Settings \u203a Display links."),
    "refused": ("This display link has expired or been revoked",
                "Create a new one in Settings \u203a Display links."),
    "failed": ("Could not open the display",
               "The server could not be reached. Open the display link again to retry."),
}

_STYLE = (
    "body{margin:0;min-height:100vh;display:grid;place-items:center;"
    "font:16px/1.5 system-ui,sans-serif;background:#0b0d10;color:#e6e8eb}"
    "main{max-width:32rem;padding:2rem;text-align:center}"
    "h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:0;color:#9aa3ad}"
    "@media(prefers-color-scheme:light){body{background:#fff;color:#14171a}p{color:#5b6670}}"
)

# `{refused}` / `{failed}` are filled with the titles/descriptions at import (json-escaped).
_SCRIPT = """\
(function(){
var COPY=%s;
var m=document.querySelector('main');
function show(k){m.innerHTML='';var h=document.createElement('h1');h.textContent=COPY[k][0];
var p=document.createElement('p');p.textContent=COPY[k][1];m.append(h,p);m.setAttribute('role','alert');}
var token=new URLSearchParams(location.search).get('token');
history.replaceState(null,'','/kiosk');
if(!token){show('incomplete');return;}
fetch('/api/kiosk/session',{method:'POST',credentials:'same-origin',
redirect:'manual',headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:new URLSearchParams({token:token}).toString()}).then(function(r){
if(r.type==='opaqueredirect'||r.ok){location.replace('%s');return;}
show(r.status===401||r.status===403?'refused':'failed');
}).catch(function(){show('failed');});
})();
"""


def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


def _build_page() -> tuple[str, str]:
    import json  # noqa: PLC0415
    script = _SCRIPT % (json.dumps({k: list(v) for k, v in _COPY.items()}), FIRST_PAGE)
    csp = ("default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
           f"connect-src 'self'; script-src {_csp_hash(script)}; style-src {_csp_hash(_STYLE)}")
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex"><title>Opening display\u2026</title>'
        f"<style>{_STYLE}</style></head><body><main role=\"status\"><p>Opening display\u2026</p></main>"
        f"<script>{script}</script>"
        f"<noscript><main><h1>{_COPY['failed'][0]}</h1><p>This page needs JavaScript.</p></main></noscript>"
        "</body></html>"
    )
    return html, csp


def _build_incomplete_page() -> tuple[str, str]:
    title, description = _COPY["incomplete"]
    csp = f"default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; connect-src 'self'; style-src {_csp_hash(_STYLE)}"
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex"><title>Display link</title>'
        f"<style>{_STYLE}</style></head><body><main role=\"alert\">"
        f"<h1>{title}</h1><p>{description}</p></main></body></html>"
    )
    return html, csp


_EXCHANGE_PAGE, _EXCHANGE_CSP = _build_page()
_INCOMPLETE_PAGE, _INCOMPLETE_CSP = _build_incomplete_page()


@page_router.get(KIOSK_PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
async def kiosk_page(request: Request) -> HTMLResponse:
    """The display link's landing page — reachable with no identity and no cookie."""
    html, csp = ((_EXCHANGE_PAGE, _EXCHANGE_CSP) if "token" in request.query_params
                 else (_INCOMPLETE_PAGE, _INCOMPLETE_CSP))
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        # The URL carries the token: nothing the page loads or links to may see it.
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
    })
