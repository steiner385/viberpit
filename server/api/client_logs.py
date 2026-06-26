"""Ingest frontend errors/warnings and replay them through the backend logging
pipeline so they share one redaction filter, rotation policy and query surface
(journald + ccswitch.jsonl), correlated by session_id / request_id.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.auth.middleware import current_identity
from server.api.ratelimit import client_logs_rate_limit
from server.config import get_settings
from server.obs.context import bind_session, log_event

router = APIRouter(prefix="/api/client-logs", tags=["client-logs"])

log = logging.getLogger("client")

_MAX_ENTRIES = 100
_MAX_MSG = 4000
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warn": logging.WARNING, "warning": logging.WARNING,
           "error": logging.ERROR}


class ClientLogEntry(BaseModel):
    level: str = "error"
    msg: str = ""
    stack: str | None = None
    url: str | None = None
    userAgent: str | None = None
    clientId: str | None = None
    appVersion: str | None = None
    requestId: str | None = None
    sessionId: str | None = None


class ClientLogBatch(BaseModel):
    entries: list[ClientLogEntry] = Field(default_factory=list)


@router.post("", status_code=202, dependencies=[Depends(client_logs_rate_limit)])
async def ingest(batch: ClientLogBatch, identity: str = Depends(current_identity)) -> dict:
    if len(batch.entries) > _MAX_ENTRIES:
        raise HTTPException(status_code=413, detail="too many log entries")

    floor = _LEVELS.get(get_settings().client_log_level.lower(), logging.WARNING)
    accepted = 0
    for entry in batch.entries:
        level = _LEVELS.get(entry.level.lower(), logging.ERROR)
        if level < floor:
            continue
        msg = (entry.msg or "")[:_MAX_MSG]
        with bind_session(entry.sessionId):
            log_event(
                log, level, "client.error",
                msg=msg, stack=(entry.stack or "")[:_MAX_MSG],
                url=entry.url, ua=entry.userAgent, client_id=entry.clientId,
                app_version=entry.appVersion, request_id=entry.requestId,
                identity=identity,
            )
            # Re-emit a plain record carrying source=frontend for ergonomic filtering.
            log.log(level, msg, extra={"source": "frontend", "session_id": entry.sessionId})
        accepted += 1
    return {"accepted": accepted}
