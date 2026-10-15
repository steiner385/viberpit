"""Product automation policy (spec 2026-09-12-product-automation-policy-design §4): the ONE
schema (`PRODUCT_POLICY_SCHEMA`) and the ONE validated reading (`effective_policy`) for every
product's automation knobs — whether the Backlog Runner dispatches for it, how far down its
board it reaches, its pin, cap mode and pipeline cap, its attempt cap, its session model.

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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

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
from server.sdlc.paths import ARTIFACT_LAYOUTS, DEFAULT_ARTIFACT_LAYOUT
from server.sessions.model_catalog import TIER_ORDER
from server.sessions.model_request import MODEL_TIERS

log = logging.getLogger(__name__)

DISPATCH_MODES: tuple[str, ...] = ("off", "propose", "auto")
SESSION_ASSISTANCE_MODES: tuple[str, ...] = ("on", "off")
#: The needs-you hygiene automation's vocabulary, for its own mode and for this
#: per-product key alike (design 2026-10-14 §2): the effective mode is the lower
#: of the two, in this order.
NEEDS_YOU_AUTOMATION_MODES: tuple[str, ...] = ("off", "dry_run", "on")

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
    ConfigField(key="max_in_flight", label="Pin", type="int", default=1, min=0,
                surface="capacity",
                help="This product's concurrent runner sessions: a hard cap while Cap mode is "
                     "pinned, the fallback while auto and no plan exists. 0 pauses issue "
                     "dispatch without losing the rest of its policy — but it pauses only NEW "
                     "claims: a session already holding its slot is never interrupted by a cap "
                     "that dropped under it (D8), so tapping an outstanding proposal or letting "
                     "a waiting-account retry through still runs."),
    ConfigField(key="capacity_mode", label="Cap mode", type="enum", default="auto",
                choices=["auto", "pinned"], surface="capacity",
                help="auto: the capacity governor sets this product's cap from the fleet's "
                     "real headroom and its rank. pinned: the pin is a hard cap the governor "
                     "respects."),
    # Spec 2026-09-20 D13 (C23): the pin bounds SESSIONS, not PRs in CI — once PR
    # Doctor covers a repo a slot frees at hand-off, so a fast-claiming product
    # can stack any number of PRs in CI. This caps the product's OPEN, UNMERGED
    # automation PRs (counted from the runner's durable target states: every row
    # except gave_up and token-less handed_off, i.e. the same rule as the
    # per-repo fairness load). Blank = unlimited; 0 refuses every new claim.
    ConfigField(key="max_pipeline_prs", label="Pipeline cap", type="int", default=None, min=0,
                nullable=True, surface="capacity",
                help="Most claims this product may have in its pipeline at once: live "
                     "runner sessions plus PRs handed off to PR Doctor and not yet merged. "
                     "A full pipeline refuses new claims so a fast-claiming product cannot "
                     "stack CI. A cap below the pin caps the pin."),
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
    #
    # Default 0 since the model ladder landed (spec 2026-09-18 §4, decision 2: "park
    # only above the top tier"): the LADDER's length is the cap now, and the old
    # default of 2 made ``ladder_top`` unreachable — the queue skipped the item as
    # ``needs_spec`` on the very bounce that wrote the rung hold, so nothing ever
    # consumed it and the owner hand-off never fired. Kept as an optional STRICTER
    # dial: when an operator sets it, the sweep hands the issue to the owner at that
    # bounce instead of writing a rung hold nothing can redeem.
    ConfigField(key="give_up_after_bounces", label="Give up after bounces", type="int",
                default=0, min=0,
                help="Stop short of the model ladder's top: hand the issue to its owner "
                     "after this many finalised non-merged outcomes on the same body "
                     "(the queue then skips it as needs_spec). 0 (the default) lets the "
                     "ladder run its full length. Editing the issue body restarts the count."),
    # #3109: the age past which the runner stops waiting on a claim nothing can
    # move. Policy, never proof: it is consulted only once the ownership store's
    # own stopped-work evidence already allows a release (``ownership.py`` — an
    # elapsed timer is not a ReleaseProof). Two shapes read it: a handed-off PR
    # with no activity this long (PR Doctor never works a draft, so a draft whose
    # session is gone sits forever — KinDash#15256 behind #15268 for 41 days), and
    # a launch whose outcome stayed unknown and whose reserved session never
    # appeared. 7 days is deliberately long: a slow, live PR touched within the
    # window is never re-dispatched. 0 waits forever (the pre-#3109 behaviour).
    ConfigField(key="stale_claim_days", label="Release stale claims after (days)", type="int",
                default=7, min=0,
                help="Return an issue to the queue when its claim cannot progress: the "
                     "session is gone and its pull request (often a draft PR Doctor will not "
                     "touch) has had no activity for this many days, or its session never "
                     "started. 0 keeps such claims until a person clears them."),
    ConfigField(key="default_model", label="Session model", type="enum", default="auto",
                choices=AUTOMATION_MODEL_CHOICES,
                help="Model for this product's sessions; 'auto' assesses each job."),
    ConfigField(key="respect_dependencies", label="Respect issue dependencies", type="bool",
                default=True),
    # Additive on top of the global bot-noise filter (``server.pm.queue.SKIP_ISSUE_LABELS``):
    # a product's own workflow labels (e.g. "on-hold", "needs-design") that mean
    # "not implementable right now" for THIS product specifically, without touching
    # the dependencies/renovate filter every product still gets for free.
    ConfigField(key="skip_labels", label="Labels to skip", type="string-list", default=[],
                help="Issues carrying any of these labels are skipped by the queue for "
                     "this product, in addition to the built-in bot-noise filter "
                     "(dependencies, renovate, viberpit-ignore)."),
    # #2683: ``server.pm.queue.is_self_closing_tracker``. On by default — such an issue
    # closes itself, so a dispatch mostly ends "no PR needed" — but a flaky tracker IS
    # fixable, so a product that wants its trackers worked turns this off.
    ConfigField(key="skip_self_closing_trackers", label="Skip self-closing trackers",
                type="bool", default=True,
                help="Skip issues a CI workflow files and closes by itself: [flaky] test "
                     "trackers, 'Nightly/Weekly … failed' rollups and recurring '… Digest' "
                     "reports. Turn off to have the runner work them."),
    ConfigField(key="trusted_authors", label="Trusted issue authors", type="string-list",
                default=[],
                help="GitHub logins whose issues the queue may dispatch. Anyone else's "
                     "issue is skipped as 'untrusted'. Write a GitHub App as app/<slug> "
                     "and a user by bare login. The github-actions app is always trusted. "
                     "Trust follows the current GitHub login, so remove a trusted "
                     "account that renames (someone else can register its old name)."),
    # #2679: classes of issue whose approval-gated ladder climb ("bounced at opus;
    # fable needs approval") is approved by the runner itself, within a budget —
    # ``server/pm/auto_escalation.py``. Budget 0 (the default) keeps every such
    # escalation waiting for a person, as before.
    ConfigField(key="auto_escalate_match", label="Auto-approve escalation for",
                type="string-list", default=[],
                help="Issues carrying one of these as a label, or containing it in the "
                     "title (e.g. flaky, [flaky], deploy-verify), have an escalation that "
                     "needs approval approved automatically while the budget lasts. A "
                     "self-closing tracker (e.g. [flaky]) is only worked with 'Skip "
                     "self-closing trackers' off."),
    ConfigField(key="auto_escalate_budget", label="Auto-approved escalations per week",
                type="int", default=0, min=0,
                help="How many escalations the runner may approve by itself in any 7 days. "
                     "0 turns automatic approval off; past the budget an escalation waits "
                     "for a person."),
    # Phase 2 (spec §4, §8): the PM Triage loop's own dispatch decision. Ordered
    # by ``server/pm/classification.py:CONFIDENCE_LEVELS`` — "medium" means "medium
    # or better", so the choices read as a descending strictness, not a set.
    ConfigField(key="triage_auto_accept", label="Triage auto-accept", type="enum",
                default="off", choices=["off", "high", "medium"],
                help="Accept this product's classification proposals at or above this "
                     "confidence without a human; medium includes high. Never a no-fit "
                     "proposal; onto a capability whose doc is still proposed only by the "
                     "repeat rule below."),
    # #2683: the two rules that keep the triage inbox for proposals that matter.
    ConfigField(key="triage_auto_accept_dispatch_off", label="Auto-accept while Dispatch is off",
                type="bool", default=False,
                help="Accept this product's classification proposals whatever their confidence "
                     "while Dispatch is off: nothing is dispatched, so a wrong accept costs "
                     "nothing. Never a no-fit; onto a still-proposed capability only by the "
                     "repeat rule below, which this setting arms only while Dispatch is off. "
                     "Undoable."),
    # #2683 follow-up: N human accepts onto one capability ARE the endorsement. Read
    # through ``server.pm.triage_accept.repeat_threshold_for`` — the ONE place the
    # opt-in governance below is applied.
    ConfigField(key="triage_auto_accept_repeat", label="Auto-accept after repeat human accepts",
                type="int", default=3, min=0,
                help="Accept a proposal whatever its confidence once a person has accepted "
                     "this many earlier proposals onto the same capability as proposed, even "
                     "while that capability is still proposed. Machine accepts never count. "
                     "Applies only to a product that has opted in to auto-accept (Triage "
                     "auto-accept above off, or Auto-accept while Dispatch is off, which "
                     "counts only while Dispatch is off); a product that never opted in "
                     "never sees a machine accept. Never a no-fit or a sunset capability. "
                     "0 turns it off. Undoable."),
    ConfigField(key="triage_dismiss_bot_artifacts", label="Dismiss bot artifacts", type="bool",
                default=True,
                help="Keep bot output out of triage: items authored by a [bot] account, or "
                     "titled like recognised bot output (recurring digests, regenerated-index "
                     "PRs, and the patterns below), are not sent to classification, and a "
                     "pending proposal for one is dismissed. Each is recorded as a dismissed "
                     "proposal you can undo from the inbox; nothing is parked under an owner "
                     "hold. Turning this off sends those items back to classification."),
    ConfigField(key="triage_bot_artifact_patterns", label="Bot artifact title patterns",
                type="string-list", default=[],
                help="Extra title patterns (case-insensitive regular expressions) treated as "
                     "bot output for this product, on top of the built-in ones, e.g. "
                     "^chore: audit codebase for\\b"),
    # Roadmap auto-execution (spec 2026-09-14 D2/D3): how bold the SDLC front chain
    # may be for this product. The gate registry (server/sdlc/gates.py) decides
    # which gates each level covers; `cautious` is today's behaviour.
    ConfigField(key="risk_tolerance", label="Risk tolerance", type="enum",
                default="cautious", choices=list(RISK_TOLERANCES),
                help="cautious: every roadmap gate waits for you. balanced: low-risk gates "
                     "resolve themselves (artifact PR merges, stage advances). bold: also "
                     "medium-risk gates (plan decomposition accept, model upgrades)."),
    # Design 2026-10-14 §2: the product's say over the Needs-you Hygiene automation.
    # It only RESTRICTS the automation's own mode; risk_tolerance above decides
    # whether an action is applied or left as a recommendation.
    ConfigField(key="needs_you_automation", label="Needs-you hygiene", type="enum",
                default="on", choices=list(NEEDS_YOU_AUTOMATION_MODES),
                help="Whether the Needs-you Hygiene automation tidies this product: closes "
                     "issues the board has shown as Done for a day, moves stale Blocked "
                     "cards back to the queue, ends system holds the queue already skips, "
                     "and prunes resolved dependency edges. on: act — risk tolerance "
                     "decides how (balanced or bold apply; cautious leaves a "
                     "recommendation for you to approve). dry_run: only record what it "
                     "would do. off: leave this product alone."),
    # Design 2026-10-14 §4: the parked review runs weekly, and sooner (at most daily)
    # while more than this many issues are parked. Read through
    # ``server/pm/parked_review.py:run_due``.
    ConfigField(key="parked_review_threshold", label="Parked review threshold", type="int",
                default=20, min=0,
                help="The Needs-you Hygiene automation reviews this product's parked issues "
                     "weekly, and daily while more than this many are parked: a helper model "
                     "reads each one and the merged PRs that reference it and may recommend "
                     "closing it (already fixed, or obsolete) or unparking it. Only ever a "
                     "recommendation for you to approve — a parked label is never removed "
                     "without you. 0: weekly only."),
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
    # #1611 phase 3 (Session assistance): the product's own say over whether the
    # Autopilot automation answers, approves and nudges its sessions. It can only
    # RESTRICT: ``on`` (the default) leaves the global Autopilot mode, guardrails and
    # Repositories scope in charge, exactly as before this key existed, and ``off``
    # takes the product's sessions out of Autopilot's governance. It never grants
    # assistance the global setting withholds.
    ConfigField(key="session_assistance", label="Session assistance", type="enum",
                default="on", choices=list(SESSION_ASSISTANCE_MODES),
                help="on: Autopilot may answer, approve and nudge this product's sessions "
                     "as its global mode and guardrails allow. off: Autopilot leaves this "
                     "product's sessions alone; a person steers them. Never grants more "
                     "than the global Autopilot setting."),
    # #2416: lets a product on spec-kit (or another convention) keep its specs
    # where its own tooling already expects them, instead of every synthesizer
    # spec/plan artifact being a manual `POST .../artifacts` adopt.
    ConfigField(key="artifact_layout", label="Artifact layout", type="enum",
                default=DEFAULT_ARTIFACT_LAYOUT, choices=list(ARTIFACT_LAYOUTS),
                help="Where this product's SDLC stage artifacts are committed. docs_sdlc: "
                     "docs/sdlc/<slug>/<kind>.md (default). speckit: specs/<slug>/<kind>.md, "
                     "for a product already on spec-kit's own artifact layout."),
]

# Keys this schema USED to have and deliberately dropped (spec 2026-09-18 §4, plan
# 4a task 2): ``max_attempts`` retired in favour of ``ladder_top`` + the model
# ladder. A row a product's policy was already stored with still carries it, and
# ``validate_config`` rejects unknown keys, so without this list every hand-edited
# or pre-flip row would 422/fall back to all-defaults the moment it was read.
PRODUCT_POLICY_RETIRED_KEYS: tuple[str, ...] = ("max_attempts", "weight")

# In-memory ANNOTATIONS a reader may hang on a validated policy: never stored, never
# schema fields, never accepted from a client (``merge_policy_patch`` validates
# directly and still rejects them, so no patch can persist one). ``ladder_top_configured``
# is the Backlog Runner's record of the ``ladder_top`` the model-approval gate clamped
# away (spec 2026-09-18 §4, decision 2 amendment).
#
# ``effective_policy`` STRIPS them before validating so that re-reading an annotated
# snapshot stays idempotent. Without that, ``validate_config``'s unknown-key rule would
# read an annotation as a corrupt row and degrade the whole product to defaults —
# dispatch ``off`` — which is exactly the silent failure the retired-key list exists to
# prevent.
PRODUCT_POLICY_ANNOTATIONS: frozenset[str] = frozenset({"ladder_top_configured"})

# Logged once per process, not once per bad row: a corrupt policy is a per-deploy fact
# ("something upstream needs fixing"), not a per-request one, and every dispatch-loop tick
# would otherwise re-log the same row.
_warned_bad_policy = False


def effective_policy(raw: dict | None) -> dict:
    """The validated policy with every key present. ``None`` (never opted in) and an
    unparseable blob both read as the defaults — dispatch ``off`` — and the latter logs once
    per process. Never raises.

    Reading an already-validated policy back is idempotent, including one carrying an
    in-memory annotation (``PRODUCT_POLICY_ANNOTATIONS``): the annotation is dropped,
    never mistaken for a corrupt row."""
    global _warned_bad_policy
    if raw is not None:
        if isinstance(raw, dict) and not PRODUCT_POLICY_ANNOTATIONS.isdisjoint(raw):
            raw = {k: v for k, v in raw.items() if k not in PRODUCT_POLICY_ANNOTATIONS}
        try:
            return validate_config(PRODUCT_POLICY_SCHEMA, raw, retired=PRODUCT_POLICY_RETIRED_KEYS)
        except (ValueError, TypeError):
            if not _warned_bad_policy:
                log.warning("product_policy.invalid_stored_policy: falling back to defaults",
                            exc_info=True)
                _warned_bad_policy = True
    return validate_config(PRODUCT_POLICY_SCHEMA, {}, retired=PRODUCT_POLICY_RETIRED_KEYS)


# GitHub App logins that are UNREGISTERED as user logins (verified 2026-10-09). The mirror
# records the apps as ``app/<slug>``, so a BARE entry can only ever match a user who
# registers that login -- i.e. it would trust a squatter. Rejected on write, ignored on read.
BARE_APP_LOGINS = frozenset({"github-actions", "viberpit"})


def _reject_bare_app_logins(entries: object) -> None:
    for entry in entries or ():  # type: ignore[attr-defined]
        login = str(entry).strip().lower()
        if login in BARE_APP_LOGINS:
            raise ValueError(
                f"trusted_authors: {login!r} would trust a GitHub USER who registers that "
                f"login, not the app; write the app as 'app/{login}'")


def _reject_bad_patterns(entries: object) -> None:
    """``triage_bot_artifact_patterns`` must compile: a typo'd regex rejected on
    write is a 422 the operator sees; accepted, it would silently match nothing
    (the reader, ``triage_accept.is_bot_artifact``, skips one that does not)."""
    for entry in entries or ():  # type: ignore[attr-defined]
        try:
            re.compile(str(entry), re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"triage_bot_artifact_patterns: {entry!r} is not a valid pattern: {exc}") from None


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
    _reject_bare_app_logins(patch.get("trusted_authors"))
    _reject_bad_patterns(patch.get("triage_bot_artifact_patterns"))
    merged = {k: v for k, v in {**current, **patch}.items()
              if k not in PRODUCT_POLICY_RETIRED_KEYS}
    validated = validate_config(PRODUCT_POLICY_SCHEMA, merged, retired=PRODUCT_POLICY_RETIRED_KEYS)
    return {key: validated[key] for key in merged}


class PolicyEditConflict(Exception):
    """The observed policy changed; the caller must refresh before editing."""


class PolicyExpectationMismatch(PolicyEditConflict):
    """A caller-supplied ``expected`` value is not the field's current effective value.

    ``mismatches`` is ``{field: {"expected": …, "actual": …}}``. A conflict subclass so a
    caller that only knows ``PolicyEditConflict`` still refuses rather than writes."""

    def __init__(self, mismatches: dict[str, dict]) -> None:
        stale = "; ".join(f"{k} is {v['actual']!r}, not {v['expected']!r}"
                          for k, v in sorted(mismatches.items()))
        self.message = (f"The policy changed since it was read: {stale}. Re-read it and "
                        "confirm the change again.")
        super().__init__(self.message)
        self.mismatches = mismatches


def check_expected_values(current: dict | None, expected: Mapping) -> None:
    """Raise ``PolicyExpectationMismatch`` unless every ``expected`` field equals its
    current EFFECTIVE value (schema defaults filled — what ``effectivePolicy`` shows).

    ``expected`` goes through the schema first, so an unknown key or a mistyped value is
    a ``ValueError`` (422) rather than a guard that can never match or silently passes."""
    wanted = validate_config(PRODUCT_POLICY_SCHEMA, dict(expected),
                             retired=PRODUCT_POLICY_RETIRED_KEYS)
    actual = effective_policy(current)
    mismatches = {k: {"expected": wanted[k], "actual": actual.get(k)}
                  for k in expected if wanted[k] != actual.get(k)}
    if mismatches:
        raise PolicyExpectationMismatch(mismatches)


def policy_changes(before: dict | None, after: dict | None) -> dict[str, dict]:
    """``{field: {"before", "after"}}`` for every field whose EFFECTIVE value differs
    between two stored policies — the audit's human-readable diff of a write."""
    b, a = effective_policy(before), effective_policy(after)
    return {k: {"before": b.get(k), "after": a.get(k)} for k in a if b.get(k) != a.get(k)}


async def patch_project_policy(
    db: AsyncSession, project: Project, patch: dict | None, *,
    expected_updated_at: datetime | None = None, check_expected: bool = False,
    expected_values: Mapping | None = None,
) -> dict | None:
    """Sparse policy write with an atomic revision check, shared with migration.

    Even compatibility callers without a client revision compare against the row
    they read. Never retry a stale patch automatically. Resets advance revision
    too, so a stale edit or the legacy migration cannot resurrect deleted intent.

    ``expected_values`` (``{field: value}``) additionally requires each named field to
    still hold that effective value. It is checked against the same observed row the
    revision-guarded UPDATE below is conditioned on, so a write that lands between the
    check and the UPDATE fails the revision guard instead of slipping past the check.
    """
    observed = project.automation_policy_updated_at
    if check_expected and iso_utc(expected_updated_at) != iso_utc(observed):
        raise PolicyEditConflict
    merged = merge_policy_patch(project.automation_policy, patch)
    if expected_values:
        check_expected_values(project.automation_policy, expected_values)
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


def skip_labels(policy: "Mapping | None") -> frozenset[str]:
    """THE queue-shaped reading of a policy's ``skip_labels``: lowercased, stripped,
    a set — what ``queue.skip_reason`` takes as ``extra_skip_labels``. One function
    for the runner's pool, ``GET /api/pm/queue``, the escalation read model and the
    hold-hygiene pass, so no consumer can normalise differently and let a label
    through that another skips. Tolerates a partial row and ``None`` like ``is_on``.
    """
    return frozenset(str(lbl).strip().lower() for lbl in (policy or {}).get("skip_labels") or ()
                     if str(lbl).strip())


def skips_self_closing_trackers(policy: "Mapping | None") -> bool:
    """THE queue-shaped reading of ``skip_self_closing_trackers`` — what
    ``queue.skip_reason`` takes as ``skip_trackers``. Same single-reader rule as
    ``skip_labels``; a partial row or ``None`` reads as the schema default (on)."""
    return bool((policy or {}).get("skip_self_closing_trackers", True))


def trusted_authors(policy: "Mapping | None") -> frozenset[str]:
    """THE queue-shaped reading of ``trusted_authors`` — lowercased, stripped, a set.
    Same single-reader rule as ``skip_labels``."""
    logins = (str(a).strip().lower() for a in (policy or {}).get("trusted_authors") or ())
    # A bare app login (``BARE_APP_LOGINS``) is never honoured, even from a row written
    # before write-time validation rejected it: it could only match a squatting user.
    return frozenset(a for a in logins if a and a not in BARE_APP_LOGINS)


def rank_kwargs(policy: "Mapping", *, min_status: str | None = None,
                respect_dependencies: bool | None = None) -> dict[str, Any]:
    """THE policy → ``queue.rank`` / ``queue.skip_reason`` keyword composition: the
    product's reach (``queue_min_status``, ``respect_dependencies``), its D8 bounce
    threshold and the three readers above. One function for the runner's pool,
    ``GET /api/pm/queue``, the escalation read model and the map's waiting counters,
    so no two views of the pool can rank one product under different rules.
    *min_status* / *respect_dependencies* are a view's explicit what-if overrides;
    ``None`` reads the policy. ``now`` (and ``skipped_out``) stay the caller's."""
    return {
        "min_status": (min_status if min_status is not None
                       else str(policy["queue_min_status"])),
        "respect_dependencies": (respect_dependencies if respect_dependencies is not None
                                 else bool(policy["respect_dependencies"])),
        "give_up_after_bounces": int(policy.get("give_up_after_bounces", 0)),
        "extra_skip_labels": skip_labels(policy),
        "trusted_authors": trusted_authors(policy),
        "skip_trackers": skips_self_closing_trackers(policy),
    }


async def policy_for_repo(db: AsyncSession, repo: str) -> dict | None:
    """The effective policy of the ONE product that owns *repo*, or ``None`` when no
    product uniquely does (``repo_project.owning_projects_by_repo``). For a caller
    holding a mirrored issue but not the queue's product-grouped rows — the
    approve-escalation tap — this is how it finds the policy that governs the issue."""
    from server.pm.repo_project import owning_projects_by_repo
    owner = (await owning_projects_by_repo(db, [repo])).get(repo)
    if owner is None:
        return None
    return (await load_policies(db)).get(owner.slug)


async def assistance_withheld_repos(db: AsyncSession, repos: "Collection[str]") -> frozenset[str]:
    """The subset of *repos* whose ONE owning product set ``session_assistance`` off.

    Restrict-only by construction: a repo no product uniquely owns (missing or
    ambiguous ownership) is never withheld here, so the global Autopilot scope
    keeps governing it exactly as it did before the product dial existed."""
    from server.pm.repo_project import owning_projects_by_repo
    wanted = {r for r in repos if r}
    if not wanted:
        return frozenset()
    owners = await owning_projects_by_repo(db, wanted)
    if not owners:
        return frozenset()
    policies = await load_policies(db)
    return frozenset(
        repo for repo, owner in owners.items()
        if policy_for_product(policies, owner.slug).get("session_assistance", "on") == "off")


def policies_by_slug(db: Session) -> dict[str, dict]:
    """slug -> effective policy for every Project (sync session; the queue loader's session).

    Overlaid with the capacity governor's live plan (phase 2, spec §6.3) before
    ``effective_policy`` validates each entry — a lazy import avoids a module
    cycle (``capacity_apply`` imports ``effective_policy`` from this module)."""
    from server.automations.capacity_apply import apply_product_plan
    from server.automations.capacity_store import live_plan_sync
    rows = {slug: policy for slug, policy in
            db.execute(select(Project.slug, Project.automation_policy)).all() if slug is not None}
    plan = live_plan_sync(db)
    return {slug: effective_policy(raw) for slug, raw in apply_product_plan(rows, plan).items()}


# The reserved key a runner policy SNAPSHOT carries its no-product default under
# (spec 2026-09-18 §4, decision 2 amendment). A target with no product still needs a
# policy — pre-flip claims carry none — and since the Backlog Runner's snapshot has
# its ``ladder_top`` clamped to the model-approval gate, that default cannot be
# rebuilt with a bare ``effective_policy(None)`` on the far side of the seam without
# a SECOND clamp. Carrying it in the mapping keeps one. Empty because no ``Project``
# lookup can ever reach it: every per-product read guards on a truthy slug.
NO_PRODUCT_POLICY_KEY = ""


def policy_for_product(policies: "dict[str, dict]", product: "str | None") -> dict:
    """THE "which policy governs this target" fallback chain over a runner policy
    SNAPSHOT: the product's own entry, else the snapshot's no-product default
    (``NO_PRODUCT_POLICY_KEY`` — carrying the same approval-gate clamp on
    ``ladder_top`` that every product's entry got), else the pure schema defaults.

    One implementation for all three readers — the loop's ``_policy_for``, the
    spawn's ``_backlog_ladder_rung`` and the tap's ``_backlog_policy_model`` — so a
    product-less claim cannot end up with a ladder one tier higher on one side of
    the seam than the other. Only a snapshot from a seam that carries no default (a
    test's, or a pre-clamp one) reaches the pure ``effective_policy(None)``.
    """
    if product:
        policy = policies.get(product)
        if policy is not None:
            return policy
    return policies.get(NO_PRODUCT_POLICY_KEY) or effective_policy(None)


async def load_policies(db: AsyncSession) -> dict[str, dict]:
    """Async twin of ``policies_by_slug`` for the API/orchestrator paths — same
    reading, same capacity-governor overlay, no second rule (fix round 1, F5).
    The plan is read on the caller's own session (``live_plan_async``), like
    ``policies_by_slug``'s ``live_plan_sync(db)``."""
    from server.automations.capacity_apply import apply_product_plan
    from server.automations.capacity_store import live_plan_async
    plan = await live_plan_async(db)
    rows = (await db.execute(select(Project.slug, Project.automation_policy))).all()
    policies = {slug: policy for slug, policy in rows if slug is not None}
    return {slug: effective_policy(raw) for slug, raw in apply_product_plan(policies, plan).items()}


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
    #: The OPERATOR paused this product (#2684): its stored in-flight cap or its
    #: pipeline cap is 0. A 0 the capacity plan derived is load, not a pause — a
    #: manual dispatch may queue behind it, since the next plan tick re-splits.
    paused: bool = False

    @property
    def permitted(self) -> bool:
        return self.state == "permitted"


def _pr_doctor_priority_text(refs: tuple[str, ...]) -> str:
    """Why the product is reserved, naming the PRs when the caller supplied them.

    The Capacity page renders this as the product's "waiting on" (the binding
    constraint's text), and "a stuck pull request" alone gave an operator
    nothing to go and check — the #2202 deadlock was diagnosable only from
    journal logs. Bounded at three refs so one very stuck product cannot push
    a paragraph into the page. No refs supplied (a caller that only knows the
    gate is on) keeps the original sentence.
    """
    if not refs:
        return ("PR Doctor has a stuck pull request pending a fix for this product; "
                "capacity is reserved until it clears.")
    shown = ", ".join(refs[:3]) + (f" +{len(refs) - 3} more" if len(refs) > 3 else "")
    if len(refs) == 1:
        return (f"PR Doctor has a stuck pull request pending a fix for this product "
                f"({shown}); capacity is reserved until it clears.")
    return (f"PR Doctor has {len(refs)} stuck pull requests pending a fix for this "
            f"product ({shown}); capacity is reserved until they clear.")


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
    pipeline_count: int | None = None,
    pipeline_cap: int | None = None,
    pipeline_existing_slot: bool = False,
    pr_doctor_gated: bool = False,
    pr_doctor_pending_refs: tuple[str, ...] = (),
    runner_ceiling_note: str | None = None,
    product_capacity_note: str | None = None,
    capacity_from_plan: bool = False,
) -> ImplementationDecision:
    """Compose current implementation admission policy without I/O or claims.

    Callers gather verified canonical coverage and validated runner configuration.
    An intentional manual request bypasses scheduled mode only, never capacity or
    ownership. ``existing_slot`` means this proposal already occupies the supplied
    global/product counts; it does not exempt a zero cap. This function does not
    authorize continued work or other responsibilities. Extra constraints retain
    their authoritative reader's text and links, sorted by code for stable output.
    ``pipeline_cap`` None means no pipeline cap: no constraint is emitted at all
    (D13, blank = unlimited).
    ``runner_ceiling_note`` / ``product_capacity_note`` say where a cap came from
    (the capacity plan, a pin) and are appended to that ceiling's ``_full`` text
    only, so a refusal names its binding constraint (#2684). ``capacity_from_plan``
    says the policy's ``max_in_flight`` is the capacity plan's, not the operator's,
    so a 0 there does not set ``paused``.
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

    def capacity(name: str, count: int | None, cap: int | None, *, occupied: bool = False,
                 full_text: str | None = None, note: str | None = None) -> None:
        if type(count) is not int or type(cap) is not int or count < 0 or cap < 0:
            add(f"{name}_unknown", "unknown", f"The {name.replace('_', ' ')} capacity is unavailable.")
            return
        # Ruling 20 (D8: "admission refuses new claims; it never kills a
        # session"): a claim that already holds its slot is never refused by
        # a cap that moved under it. This is the whole ceiling check for an
        # occupied claim — a cap of 0 (paused) or a count that has since
        # exceeded the cap does not refuse it. `occupied=False` (a NEW claim)
        # keeps the prior behaviour unchanged, including `cap == 0` refusing.
        if occupied:
            add(f"{name}_held", "permitted",
                f"This claim already holds a {name.replace('_', ' ')} slot ({count}/{cap}).")
        elif cap == 0 or count >= cap:
            text = full_text or f"The {name.replace('_', ' ')} limit is reached ({count}/{cap})."
            add(f"{name}_full", "refused", f"{text[:-1]}: {note}." if note and text.endswith(".") else text)
        else:
            add(f"{name}_available", "permitted", f"The {name.replace('_', ' ')} limit has capacity ({count}/{cap}).")

    # "runner_ceiling": decide()'s global_cap is the Backlog Runner's max_concurrent
    # (spec 2026-09-20 D8/C16), not AppSettings' global cap — that one is
    # global_cap_full in the readout.
    capacity("runner_ceiling", global_count, global_cap, occupied=existing_slot, note=runner_ceiling_note)
    capacity("product_capacity", product_count, effective["max_in_flight"], occupied=existing_slot,
             note=product_capacity_note)
    # `occupied=existing_slot` here too (deferred-now 2, #2040): the caller's own
    # `existing_token in accounting` decrement only exempts a claim still inside
    # the rolling 24h window it was counted in — a claim re-validated after that
    # window rolls over would otherwise be refused by `daily_budget_full` despite
    # already holding its slot, the same D8 violation the other two ceilings fixed.
    capacity("daily_budget", daily_count, daily_cap, occupied=existing_slot)
    # Spec 2026-09-20 D13: open, unmerged automation PRs vs the product's pipeline
    # cap. Blank cap = unlimited: nothing is emitted. `occupied` keeps governor-spec
    # D8 — a claim that already holds its slot is never refused by a cap — but the
    # pipeline's occupancy is judged by the same rule as its count
    # (`pipeline_existing_slot`, `counts_toward_pipeline`), not `existing_slot`
    # (`is_in_flight`): a handed-off row is still occupying its pipeline slot even
    # though it no longer holds a runner slot.
    if pipeline_cap is not None:
        capacity("product_pipeline", pipeline_count, pipeline_cap, occupied=pipeline_existing_slot,
                 full_text=f"pipeline full: {pipeline_count} claims and PRs open (cap {pipeline_cap})")
    # spec 2026-09-21 pr-doctor-priority: PR Doctor always wins available
    # capacity for a product with a pending stuck PR. Same D8 carve-out as
    # every other capacity check — an occupied claim is never refused.
    if pr_doctor_gated:
        if existing_slot:
            add("pr_doctor_priority_held", "permitted",
                "This claim already holds its slot; PR Doctor's priority does not interrupt it.")
        else:
            add("pr_doctor_priority", "refused", _pr_doctor_priority_text(pr_doctor_pending_refs))
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
        paused=((effective["max_in_flight"] == 0 and not capacity_from_plan)
                or effective["max_pipeline_prs"] == 0),
    )
