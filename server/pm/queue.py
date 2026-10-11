"""Global priority queue over the 003 mirror (spec §7a). Pure core; the loader lives in
``queue_store.py`` (Task 3). The runner, the grouper, the status card and ``GET /api/pm/queue``
all rank through ``rank`` — one definition of "what agents will do next".

Decision C-2: a row whose board Status is unset or outside the spec vocabulary
(``STATUS_OPTIONS``) is treated as Backlog-equivalent rather than excluded — see
``skip_reason`` for the mechanics and rationale."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .desired_state import (HELD_KINDS, STATUS_BACKLOG, STATUS_BLOCKED,
                            STATUS_OPTIONS, STATUS_READY)
from .outcomes import bounced_out

# Issues the runner must never treat as implementable work (#637). A backlog
# session writes code and opens a PR — but bot/app accounts open automated
# meta-tickets (Renovate's "Dependency Dashboard", Dependabot bumps) that have
# nothing to implement, so a spawned session just burns a slot. GitHub's REST API
# suffixes every bot/app login with ``[bot]`` (``renovate[bot]``,
# ``dependabot[bot]``, ``github-actions[bot]``, …) and the author leg fires on
# that suffix. The mirror, however, is synced over GraphQL (``github_sync``),
# whose ``author { login }`` carries NO suffix (``github-actions`` —
# ``alert_series.AUTOMATION_LOGINS``), so for mirrored rows the author leg is
# inert and labels and title carry the filter. Deliberately so: the same login
# files issues that ARE work (``deploy-monitor`` incidents). The auto-managed
# artifacts it files — ``forced-deploy-audit`` latches, watchdogs, rollups,
# digests — are ``TRACKER_LABELS`` below (#2683 reversed the earlier call that
# ``forced-deploy-audit`` latches were work: they auto-close on a green nightly,
# and the repo's own instructions forbid closing them with a PR; #2921/#2923/
# #2930). A product names further noise by label (``skip_labels`` in its policy);
# the author leg stays for REST-shaped callers. ``viberpit-ignore`` is the
# operator's own "never dispatch this" label, honoured for EVERY product (#2683):
# a product used to have to list it in its ``skip_labels``, and a typo there
# silently let every tagged issue through (cairnea/KinDash#17427).
SKIP_ISSUE_LABELS = frozenset({"dependencies", "renovate", "viberpit-ignore"})
SKIP_ISSUE_TITLES = frozenset({"dependency dashboard"})

# A person's own "not now" on the issue itself (#2683, steiner385/viberpit#1432:
# parked by label, a decision "Leave parked, no PR now" accepted — which releases
# the decision hold — and the runner claimed it the next day). Skipped as its own
# reason, ``parked``, and no policy can opt back in. Not ``held``: the status card's
# held count is read beside the holds list, which lists real holds only.
PARKED_LABELS = frozenset({"parked"})

# Self-closing trackers (#2683): issues a CI workflow files AND closes by itself —
# a per-test ``[flaky]`` tracker closes after N green scheduled runs, a
# "Nightly failed — <date>" rollup on the next green run, a recurring "… Digest"
# is rewritten in place. A dispatched session can only report "no PR needed; it
# closes itself", which the runner used to hand the owner as an ``owner_action``
# hold (11 of the KinDash Held inbox on 2026-10-11). Unlike ``bot`` this is a
# per-product choice (``skip_self_closing_trackers``, default on): a flaky test IS
# fixable, and a product may want its trackers worked. Lowercase label spellings
# as filed on cairnea/KinDash: the ``red-watchdog`` rolling issue closes the day
# its workflow goes green (#17030 and #17388 were re-dispatched ~70 times), the
# ``nightly-consecutive-failure`` escalation on a green nightly, a
# ``forced-deploy-audit`` latch once a green nightly runs on the forced SHA, and
# the ``nightly-scoreboard`` is a permanent singleton the workflow rewrites.
TRACKER_LABELS = frozenset({"flaky", "nightly-failure", "weekly-failure", "midday-e2e-failure",
                            "red-watchdog", "nightly-consecutive-failure",
                            "forced-deploy-audit", "nightly-scoreboard"})
# ``flaky`` alone marks a tracker only on an issue an AUTOMATION filed (the flaky
# detector's): a person's "fix flaky test X" carrying the label is work (#2683
# review). The detector's ``[flaky]`` title tag counts whoever filed it.
_AUTOMATION_ONLY_TRACKER_LABELS = frozenset({"flaky"})
# Exactly the title shapes the KinDash workflows write, nothing looser (#2683
# review: "Daily backup failed", "Nightly sync failed (calendar)" and "Weekly report
# failed - timeout" are bug reports, not trackers).
_TRACKER_TITLE = re.compile(
    r"^\s*\[flaky\]"                         # the per-test flaky tracker's tag
    r"|^\s*flaky test burndown\b"            # … and its burndown digest
    r"|^\s*red-watchdog:"                    # "red-watchdog: workflow "Nightly" — 5+ …"
    # The scheduled-run rollups: "Nightly failed — 2026-10-09", "Midday e2e failed —
    # 2026-10-06" (an em/en dash and the run's date), and the auto-revert's
    # "weekly full-ci failed — culprit identified — …".
    r"|^\s*(?:nightly|weekly|midday)(?:\s+e2e)?\s+failed\s+[—–]\s+\d{4}-\d{2}-\d{2}\s*$"
    r"|^\s*(?:nightly|weekly)\s+full-ci\s+failed\s+[—–]\s+culprit\b",
    re.IGNORECASE)
# A generated report: its title is "<emoji> … Digest" ("🧮 UI Maintenance Cost &
# Architecture ROI Digest", "📊 CI / Delivery Observability Digest"). The leading
# pictograph (Unicode category So) is what separates it from a feature named
# "… Digest" ("Add Weekly Family Digest").
_DIGEST_TITLE = re.compile(r"\bDigest\s*$")


def _is_generated_digest(title: str) -> bool:
    t = title.strip()
    return bool(t) and unicodedata.category(t[0]) == "So" and bool(_DIGEST_TITLE.search(t))


def is_automation_author(author: str) -> bool:
    """Whether *author* is a GitHub App/bot as the mirror (``app/<slug>``) or the REST
    API (``<slug>[bot]``) records one — never a bare login, which a user can own."""
    login = (author or "").strip().lower()
    return login.startswith("app/") or login.endswith("[bot]")

# Labels that mark work the queue must serve BEFORE ordinary backlog, whatever
# its score or age (#16150, #16151). An automation that reports a broken
# environment files an issue that is, by construction, unscored (no human has
# groomed it) and brand new — which under a score-then-oldest ordering is the
# worst position in the queue, not the best. Two such issues reported both admin
# environments down and sat at 328th and 329th of 332 while the environments
# stayed broken, behind three-day-old test flakes.
#
# Lowercase; compared case-insensitively like SKIP_ISSUE_LABELS. The label is
# OWNED by the automation that files the issue
# (``server.automations.deploy_monitor.LABEL``), but importing an automation into
# this pure ranking core would invert the layering — so the coupling is pinned by
# ``test_urgent_labels_pin_the_deploy_monitor_label`` instead, exactly as
# MIN_STATUS_OPTIONS pins its coupling to STATUS_OPTIONS below.
URGENT_LABELS = frozenset({"deploy-monitor"})

SKIP_REASONS = ("status", "blocked", "live_session", "claimed", "assigned", "epic_open", "bot",
                 "tracker", "untrusted", "held", "parked", "not_needed", "needs_spec")

# Logins that file issues which ARE work (deploy-monitor, forced-deploy-audit) — always
# trusted. Mirrors alert_series.AUTOMATION_LOGINS; pinned by a test rather than imported
# to keep this ranking core free of automation imports (same pattern as URGENT_LABELS).
TRUSTED_AUTOMATION_LOGINS = frozenset({"github-actions"})

# The only two ``min_status`` values the queue accepts today (C1 will feed this from
# stored runner config). Explicit rather than derived from a slice of STATUS_OPTIONS
# at call time — see MIN_STATUS_OPTIONS below for the pinned coupling.
_MIN_STATUS_RANK = {STATUS_BACKLOG: 0, STATUS_READY: 1}

# The board-display order (STATUS_OPTIONS, which gh_reconcile._ensure_fields also uses
# to seed the Status field's options) happens to put Backlog and Ready first. This
# constant pins that coupling explicitly rather than depending on it silently — see
# test_min_status_options_are_the_first_two_board_statuses.
MIN_STATUS_OPTIONS = (STATUS_BACKLOG, STATUS_READY)


@dataclass(frozen=True)
class QueueRow:
    """One mirrored board item + the facts the ranking rules need."""

    ref: str
    repo: str
    number: int
    product: str
    title: str
    body: str
    url: str
    status: str | None
    score: float | None
    gh_created_at: datetime | None
    gh_updated_at: datetime | None
    assignees: tuple[str, ...]
    author: str
    labels: tuple[str, ...]
    open_children: int
    open_blockers: tuple[str, ...]
    live_session: bool
    claimed: bool
    # Spec 2026-09-18 §3: the hold on the item. ``owner_action`` / ``decision`` park
    # the row (``skip_reason`` → "held"); ``evidence`` / ``stronger_model`` ride
    # through to the entry so the spawn can pick its prompt and rung.
    hold_kind: str | None = None
    # D8: how many FINALISED (non-``running``/``merged``) dispatches this exact
    # body (by ``body_fingerprint``) has already had — the loader
    # (``queue_store.load_queue_rows``) computes it, ``skip_reason`` compares it
    # against the product's ``give_up_after_bounces``. Defaulted 0 (never
    # bounced) so a caller with no outcome history keeps constructing rows
    # positionally.
    bounces: int = 0
    # Spec 2026-09-30 §3: the hold's provenance, threaded for the escalation read
    # model (``server/pm/escalation.py``) so it can say who parked the item, why
    # and for how long. Defaulted so every positional construction keeps working.
    hold_reason: str | None = None
    hold_by: str | None = None
    hold_set_at: datetime | None = None
    # The mirror row's id, so a held item the escalation read model lists can be
    # ACTED on (Retry / approve-escalation are per-work-item routes) without the
    # client resolving ``ref`` → id itself. Defaulted for positional constructors.
    work_item_id: str | None = None
    # #2683 not-needed suppression (``outcomes.not_needed_suppression``, computed by
    # the loader): until when the queue skips the issue as ``not_needed``, and
    # whether a not-needed verdict still stands on its current material at all (the
    # hold-hygiene pass reads that, whatever the window).
    not_needed_until: datetime | None = None
    not_needed_standing: bool = False


@dataclass(frozen=True)
class QueueEntry:
    ref: str
    repo: str
    number: int
    product: str
    title: str
    body: str
    url: str
    score: float | None
    # Carried off the row purely so ``rank_key`` applies to an ENTRY too: a caller
    # that merges several ``rank`` results (the runner ranks one product at a time)
    # has to re-sort by the same key, and age is the tie-break. Defaulted so every
    # positional construction keeps working.
    gh_created_at: datetime | None = None
    # Same reason as ``gh_created_at``: urgency is decided from the ROW's labels,
    # which an entry does not carry, so the verdict rides along. Without it the
    # cross-product merge would re-sort by a key that can no longer see urgency
    # and quietly undo the preemption.
    urgent: bool = False
    # Carried off the row so a caller downstream of ranking (item 4's spawn) can
    # still pick the hold's prompt/rung without re-reading the row.
    hold_kind: str | None = None


def is_bot_login(author: str | None) -> bool:
    """THE author half of the ``bot`` verdict: a ``[bot]`` login. Shared with the
    triage loop's bot-artifact recognition (``triage_accept.is_bot_artifact``) so
    the queue and triage can never disagree about who is a bot. Deliberately NOT
    ``github-actions`` (bare or ``app/``): it files the recurring CI alert issues
    (#2680) the runner is meant to work."""
    return (author or "").endswith("[bot]")


def is_bot_noise(*, author: str, labels: Iterable[str], title: str,
                 extra_skip_labels: frozenset[str] = frozenset()) -> bool:
    """THE ``bot`` verdict: the built-in bot-noise filter plus a product's own
    ``skip_labels`` (lowercased by the caller — ``product_policy.skip_labels``).
    Exported because ``skip_reason`` is not its only consumer: the hold-hygiene
    pass and the approve-escalation tap (``server/pm/policy_skip.py``) must judge
    an issue by exactly this rule, not a copy that can drift (KinDash#16652)."""
    if is_bot_login(author):
        return True
    if {str(lbl).strip().lower() for lbl in labels} & (SKIP_ISSUE_LABELS | extra_skip_labels):
        return True
    return (title or "").strip().lower() in SKIP_ISSUE_TITLES


def is_self_closing_tracker(labels: Iterable[str], title: str, *, author: str = "") -> bool:
    """THE ``tracker`` verdict (``TRACKER_LABELS`` and the tracker title shapes): an
    issue its own workflow closes. One predicate for the queue's skip, the
    hold-hygiene pass and the approve-escalation tap (``server/pm/policy_skip.py``)
    and the runner's not-needed settle, so they cannot disagree about which issues
    are trackers. Whether a product SKIPS them is its policy's call
    (``product_policy.skips_self_closing_trackers``), not this predicate's."""
    found = {str(lbl).strip().lower() for lbl in labels} & TRACKER_LABELS
    if found - _AUTOMATION_ONLY_TRACKER_LABELS:
        return True
    if found and is_automation_author(author):
        return True
    t = title or ""
    return bool(_TRACKER_TITLE.search(t) or _is_generated_digest(t))


def is_parked(labels: Iterable[str]) -> bool:
    """Whether a person parked the issue by label (``PARKED_LABELS``)."""
    return bool({str(lbl).strip().lower() for lbl in labels} & PARKED_LABELS)


def is_trusted_author(author: str, trusted: frozenset[str]) -> bool:
    """Whether *author* may have their issue dispatched to an agent. A public repo lets
    anyone open an issue; a dispatched session writes code that auto-merges and deploys,
    so an untrusted author must never reach the runner. Fail-closed: empty/ghost is
    untrusted. *trusted* is already lowercased (``product_policy.trusted_authors``); a
    GitHub App is listed there as ``app/<slug>``, a user by bare login. Trust is keyed on
    the CURRENT GitHub login (the mirror stores logins, which users can rename), so a
    trusted account that renames must be removed from the list -- otherwise whoever
    registers the freed login inherits its trust."""
    login = (author or "").strip().lower()
    if not login or login == "ghost":
        return False
    # The mirror records a Bot as ``app/<slug>`` (``github_sync._author``); the REST
    # shape is ``<slug>[bot]``. Bot and User logins share a namespace, so a BARE login
    # is a user and is never given a bot's trust — anyone can register a user named
    # ``github-actions`` or ``viberpit``.
    if login.startswith("app/") or login.endswith("[bot]"):
        slug = login.removeprefix("app/").removesuffix("[bot]")
        return bool(slug) and (f"app/{slug}" in trusted or slug in TRUSTED_AUTOMATION_LOGINS)
    return login in trusted


def _is_bot(row: QueueRow, extra_skip_labels: frozenset[str] = frozenset()) -> bool:
    return is_bot_noise(author=row.author, labels=row.labels, title=row.title,
                        extra_skip_labels=extra_skip_labels)


def is_urgent(row: "QueueRow | QueueEntry") -> bool:
    """Whether this item preempts ordinary backlog (see ``URGENT_LABELS``).

    Reads labels off a ``QueueRow`` and the carried verdict off a ``QueueEntry``,
    for the same reason ``rank_key`` accepts both: the runner ranks each product
    separately and re-sorts the merged ENTRIES, which have no labels.
    """
    labels = getattr(row, "labels", None)
    if labels is not None:
        return bool({str(lbl).strip().lower() for lbl in labels} & URGENT_LABELS)
    return bool(getattr(row, "urgent", False))


def _min_status_rank(min_status: str) -> int:
    try:
        return _MIN_STATUS_RANK[min_status]
    except KeyError:
        raise ValueError(
            f"min_status must be one of {MIN_STATUS_OPTIONS}, got {min_status!r}") from None


def skip_reason(
    row: QueueRow,
    *,
    min_status: str,
    respect_dependencies: bool,
    now: datetime,
    give_up_after_bounces: int = 0,
    extra_skip_labels: frozenset[str] = frozenset(),
    trusted_authors: frozenset[str] = frozenset(),
    skip_trackers: bool = True,
) -> str | None:
    """Decision C-2: a row whose board Status is ``None`` (never stamped by the
    reconciler) or outside the spec vocabulary (e.g. GitHub's default "Todo" column)
    is treated as Backlog-equivalent, not excluded — an unreconciled board would
    otherwise yield an empty queue. It is then gated by ``min_status`` exactly like a
    real Backlog row: eligible in Backlog mode, skipped as "status" in Ready mode.
    """
    min_rank = _min_status_rank(min_status)
    # Checked BEFORE the status gate: the reconciler stamps the held board Status
    # from exactly this hold, so a status-first check would report these rows as
    # plain "status" skips and the card would lose its held gauge.
    if row.hold_kind in HELD_KINDS:
        return "held"
    if is_parked(row.labels):
        return "parked"
    # #2683: a session found this issue needed no PR and nothing material has
    # changed since — checked before the status gate for the same reason as
    # ``held``: the reason is the useful answer, not "status".
    if row.not_needed_until is not None and now < row.not_needed_until:
        return "not_needed"
    # D8: this body has already bounced enough — a spec edit (which changes the
    # fingerprint the count is keyed on) is what re-arms it, not time.
    if bounced_out(row.bounces, give_up_after_bounces):
        return "needs_spec"
    status = row.status if row.status in STATUS_OPTIONS else STATUS_BACKLOG
    if status not in (STATUS_BACKLOG, STATUS_READY, STATUS_BLOCKED):
        return "status"  # Done / In progress / Held
    if status == STATUS_BLOCKED or (respect_dependencies and row.open_blockers):
        return "blocked"
    # status is now guaranteed Backlog or Ready (Blocked returned above).
    if _MIN_STATUS_RANK[status] < min_rank:
        return "status"
    if row.live_session:
        return "live_session"
    if row.claimed:
        return "claimed"
    if row.assignees:
        return "assigned"
    if row.open_children:
        return "epic_open"
    if _is_bot(row, extra_skip_labels):
        return "bot"
    if skip_trackers and is_self_closing_tracker(row.labels, row.title, author=row.author):
        return "tracker"
    if not is_trusted_author(row.author, trusted_authors):
        return "untrusted"
    return None


def rank_key(row: "QueueRow | QueueEntry") -> tuple:
    """THE queue ordering, as a sort key: urgent work first, then highest score
    (an unscored row last), then oldest first, then ref.

    Urgency LEADS because the other legs actively work against it: an incident
    filed by an automation is unscored and newest, so score-then-age sorts it to
    the very back of the queue at exactly the moment it matters most (#16150).

    Exported because ``rank`` is not always the last word on ordering: the Backlog
    Runner ranks each product separately (each has its own ``queue_min_status`` /
    ``respect_dependencies``) and then merges, and that merge must sort by exactly
    this key rather than a hand-copied tuple that can drift from it. Takes anything
    carrying ``score`` / ``gh_created_at`` / ``ref``, so it applies to a ``QueueRow``
    before ranking and a ``QueueEntry`` after it.

    A missing ``gh_created_at`` sorts last via its own boolean leg rather than a
    sentinel datetime, so the key never compares a naive datetime against an aware
    one (which raises).
    """
    created = row.gh_created_at
    return (not is_urgent(row), row.score is None, -(row.score or 0.0), created is None,
            created or datetime.min, row.ref)


def rank(
    rows: Iterable[QueueRow],
    *,
    min_status: str = STATUS_READY,
    respect_dependencies: bool = True,
    now: datetime,
    give_up_after_bounces: int = 0,
    extra_skip_labels: frozenset[str] = frozenset(),
    trusted_authors: frozenset[str] = frozenset(),
    skip_trackers: bool = True,
) -> tuple[list[QueueEntry], dict[str, int]]:
    kept: list[QueueRow] = []
    skipped: dict[str, int] = {}
    for row in rows:
        why = skip_reason(
            row,
            min_status=min_status,
            respect_dependencies=respect_dependencies,
            now=now,
            give_up_after_bounces=give_up_after_bounces,
            extra_skip_labels=extra_skip_labels,
            trusted_authors=trusted_authors,
            skip_trackers=skip_trackers,
        )
        if why:
            skipped[why] = skipped.get(why, 0) + 1
        else:
            kept.append(row)
    kept.sort(key=rank_key)
    return [
        QueueEntry(r.ref, r.repo, r.number, r.product, r.title, r.body, r.url, r.score,
                   r.gh_created_at, is_urgent(r), hold_kind=r.hold_kind)
        for r in kept
    ], skipped


def to_candidate(e: QueueEntry) -> dict:
    """The runner/grouper candidate shape."""
    return {
        "ref": e.ref,
        "number": e.number,
        "repo": e.repo,
        "title": e.title,
        "body": e.body,
        "url": e.url,
        "score": e.score,
        "product": e.product,
        # The grouper re-orders this pool into the plan the runner pops, so the
        # urgency verdict has to cross that hand-off as data — a candidate dict is
        # all it receives.
        "urgent": e.urgent,
        # Carried so the runner's spawn (item 4) can pick the hold's prompt/rung
        # without re-reading the row — same reason ``urgent`` rides along.
        "hold_kind": e.hold_kind,
    }
