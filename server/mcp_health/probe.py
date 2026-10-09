"""On-demand MCP health probe (spec 2026-10-09 §3).

Opens a ClaudeSDKClient under an account's EXISTING cfg dir, never sends a prompt (control
requests start no turn — verified in docs/superpowers/spikes/2026-10-09-mcp-status-verification.md),
polls get_mcp_status until nothing is pending, and closes. It never composes or writes the
cfg dir: a concurrent OAuth login or live session may own it."""
from __future__ import annotations

import asyncio
import inspect
import logging
import weakref
from dataclasses import replace
from pathlib import Path

from server.hostname import self_host_name
from server.mcp_health import store
from server.mcp_health.status import McpServerState, sanitize_statuses
from server.sessions.account_auth import account_store_io, helper_env

log = logging.getLogger(__name__)
_MAX_CONCURRENT = 2
# One semaphore per running loop: the suite (and the daemon's helper threads) use many loops.
_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary())
_inflight: dict[str, asyncio.Task] = {}
_STARTUP_MARGIN_S = 8.0     # CLI startup + close, on top of the settle budget


class ProbeSkipped(Exception):
    """An account could not be probed; ``str(exc)`` is the reason reported in ``skipped``."""


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
    """A prompt-less client over a host-local MIRROR of the account's cfg dir.

    ``helper_env`` (no ``writes_back``) points CLAUDE_CONFIG_DIR at ``scratch_config``'s copy
    (``.claude.json`` mcpServers and ``.credentials.json`` mcpOAuth copied; plugins, commands
    and settings symlinked) and injects the account's setup token, so the CLI neither reads a
    torn shared file (#1005) nor rotates the real ``claudeAiOauth``. Deliberately NOT
    ``stateless_helper_options`` (strict_mcp_config + no servers would hide every server)."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from server.sessions.claude_bin import cli_path_kwargs
    from server.sessions.helper_call import helper_cwd
    env = await account_store_io(helper_env, str(cfg_dir), account)
    if env.get("CLAUDE_CONFIG_DIR") == str(cfg_dir):      # scratch fail-open / kill switch
        raise ProbeSkipped("no scratch config mirror")
    return ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd=helper_cwd(), env=env, extra_args={"no-session-persistence": None},
        **(await asyncio.to_thread(cli_path_kwargs))))


async def probe_account(account: str, *, client_factory=None, settle_s: float = 25.0,
                        interval_s: float = 2.0, budget_s: float | None = None) -> list[McpServerState]:
    """Probe one account. Concurrent callers share ONE bounded task; a cancelled caller
    leaves it running for the others."""
    task = _inflight.get(account)
    if task is None:
        budget = budget_s if budget_s is not None else settle_s + _STARTUP_MARGIN_S
        task = asyncio.ensure_future(
            _bounded(account, client_factory or _default_client, settle_s, interval_s, budget))
        _inflight[account] = task

        def _done(t: asyncio.Task, account: str = account) -> None:
            if _inflight.get(account) is t:
                del _inflight[account]
            if not t.cancelled():
                t.exception()                  # mark retrieved: no "never retrieved" warning

        task.add_done_callback(_done)
    return await asyncio.shield(task)


async def _bounded(account, factory, settle_s, interval_s, budget_s) -> list[McpServerState]:
    slot = False

    async def run() -> list[McpServerState]:
        nonlocal slot
        cfg = _cfg_dir_for(account)
        if cfg is None:
            raise LookupError("no config dir on this host")
        async with _global_sem():
            slot = True
            return await _run(account, cfg, factory, settle_s, interval_s)

    try:
        async with asyncio.timeout(max(budget_s, 0.0)):
            return await run()
    except TimeoutError:
        raise ProbeSkipped("timed out" if slot else "budget exhausted") from None


async def _run(account, cfg, factory, settle_s, interval_s) -> list[McpServerState]:
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


async def probe(accounts: list[str], *, deadline_s: float = 33.0, **kw) -> dict:
    """Probe *accounts* (concurrently, ``_MAX_CONCURRENT`` live clients) inside ONE overall
    budget. Every failure, including the DB write, is contained to its own account."""
    host = self_host_name()
    skipped: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s

    async def one(account: str) -> None:
        try:
            states = await probe_account(account, budget_s=deadline - loop.time(), **kw)
            await store.upsert_states(account, host, states, source="probe", session_id=None)
        except (LookupError, ProbeSkipped) as exc:
            skipped.append({"account": account, "reason": str(exc.args[0]) if exc.args else type(exc).__name__})
        except Exception as exc:
            log.debug("mcp probe failed account=%s", account, exc_info=True)
            skipped.append({"account": account, "reason": type(exc).__name__})

    await asyncio.gather(*(one(a) for a in accounts))
    return {"rows": [r for r in await store.rows() if r["host"] == host], "skipped": skipped}
