"""Static registry of code-defined automations + startup row upsert."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Automation as AutomationRow

from .base import DEFAULT_WORK_CLASS, Automation, ConfigSchema, work_class_of
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

    def schema_for(self, automation_id: str) -> ConfigSchema:
        """THE config schema for *automation_id*: its own fields plus whatever its
        audited engine ceiling adds (Phase 7, Task 7.1).

        Every reader goes through here — the API validates a PATCH against it,
        publishes it as ``configSchema``, and ``read_automation_config`` reads
        through it — so an ``engine`` key cannot be accepted by one and rejected
        as unknown by another."""
        from .engine_policy import effective_config_schema

        return effective_config_schema(self._by_id[automation_id])

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


def work_class_for_origin(origin: str | None) -> str:
    """The work class of a session spawned by *origin* (``Session.origin``).

    A human session (``None``) and an id the registry does not know — an
    ad-hoc recurring-scheduler automation — both answer ``development``: the
    sessions list must never hide a session on a guess. See ``work_class_of``.
    """
    if origin is None:
        return DEFAULT_WORK_CLASS
    try:
        automation = registry.get(origin)
    except KeyError:
        return DEFAULT_WORK_CLASS
    return work_class_of(automation)


def origin_label(origin: str | None) -> str | None:
    """The display name of the automation *origin*, or the id itself when the
    registry does not know it, or ``None`` for a human session. The browser
    renders this rather than keeping its own id→name map."""
    if origin is None:
        return None
    try:
        return registry.get(origin).name
    except KeyError:
        return origin


def assisted_by_label(assisted_by: str | None) -> str | None:
    """``origin_label`` for ``Session.assisted_by`` — same lookup, named for
    its reader so a call site cannot pass the wrong column."""
    return origin_label(assisted_by)


from .pr_doctor import PrDoctor  # noqa: E402  (register the built-in automations)
from .backlog_runner import BacklogRunner  # noqa: E402
from .autopilot import Autopilot  # noqa: E402
from .cicd_doctor import CicdDoctor  # noqa: E402
from .archive_audit import ArchiveAudit  # noqa: E402
from .sdlc_intake import SdlcIntake  # noqa: E402
from .sdlc_synthesizer import SdlcSynthesizer  # noqa: E402
from .control_band_monitor import ControlBandMonitor  # noqa: E402
from .pm_sync import PmSync  # noqa: E402
from .pm_triage import PmTriage  # noqa: E402
from .pm_onboard import PmOnboard  # noqa: E402
from .pm_classify import PmClassify  # noqa: E402
from .gh_reconcile import GhReconcile  # noqa: E402
from .error_triage import ErrorTriage  # noqa: E402
from .deploy_monitor import DeployMonitor  # noqa: E402

registry.register(PrDoctor())
registry.register(BacklogRunner())
registry.register(Autopilot())
registry.register(CicdDoctor())
registry.register(ArchiveAudit())
registry.register(SdlcIntake())
registry.register(SdlcSynthesizer())
registry.register(ControlBandMonitor())
registry.register(PmSync())
registry.register(PmTriage())
registry.register(PmOnboard())
registry.register(PmClassify())
registry.register(GhReconcile())
registry.register(ErrorTriage())
registry.register(DeployMonitor())
