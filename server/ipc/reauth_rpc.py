"""Daemon RPC surface for the reauth state machine — thin wrappers over a ReauthManager.

The edge proxies start/status/cancel here in remote mode so the reauth browser launches
on the daemon host, where the X11 virtual displays live (server/sessions/x11manager.py)."""
from __future__ import annotations

from fastapi import FastAPI

from server.ipc.daemon import register_rpc


def register_reauth(app: FastAPI, reauth) -> None:
    async def reauth_start(p: dict) -> dict:
        return (await reauth.start(p["name"], p["identity"], mode=p.get("mode", "login"))).to_dict()

    async def reauth_status(p: dict) -> dict:
        return reauth.status(p.get("name", "")).to_dict()

    async def reauth_cancel(p: dict) -> dict:
        return (await reauth.cancel(p.get("name", ""))).to_dict()

    async def reauth_submit_code(p: dict) -> dict:
        return (await reauth.submit_code(p["name"], p["code"])).to_dict()

    register_rpc(app, "reauth_start", reauth_start)
    register_rpc(app, "reauth_status", reauth_status)
    register_rpc(app, "reauth_cancel", reauth_cancel)
    register_rpc(app, "reauth_submit_code", reauth_submit_code)
