"""One fake ``AutomationContext`` for the orchestrator spawn-seam tests.

There were two of these — an identical ``_SpawnRec`` in
``test_pm_orchestrator_seams.py`` and another in ``test_pm_triage_seams.py`` —
and that duplication cost a red CI shard: #1268/#1269 moved the engine
resolution ahead of the account probe and the clone, so every PM spawn site now
makes **two** calls on its context, and only one of the two copies was taught the
new contract. A fake that stands in for a real contract is a source of truth
about that contract, so there is one (constitution VI).

The contract it models, in call order:

1. ``_existing_session(key)`` → the live session already spawned for a dedupe key,
   or ``None``. ``spawn_session`` consults it itself; a caller that must not pay
   for the spawn at all (the materialize seam) asks first.
2. ``launch_plan_or_refuse(origin_ref)`` → ``(engine, config)``, or ``None`` when
   the origin's audited ceiling refuses the launch. A caller must treat ``None``
   as "no session exists" and spend nothing further.
3. ``spawn_or_join_session(**kwargs)`` → ``(session record or None, created)``.
   ``spawn_session(**kwargs)`` is a thin wrapper that discards ``created``, for a
   caller that only wants the session and never chains a follow-up onto it.
4. ``emit(level, summary, **kw)`` → one activity event (best-effort in the real
   context, recorded here so a test can assert what the feed was told).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from viberpit.engines import AgentEngine


class FakeAutomationContext:
    """Records both calls a spawn site makes; answers *engine* for the plan.

    *refuse* makes ``launch_plan_or_refuse`` answer ``None``, which is how a
    test asserts that a refused launch neither probes an account nor clones.
    """

    def __init__(self, engine: AgentEngine = AgentEngine.CLAUDE_CODE, *,
                 refuse: bool = False, config: dict | None = None,
                 existing: Any = None,
                 automation_id: str = "sdlc-synthesizer") -> None:
        #: The ``Session.origin`` this context's sessions carry — a spawn site
        #: that asks the DB about its own live sessions (the materialize gate,
        #: C4-R14) filters on it.
        self.automation_id = automation_id
        self._engine = engine
        self._refuse = refuse
        self._config = config if config is not None else {}
        self._existing = existing
        #: ``origin_ref`` per ``launch_plan_or_refuse`` call, in order.
        self.plans: list[str] = []
        #: kwargs per ``spawn_session`` call, in order.
        self.calls: list[dict] = []
        #: ``(level, summary, kwargs)`` per ``emit`` call, in order.
        self.events: list[tuple[str, str, dict]] = []

    async def _existing_session(self, key: str) -> Any:
        return self._existing

    async def launch_plan_or_refuse(self, origin_ref: str) -> tuple[Any, dict] | None:
        self.plans.append(origin_ref)
        if self._refuse:
            return None
        return self._engine, self._config

    async def spawn_or_join_session(self, **kw: Any) -> tuple[Any, bool]:
        """Find-or-create, modelled: an *existing* record whose ``origin_ref``
        matches this call's dedupe key is handed BACK instead of spawning, with
        ``created=False``.

        That is what the real context does, and a fake that skipped it hid the
        C4-R15 resurrection bug — a finished session returned by the dedupe, then
        re-ingested into a byte-identical proposal — and, since #2235, would also
        hide a caller re-chaining a follow-up ingest onto a session it didn't
        actually spawn."""
        self.calls.append(kw)
        key = kw.get("dedupe_key") or kw.get("origin_ref")
        if (self._existing is not None
                and getattr(self._existing, "origin_ref", key) == key):
            return self._existing, False
        return SimpleNamespace(id=f"sess-{len(self.calls)}", model=None,
                               origin_ref=kw.get("origin_ref"), status="idle"), True

    async def spawn_session(self, **kw: Any) -> Any:
        rec, _created = await self.spawn_or_join_session(**kw)
        return rec

    async def emit(self, level: str, summary: str, **kw: Any) -> None:
        self.events.append((level, summary, kw))
