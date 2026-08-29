"""Host-aware orchestrator facade (spec: 2026-08-29-multi-host-runner-dispatch-design).

Becomes app.state.sessions in remote mode. Routes each edge->session call to the
right daemon; unrouted calls go to the primary host's REAL bound method, so sync/
async behaviour and the @rpc_deadline marker are preserved for free.
"""
from server.sessions.fleet_router import FleetRouter, NoHostAvailable


class FleetOrchestrator:
    def __init__(self, router: FleetRouter, primary: str) -> None:
        self._router = router
        self._primary = primary
        self._host_cache: dict[str, str] = {}  # session_id -> host (populated in Task 2/3)

    def _primary_orch(self):
        return self._router.get(self._primary)

    def __getattr__(self, name: str):
        # Explicit overrides (create_session, per-session, fan-out, subscribe) are real
        # methods and never reach __getattr__. Everything else = the primary's own bound
        # method: correct sync/async, and the @rpc_deadline marker travels with it.
        return getattr(self._primary_orch(), name)
