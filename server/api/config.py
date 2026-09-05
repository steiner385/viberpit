"""Config API — exposes server-side config that the frontend needs."""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select as sa_select

from ccswitch.engines import AgentEngine
from server.auth.middleware import current_identity
from server.config import get_settings
from server.db import db_session
from server.db.models import Host
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.engine import capabilities_to_wire
from server.sessions.host_engines import (
    engine_dispatchable,
    expected_contract_sha256,
    parse_engines_info,
)
from server.stt.engine import is_available as stt_available

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", tags=["config"])


def _capabilities_for(engine: AgentEngine) -> dict[str, bool]:
    """The engine's declared control surface, read from the driver that owns it.

    Never restated here: each driver class publishes its own ``capabilities``, and
    a second copy in the config route is exactly how a UI ends up offering a
    control the engine does not implement.
    """
    if engine is AgentEngine.CLAUDE_CODE:
        return capabilities_to_wire(ClaudeAgentDriver.capabilities)
    from server.codex.capabilities import CODEX_CAPABILITIES

    return capabilities_to_wire(CODEX_CAPABILITIES)


async def _engine_offers() -> list[dict]:
    """Which engines this deployment may currently offer, and why not.

    Aggregated over the ENABLED hosts because that is the question the launch UI is
    really asking: "if the user picks Codex, will anything accept it?". One
    compatible runner is enough — dispatch ranks and fails over across the rest.

    A registry read failure degrades to Claude-only rather than raising: the config
    route is what boots the whole browser app, and a fleet hiccup must not blank it.
    """
    try:
        async with db_session() as db:
            rows = (await db.execute(
                sa_select(Host).where(Host.enabled.is_(True)))).scalars().all()
    except Exception:  # noqa: BLE001 -- see docstring
        log.warning("config: fleet registry unreadable — offering claude only",
                    exc_info=True)
        rows = []

    offers: list[dict] = []
    for engine in AgentEngine:
        hosts: list[str] = []
        reasons: dict[str, str] = {}
        # An edge-side precondition, checked before the per-host loop because it is
        # not a property of any runner: Codex runners authenticate ONLY with token
        # envelopes minted by the credential authority, so with no authority
        # configured every capable runner in the fleet would still produce a session
        # that cannot log in. Offering it would be offering a guaranteed failure.
        blocker = _edge_blocker(engine)
        for row in ([] if blocker else rows):
            ok, why = engine_dispatchable(
                parse_engines_info(row.engines), engine,
                expected_contract_sha256=expected_contract_sha256(engine))
            if ok:
                hosts.append(row.name)
            else:
                reasons[row.name] = why or "unavailable"
        offers.append({
            "engine": engine.value,
            # Claude stays offerable on a fleet that has never been probed: the
            # compatibility default makes every runner Claude-capable, and a
            # single-host deployment mid-first-probe must still be able to launch.
            "available": bool(hosts) or (
                engine is AgentEngine.CLAUDE_CODE and not blocker),
            "hosts": sorted(hosts),
            "unavailableReasons": dict(sorted(reasons.items())),
            # Set when the whole engine is off for an edge-side reason; then
            # `unavailableReasons` is empty because no runner is at fault.
            "blockedReason": blocker,
            "capabilities": _capabilities_for(engine),
        })
    return offers


def _edge_blocker(engine: AgentEngine) -> str | None:
    """An engine-wide reason no runner could help with, or None."""
    if engine is not AgentEngine.CODEX:
        return None
    try:
        from server.codex.auth_endpoint import authority_configured
    except Exception:  # pragma: no cover — an edge built without the codex extras
        return "authority_unconfigured"
    return None if authority_configured() else "authority_unconfigured"


@router.get("")
async def get_config(_identity: str = Depends(current_identity)) -> dict:
    settings = get_settings()
    return {
        "repoRoot": settings.repo_root,
        "adoptionEnabled": settings.enable_adoption,
        # True only when the flag is on AND the `stt` extra is importable, so the UI
        # never offers a mic engine the edge cannot actually run.
        "sttEnabled": bool(settings.enable_stt and stt_available()),
        # Per-engine offerability + capabilities. The browser renders the Agent
        # selector and every engine-conditional control from THIS, so a runner that
        # cannot start Codex is never offered as a Codex choice.
        "engines": await _engine_offers(),
    }
