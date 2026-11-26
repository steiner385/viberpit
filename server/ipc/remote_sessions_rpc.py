"""Daemon RPCs for remote sessions (plan 2026-11-25-remote-sessions-reverse).

Served by every daemon; only the primary (the refresh authority, where the accounts'
claude.ai logins may be used) answers anything but ``REMOTE_SESSIONS_DISABLED`` —
``service.enabled_by_config``. The edge's ``FleetOrchestrator`` sends them to the primary.
Each watcher's events are published on this daemon's event bus as ``remote:<session id>``,
the stream the edge's live view subscribes to.

* ``remote_sessions_list``  ``{}`` -> ``{enabled, sessions, accounts}``
* ``remote_session_open``   ``{session_id}`` -> ``{epoch, head, events}``
* ``remote_session_touch``  ``{session_id}`` -> ``{epoch}``
* ``remote_session_prompt`` ``{session_id, text}`` -> ``{ok, account}``
* ``remote_session_answer`` ``{session_id, ask_id, decision?, message?, selected?, mode?}``
  -> ``{ok, sent, account, kind}``
* ``remote_control_machine_ids`` ``{}`` -> ``{host, machineIds: {id: source}}`` — the
  Remote Control machine ids THIS host's config files carry (``machines.local_payload``).
  Answered by every daemon, kill switch or not: it reads only local files, and the edge
  asks every host to name the machine a listed session runs on.

A refusal raises ``RemoteSessionsError``; its ``error_data`` carries the code across the
envelope.
"""
from __future__ import annotations

import asyncio
from typing import Any

from server.ipc.daemon import register_rpc
from server.remote_sessions import machines
from server.remote_sessions.service import RemoteSessionsService, service_for

_ANSWER_KEYS = ("decision", "message", "selected", "mode")


def register_remote_sessions(app: Any, orchestrator: Any) -> RemoteSessionsService:
    svc = service_for(orchestrator)
    svc.set_publisher(app.state.publish, app.state.release_ring)
    app.state.remote_sessions = svc

    async def remote_sessions_list(params: dict) -> dict:
        return await svc.list_sessions()

    async def remote_session_open(params: dict) -> dict:
        return await svc.open(str(params.get("session_id") or ""))

    async def remote_session_touch(params: dict) -> dict:
        return svc.touch(str(params.get("session_id") or ""))

    async def remote_session_prompt(params: dict) -> dict:
        return await svc.send_prompt(str(params.get("session_id") or ""), params.get("text"))

    async def remote_session_answer(params: dict) -> dict:
        answer = {k: params[k] for k in _ANSWER_KEYS if k in params}
        return await svc.answer(str(params.get("session_id") or ""),
                                str(params.get("ask_id") or ""), **answer)

    async def remote_control_machine_ids(params: dict) -> dict:
        return await asyncio.to_thread(machines.local_payload)

    register_rpc(app, "remote_sessions_list", remote_sessions_list)
    register_rpc(app, "remote_control_machine_ids", remote_control_machine_ids)
    register_rpc(app, "remote_session_open", remote_session_open)
    register_rpc(app, "remote_session_touch", remote_session_touch)
    register_rpc(app, "remote_session_prompt", remote_session_prompt)
    register_rpc(app, "remote_session_answer", remote_session_answer)
    return svc
