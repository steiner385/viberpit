"""Product automation policy (spec 2026-09-12-product-automation-policy-design §4): the ONE
schema (`PRODUCT_POLICY_SCHEMA`) and the ONE validated reading (`effective_policy`) for every
product's automation knobs — whether the Backlog Runner dispatches for it, how far down its
board it reaches, its in-flight cap and weight, its attempt cap, its session model.

`triage_auto_accept` joined the schema in phase 2 (the PM Triage loop's auto-accept tier);
`give_up_after_bounces` joined it in phase 3 (the dispatch-bounce threshold D8). `risk_tolerance`,
`max_initiatives_in_flight` and `unattended_model_floor` joined it in the 2026-09-14 roadmap
auto-execution spec's phase 1 (the risk dial, the initiative concurrency cap, and the model-floor
mitigation for unattended gate resolutions).

Stored on ``Project.automation_policy`` as a JSON blob validated through this schema at write
time (``PATCH /api/pm/projects/{slug}/policy``) — so a row in the DB is already valid. But
``effective_policy`` reads it again on every use (never trusts the write-time validation alone)
because a hand-edited row or a shipped schema change that forgot to retire an old key must never
take a product's dispatch decision down with it: every reader falls back to pure defaults
(dispatch ``off``, the safe "not opted in" state) rather than raising.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from server.pm.repo_project import RepoOwnership

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from server.automations.base import ConfigField, validate_config
from server.automations.model_pick import AUTOMATION_MODEL_CHOICES
from server.db.models import Project
from server.db.clock import naive_utc_now
from server.time_iso import iso_utc, parse_iso_utc
from server.pm.desired_state import STATUS_READY
from server.pm.queue import MIN_STATUS_OPTIONS
from server.sdlc.gates import RISK_TOLERANCES
from server.sessions.model_catalog import TIER_ORDER
from server.sessions.model_request import MODEL_TIERS

log = logging.getLogger(__name__)

DISPATCH_MODES: tuple[str, ...] = ("off", "propose", "auto")

PRODUCT_POLICY_SCHEMA: list[ConfigField] = [
    ConfigField(key="dispatch", label="Dispatch", type="enum", default="off",
                choices=list(DISPATCH_MODES),
                help="Whether the Backlog Runner works this product's board: 'propose' "
                     "announces a batch without spawning, 'auto' spawns."),
    ConfigField(key="queue_min_status", label="Minimum board status", type="enum",
                default=STATUS_READY, choices=list(MIN_STATUS_OPTIONS),
                help="How far down the board the runner may reach for this product."),
    # ``min=0`` on the cap, not 1: 0 is a meaningful setting — PAUSED, the "stop
    # working this product but keep its other settings" state an operator would
    # otherwise have to fake by flipping dispatch off. A NEGATIVE cap is what is
    # rejected; ``_refill`` treats every cap literally, so a stored ``-1`` would
    # read as an unreachable ceiling, i.e. unlimited.
    ConfigField(key="max_in_flight", label="Max in flight", type="int", default=1, min=0,
                help="This product's concurrent runner sessions. 0 pauses issue "
                     "dispatch without losing the rest of its policy."),
    ConfigField(key="weight", label="Weight", type="int", default=1, min=0,
                help="This product's share when a freed slot is contested (the "
                     "'weighted' strategy)."),
    # The model ladder (spec 2026-09-18 §4, decision 2) replaces the flat attempt
    # cap: a bounce climbs one tier (``server/pm/outcomes.py:next_rung``) rather
    # than counting toward a fixed number, and a bounce AT this tier is the one
    # that hands the issue to the owner.
    ConfigField(key="ladder_top", label="Ladder top tier", type="enum", default="fable",
                choices=list(TIER_ORDER),
                help="The strongest model the Backlog Runner climbs to on repeated "
                     "bounces; a bounce at this tier hands the issue to the owner."),
    # Phase 3 (spec §6/§7, D8): THE bounce threshold, compared through
    # ``server/pm/outcomes.py:bounced_out`` by both consumers. ``min=0`` because 0 is
    # meaningful ("never give up"); a NEGATIVE threshold is what is rejected, since
    # ``bounced_out`` would then fire on the very first finalised outcome.
    ConfigField(key="give_up_after_bounces", label="Give up after bounces", type="int",
                default=2, min=0,
                help="Finalised non-merged outcomes on the same issue body before the "
                     "queue skips it as needs_spec and the roadmap flags it; 0 never "
                     "gives up. Editing the issue body restarts the count."),
    ConfigField(key="default_model", label="Session model", type="enum", default="auto",
                choices=AUTOMATION_MODEL_CHOICES,
                help="Model for this product's sessions; 'auto' assesses each job."),
    ConfigField(key="respect_dependencies", label="Respect issue dependencies", type="bool",
                default=True),
    # Phase 2 (spec §4, §8): the PM Triage loop's own dispatch decision. Ordered
    # by ``server/pm/classification.py:CONFIDENCE_LEVELS`` — "medium" means "medium
    # or better", so the choices read as a descending strictness, not a set.
    ConfigField(key="triage_auto_accept", label="Triage auto-accept", type="enum",
                default="off", choices=["off", "high", "medium"],
                help="Accept this product's classification proposals at or above this "
                     "confidence without a human; medium includes high. Never a no-fit "
                     "proposal, never one whose capability doc is still proposed."),
    # Roadmap auto-execution (spec 2026-09-14 D2/D3): how bold the SDLC front chain
    # may be for this product. The gate registry (server/sdlc/gates.py) decides
    # which gates each level covers; `cautious` is today's behaviour.
    ConfigField(key="risk_tolerance", label="Risk tolerance", type="enum",
                default="cautious", choices=list(RISK_TOLERANCES),
                help="cautious: every roadmap gate waits for you. balanced: low-risk gates "
                     "resolve themselves (artifact PR merges, stage advances). bold: also "
                     "medium-risk gates (plan decomposition accept, model upgrades)."),
    # ``min=0``: 0 pauses new artifact sessions for the product without losing its
    # other settings; gate resolutions are never capped (they finish work).
    ConfigField(key="max_initiatives_in_flight", label="Max initiatives in flight", type="int",
                default=2, min=0,
                help="How many of this product's initiatives may be producing a spec or "
                     "plan at once. 0 pauses new artifact sessions."),
    # Spec 2026-09-14 D11: model capability as a risk mitigation. Phase 1 stores it;
    # phase 2 applies it as the floor for sessions whose output a covered gate will
    # consume without a human read (inert under `cautious`).
    ConfigField(key="unattended_model_floor", label="Unattended model floor", type="enum",
                default="none", choices=["none", *MODEL_TIERS],
                help="Minimum model for a spec, plan or decompose session whose output will "
                     "be merged or accepted without a human read. Inert under cautious."),
]

# Keys this schema USED to have and deliberately dropped (spec 2026-09-18 §4, plan
# 4a task 2): ``max_attempts`` retired in favour of ``ladder_top`` + the model
# ladder. A row a product's policy was already stored with still carries it, and
# ``validate_config`` rejects unknown keys, so without this list every hand-edited
# or pre-flip row would 422/fall back to all-defaults the moment it was read.
PRODUCT_POLICY_RETIRED_KEYS: tuple[str, ...] = ("max_attempts",)

# Logged once per process, not once per bad row: a corrupt policy is a per-deploy fact
# ("something upstream needs fixing"), not a per-request one, and every dispatch-loop tick
# would otherwise re-log the same row.
_warned_bad_policy = False


def effective_policy(raw: dict | None) -> dict:
    """The validated policy with every key present. ``None`` (never opted in) and an
    unparseable blob both read as the defaults — dispatch ``off`` — and the latter logs once
    per process. Never raises."""
    global _warned_bad_policy
    if raw is not None:
        try:
            return validate_config(PRODUCT_POLICY_SCHEMA, raw, retired=PRODUCT_POLICY_RETIRED_KEYS)
        except (ValueError, TypeError):
            if not _warned_bad_policy:
                log.warning("product_policy.invalid_stored_policy: falling back to defaults",
                            exc_info=True)
                _warned_bad_policy = True
    return validate_config(PRODUCT_POLICY_SCHEMA, {}, retired=PRODUCT_POLICY_RETIRED_KEYS)


def merge_policy_patch(current: dict | None, patch: dict | None) -> dict | None:
    """Validate both inputs, retaining only explicitly stored/supplied keys.

    Null is a deliberate reset, including recovery from corrupt stored policy.
    Default filling belongs to readers and must never turn into persisted intent.
    A retired key surviving in ``current`` (a pre-retirement row) or ``patch`` (a
    stale client) is dropped here rather than carried forward — ``validate_config``
    never puts it in its output, so re-persisting it would resurrect exactly the
    key retirement means to stop writing.
    """
    if patch is None:
        return None
    if not isinstance(patch, dict) or (current is not None and not isinstance(current, dict)):
        raise ValueError("policy must be an object or null")
    current = {} if current is None else current
    validate_config(PRODUCT_POLICY_SCHEMA, current, retired=PRODUCT_POLICY_RETIRED_KEYS)
    validate_config(PRODUCT_POLICY_SCHEMA, patch, retired=PRODUCT_POLICY_RETIRED_KEYS)
    merged = {k: v for k, v in {**current, **patch}.items()
              if k not in PRODUCT_POLICY_RETIRED_KEYS}
    validated = validate_config(PRODUCT_POLICY_SCHEMA, merged, retired=PRODUCT_POLICY_RETIRED_KEYS)
    return {key: validated[key] for key in merged}


class PolicyEditConflict(Exception):
    """The observed policy changed; the caller must refresh before editing."""


async def patch_project_policy(
    db: AsyncSession, project: Project, patch: dict | None, *,
    expected_updated_at: datetime | None = None, check_expected: bool = False,
) -> dict | None:
    """Sparse policy write with an atomic revision check, shared with migration.

    Even compatibility callers without a client revision compare against the row
    they read. Never retry a stale patch automatically. Resets advance revision
    too, so a stale edit or the legacy migration cannot resurrect deleted intent.
    """
    observed = project.automation_policy_updated_at
    if check_expected and iso_utc(expected_updated_at) != iso_utc(observed):
        raise PolicyEditConflict
    merged = merge_policy_patch(project.automation_policy, patch)
    revision = naive_utc_now().replace(tzinfo=timezone.utc)
    previous = parse_iso_utc(iso_utc(observed))
    if previous is not None:
        revision = max(revision, previous + timedelta(microseconds=1))
    conditions = [Project.id == project.id, Project.automation_policy_updated_at == observed]
    if project.automation_policy is None:
        conditions.append(Project.automation_policy.is_(None))
    result = await db.execute(
        update(Project).where(*conditions).values(automation_policy=merged, automation_policy_updated_at=revision)
        .returning(Project.id).execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        raise PolicyEditConflict
    await db.refresh(project)
    return merged


def is_on(policy: "dict | None") -> bool:
    """Whether this product is opted in to the Backlog Runner.

    The ONE reading of "dispatch is not off", shared by the queue's scope rule, the
    selection knobs and the status card, so the four surfaces cannot disagree about
    which products the runner works. Tolerates a partial row (the card's per-product
    count dicts) and ``None``: anything without an explicit non-``off`` dispatch is
    off.
    """
    return (policy or {}).get("dispatch", "off") != "off"


def policies_by_slug(db: Session) -> dict[str, dict]:
    """slug -> effective policy for every Project (sync session; the queue loader's session)."""
    rows = db.execute(select(Project.slug, Project.automation_policy)).all()
    return {slug: effective_policy(policy) for slug, policy in rows if slug is not None}


async def load_policies(db: AsyncSession) -> dict[str, dict]:
    """Async twin of ``policies_by_slug`` for the API/orchestrator paths — same reading, no
    second rule."""
    rows = (await db.execute(select(Project.slug, Project.automation_policy))).all()
    return {slug: effective_policy(policy) for slug, policy in rows if slug is not None}


DecisionState = Literal["permitted", "refused", "unknown"]


@dataclass(frozen=True)
class ImplementationConstraint:
    """An observed prerequisite, supplied by its existing authoritative reader."""

    code: str
    state: DecisionState
    text: str
    link: str | None = None


@dataclass(frozen=True)
class ImplementationDecision:
    """Product-policy eligibility at an instant, never an execution token."""

    state: DecisionState
    constraints: tuple[ImplementationConstraint, ...]
    configured_dispatch: str
    policy_source: str
    product_id: str | None
    product_slug: str | None
    canonical_repo: str | None
    approval_required: bool
    observed_at: str
    policy_revision: str
    remaining_checks: tuple[str, ...]
    note: str = (
        "Product implementation policy eligibility only. Remaining target checks must "
        "pass before dispatch. This preview is not a future authorization token; "
        "ownership, policy and capacity must be rechecked at execution."
    )

    @property
    def permitted(self) -> bool:
        return self.state == "permitted"


def decide_implementation(
    *,
    ownership: RepoOwnership | None,
    requested_product_slug: str | None,
    policy: dict | None,
    trigger: Literal["automatic", "manual"],
    effect: Literal["implementation", "proposal"],
    runner_mode: str | None,
    global_count: int | None,
    global_cap: int | None,
    product_count: int | None,
    daily_count: int | None,
    daily_cap: int | None,
    observed_at: str,
    starts_new_session: bool = True,
    existing_slot: bool = False,
    prerequisites: tuple[ImplementationConstraint, ...] = (),
    remaining_checks: tuple[str, ...] = (),
) -> ImplementationDecision:
    """Compose current implementation admission policy without I/O or claims.

    Callers gather verified canonical coverage and validated runner configuration.
    An intentional manual request bypasses scheduled mode only, never capacity or
    ownership. ``existing_slot`` means this proposal already occupies the supplied
    global/product counts; it does not exempt a zero cap. This function does not
    authorize continued work or other responsibilities. Extra constraints retain
    their authoritative reader's text and links, sorted by code for stable output.
    """
    effective = effective_policy(policy)
    dispatch = str(effective["dispatch"])
    constraints: list[ImplementationConstraint] = []

    def add(code: str, state: DecisionState, text: str, link: str | None = None) -> None:
        constraints.append(ImplementationConstraint(code, state, text, link))

    owner = ownership.owner if ownership is not None else None
    canonical = ownership.canonical_repo if ownership is not None else None
    if ownership is None or ownership.state == "unresolved":
        add("ownership_unresolved", "unknown", "Canonical product ownership is unresolved.")
    elif ownership.state == "ambiguous":
        add("ownership_ambiguous", "refused", ownership.reason)
    elif ownership.state == "missing":
        add("ownership_missing", "refused", "No product owns this repository; fleet scope is not implied.")
    elif owner is None or not canonical:
        add("ownership_unverified", "unknown", "Verified canonical product ownership is required.")
    elif requested_product_slug is not None and requested_product_slug != owner.slug:
        add("product_mismatch", "refused", "The requested product does not own this repository.")
    else:
        add("ownership_unique", "permitted", "Verified repository ownership identifies one product.")

    if trigger not in {"automatic", "manual"} or effect not in {"implementation", "proposal"}:
        add("action_unknown", "unknown", "The requested implementation action is unsupported.")
    if effect == "implementation" and not starts_new_session:
        add("action_not_admission", "unknown", "Continued work is outside implementation admission policy.")
    if runner_mode not in {"on", "off"}:
        add("runner_unknown", "unknown", "Validated runner availability is unavailable.")
    elif trigger == "manual":
        add("runner_manual", "permitted", "An intentional manual request bypasses scheduled runner mode.")
    elif runner_mode == "off":
        add("runner_off", "refused", "Scheduled issue dispatch is off.")
    else:
        add("runner_on", "permitted", "Scheduled issue dispatch is available.")

    approval_required = trigger == "automatic" and dispatch == "propose" and effect == "implementation"
    if trigger == "manual":
        add("dispatch_manual", "permitted", "An intentional manual request bypasses the product's scheduled dispatch mode.")
    elif dispatch == "off":
        add("dispatch_off", "refused", "Automatic issue dispatch is off for this product.")
    elif approval_required:
        add("approval_required", "refused", "This product requires approval before starting implementation.")
    else:
        add("dispatch_allowed", "permitted", "The configured product mode permits this dispatch effect.")

    def capacity(name: str, count: int | None, cap: int | None, *, occupied: bool = False) -> None:
        if type(count) is not int or type(cap) is not int or count < 0 or cap < 0:
            add(f"{name}_unknown", "unknown", f"The {name.replace('_', ' ')} capacity is unavailable.")
        elif cap == 0 or count - int(occupied and count > 0) >= cap:
            add(f"{name}_full", "refused", f"The {name.replace('_', ' ')} limit is reached ({count}/{cap}).")
        else:
            add(f"{name}_available", "permitted", f"The {name.replace('_', ' ')} limit has capacity ({count}/{cap}).")

    capacity("global_capacity", global_count, global_cap, occupied=existing_slot)
    capacity("product_capacity", product_count, effective["max_in_flight"], occupied=existing_slot)
    capacity("daily_budget", daily_count, daily_cap)
    constraints.extend(sorted(prerequisites, key=lambda item: (item.code, item.state, item.text, item.link or "")))
    states = {item.state for item in constraints}
    state: DecisionState = "refused" if "refused" in states else "unknown" if "unknown" in states else "permitted"
    return ImplementationDecision(
        state=state, constraints=tuple(constraints), configured_dispatch=dispatch,
        policy_source="product_policy" if policy == effective else "product_policy_with_safe_defaults",
        product_id=owner.project_id if owner else None, product_slug=owner.slug if owner else None,
        canonical_repo=canonical, approval_required=approval_required, observed_at=observed_at,
        policy_revision=hashlib.sha256(json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        remaining_checks=remaining_checks,
    )
