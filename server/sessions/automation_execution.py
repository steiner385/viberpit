"""Execution fencing at the native boundary for owned automation sessions."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any


class AutomationExecutionFence:
    def __init__(self, before_execution: Callable[[], Awaitable[bool | None]] | None = None,
                 after_close: Callable[[Any], Awaitable[None]] | None = None):
        self.before_execution = before_execution
        self.after_close = after_close
        self.frozen = False

    async def enter(self) -> bool:
        if self.frozen:
            raise RuntimeError("automation execution was closed for ownership reconciliation")
        known = await self.before_execution() if self.before_execution is not None else False
        # A reconciliation can freeze while the durable generation write awaits.
        if self.frozen:
            raise RuntimeError("automation execution was closed for ownership reconciliation")
        return known is True

    async def closed(self, driver: Any) -> None:
        if self.after_close is not None:
            try:
                await self.after_close(driver)
            except Exception:
                logging.getLogger(__name__).warning("automation close proof unknown", exc_info=True)
