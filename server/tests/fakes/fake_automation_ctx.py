"""One fake ``AutomationContext`` for the orchestrator spawn-seam tests.

There were two of these — an identical ``_SpawnRec`` in
``test_pm_orchestrator_seams.py`` and another in ``test_pm_triage_seams.py`` —
and that duplication cost a red CI shard: #1268/#1269 moved the engine
resolution ahead of the account probe and the clone, so every PM spawn site now
makes **two** calls on its context, and only one of the two copies was taught the
new contract. A fake that stands in for a real contract is a source of truth
about that contract, so there is one (constitution VI).

The contract it models, in call order:

1. ``launch_plan_or_refuse(origin_ref)`` → ``(engine, config)``, or ``None`` when
   the origin's audited ceiling refuses the launch. A caller must treat ``None``
   as "no session exists" and spend nothing further.
2. ``spawn_session(**kwargs)`` → the session record, or ``None``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ccswitch.engines import AgentEngine


class FakeAutomationContext:
    """Records both calls a spawn site makes; answers *engine* for the plan.

    *refuse* makes ``launch_plan_or_refuse`` answer ``None``, which is how a
    test asserts that a refused launch neither probes an account nor clones.
    """

    def __init__(self, engine: AgentEngine = AgentEngine.CLAUDE_CODE, *,
                 refuse: bool = False, config: dict | None = None) -> None:
        self._engine = engine
        self._refuse = refuse
        self._config = config if config is not None else {}
        #: ``origin_ref`` per ``launch_plan_or_refuse`` call, in order.
        self.plans: list[str] = []
        #: kwargs per ``spawn_session`` call, in order.
        self.calls: list[dict] = []

    async def launch_plan_or_refuse(self, origin_ref: str) -> tuple[Any, dict] | None:
        self.plans.append(origin_ref)
        if self._refuse:
            return None
        return self._engine, self._config

    async def spawn_session(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return SimpleNamespace(id=f"sess-{len(self.calls)}", model=None)
