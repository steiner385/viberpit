"""Static registry of code-defined automations + startup row upsert."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Automation as AutomationRow

from .base import Automation
from .context import AutomationContext


class AutomationRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, Automation] = {}

    def register(self, automation: Automation) -> None:
        if automation.id in self._by_id:
            raise ValueError(f"automation {automation.id!r} already registered")
        self._by_id[automation.id] = automation

    def get(self, automation_id: str) -> Automation:
        return self._by_id[automation_id]

    def all(self) -> list[Automation]:
        return list(self._by_id.values())

    def context_for(self, automation_id: str, *, create_session=None) -> AutomationContext:
        return AutomationContext(automation_id, create_session=create_session)

    async def ensure_rows(self) -> None:
        """Upsert one row per registered automation. Creates absent rows with
        enabled=False; NEVER overwrites existing state (idempotent across restarts)."""
        async with db_session() as db:
            existing = set((await db.execute(select(AutomationRow.id))).scalars().all())
            for a in self._by_id.values():
                if a.id not in existing:
                    db.add(AutomationRow(id=a.id, enabled=False))
            await db.flush()


registry = AutomationRegistry()

from .pr_doctor import PrDoctor  # noqa: E402  (register the built-in automations)

registry.register(PrDoctor())
