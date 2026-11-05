"""Whether release-milestone ladders refresh on their own (spec 2026-11-05 §8).

Kept out of ``server/automations/`` so the milestone routes can import it at module
top: importing anything under ``server.automations`` runs that package's registry,
which imports ``server/pm/milestone_refresh.py`` — a cycle when the milestone code is
the first thing a process loads. The automation row is read through the Milestones
Refresh automation's own ``read_config``, imported at call time.
"""
from __future__ import annotations


async def automatic_refresh_enabled() -> bool:
    """Whether ladders refresh on their own (the automation is not ``off``). The view
    reads this to say "automatic refresh is off"."""
    from server.automations.milestones_refresh import read_config
    mode, _config = await read_config()
    return mode != "off"
