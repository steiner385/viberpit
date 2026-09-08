"""Library API — plugins, skills, slash commands, MCP servers over the
shared config base. Read routes are direct filesystem scans; mutations are
atomic writes + audit events. No orchestrator/daemon involvement."""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ccswitch import brand
from server.audit import record_audit
from server.auth.middleware import current_identity
from server.codex.capabilities import CODEX_CAPABILITIES, CODEX_LIBRARY_UNSUPPORTED_DETAIL
from server.db import get_db
from server.library import mutate, scan
from server.library.mutate import ConfigUnreadableError
from server.library.names import (
    valid_command_path,
    valid_mcp_name,
    valid_plugin_id,
    valid_skill_name,
)
from server.mcp_auth import McpAuthBusy
from server.sessions.claude_driver import ClaudeAgentDriver

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/engines")
async def get_library_engine_support(_identity: str = Depends(current_identity)) -> dict:
    """Which engines' shared configuration this Library surface manages.

    A static per-engine fact, not a live probe: `/plugins`, `/skills`,
    `/commands` and `/mcp` above always scan Claude's shared config directory
    (`CLAUDE_CONFIG_DIR`) and nothing else. Codex isolates each account's
    configuration under a credential-authority-owned `CODEX_HOME` instead
    (see `server/codex/capabilities.py`), so there is no shared, scannable
    surface for it here — `supported: false` reports that honestly rather than
    a consumer inferring "no Codex data" from an empty or missing response.
    """
    return {
        "claudeCode": {"supported": ClaudeAgentDriver.capabilities.library_management},
        "codex": {
            "supported": CODEX_CAPABILITIES.library_management,
            "detail": (
                None if CODEX_CAPABILITIES.library_management
                else CODEX_LIBRARY_UNSUPPORTED_DETAIL
            ),
        },
    }


class ToggleBody(BaseModel):
    enabled: bool


class ContentBody(BaseModel):
    content: str


class McpBody(BaseModel):
    config: dict


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": "invalid_name", "message": message})


@router.get("/plugins")
async def get_plugins(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_plugins()


@router.get("/plugins/{plugin_id}")
async def get_plugin(plugin_id: str, _identity: str = Depends(current_identity)) -> dict:
    if not valid_plugin_id(plugin_id):
        raise _bad("invalid plugin id")
    detail = scan.plugin_detail(plugin_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_plugin"})
    return detail


@router.patch("/plugins/{plugin_id}")
async def toggle_plugin(
    plugin_id: str, body: ToggleBody,
    identity: str = Depends(current_identity), db=Depends(get_db),
) -> Any:
    if not valid_plugin_id(plugin_id):
        raise _bad("invalid plugin id")
    try:
        found = mutate.set_plugin_enabled(plugin_id, body.enabled)
    except ConfigUnreadableError as err:
        return JSONResponse(status_code=409, content={"error": "config_unreadable", "path": err.path.name})
    if not found:
        raise HTTPException(status_code=404, detail={"error": "unknown_plugin"})
    await record_audit(db=db, type="library_plugin_toggled", identity=identity,
                       detail={"plugin": plugin_id, "enabled": body.enabled})
    return {"id": plugin_id, "enabled": body.enabled}


@router.get("/skills")
async def get_skills(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_skills()


@router.get("/skills/{name}")
async def get_skill(name: str, plugin: str | None = None,
                    _identity: str = Depends(current_identity)) -> dict:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    out = scan.read_skill(name, plugin=plugin)
    if out is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_skill"})
    return out


@router.put("/skills/{name}")
async def put_skill(name: str, body: ContentBody,
                    identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    mutate.write_skill(name, body.content)
    await record_audit(db=db, type="library_skill_saved", identity=identity,
                       detail={"skill": name})
    return {"name": name}


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str,
                       identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    if not mutate.delete_skill(name):
        raise HTTPException(status_code=404, detail={"error": "unknown_skill"})
    await record_audit(db=db, type="library_skill_deleted", identity=identity,
                       detail={"skill": name})


@router.get("/commands")
async def get_commands(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_commands()


@router.get("/commands/{path:path}")
async def get_command(path: str, plugin: str | None = None,
                      _identity: str = Depends(current_identity)) -> dict:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    out = scan.read_command(path, plugin=plugin)
    if out is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_command"})
    return out


@router.put("/commands/{path:path}")
async def put_command(path: str, body: ContentBody,
                      identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    mutate.write_command(path, body.content)
    await record_audit(db=db, type="library_command_saved", identity=identity,
                       detail={"command": path})
    return {"path": path}


@router.delete("/commands/{path:path}", status_code=204)
async def delete_command(path: str,
                         identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    if not mutate.delete_command(path):
        raise HTTPException(status_code=404, detail={"error": "unknown_command"})
    await record_audit(db=db, type="library_command_deleted", identity=identity,
                       detail={"command": path})


@router.get("/mcp")
async def get_mcp(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_mcp_servers()


@router.put("/mcp/{name}")
async def put_mcp(name: str, body: McpBody,
                  identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    error = mutate.validate_mcp_config(body.config)
    if error:
        raise HTTPException(status_code=400, detail={"error": "invalid_config", "message": error})
    try:
        mutate.put_mcp_server(name, body.config)
    except ConfigUnreadableError as err:
        return JSONResponse(status_code=409, content={"error": "config_unreadable", "path": err.path.name})
    transport = scan.infer_transport(body.config)
    # Audit name+transport only — config/headers/env may contain tokens.
    await record_audit(db=db, type="library_mcp_saved", identity=identity,
                       detail={"name": name, "transport": transport})
    return {"name": name, "transport": transport}


@router.delete("/mcp/{name}")
async def delete_mcp(name: str,
                     identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    try:
        found = mutate.delete_mcp_server(name)
    except ConfigUnreadableError as err:
        return JSONResponse(status_code=409, content={"error": "config_unreadable", "path": err.path.name})
    if not found:
        raise HTTPException(status_code=404, detail={"error": "unknown_server"})
    await record_audit(db=db, type="library_mcp_deleted", identity=identity,
                       detail={"name": name})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# MCP OAuth auth flow (one-at-a-time, via McpAuthManager on app.state)
# ---------------------------------------------------------------------------


class RedirectBody(BaseModel):
    redirectUrl: str


def _mcp_auth(request: Request):  # noqa: ANN201
    mgr = getattr(request.app.state, "mcp_auth", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail={"error": "mcp_auth_unavailable"})
    return mgr


@router.post("/mcp/{name}/auth", status_code=202)
async def mcp_auth_start(
    name: str,
    request: Request,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    try:
        state = await _mcp_auth(request).start(name)
    except McpAuthBusy as exc:
        raise HTTPException(status_code=409, detail={"error": "mcp_auth_busy", "server": exc.server})
    # Audit name only — never log the redirect URL or tokens.
    await record_audit(db=db, type="library_mcp_auth_started", identity=identity,
                       detail={"name": name})
    return state.to_dict()


@router.get("/mcp/{name}/auth")
async def mcp_auth_status(
    name: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    return _mcp_auth(request).status(name).to_dict()


@router.post("/mcp/{name}/auth/redirect")
async def mcp_auth_redirect(
    name: str,
    body: RedirectBody,
    request: Request,
    _identity: str = Depends(current_identity),
) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    state = await _mcp_auth(request).submit_redirect(name, body.redirectUrl)
    return state.to_dict()


@router.post("/mcp/{name}/auth/cancel")
async def mcp_auth_cancel(
    name: str,
    request: Request,
    _identity: str = Depends(current_identity),
) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    return (await _mcp_auth(request).cancel(name)).to_dict()


# ---------------------------------------------------------------------------
# AI Skill Draft (issue #595) — must come before /skills/{name} but FastAPI
# prefers literal path segments, so placement here (after all /{name} routes)
# is fine since /skills/draft is a distinct literal segment.
# ---------------------------------------------------------------------------


class SkillDraftRequest(BaseModel):
    description: str
    examples: str | None = None


@router.post("/skills/draft")
async def draft_skill(
    body: SkillDraftRequest,
    _identity: str = Depends(current_identity),
) -> dict:
    """Generate a SKILL.md draft via AI. Returns {name, content}.

    Uses the Anthropic Messages API directly. Requires ANTHROPIC_API_KEY env var.
    Returns 503 if no key is configured, 502 on API error.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={"error": "no_api_key", "message": "No ANTHROPIC_API_KEY configured"},
        )

    system = (
        f"You are a {brand.NAME} skill author. Generate a complete SKILL.md file.\n"
        "The format is:\n"
        "---\n"
        "name: <kebab-case-slug>\n"
        "description: <one line describing WHEN to invoke this skill — include trigger words>\n"
        "---\n\n"
        "<body: step-by-step instructions for Claude to follow>\n\n"
        "Return ONLY the raw SKILL.md content, no explanation."
    )
    user_msg = f"Skill description: {body.description}"
    if body.examples:
        user_msg += f"\n\nExamples:\n{body.examples}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            },
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={"error": "api_error", "message": "AI model unavailable"},
        )

    content = resp.json()["content"][0]["text"].strip()
    name_match = re.search(r"^name:\s*(\S+)", content, re.MULTILINE)
    skill_name = name_match.group(1) if name_match else "generated-skill"
    return {"name": skill_name, "content": content}


# ---------------------------------------------------------------------------
# Plugin Marketplace (issue #596)
# ---------------------------------------------------------------------------


@router.get("/marketplace")
async def list_marketplace(
    _identity: str = Depends(current_identity),
) -> dict:
    """Return available plugins from the cached marketplace catalog.

    Reads from the shared plugin-catalog-cache.json written by the Claude Code
    plugin system. Returns an empty list if the cache does not exist yet.
    """
    from ccswitch import paths as _paths
    catalog_path = _paths.shared_base() / "plugins" / "plugin-catalog-cache.json"
    if not catalog_path.exists():
        return {"plugins": [], "lastUpdated": None}
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return {"plugins": [], "lastUpdated": None}

    plugins = data.get("plugins", data) if isinstance(data, dict) else data
    last_updated = data.get("lastUpdated") if isinstance(data, dict) else None
    return {"plugins": plugins or [], "lastUpdated": last_updated}
