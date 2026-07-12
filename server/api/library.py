"""Library API — plugins, skills, slash commands, MCP servers over the
shared config base. Read routes are direct filesystem scans; mutations are
atomic writes + audit events. No orchestrator/daemon involvement."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.library import mutate, scan
from server.library.names import (
    valid_command_path,
    valid_mcp_name,
    valid_plugin_id,
    valid_skill_name,
)

router = APIRouter(prefix="/api/library", tags=["library"])


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
) -> dict:
    if not valid_plugin_id(plugin_id):
        raise _bad("invalid plugin id")
    if not mutate.set_plugin_enabled(plugin_id, body.enabled):
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
                  identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    error = mutate.validate_mcp_config(body.config)
    if error:
        raise HTTPException(status_code=400, detail={"error": "invalid_config", "message": error})
    mutate.put_mcp_server(name, body.config)
    transport = scan.infer_transport(body.config)
    # Audit name+transport only — config/headers/env may contain tokens.
    await record_audit(db=db, type="library_mcp_saved", identity=identity,
                       detail={"name": name, "transport": transport})
    return {"name": name, "transport": transport}


@router.delete("/mcp/{name}", status_code=204)
async def delete_mcp(name: str,
                     identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    if not mutate.delete_mcp_server(name):
        raise HTTPException(status_code=404, detail={"error": "unknown_server"})
    await record_audit(db=db, type="library_mcp_deleted", identity=identity,
                       detail={"name": name})
