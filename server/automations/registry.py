"""Static registry of code-defined automations + startup row upsert."""
from __future__ import annotations

from sqlalchemy import select

from server.db import db_session
from server.db.models import Automation as AutomationRow

from .base import DEFAULT_WORK_CLASS, Automation, ConfigSchema, work_class_of
from .context import AutomationContext
from .oversight import AutomationOversight, Relationship, Responsibility


class AutomationRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, Automation] = {}
        self._oversight: dict[str, AutomationOversight] = {}

    def register(self, automation: Automation, *,
                 oversight: AutomationOversight | None = None) -> None:
        if automation.id in self._by_id:
            raise ValueError(f"automation {automation.id!r} already registered")
        if oversight is not None:
            if set(oversight.mode_descriptions) != set(automation.modes):
                raise ValueError(f"{automation.id!r}: oversight descriptions must cover exactly its modes")
            self._oversight[automation.id] = oversight
        self._by_id[automation.id] = automation

    def get(self, automation_id: str) -> Automation:
        return self._by_id[automation_id]

    def oversight_for(self, automation_id: str) -> AutomationOversight | None:
        """Canonical display metadata; optional only for ad-hoc/test registrations."""
        self.get(automation_id)
        return self._oversight.get(automation_id)

    def validate_oversight(self, *, require_all: bool = False) -> None:
        """Resolve forward references after registration, without startup I/O."""
        for automation in self.all():
            metadata = self.oversight_for(automation.id)
            if metadata is None:
                if require_all:
                    raise ValueError(f"{automation.id!r}: missing oversight metadata")
                continue
            if set(metadata.mode_descriptions) != set(automation.modes):
                raise ValueError(f"{automation.id!r}: oversight mode descriptions have drifted")
            for relationship in metadata.relationships:
                if relationship.target_id not in self._by_id:
                    raise ValueError(
                        f"{automation.id!r}: unknown relationship target {relationship.target_id!r}")

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
from .capacity_governor import CapacityGovernor  # noqa: E402

# Current effects, not future policy. Keep metadata beside each registration.
registry.register(PrDoctor(), oversight=AutomationOversight(
    Responsibility.EXECUTION, 'Managed PRs within configured repository coverage.',
    {
        'off': 'Disable automatic PR recovery.',
        'propose': 'Identify recovery actions for an operator to trigger; do not run repairs automatically.',
        'auto': 'Run configured branch updates and repair sessions within attempt limits. '
                'Conflict resolution requires its separate setting; review approval gates remain.',
    },
    (Relationship('shares_work_with', 'backlog-runner',
                  'Recovers implementation PRs when recovery coverage permits.'),),
))
registry.register(BacklogRunner(), oversight=AutomationOversight(
    Responsibility.EXECUTION, 'Eligible work in configured repositories, under each product dispatch policy.',
    {
        'off': 'Disable background issue dispatch.',
        'on': 'Apply each product policy: leave dispatch off, propose a batch, or start implementation '
              'sessions automatically, subject to dependencies and shared capacity.',
    },
    (Relationship('uses', 'pm-sync', 'Reads mirrored backlog and dependencies; freshness affects eligibility.'),
     Relationship('hands_off_to', 'pr-doctor',
                  'Can release a slot for an armed PR when PR recovery coverage is available.'),
     Relationship('shares_work_with', 'autopilot', 'Session assistance can advance implementation sessions.')),
))
registry.register(Autopilot(), oversight=AutomationOversight(
    Responsibility.EXECUTION, 'Governed sessions across products, subject to guardrails and human steering.',
    {
        'off': 'Disable automatic session assistance.',
        'propose': 'Evaluate sessions and report proposed actions or completion; escalate when human help is needed.',
        'auto': 'Apply routine answers and nudges, approve permitted tool requests when configured, '
                'escalate to humans, and archive completed sessions under completion and human-steering guards.',
    },
    (Relationship('shares_work_with', 'archive-audit',
                  'Shares the completion predicate; Archive Audit rechecks recently archived sessions.'),),
))
registry.register(CicdDoctor(), oversight=AutomationOversight(
    Responsibility.IMPROVEMENT, 'CI health in configured repositories, subject to detector policies.',
    {
        'off': 'Disable background CI health analysis.',
        'propose': 'Analyze CI health; automatically file or reopen qualifying GitHub issues when File GitHub issues '
                   'is enabled. Close resolved issues even when filing is disabled. Track findings and graduation '
                   'candidates without launching repair sessions.',
    },
    (Relationship('shares_work_with', 'pr-doctor',
                  'Examines CI system health while PR Doctor repairs individual PRs.'),),
))
registry.register(ArchiveAudit(), oversight=AutomationOversight(
    Responsibility.EXECUTION, 'Recently archived sessions within the configured lookback window.',
    {
        'off': 'Disable periodic completion rechecks.',
        'act': 'Recheck archived sessions and automatically unarchive premature completions. '
               'Unarchiving and starting another turn are distinct actions.',
    },
    (Relationship('shares_work_with', 'autopilot',
                  'Rechecks the shared completion predicate and can undo a premature archive.'),),
))
registry.register(SdlcIntake(), oversight=AutomationOversight(
    Responsibility.PREPARATION, 'Configured repository intake triggers and queued improvement initiatives.',
    {
        'off': 'Disable background intake processing.',
        'propose': 'Create initiatives and launch intent artifact proposal sessions; human advancement is required.',
        'auto': 'Launch intent proposal sessions; human advancement remains required by default, '
                'subject to separate stage-advancement configuration.',
    },
    (Relationship('hands_off_to', 'sdlc-synthesizer',
                  'After human advancement, prepares the next initiative artifacts.'),),
))
registry.register(SdlcSynthesizer(), oversight=AutomationOversight(
    Responsibility.PREPARATION, 'Initiatives at eligible specification and plan stages.',
    {
        'off': 'Disable background artifact synthesis, plan decomposition and gate resolution.',
        'on': 'Launch specification, plan and merged-plan decomposition sessions in priority order '
              'under each product\'s front-chain cap; resolve stage gates only as far as the '
              'product\'s risk tolerance allows (cautious keeps every human gate).',
    },
    (Relationship('hands_off_to', 'backlog-runner',
                  'Approved materialization creates issues for dispatch under product policy.'),),
))
registry.register(ControlBandMonitor(), oversight=AutomationOversight(
    Responsibility.IMPROVEMENT, 'Configured metric bands across repositories and global maintenance.',
    {
        'off': 'Disable background metric-band checks.',
        'monitor': 'Detect breaches and record triage findings; do not launch diagnoses or queue intake work.',
        'propose': 'Record breaches and, by response tier, launch read-only diagnosis sessions '
                   'or queue initiatives for intake, within configured limits.',
    },
    (Relationship('hands_off_to', 'sdlc-intake', 'Response tiers can queue improvement initiatives for intake.'),),
))
registry.register(PmSync(), oversight=AutomationOversight(
    Responsibility.CONNECTION, 'GitHub repositories and project boards belonging to onboarded products.',
    {
        'off': 'Disable background GitHub mirror refreshes.',
        'on': 'Refresh local work-item, dependency, board, and feature indexes. Read GitHub without writing changes to it.',
    },
    (Relationship('shares_work_with', 'gh-reconcile',
                  'Reads external facts while GitHub Reconcile writes desired decisions outward.'),),
))
registry.register(PmTriage(), oversight=AutomationOversight(
    Responsibility.ORGANIZATION, 'Onboarded products with classification and capability-map work.',
    {
        'off': 'Disable background triage processing.',
        'propose': 'Launch classification and map-synthesis sessions, harvest proposals, and automatically '
                   'accept qualifying classifications when the product auto-accept policy permits it.',
    },
    (Relationship('uses', 'pm-classify', 'Invokes the shared classification session implementation.'),
     Relationship('uses', 'pm-onboard', 'Uses shared map synthesis to refresh capability maps.'),
     Relationship('uses', 'pm-sync', 'Classifies the mirrored backlog against the feature index.')),
))
registry.register(PmOnboard(), oversight=AutomationOversight(
    Responsibility.ORGANIZATION,
    'Selected product: invoked by setup or map refresh to launch capability-map synthesis sessions.', {},
))
registry.register(PmClassify(), oversight=AutomationOversight(
    Responsibility.ORGANIZATION,
    'Selected product work items: invoked by an operator or triage to launch classification proposal sessions.', {},
))
registry.register(GhReconcile(), oversight=AutomationOversight(
    Responsibility.CONNECTION, 'Product-owned GitHub boards and issue relationships, subject to freshness and write limits.',
    {
        'off': 'Disable background GitHub reconciliation.',
        'dry_run': 'Compute and journal proposed GitHub updates without applying them.',
        'apply': 'Write desired board fields, membership, and blocked-by relationships within safeguards. '
                 'Board schema changes require the separate migration setting.',
    },
    (Relationship('requires', 'pm-sync', 'Needs a sufficiently fresh GitHub mirror; stale facts defer writes.'),),
))
registry.register(ErrorTriage(), oversight=AutomationOversight(
    Responsibility.IMPROVEMENT, 'ViberPit runtime warnings and errors across edge, daemon, and workers; global maintenance.',
    {
        'off': 'Disable automatic runtime-error issue filing.',
        'file': 'Automatically file qualifying runtime errors and warnings as GitHub issues within deduplication '
                'and filing limits; do not launch repair sessions.',
    },
))
registry.register(DeployMonitor(), oversight=AutomationOversight(
    Responsibility.IMPROVEMENT, 'GitHub deployment environments in configured repositories.',
    {
        'off': 'Disable background deployment monitoring.',
        'watch': 'Detect deployment incidents and notify; do not automatically open new issues. '
                 'Existing incident issues can still close on recovery; manual filing remains available.',
        'file': 'Detect incidents, automatically file or update GitHub issues, and close incident issues '
                'on recovery, respecting caps, acknowledgement, and mute state.',
    },
    (Relationship('shares_work_with', 'cicd-doctor',
                  'Monitors deployment outcomes while CI/CD Doctor monitors build and test health.'),),
))
registry.register(CapacityGovernor(), oversight=AutomationOversight(
    Responsibility.EXECUTION, 'Fleet concurrency and per-product in-flight caps, derived from account quota, host capacity and the runner ledger.',
    {
        'off': 'Disable capacity planning; every cap stays as configured.',
        'monitor': 'Compute the plan every tick and show it on Delivery → Capacity; apply nothing.',
        'act': 'Compute and apply the plan to auto-mode products and the runner ceiling (phase 2); pinned caps are respected.',
    },
    (Relationship('uses', 'pm-sync', 'Reads mirrored eligible counts per product.'),
     Relationship('shares_work_with', 'backlog-runner', 'Sets what the runner may claim per product.')),
))
registry.validate_oversight(require_all=True)
