"""On-demand MCP health probe (spec 2026-10-09 §3).

Opens a ClaudeSDKClient under an account's EXISTING cfg dir, never sends a prompt (control
requests start no turn — verified in docs/superpowers/spikes/2026-10-09-mcp-status-verification.md),
polls get_mcp_status until nothing is pending, and closes. It never composes or writes the
cfg dir: a concurrent OAuth login or live session may own it."""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import weakref
from dataclasses import replace
from pathlib import Path

from server.hostname import self_host_name
from server.mcp_health import store
from server.mcp_health.status import McpServerState, sanitize_statuses

log = logging.getLogger(__name__)
_MAX_CONCURRENT = 2
# One semaphore per running loop: the suite (and the daemon's helper threads) use many loops.
_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())
_inflight: dict[str, asyncio.Future] = {}


def _global_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = _sems[loop] = asyncio.Semaphore(_MAX_CONCURRENT)
    return sem


def _cfg_dir_for(account: str) -> Path | None:
    from viberpit import paths
    d = paths.account_cfg_dir(account)
    return d if (d / ".claude.json").is_file() else None


async def _default_client(account: str, cfg_dir: Path):
    """A prompt-less client over the account's own cfg dir. Deliberately NOT
    ``stateless_helper_options`` (that forces strict_mcp_config + no servers, which would
    hide every server), and no ``setting_sources`` override so user servers and plugins load."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from server.sessions.claude_bin import cli_path_kwargs
    from server.sessions.helper_call import helper_cwd
    return ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd=helper_cwd(), env=dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg_dir)),
        extra_args={"no-session-persistence": None},
        **(await asyncio.to_thread(cli_path_kwargs))))


async def probe_account(account: str, *, client_factory=None, settle_s: float = 25.0,
                        interval_s: float = 2.0) -> list[McpServerState]:
    if account in _inflight:
        return await asyncio.shield(_inflight[account])
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[account] = fut
    try:
        states = await _run(account, client_factory or _default_client, settle_s, interval_s)
        fut.set_result(states)
        return states
    except BaseException as exc:
        fut.set_exception(exc)
        fut.exception()                  # mark retrieved: no "never retrieved" warning
        raise
    finally:
        _inflight.pop(account, None)


async def _run(account, factory, settle_s, interval_s) -> list[McpServerState]:
    cfg = _cfg_dir_for(account)
    if cfg is None:
        raise LookupError("no config dir on this host")
    async with _global_sem():
        client = factory(account, cfg)
        if inspect.isawaitable(client):
            client = await client
        async with client:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + settle_s
            states = sanitize_statuses(await client.get_mcp_status())
            while any(s.status == "pending" for s in states) and loop.time() < deadline:
                await asyncio.sleep(interval_s)
                states = sanitize_statuses(await client.get_mcp_status())
    return [replace(s, error=s.error or f"timed out after {int(settle_s)}s") if s.status == "pending" else s
            for s in states]


async def probe(accounts: list[str], **kw) -> dict:
    host = self_host_name()
    skipped: list[dict] = []

    async def one(account: str) -> None:
        try:
            states = await probe_account(account, **kw)
        except LookupError as exc:
            skipped.append({"account": account, "reason": str(exc)})
            return
        except Exception as exc:
            log.debug("mcp probe failed account=%s", account, exc_info=True)
            skipped.append({"account": account, "reason": type(exc).__name__})
            return
        await store.upsert_states(account, host, states, source="probe", session_id=None)

    await asyncio.gather(*(one(a) for a in accounts))     # _global_sem bounds live clients
    return {"rows": [r for r in await store.rows() if r["host"] == host], "skipped": skipped}
