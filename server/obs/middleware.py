"""ASGI middleware that assigns a per-request correlation id.

Sets request_id in a contextvar (so every log record during the request is
stamped), echoes it as the X-Request-ID response header (so a browser error can
name the request it came from), and emits one INFO http.request breadcrumb on
completion. The contextvar token is always reset, so ids never leak between
requests.
"""
from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from server.obs.context import log_event, new_request_id, request_id_var

log = logging.getLogger("server.obs")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        rid = request.headers.get("X-Request-ID") or new_request_id()
        token = request_id_var.set(rid)
        start = time.monotonic()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            log_event(
                log, logging.INFO, "http.request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round((time.monotonic() - start) * 1000, 1),
            )
            request_id_var.reset(token)
