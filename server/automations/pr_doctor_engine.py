"""Pure PR Doctor decision engine — no I/O, never raises into the poller.

classify(info) -> failure mode; plan_recovery(...) -> a RecoveryAction the
orchestrator (Plan B) executes; plus per-target retry/re-arm/terminate helpers.
v1 handles failing CI, out-of-date branch, and merge conflict (stuck/queued
checks are deferred to v2).

``ci_red_is_unproven`` (issue #2218) is the freshness half of ``failing_ci``: a
rollup can read FAILURE because its checks were CANCELLED (a push superseded the
run) or have not reported yet, and neither is evidence the branch is broken.

v2 (issue #806) adds ``comment_command`` — an ``@claude`` review comment on an
otherwise-healthy Initiative PR spawns a targeted fix session. It is lowest
priority: a PR with failing CI or a merge conflict gets the real fix first, and
comment-command fixes only fire once the PR is otherwise green.

Give-up accounting — why a monotonic ceiling:
Fix injection is ASYNC. ``record_attempt`` records the PRE-fix head_sha, so when
the agent's fix pushes a NEW (still-failing) head, the next poll's ``apply_rearm``
sees an unknown sha and cannot tell "our own fix's push" apart from "an external
human push" — both look external → it re-arms ``attempts`` to 0. If give-up keyed
on the re-armable counter it would never fire (each attempt re-arms itself away).
So give-up keys on a MONOTONIC, re-arm-IMMUNE total (``total_attempts`` /
``total_mechanical``) — the count of distinct injections over the PR's lifetime.
It is sticky: once the ceiling is reached PR Doctor stops auto-acting and hands
off via needs-human (a human who keeps pushing after N failed attempts is already
in the loop). The re-armable ``attempts`` still exists — it drives the
orchestrator's per-head one-shot guard — but no longer drives give-up."""
from __future__ import annotations

from dataclasses import dataclass

from server.github.pr_status import PullRequestInfo

# First match wins — conflict before CI (never fight CI on an unmergeable branch).
# ``comment_command`` is last: fires only when the PR is otherwise green.
FAILURE_PRIORITY = ("blocked", "stuck_enqueue", "merge_conflict", "out_of_date",
                    "failing_ci", "comment_command")


# Check-run conclusions that carry NO verdict about the branch: the check either
# has not reported yet ("pending" — a null GraphQL conclusion, i.e. QUEUED or
# IN_PROGRESS) or was killed before it could ("cancelled").
_NO_VERDICT = frozenset({"pending", "cancelled"})


def ci_red_is_unproven(info: PullRequestInfo) -> bool:
    """True when *info*'s red rollup is not evidence that the branch is broken (#2218).

    Also guards the ``stuck_enqueue`` arm of ``classify`` (#2324): a still-in-flight
    check makes BLOCKED+armed+not-in-queue indistinguishable from a genuinely
    silently-refused enqueue, so the same freshness test applies there too.

    ``checks == "failing"`` comes from ``statusCheckRollup.state``, which goes
    FAILURE as soon as ANY context is non-green — including a context that never
    produced a verdict. Two shapes make that red a lie, and both are readable off
    the ``checks_detail`` the poller already fetches:

    * **Still in flight.** A check with conclusion ``pending`` has not reported.
      Acting now dispatches a repair session against a partial result that
      usually resolves itself a few minutes later (seen repeatedly on PRs whose
      slowest ``server-tests`` shard was simply still running).
    * **Superseded.** ``.github/workflows/ci.yml`` sets ``cancel-in-progress:
      true`` for non-``master`` refs, so a second push inside a CI cycle cancels
      the first run's jobs. GitHub's rollup counts CANCELLED as FAILURE, and the
      ``if: always()`` ``lint-and-test`` aggregator converts those cancellations
      into a hard ``failure`` on a *required* context — so the superseded sha
      reads ``BLOCKED`` + failing even though nothing about the diff is wrong.
      PR #2213 (2026-09-22) burned a dispatched session exactly this way: its
      sha ``42b60b37`` carried five ``cancelled`` jobs and one aggregator
      ``failure`` while the run on the real head went fully green.

    Deliberately NOT fixed in the workflow: an aggregator reporting ``success``
    for a required shard that never finished would let branch protection merge an
    unverified sha. The guard belongs here.

    Known trade-off: a repo whose matrix uses ``fail-fast: true`` cancels the
    surviving shards when one genuinely fails, which reads the same way and
    defers a real ``failing_ci``. This repo's matrix is ``fail-fast: false``
    (``.github/workflows/ci.yml``), and deferring a real red costs a poll cycle
    while acting on a fake one costs a session and risks a junk commit pushed to
    a healthy branch — so the guard errs toward deferring.

    Fails CLOSED: with no per-check data at all (a GitHub App without
    ``checks:read``, or a daemon→edge payload that carries none) this returns
    False and ``classify`` keeps its previous behaviour.
    """
    return any(c.conclusion in _NO_VERDICT for c in info.checks_detail)


def classify(info: PullRequestInfo) -> str | None:
    """Return the v1 failure mode for *info*, or None (terminal/draft/healthy).
    Pure + total. Checks are evaluated in FAILURE_PRIORITY order (blocked →
    merge_conflict → out_of_date → failing_ci) — keep in sync with that constant.

    ``failing_ci`` additionally requires that some check-run actually EARNED the
    red — see ``ci_red_is_unproven`` (#2218)."""
    if info.state in ("merged", "closed"):
        return None
    if info.merge_state_status == "DRAFT" or info.is_draft:
        return None
    if info.merge_state_status == "BLOCKED":
        # BLOCKED gates the merge, but the CAUSE splits three ways:
        # 1. A failing *required* status check (branch protection) → failing_ci.
        #    A failing check is exactly what PR Doctor fixes, so classify it
        #    actionably rather than as a review gate.
        # 2. Armed + not in queue → stuck_enqueue: GitHub silently refused to
        #    enqueue the PR (e.g. a shadowed required check). A mechanical re-arm
        #    often unsticks it. This is NOT a human review gate.
        # 3. Everything else (review/approval required) → blocked: notify only.
        if info.checks == "failing":
            # A red no check-run earned is not a repair job — and must not drop
            # through to the stuck_enqueue arm below either, or an armed PR whose
            # run was merely superseded gets a mechanical update-branch pushed at
            # it (PR #2213 was armed and not in a queue). Wait for the next poll.
            return None if ci_red_is_unproven(info) else "failing_ci"
        # Same freshness guard as the failing_ci arm above, and for the same
        # reason: this repo has no real GitHub merge queue (`in_merge_queue` is
        # never true), so "armed + BLOCKED + not in queue" is indistinguishable
        # from "a required check is still mid-run" at a single snapshot — every
        # PR with auto-merge enabled is BLOCKED for the first several minutes of
        # its life purely because its checks haven't all reported yet. Only a
        # check that has genuinely reported (or a required check that never
        # shows up at all — the shadowed-check case this arm exists for) leaves
        # `ci_red_is_unproven` False. Caught live on #2324: opened, auto-merge
        # armed, checks still running — misclassified as stuck_enqueue within
        # two minutes and "fixed" with a needless branch update, which gated
        # the whole product's Backlog Runner capacity on nothing.
        if info.auto_merge_enabled and not info.in_merge_queue and not ci_red_is_unproven(info):
            return "stuck_enqueue"
        return "blocked"  # review/approval gate — notify only (non-goal to approve)
    if info.mergeable == "CONFLICTING" or info.merge_state_status == "DIRTY":
        return "merge_conflict"
    if info.merge_state_status == "BEHIND":
        # A PR in the merge queue cannot have its branch updated — GitHub rejects
        # update-branch with 422. Leave it alone; the queue will handle the merge.
        if info.in_merge_queue:
            return None
        return "out_of_date"
    # failing_ci only for a *required* failure: UNSTABLE = a non-required check is
    # red on a mergeable branch → not actionable. (merge_state_status is the
    # required-ness discriminator the poller always has.)
    if info.checks == "failing" and info.merge_state_status != "UNSTABLE":
        return None if ci_red_is_unproven(info) else "failing_ci"
    return None


def classify_comment_command(comment_body: str) -> str | None:
    """Return ``"comment_command"`` when *comment_body* contains an ``@claude``
    directive, or ``None`` otherwise.  Pure + total.

    This is the PR Doctor bridge to ``review_policy.extract_at_claude_command``:
    it speaks the failure-mode vocabulary so the poller can pass the result
    straight to ``plan_recovery``. The orchestrator calls it on each unresolved
    review comment; the first match stops the scan (the poller feeds one comment
    at a time or a batch — callers normalise that before calling here).
    """
    from server.sdlc.review_policy import extract_at_claude_command
    return "comment_command" if extract_at_claude_command(comment_body) else None


# ---------------------------------------------------------------------------
# Recovery action dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Notify:
    pass


@dataclass(frozen=True)
class Propose:
    kind: str  # the failure mode, surfaced as a one-tap action


@dataclass(frozen=True)
class Mechanical:
    kind: str  # "out_of_date"


@dataclass(frozen=True)
class SessionFix:
    kind: str  # "failing_ci" | "merge_conflict" | "comment_command"


@dataclass(frozen=True)
class GiveUp:
    pass


RecoveryAction = Notify | Propose | Mechanical | SessionFix | GiveUp

# Which failure modes are mechanical (no session) vs session-driven, and the
# config flag that gates each in auto mode.
_MECHANICAL = {"out_of_date", "stuck_enqueue"}
_AUTO_FLAG = {
    "out_of_date": "auto_update_out_of_date",
    "failing_ci": "auto_fix_failing_ci",
    "merge_conflict": "auto_resolve_conflicts",
    "stuck_enqueue": "auto_fix_stuck_enqueue",
    # comment_command: @claude fix directives on Initiative PRs (Phase 4 #806).
    "comment_command": "auto_fix_comment_commands",
}


def plan_recovery(failure_mode: str | None, mode: str, config: dict,
                  state: dict) -> RecoveryAction:
    """Map a failure mode + automation mode + config + per-target state to an
    action. Pure + total. (Repo-scope gating happens in the orchestrator before
    this is called — see Plan B — so `enabled_repos` is not consulted here.)"""
    if failure_mode is None:
        return Notify()
    if failure_mode == "blocked":
        return Notify()  # review required — never auto-act
    if mode == "off":
        return Notify()
    if state.get("gave_up"):
        return Notify()
    if mode == "propose":
        return Propose(kind=failure_mode)
    # mode == "auto": gate risky modes; fall back to propose when the flag is off.
    # Use .get() so an unrecognised/future failure mode (e.g. "stuck_checks")
    # produces a safe Notify() rather than a KeyError.
    flag_key = _AUTO_FLAG.get(failure_mode)
    if flag_key is None:
        return Notify()
    if not config.get(flag_key, False):
        return Propose(kind=failure_mode)
    mechanical = failure_mode in _MECHANICAL
    # Give up on the MONOTONIC total (re-arm-immune), not the re-armable counter:
    # with async inject our own fix's push is indistinguishable from an external
    # push, so only the total ceiling reliably bounds total injections.
    total_key = "total_mechanical" if mechanical else "total_attempts"
    cap = config.get("mechanical_attempts", 2) if mechanical else config.get("max_attempts", 3)
    if state.get(total_key, 0) >= cap:
        return GiveUp()
    return Mechanical(kind=failure_mode) if mechanical else SessionFix(kind=failure_mode)


# ---------------------------------------------------------------------------
# Per-target state machine helpers
# ---------------------------------------------------------------------------

# The durable (kind -> head SHA) dispatch dedupe, inside the per-target state row
# (#2912/#2920). Neither ``apply_rearm`` nor the recovery clear drops it — a new
# head is what re-arms it, by no longer matching.
ACTED_HEADS = "acted_heads"

def apply_rearm(state: dict, info: PullRequestInfo) -> dict:
    """Reset attempt counters when the PR changed in a way that deserves fresh
    tries: an EXTERNAL push (head_sha not one PR Doctor itself produced) or a
    different failure class. Records the current head + failure class. Pure —
    returns a new dict."""
    out = dict(state)
    fm = classify(info)
    known_shas = {state.get("last_head_sha"), state.get("last_action_sha")}
    external_push = info.head_sha and info.head_sha not in known_shas
    changed_class = fm is not None and fm != state.get("last_failure")
    if external_push or changed_class:
        # Re-arm the per-head one-shot budget only. ``total_attempts`` /
        # ``total_mechanical`` are the re-arm-IMMUNE hard ceiling that drives
        # give-up; they are deliberately NOT reset here (carried through
        # untouched via the ``dict(state)`` copy).
        out["attempts"] = 0
        out["mechanical_attempts"] = 0
        out["gave_up"] = False
    out["last_head_sha"] = info.head_sha
    if fm is not None:
        out["last_failure"] = fm
    return out


def record_attempt(state: dict, action, head_sha: str) -> dict:
    """Consume one attempt of the right kind and record the resulting head as
    last_action_sha (so the next poll's apply_rearm won't treat our own push as
    external). Call ONLY after a real injection / gh success."""
    out = dict(state)
    if isinstance(action, Mechanical):
        out["mechanical_attempts"] = out.get("mechanical_attempts", 0) + 1
        # Monotonic, re-arm-immune ceiling — total distinct mechanical injections.
        out["total_mechanical"] = out.get("total_mechanical", 0) + 1
    else:  # SessionFix
        out["attempts"] = out.get("attempts", 0) + 1
        # Monotonic, re-arm-immune ceiling — total distinct session injections.
        out["total_attempts"] = out.get("total_attempts", 0) + 1
    out["last_action_sha"] = head_sha
    if head_sha:
        out[ACTED_HEADS] = {**(out.get(ACTED_HEADS) or {}), action.kind: head_sha}
    return out


def acted_at_head(rearmed: dict, action: RecoveryAction, head_sha: str | None) -> bool:
    """THE per-head one-shot guard: PR Doctor already injected a fix of this
    action's kind at *head_sha* (``record_attempt`` wrote ``last_action_sha`` and
    consumed an attempt), so it will not act again until a push re-arms the
    target. Pure. *rearmed* is ``apply_rearm``'s output, never the raw row — the
    re-arm is what turns an external push into a fresh budget.

    Shared by the sweep (``_pr_doctor_step``, which skips the target) and the
    Backlog Runner admission gate (``_pr_doctor_pending_products``): a product was
    held "reserved" for a PR PR Doctor had ALREADY updated at that head
    (2026-09-30 audit, D6) because the gate re-derived "will fix" from
    ``plan_recovery`` alone and never asked this question.

    Keyed on (kind, head) through ``ACTED_HEADS`` as well (#2912/#2920): the
    re-armable ``attempts`` alone was not a dedupe — ``apply_rearm`` zeroes it on
    any failure-class change at the SAME head, and the recovery clear wipes
    ``last_action_sha`` when CI goes green without a push (a flaky-shard rerun),
    so a stale red or DIRTY snapshot at an already-handled head re-dispatched
    the identical prompt."""
    if not head_sha:
        return False
    kind = getattr(action, "kind", None)
    if kind and (rearmed.get(ACTED_HEADS) or {}).get(kind) == head_sha:
        return True
    if rearmed.get("last_action_sha") != head_sha:
        return False
    attempts = (rearmed.get("mechanical_attempts", 0) if isinstance(action, Mechanical)
                else rearmed.get("attempts", 0))
    return attempts > 0


def stale_dispatch_reason(kind: str, snapshot: PullRequestInfo,
                          live: PullRequestInfo | None) -> str | None:
    """Why a session fix decided on a cached *snapshot* must not dispatch, judged
    on a *live* read taken immediately before (#2912/#2920) — or None to go ahead.

    * ``"merged"`` / ``"closed"`` — the PR is terminal (#2920 merged green
      between the red snapshot and the dispatch).
    * ``"head_moved"`` — the head is no longer the one the decision was made
      at; the next sweep re-judges the new head on fresh data.
    * ``"resolved"`` — the live PR no longer classifies as *kind*: CI green or
      still pending with nothing proven red, or a conflict that is now
      mergeable (#2912). ``classify`` is the single judge, so the live read is
      held to the exact standard that produced the decision.

    ``live is None`` (the read failed) fails OPEN to the snapshot decision, so a
    GitHub hiccup degrades to the old behaviour — still bounded by the durable
    (kind, head) dedupe in ``acted_at_head``. Pure."""
    if live is None:
        return None
    if target_terminal(live):
        return live.state
    if snapshot.head_sha and live.head_sha and live.head_sha != snapshot.head_sha:
        return "head_moved"
    if kind in ("failing_ci", "merge_conflict") and classify(live) != kind:
        return "resolved"
    return None


def target_terminal(info: PullRequestInfo) -> bool:
    return info.state in ("merged", "closed")
