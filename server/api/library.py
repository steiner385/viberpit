"""Library API — plugins, skills, slash commands, MCP servers over the
shared config base. Read routes are direct filesystem scans; mutations are
atomic writes + audit events, except plugin and marketplace mutations (D-16c),
which run the ``claude plugin`` CLI on every host through the daemon RPC."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select

from viberpit import accounts, brand, creds
from server.audit import record_audit
from server.auth.identity import human_gate_via
from server.auth.middleware import current_identity
from server.codex.capabilities import CODEX_CAPABILITIES, CODEX_LIBRARY_UNSUPPORTED_DETAIL
from server.db import db_session, get_db
from server.db.models import INACTIVE_SESSION_STATUSES, Session as SessionRow
from server.library import curated, directory_api, mutate, plugin_cli, plugin_policy, scan
from server.library.mutate import ConfigUnreadableError
from server.library.snapshots import snapshot_before_mutation
from server.library.names import (
    valid_command_path,
    valid_mcp_name,
    valid_plugin_id,
    valid_skill_name,
)
from server.config import get_settings
from server.ipc.client import DaemonTimeout
from server.api.repos import known_repo_slugs
from server.mcp_auth import McpAuthBusy
from server.mcp_scope import store as scope_store
from server.mcp_scope.resolve import McpToolRules, tools_wire
from server.mcp_scope.servers import configured_server_keys
from server.mcp_health import grant_health
from server.mcp_health import grants as mcp_grants
from server.mcp_health import overall as mcp_overall
from server.mcp_health import store as mcp_store
from server.sessions.facade import call_with_deadline
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.flag_settings import MCP_TOOL_UNKNOWN, MCP_TOOLS_UNAVAILABLE
from server.sessions.session_actions import (SESSION_CONTROL_HUMAN_ONLY_CODE, ActionError,
                                             require_human)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])


def _require_known_account(account: str | None) -> None:
    """A request-supplied account must be an exact member of the known account set: the
    name later becomes a path under the account store, so anything else (``../x``, ``a/b``,
    ``""``) is refused before any probe or config-dir write can run."""
    if account is None:
        return
    from viberpit import accounts
    if account not in accounts._anthropic_account_names():
        raise HTTPException(status_code=400, detail={"error": "unknown_account"})


async def _status_rows() -> list[dict]:
    return await mcp_store.rows() if get_settings().mcp_status_enabled else []


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
async def get_plugins(_identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    """Installed plugins and configured marketplaces. Each marketplace carries its
    ``normalizedSource`` (what a Confirm must echo back), ``curation``
    (``confirmed`` | ``unconfirmed`` | ``source_changed``) and the ``confirmedSource`` on
    record; ``installEnabled`` is the kill-switch signal."""
    out = await asyncio.to_thread(scan.list_plugins)
    records = await curated.confirmed(db)
    for m in out["marketplaces"]:
        norm = plugin_policy.normalize_source(m.get("source"))
        m["normalizedSource"] = norm
        m["confirmedSource"] = records.get(m["name"])
        m["curation"] = curated.curation_state(m["name"], records, norm)
    out["installEnabled"] = get_settings().plugin_install_enabled
    return out


# ---------------------------------------------------------------------------
# Plugin marketplace (D-16c). The CLI wrapper (server/library/plugin_cli.py) is the only
# writer, fanned out to every host (orch.plugin_op_everywhere); plugin_policy decides
# what may be added or installed — here, and again on every host against its own state.
# Every mutation is human-only (not even the local-host opt-in) and audited.
# ---------------------------------------------------------------------------

PLUGIN_INSTALL_DISABLED = "plugin_install_disabled"


class ConfirmBody(BaseModel):
    confirm: bool = False


class MarketplaceAddBody(BaseModel):
    source: str
    confirm: bool = False


class MarketplaceConfirmBody(BaseModel):
    source: str  # the normalizedSource the UI showed
    confirm: bool = False


def _install_off() -> JSONResponse | None:
    """Kill switch: install, update, add and confirm routes answer 404 while it is off."""
    if get_settings().plugin_install_enabled:
        return None
    return JSONResponse(status_code=404, content={"error": PLUGIN_INSTALL_DISABLED})


def _plugin_human_gate(request: Request, identity: str) -> str | None:
    """Only an attested person may change what code every session on every host runs;
    the local-host opt-in is not honoured (``trust_local_host=False``). Returns ``via``."""
    via = human_gate_via(request)
    try:
        require_human(identity, via, action="install, update, remove or toggle plugins",
                      code=SESSION_CONTROL_HUMAN_ONLY_CODE, trust_local_host=False)
    except ActionError as exc:
        log.info("library.plugin_refused code=%s identity=%s via=%s", exc.code, identity, via)
        raise HTTPException(status_code=exc.http,
                            detail={"code": exc.code, "message": exc.message}) from None
    return via


def _policy_http(exc: plugin_policy.PolicyRefusal, subject: str) -> HTTPException:
    log.info("library.plugin_refused code=%s reason=%s subject=%s", exc.code, exc.reason, subject)
    detail = {"code": exc.code, "message": exc.message}
    if exc.reason:
        detail["reason"] = exc.reason
    return HTTPException(status_code=exc.http, detail=detail)


def _require_confirm(confirm: bool, subject: str) -> None:
    if not confirm:
        raise _policy_http(plugin_policy.PolicyRefusal(
            plugin_policy.CONFIRM_REQUIRED, "Confirm after reviewing what this runs.", 400), subject)


def _valid_plugin_or_400(plugin_id: str) -> None:
    if not valid_plugin_id(plugin_id):
        raise _bad("invalid plugin id")


def _valid_marketplace_or_400(name: str) -> None:
    if not plugin_policy.valid_marketplace_name(name):
        raise _bad("invalid marketplace name")


async def _fan_out(request: Request, op: str, args: dict) -> dict:
    """Snapshot, then run *op* on every host. ``ok`` is true only when at least one host
    answered and every host reported ``ok`` or ``noop``; a partial failure is reported,
    never rolled back, and zero hosts is never success (nothing was changed)."""
    await asyncio.to_thread(snapshot_before_mutation)  # #604 pre-mutation safety net
    # Capture lazily — FleetOrchestrator replaces app.state.sessions after router build.
    orch = request.app.state.sessions
    hosts = await orch.plugin_op_everywhere(op, args)
    return {"op": op, "hosts": hosts, "ok": _all_ok(hosts)}


def _all_ok(hosts: list[dict]) -> bool:
    return bool(hosts) and all(h.get("outcome") in ("ok", "noop") for h in hosts)


async def _check_install(db, plugin_id: str) -> dict:
    """``plugin_policy.check_install`` on the edge, over the confirmed records and this
    host's catalogs (the Directory cache refreshed first when its TTL has passed). Returns
    the RPC args: every host re-checks against its own state, so a curated marketplace's
    confirmed source rides along as ``expected_source``."""
    records = await curated.confirmed(db)
    marketplace = plugin_id.partition("@")[2]
    if marketplace == plugin_policy.DIRECTORY_MARKETPLACE:
        await asyncio.to_thread(plugin_cli.refresh_available)
    catalog = await asyncio.to_thread(scan.install_catalog, records)
    try:
        plugin_policy.check_install(plugin_id, records, catalog)
    except plugin_policy.PolicyRefusal as exc:
        raise _policy_http(exc, plugin_id) from None
    if marketplace == plugin_policy.DIRECTORY_MARKETPLACE:
        # The cache passed, but it is forgeable: the live Directory must agree (fail closed).
        listing = (catalog.directory or {})[plugin_id.partition("@")[0]]
        live = await directory_api.fetch_listing(str(listing.get("id")))
        if live is None:
            log.info("library.plugin_refused code=%s subject=%s",
                     plugin_policy.DIRECTORY_UNAVAILABLE, plugin_id)
            raise HTTPException(status_code=503, detail={
                "code": plugin_policy.DIRECTORY_UNAVAILABLE,
                "message": "The Anthropic Directory could not be reached to verify this "
                           "listing's review; try again."})
        try:
            plugin_policy.check_directory_listing(listing, live)
        except plugin_policy.PolicyRefusal as exc:
            raise _policy_http(exc, plugin_id) from None
    args = {"plugin_id": plugin_id}
    if marketplace in records:
        args["expected_source"] = records[marketplace]
    return args


@router.get("/plugins/browse")
async def browse_plugins(
    q: str | None = Query(default=None, max_length=200),
    marketplace: str | None = Query(default=None, max_length=64),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=scan.BROWSE_LIMIT_MAX),
    _identity: str = Depends(current_identity), db=Depends(get_db),
) -> dict:
    """Installable plugins: confirmed marketplaces (source unchanged) plus REVIEWED
    Directory listings only (unreviewed ones never reach the client). Empty while the kill
    switch is off."""
    if not get_settings().plugin_install_enabled:
        return {"plugins": [], "total": 0, "offset": offset, "limit": limit,
                "installEnabled": False}
    records = await curated.confirmed(db)
    await asyncio.to_thread(plugin_cli.refresh_available)  # no-op within the TTL
    entries = await asyncio.to_thread(scan.browse_entries, records)
    page = scan.browse_page(entries, q=q, marketplace=marketplace, offset=offset, limit=limit)
    return {**page, "installEnabled": True}


@router.post("/plugins/{plugin_id}/install")
async def install_plugin(plugin_id: str, body: ConfirmBody, request: Request,
                         identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if (off := _install_off()) is not None:
        return off
    via = _plugin_human_gate(request, identity)
    _valid_plugin_or_400(plugin_id)
    _require_confirm(body.confirm, plugin_id)
    args = await _check_install(db, plugin_id)
    out = await _fan_out(request, "install", args)
    await record_audit(db=db, type="library_plugin_installed", identity=identity,
                       detail={"pluginId": plugin_id, "hosts": out["hosts"]}, via=via)
    return out


@router.post("/plugins/{plugin_id}/update")
async def update_plugin(plugin_id: str, body: ConfirmBody, request: Request,
                        identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if (off := _install_off()) is not None:
        return off
    via = _plugin_human_gate(request, identity)
    _valid_plugin_or_400(plugin_id)
    _require_confirm(body.confirm, plugin_id)
    args = await _check_install(db, plugin_id)  # an update pulls new code: same gate
    out = await _fan_out(request, "update", args)
    await record_audit(db=db, type="library_plugin_updated", identity=identity,
                       detail={"pluginId": plugin_id, "hosts": out["hosts"]}, via=via)
    return out


@router.delete("/plugins/{plugin_id}")
async def uninstall_plugin(plugin_id: str, request: Request,
                           identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    """Uninstall on every host, keeping the plugin's data (never pruned from the UI)."""
    via = _plugin_human_gate(request, identity)
    _valid_plugin_or_400(plugin_id)
    out = await _fan_out(request, "uninstall", {"plugin_id": plugin_id})
    await record_audit(db=db, type="library_plugin_uninstalled", identity=identity,
                       detail={"pluginId": plugin_id, "hosts": out["hosts"]}, via=via)
    return out


class ReloadBody(BaseModel):
    holdOnCacheImpact: bool = True  # keep a started session's plugins when that would bust its cache


@router.post("/plugins/reload")
async def reload_plugins(body: ReloadBody, request: Request,
                         identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    """Reload plugins in every running session on every host. Installs nothing, so it is
    not behind the install kill switch; it is still human-only because it changes what
    code a live session runs."""
    via = _plugin_human_gate(request, identity)
    orch = request.app.state.sessions  # captured lazily: the fleet replaces it after build
    rows = await orch.reload_plugins_everywhere(hold_on_cache_impact=body.holdOnCacheImpact)
    await record_audit(db=db, type="library_plugins_reloaded", identity=identity,
                       detail={"holdOnCacheImpact": body.holdOnCacheImpact, "sessions": rows},
                       via=via)
    ok = bool(rows) and not any(r.get("outcome") == "failed" for r in rows)
    return {"op": "reload_plugins", "ok": ok, "sessions": rows}


async def _vetted_source(source: object) -> str:
    """``check_marketplace_source`` — including its blocking DNS check — off the loop."""
    try:
        return await asyncio.to_thread(plugin_policy.check_marketplace_source, source)
    except plugin_policy.PolicyRefusal as exc:
        raise _policy_http(exc, repr(source)[:120]) from None


@router.post("/marketplaces")
async def add_marketplace(body: MarketplaceAddBody, request: Request,
                          identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    """Add a marketplace on every host and confirm it at the source the owner was shown
    (the UI shows ``source`` verbatim before the confirm). Only a GitHub owner/repo or an
    https git URL is accepted."""
    if (off := _install_off()) is not None:
        return off
    via = _plugin_human_gate(request, identity)
    source = await _vetted_source(body.source)
    _require_confirm(body.confirm, source)
    normalized = plugin_policy.normalize_source(source)
    if normalized is None:  # unreachable for a vetted source; fail closed regardless
        raise _policy_http(plugin_policy.PolicyRefusal(
            plugin_policy.MARKETPLACE_SOURCE_REFUSED, "Unrecognized source.", 400), source)
    out = await _fan_out(request, "marketplace_add", {"source": source})
    for host in out["hosts"]:  # the name each host's CLI actually recorded
        name = host.get("marketplace")
        if host.get("outcome") not in ("ok", "noop") or not isinstance(name, str):
            continue
        try:
            plugin_policy.check_marketplace_name(name, normalized)
        except plugin_policy.PolicyRefusal as exc:
            log.info("library.plugin_refused code=%s host=%s name=%r", exc.code, host.get("host"), name)
            host.update(outcome="failed", failureCode=exc.code,
                        message=f"{exc.message} Added on this host, but not confirmed.")
    out["ok"] = _all_ok(out["hosts"])
    names = sorted({h["marketplace"] for h in out["hosts"]
                    if h.get("outcome") in ("ok", "noop") and isinstance(h.get("marketplace"), str)
                    and plugin_policy.valid_marketplace_name(h["marketplace"])})
    for name in names:
        await curated.confirm(db, name, normalized)
    out["marketplaces"] = names
    await record_audit(db=db, type="library_marketplace_added", identity=identity,
                       detail={"source": source, "normalizedSource": normalized,
                               "marketplaces": names, "hosts": out["hosts"]}, via=via)
    return out


@router.post("/marketplaces/{name}/confirm")
async def confirm_marketplace(name: str, body: MarketplaceConfirmBody, request: Request,
                              identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    """Confirm a marketplace configured outside ViberPit (or re-confirm one whose source
    changed) at the source the UI showed. ``body.source`` must equal its CURRENT
    ``normalizedSource``, so a swap between showing and clicking is refused; the source
    must pass the same policy as an add."""
    if (off := _install_off()) is not None:
        return off
    via = _plugin_human_gate(request, identity)
    _valid_marketplace_or_400(name)
    _require_confirm(body.confirm, name)
    config = (await asyncio.to_thread(scan.known_marketplaces)).get(name)
    if config is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_marketplace"})
    current = plugin_policy.normalize_source(config.get("source"))
    if current is None or body.source != current:
        raise _policy_http(plugin_policy.PolicyRefusal(
            plugin_policy.MARKETPLACE_NOT_CURATED,
            f"Marketplace {name!r} no longer points at the source shown — source changed, "
            "review it again.", reason=plugin_policy.REASON_SOURCE_CHANGED), name)
    await _vetted_source(plugin_policy.add_form(current))
    try:  # the CONFIGURED name, not just the one the repo would get
        plugin_policy.check_marketplace_name(name, current)
    except plugin_policy.PolicyRefusal as exc:
        raise _policy_http(exc, name) from None
    await curated.confirm(db, name, current)
    await record_audit(db=db, type="library_marketplace_confirmed", identity=identity,
                       detail={"name": name, "normalizedSource": current}, via=via)
    return {"name": name, "confirmedSource": current, "curation": curated.CONFIRMED}


@router.post("/marketplaces/{name}/update")
async def update_marketplace(name: str, request: Request,
                             identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if (off := _install_off()) is not None:
        return off
    via = _plugin_human_gate(request, identity)
    _valid_marketplace_or_400(name)
    records = await curated.confirmed(db)
    try:
        plugin_policy.check_curated(name, records, await asyncio.to_thread(scan.marketplace_sources))
    except plugin_policy.PolicyRefusal as exc:
        raise _policy_http(exc, name) from None
    out = await _fan_out(request, "marketplace_update",
                         {"name": name, "expected_source": records[name]})
    await record_audit(db=db, type="library_marketplace_updated", identity=identity,
                       detail={"name": name, "hosts": out["hosts"]}, via=via)
    return out


@router.delete("/marketplaces/{name}")
async def remove_marketplace(name: str, request: Request,
                             identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    """Remove on every host; the confirmed record goes whatever the hosts answer (the
    safe direction)."""
    via = _plugin_human_gate(request, identity)
    _valid_marketplace_or_400(name)
    out = await _fan_out(request, "marketplace_remove", {"name": name})
    await curated.remove(db, name)
    await record_audit(db=db, type="library_marketplace_removed", identity=identity,
                       detail={"name": name, "hosts": out["hosts"]}, via=via)
    return out


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
    plugin_id: str, body: ToggleBody, request: Request,
    identity: str = Depends(current_identity), db=Depends(get_db),
) -> Any:
    """Enable/disable on every host through the CLI (the single writer, D-16c). Stays
    available while the install kill switch is off."""
    via = _plugin_human_gate(request, identity)
    _valid_plugin_or_400(plugin_id)
    known = {p["id"] for p in (await asyncio.to_thread(scan.list_plugins))["plugins"]}
    if plugin_id not in known:
        raise HTTPException(status_code=404, detail={"error": "unknown_plugin"})
    out = await _fan_out(request, "enable" if body.enabled else "disable", {"plugin_id": plugin_id})
    await record_audit(db=db, type="library_plugin_toggled", identity=identity,
                       detail={"pluginId": plugin_id, "enabled": body.enabled,
                               "hosts": out["hosts"]}, via=via)
    return {"id": plugin_id, "enabled": body.enabled, **out}


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
    await asyncio.to_thread(mutate.write_skill, name, body.content)
    await record_audit(db=db, type="library_skill_saved", identity=identity,
                       detail={"skill": name})
    return {"name": name}


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str,
                       identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    if not await asyncio.to_thread(mutate.delete_skill, name):
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
    await asyncio.to_thread(mutate.write_command, path, body.content)
    await record_audit(db=db, type="library_command_saved", identity=identity,
                       detail={"command": path})
    return {"path": path}


@router.delete("/commands/{path:path}", status_code=204)
async def delete_command(path: str,
                         identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    if not await asyncio.to_thread(mutate.delete_command, path):
        raise HTTPException(status_code=404, detail={"error": "unknown_command"})
    await record_audit(db=db, type="library_command_deleted", identity=identity,
                       detail={"command": path})


async def _enabled_accounts() -> set[str]:
    # list_accounts takes the ledger lock; keep it off the event loop.
    return {a["name"] for a in await asyncio.to_thread(accounts.list_accounts) if a["enabled"]}


async def _live_session_ids() -> set[str]:
    """Ids of sessions that are neither ended nor archived, read from the DB (remote orchestrators lack seams)."""
    async with db_session() as db:
        return set((await db.execute(
            select(SessionRow.id).where(SessionRow.status.not_in(INACTIVE_SESSION_STATUSES)))).scalars())


@router.get("/mcp")
async def get_mcp(_identity: str = Depends(current_identity)) -> dict:
    out = scan.list_mcp_servers(status_rows=await _status_rows())
    enabled = await _enabled_accounts()
    live = await _live_session_ids()
    try:
        store = await asyncio.to_thread(creds.read_shared_mcp)
    except Exception:
        log.warning("shared MCP store unreadable; reporting no grants", exc_info=True)
        store = {}
    if not isinstance(store, dict):
        log.warning("shared MCP store is not an object; reporting no grants")
        store = {}
    try:
        health = await grant_health.load()
    except Exception:
        # Health only refines the grant state; the MCP list must not fail without it.
        log.warning("mcp grant health unreadable; reporting grants without it", exc_info=True)
        health = {}
    now_ms = int(time.time() * 1000)
    for server in out["servers"]:
        key = mcp_grants.key_for(server)  # None: no knowable CLI key, so no grant lookup
        hrow = health.get(key) if key else None
        grant = mcp_overall.grant_state(store, key=key, now_ms=now_ms, health=hrow) if key else "none"
        for row in server["status"]:
            row["counted"] = mcp_overall.counted(row, enabled_accounts=enabled, live_session_ids=live, now_ms=now_ms)
        server["overall"] = mcp_overall.derive_overall(
            grant=grant,
            rows=[r for r in server["status"] if r["counted"]], now_ms=now_ms,
            expires_at_ms=mcp_overall.best_expiry(store, key=key) if key else None,
            refresh_error=mcp_overall.refresh_error_for(grant, hrow))
    return out


class McpTestBody(BaseModel):
    account: str | None = None
    server: str | None = None


@router.post("/mcp/test")
async def mcp_test(body: McpTestBody, request: Request,
                   _identity: str = Depends(current_identity)) -> dict:
    """Run the no-prompt probe on every host (spec 2026-10-09 §3), bounded at 35 s."""
    _require_known_account(body.account)
    if not get_settings().mcp_status_enabled:
        return {"rows": [], "skipped": []}
    # Capture lazily — FleetOrchestrator replaces app.state.sessions after router build.
    orch = request.app.state.sessions
    try:
        # Deadline pushed INTO the RPC, never wait_for around it (#734).
        out = await call_with_deadline(orch.probe_mcp, body.account, timeout=35.0)
    except (asyncio.TimeoutError, DaemonTimeout):
        raise HTTPException(status_code=504, detail={"error": "mcp_probe_timeout"})
    if body.server:
        want = mcp_store.short_name(body.server)
        out = {**out, "rows": [r for r in out["rows"] if mcp_store.short_name(r["server"]) == want]}
    return out


@router.put("/mcp/{name}")
async def put_mcp(name: str, body: McpBody,
                  identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    error = mutate.validate_mcp_config(body.config)
    if error:
        raise HTTPException(status_code=400, detail={"error": "invalid_config", "message": error})
    try:
        await asyncio.to_thread(mutate.put_mcp_server, name, body.config)
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
        found = await asyncio.to_thread(mutate.delete_mcp_server, name)
    except ConfigUnreadableError as err:
        return JSONResponse(status_code=409, content={"error": "config_unreadable", "path": err.path.name})
    if not found:
        raise HTTPException(status_code=404, detail={"error": "unknown_server"})
    await scope_store.delete_server(name)  # user servers: scope key == name
    await record_audit(db=db, type="library_mcp_deleted", identity=identity,
                       detail={"name": name})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Per-repo MCP scope rules (spec 2026-10-09). Every path has a literal "scope"
# segment, so none collides with /mcp/{name}.
# ---------------------------------------------------------------------------

_SLUG_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


def _scope_off() -> JSONResponse | None:
    if get_settings().mcp_scope_enabled:
        return None
    return JSONResponse(status_code=404, content={"error": "mcp_scope_disabled"})


async def _scope_server_or_400(server: str) -> None:
    # Membership only — `server` may be `plugin:p:s` and is never treated as a path.
    if server not in await asyncio.to_thread(configured_server_keys):
        raise HTTPException(status_code=400, detail={"error": "unknown_server"})


def _scope_slug_or_400(owner: str, name: str) -> str:
    """Shape-check owner/name BEFORE any store or lookup; returns the lower-cased slug."""
    if any(not _SLUG_PART.match(p) or p in (".", "..") for p in (owner, name)):
        raise HTTPException(status_code=400, detail={"error": "invalid_repo"})
    return f"{owner}/{name}".lower()


async def _scope_repo_or_400(request: Request, owner: str, name: str) -> str:
    """Write-side guard (typos): shape, then require a repo the gh creds know.
    DELETE uses only the shape check so a rule for a lost/renamed repo stays removable."""
    slug = _scope_slug_or_400(owner, name)
    if slug not in await known_repo_slugs(request.app.state):
        raise HTTPException(status_code=400, detail={"error": "unknown_repo"})
    return slug


class McpScopeDefaultBody(BaseModel):
    defaultEnabled: bool


class McpScopeRuleBody(BaseModel):
    enabled: bool


@router.get("/mcp/scope")
async def get_mcp_scope(_identity: str = Depends(current_identity)) -> Any:
    if (off := _scope_off()) is not None:
        return off
    keys = await asyncio.to_thread(configured_server_keys)
    defaults = await scope_store.server_defaults()
    # D-16b: each server's last-seen tool list (Settings has no live session) with its
    # Settings default per tool; ``tools: null`` until a probe or a live session saw one.
    rules = McpToolRules(defaults=await scope_store.tool_defaults())
    seen = await _last_seen_tools()
    return {"servers": {k: {"defaultEnabled": defaults.get(k, True),
                            "tools": tools_wire(k, seen[k][0] if k in seen else None, rules),
                            "toolsSeenAt": seen[k][1] if k in seen else None}
                        for k in keys},
            "rules": await scope_store.all_rules()}


async def _last_seen_tools() -> dict:
    return await mcp_store.last_seen_tools() if get_settings().mcp_status_enabled else {}


@router.put("/mcp/scope/{server}")
async def put_mcp_scope_default(server: str, body: McpScopeDefaultBody,
                                identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if (off := _scope_off()) is not None:
        return off
    await _scope_server_or_400(server)
    await scope_store.set_default(server, body.defaultEnabled)
    await record_audit(db=db, type="library_mcp_scope_saved", identity=identity,
                       detail={"server": server})
    return Response(status_code=204)


@router.put("/mcp/{server}/tools/{tool}")
async def put_mcp_tool_default(server: str, tool: str, body: McpScopeRuleBody, request: Request,
                               identity: str = Depends(current_identity),
                               db=Depends(get_db)) -> Any:
    """The Settings default of one MCP tool (D-16b): new Claude sessions start with it,
    and a session can override it live. Human-only both ways (``require_human``). 204;
    404 kill switch, 400 unknown server, 403 human-only, 409 ``MCP_TOOLS_UNAVAILABLE``
    when no tool list was seen for the server yet, ``MCP_TOOL_UNKNOWN`` when the
    last-seen list lacks the tool."""
    if (off := _scope_off()) is not None:
        return off
    via = human_gate_via(request)
    try:
        require_human(identity, via, action="change MCP tool defaults",
                      code=SESSION_CONTROL_HUMAN_ONLY_CODE)
    except ActionError as exc:
        raise HTTPException(status_code=exc.http,
                            detail={"code": exc.code, "message": exc.message}) from None
    await _scope_server_or_400(server)
    seen = (await _last_seen_tools()).get(server)
    if seen is None:
        raise HTTPException(status_code=409, detail={
            "code": MCP_TOOLS_UNAVAILABLE, "message": "Test the server to load its tools."})
    if tool not in {t.name for t in seen[0]}:
        raise HTTPException(status_code=409, detail={
            "code": MCP_TOOL_UNKNOWN, "message": f"{server!r} has no tool {tool!r}"})
    await scope_store.set_tool_default(server, tool, body.enabled)
    await record_audit(db=db, type="library_mcp_tool_saved", identity=identity,
                       detail={"server": server, "tool": tool, "enabled": body.enabled},
                       via=via)
    return Response(status_code=204)


@router.put("/mcp/scope/{server}/repos/{owner}/{name}")
async def put_mcp_scope_rule(server: str, owner: str, name: str, body: McpScopeRuleBody,
                             request: Request,
                             identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if (off := _scope_off()) is not None:
        return off
    await _scope_server_or_400(server)
    repo = await _scope_repo_or_400(request, owner, name)
    await scope_store.set_repo_rule(repo, server, body.enabled)
    await record_audit(db=db, type="library_mcp_scope_saved", identity=identity,
                       detail={"server": server, "repo": repo})
    return Response(status_code=204)


@router.delete("/mcp/scope/{server}/repos/{owner}/{name}")
async def delete_mcp_scope_rule(server: str, owner: str, name: str,
                                identity: str = Depends(current_identity), db=Depends(get_db)) -> Any:
    if (off := _scope_off()) is not None:
        return off
    await _scope_server_or_400(server)
    repo = _scope_slug_or_400(owner, name)
    if not await scope_store.delete_repo_rule(repo, server):
        raise HTTPException(status_code=404, detail={"error": "unknown_rule"})
    await record_audit(db=db, type="library_mcp_scope_saved", identity=identity,
                       detail={"server": server, "repo": repo})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# MCP OAuth auth flow (one-at-a-time, via McpAuthManager on app.state)
# ---------------------------------------------------------------------------


class McpAuthStartBody(BaseModel):
    account: str | None = None


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
    body: McpAuthStartBody | None = None,
    identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> Any:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    _require_known_account(body.account if body else None)
    try:
        scan.sign_in_server(
            scan.list_mcp_servers(status_rows=await _status_rows())["servers"], name)
    except scan.SignInServerError as exc:
        if exc.reason == "not_found":
            raise HTTPException(status_code=404, detail={"error": "mcp_server_not_found"}) from None
        if exc.reason == "ambiguous":
            raise HTTPException(status_code=409, detail={
                "error": "mcp_server_ambiguous",
                "message": "Multiple servers have this name. Give the user-configured server a unique name before signing in.",
            }) from None
        raise HTTPException(status_code=400, detail={
            "error": "mcp_oauth_unsupported",
            "message": "Browser OAuth requires an HTTP or SSE server. Configure this server's credentials on the session host.",
        }) from None
    try:
        preferred = ([body.account] if body and body.account is not None else []) + \
            (await mcp_store.needs_auth_by_server(
                enabled_accounts=await _enabled_accounts())).get(name, [])
        state = await _mcp_auth(request).start(
            name, preferred=preferred, login_key=await mcp_store.login_key_for(name))
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
