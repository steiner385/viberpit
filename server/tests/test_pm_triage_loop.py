"""PmTriageLoop tests — auto-link pass, single-action dispatch, day cap, off-mode.

Every I/O seam is injected; the loop delegates the decision to the pure
``pm.engine`` and (for classify) reuses phase-2 ``classification`` via the
injected ``classify`` seam. Mirrors test_pm_sync_loop / test_sdlc_intake_loop.
"""
from __future__ import annotations

import pytest

from server.automations.pm_triage import (
    MAX_AUTO_ACCEPTS_PER_TICK,
    AutoAcceptCandidateView,
    PendingItemView,
    PmTriageLoop,
)
from server.pm.engine import NoFitCluster, StaleFeature, TriageState


class Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


class _Sess:
    def __init__(self, sid="s1"):
        self.id = sid


def _item(number, kind="issue", links=(), *, title=None, author="tony", product="viberpit"):
    return PendingItemView(
        repo="o/r", number=number, kind=kind, title=title or f"#{number}", body="",
        labels=(), linked_issue_numbers=tuple(links), author=author, product_slug=product)


def _loop(ctx, *, mode="propose", config=None, pending=None, state=None,
          autolink_pred=None, clock=None, list_pending=None,
          policies=None, candidates=None, accept_fails=(), accept_none=(),
          candidate_list_fails=False, classify_spawns=True, ignore_fails=()):
    """Wire a loop over recorder fakes. Returns (loop, rec).

    The auto-accept seams are wired whenever *candidates* is given; ``policies=None``
    then makes the policy read RAISE, and ``candidate_list_fails=True`` makes the
    candidate read raise (the two failure cases the pass must survive).
    ``classify_spawns=False`` makes the classify seam answer falsy — the shape of
    a dedupe hit / refusal that spawned nothing (#2235)."""
    rec = {"classified": [], "map_diffs": [], "stale": [], "autolinked": [],
           "auto_accepted": [], "auto_dismissed": [], "rules": {}, "reasons": {},
           "ignored": []}
    cfg = dict(config or {"drift_threshold": 5, "batch_size": 50,
                          "max_spawns_per_day": 8, "stale_days": 14})

    async def read_config():
        return mode, cfg

    async def _default_list_pending():
        return list(pending or [])

    async def auto_link(item):
        linked = bool(autolink_pred(item)) if autolink_pred else False
        if linked:
            rec["autolinked"].append(item.number)
        return linked

    async def count_state():
        # Tests pass an explicit engine view; default = derive from `pending`.
        if state is not None:
            return state
        return TriageState(
            mode=mode, unclassified_count=len(pending or []), pending_count=0,
            no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)

    async def classify(items, count):
        rec["classified"].append([i.number for i in items[:count]])
        return _Sess() if classify_spawns else 0

    async def propose_map_diff(suggestion, numbers, repo):
        rec["map_diffs"].append((suggestion, numbers, repo))
        return _Sess()

    async def mark_stale(slugs):
        rec["stale"].append(tuple(slugs))

    async def read_policies():
        if policies is None:
            raise RuntimeError("policy read failed")
        return dict(policies)

    async def list_candidates():
        if candidate_list_fails:
            raise RuntimeError("candidate read failed")
        return list(candidates or [])

    async def auto_accept(proposal_id, rule):
        if proposal_id in accept_fails:
            raise RuntimeError("accept blew up")
        if proposal_id in accept_none:
            return None
        rec["auto_accepted"].append(proposal_id)
        rec["rules"][proposal_id] = rule
        return {"workItemId": f"w-{proposal_id}", "featureId": "f1",
                "confidence": "high", "rule": rule}

    async def auto_dismiss(proposal_id, reason):
        rec["auto_dismissed"].append(proposal_id)
        rec["reasons"][proposal_id] = reason
        return True

    async def mark_ignored(item):
        if item.number in ignore_fails:
            raise RuntimeError("ignore blew up")
        rec["ignored"].append(item.number)
        return True

    wired = candidates is not None
    loop = PmTriageLoop(
        ctx, read_config=read_config,
        list_pending=list_pending or _default_list_pending,
        auto_link=auto_link, count_state=count_state, classify=classify,
        propose_map_diff=propose_map_diff, mark_stale=mark_stale,
        clock=clock or (lambda: 1000.0),
        read_policies=read_policies if wired else None,
        list_auto_accept_candidates=list_candidates if wired else None,
        auto_accept=auto_accept if wired else None,
        auto_dismiss=auto_dismiss if wired else None,
        mark_ignored=mark_ignored if wired else None)
    return loop, rec


def _cand(pid, *, product="viberpit", level="high", feature="f1", state="live",
          undone=None, number=1, title=None, author="tony", item_state="open",
          prior=0):
    return AutoAcceptCandidateView(
        proposal_id=pid, work_item_id=f"w-{pid}", product_slug=product, repo="o/r",
        number=number, confidence_level=level, proposed_feature_id=feature,
        feature_state=state, auto_undone=undone, title=title, author=author,
        item_state=item_state, prior_human_accepts=prior)


def _policy(tier, **over):
    """A product policy at auto-accept *tier*. Bot-artifact dismissal is pinned OFF
    unless a test asks for it, so the accept tests are not shaped by its default."""
    from server.automations.product_policy import effective_policy
    return {**effective_policy(None), "triage_auto_accept": tier,
            "triage_dismiss_bot_artifacts": False, **over}


@pytest.mark.asyncio
async def test_off_mode_does_nothing():
    ctx = Ctx()
    loop, rec = _loop(ctx, mode="off", pending=[_item(1)])
    await loop.tick()
    assert rec["classified"] == [] and rec["map_diffs"] == []


@pytest.mark.asyncio
async def test_auto_link_pass_runs_before_classify_and_drops_linked_prs():
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3]), _item(11, kind="issue")]
    # PR #10 references issue #3 → mechanically linked, no session.
    st = TriageState(mode="propose", unclassified_count=1, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      autolink_pred=lambda i: i.kind == "pr" and bool(i.linked_issue_numbers))
    await loop.tick()
    assert rec["autolinked"] == [10]
    # only the unlinked issue #11 reaches the classifier
    assert rec["classified"] == [[11]]


@pytest.mark.asyncio
async def test_classify_batch_bounded_by_count():
    ctx = Ctx()
    pending = [_item(n) for n in range(1, 61)]
    st = TriageState(mode="propose", unclassified_count=60, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      config={"batch_size": 50, "drift_threshold": 5,
                              "max_spawns_per_day": 8, "stale_days": 14})
    await loop.tick()
    assert len(rec["classified"][0]) == 50  # engine said ClassifyBatch(50)


@pytest.mark.asyncio
async def test_day_cap_defers_classify():
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=50, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=8, day_cap=8)
    loop, rec = _loop(ctx, pending=[_item(1)], state=st)
    await loop.tick()
    assert rec["classified"] == []


@pytest.mark.asyncio
async def test_drift_cluster_opens_map_diff_pr():
    ctx = Ctx()
    cluster = NoFitCluster(suggestion="offline-mode",
                           item_numbers=(1, 2, 3, 4, 5), repo="o/r")
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(cluster,), stale_features=(),
                     day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec["map_diffs"] == [("offline-mode", (1, 2, 3, 4, 5), "o/r")]


@pytest.mark.asyncio
async def test_stale_nudge_sets_indicators():
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(),
                     stale_features=(StaleFeature("billing", "delivering", 30),),
                     day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec["stale"] == [("billing",)]


@pytest.mark.asyncio
async def test_idle_tick_records_no_work():
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec == {"classified": [], "map_diffs": [], "stale": [], "autolinked": [],
                   "auto_accepted": [], "auto_dismissed": [], "rules": {}, "reasons": {},
                   "ignored": []}


# --------------------------------------------------------------------------- #
# The day cap must be bounded by REAL spawn state, not by the count seam.      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_loop_own_spawn_count_caps_the_day_even_when_the_seam_reports_zero():
    """After ``max_spawns_per_day`` successful classify spawns inside the rolling
    window, the next tick must NOT spawn — even though ``count_state`` keeps
    reporting ``day_spawns=0``.

    The count seam cannot see the loop's in-memory spawn ledger, so if the loop
    forwarded the seam's ``day_spawns`` verbatim the engine's budget check would
    be permanently dead and nothing would bound cost. The loop substitutes its
    own count, so the engine's check is the one that binds."""
    ctx = Ctx()
    pending = [_item(n) for n in range(1, 21)]
    # state=None ⇒ the default seam view, which always answers day_spawns=0.
    loop, rec = _loop(ctx, pending=pending,
                      config={"batch_size": 1, "drift_threshold": 5,
                              "max_spawns_per_day": 3, "stale_days": 14})
    for _ in range(3):
        await loop.tick()
    assert len(rec["classified"]) == 3
    await loop.tick()
    assert len(rec["classified"]) == 3  # cap reached — no fourth spawn
    assert any(kw.get("kind") == "ceiling" for _, _, kw in ctx.events)


@pytest.mark.asyncio
async def test_a_classify_that_spawned_nothing_does_not_burn_the_daily_cap():
    """#2235: a tick that re-calls classify for a batch whose session an earlier
    tick already spawned (dedupe hit → the seam answers falsy) spawned nothing,
    so it must not consume a slot of the daily cap — otherwise a busy tick
    exhausts the day's budget on calls that dispatched no work."""
    ctx = Ctx()
    loop, rec = _loop(ctx, pending=[_item(1)], classify_spawns=False,
                      config={"batch_size": 1, "drift_threshold": 5,
                              "max_spawns_per_day": 1, "stale_days": 14})
    for _ in range(3):
        await loop.tick()
    # The cap is 1, yet every tick got to call classify: no slot was ever spent.
    assert len(rec["classified"]) == 3
    assert loop._spawns_today == []
    assert not any(kw.get("kind") == "classify" for _, _, kw in ctx.events)


@pytest.mark.asyncio
async def test_spawns_outside_the_rolling_window_are_pruned():
    """The cap is a rolling 24h window: a spawn older than the window no longer
    counts against today's budget."""
    ctx = Ctx()
    now = [1000.0]
    loop, rec = _loop(ctx, pending=[_item(1)], clock=lambda: now[0],
                      config={"batch_size": 1, "drift_threshold": 5,
                              "max_spawns_per_day": 1, "stale_days": 14})
    await loop.tick()
    await loop.tick()
    assert len(rec["classified"]) == 1  # capped at 1 within the window
    now[0] += 24 * 60 * 60 + 1
    await loop.tick()
    assert len(rec["classified"]) == 2  # window rolled over


@pytest.mark.asyncio
async def test_an_explicit_non_zero_day_spawn_count_from_the_seam_is_honoured():
    """A persisted spawn count (seam) wins over the loop's own ledger, so a
    restarted process cannot spend the day's budget twice."""
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=5, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=4, day_cap=8)
    loop, rec = _loop(ctx, pending=[_item(1)], state=st,
                      config={"batch_size": 50, "drift_threshold": 5,
                              "max_spawns_per_day": 4, "stale_days": 14})
    await loop.tick()
    assert rec["classified"] == []


@pytest.mark.asyncio
async def test_the_larger_of_seam_and_ledger_day_spawns_binds():
    """Seam and ledger are two partial views of the same budget, so the SAFE
    combination is ``max`` — never ``or``.

    Here the seam answers a lagging ``day_spawns=1`` (one persisted session)
    while this process's ledger fills to 3. ``or`` picks the seam whenever it is
    non-zero, so the engine would keep seeing ``1 < 3`` and the loop would
    overrun ``max_spawns_per_day`` forever."""
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=20, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=1, day_cap=8)
    loop, rec = _loop(ctx, pending=[_item(n) for n in range(1, 21)], state=st,
                      config={"batch_size": 1, "drift_threshold": 5,
                              "max_spawns_per_day": 3, "stale_days": 14})
    for _ in range(4):
        await loop.tick()
    assert len(rec["classified"]) == 3  # ledger (3) binds, not the seam's 1


@pytest.mark.asyncio
async def test_a_zero_day_cap_from_the_seam_stops_all_spawns():
    """``day_cap=0`` is the STRICTEST budget, not "unset": treating it as falsy
    would turn the tightest value into the laxest (the config cap)."""
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=5, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=0)
    loop, rec = _loop(ctx, pending=[_item(1)], state=st,
                      config={"batch_size": 50, "drift_threshold": 5,
                              "max_spawns_per_day": 8, "stale_days": 14})
    await loop.tick()
    assert rec["classified"] == []


@pytest.mark.asyncio
async def test_config_cap_binds_below_the_seam_day_cap():
    """``max_spawns_per_day`` is THE cap: a seam view reporting a laxer
    ``day_cap`` must not let the loop overrun the operator's configured value."""
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=5, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=2, day_cap=99)
    loop, rec = _loop(ctx, pending=[_item(1)], state=st,
                      config={"batch_size": 50, "drift_threshold": 5,
                              "max_spawns_per_day": 2, "stale_days": 14})
    await loop.tick()
    assert rec["classified"] == []


# --------------------------------------------------------------------------- #
# Invariants the ENGINE deliberately does not carry (design §6)                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_only_items_from_list_pending_are_ever_classified():
    """``list_pending`` is the sole source of classifiable items: it yields only
    open + unclassified items with no pending proposal, so a REJECTED item
    (``needs_human``) is absent from it and can never be re-classified — even
    though the count seam still reports a larger backlog. Rejection is signal;
    there is no blind retry (design §6)."""
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=5, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[_item(11), _item(12)], state=st)
    await loop.tick()
    assert rec["classified"] == [[11, 12]]


@pytest.mark.asyncio
async def test_no_classify_session_when_every_pending_item_auto_linked():
    """The auto-link pass runs BEFORE the classifier, so a queue made entirely of
    mechanical PRs costs zero sessions — and the tick is NOT consumed: the engine
    is asked once per tick, so an emptied classify batch must fall through to the
    rest of the ladder rather than silently dropping the tick."""
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3]), _item(12, kind="pr", links=[4])]
    st = TriageState(mode="propose", unclassified_count=2, pending_count=0,
                     no_fit_clusters=(),
                     stale_features=(StaleFeature("billing", "delivering", 30),),
                     day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      autolink_pred=lambda i: True)
    await loop.tick()
    assert rec["autolinked"] == [10, 12]
    assert rec["classified"] == []
    assert rec["stale"] == [("billing",)]


@pytest.mark.asyncio
async def test_drift_still_proposed_when_every_pending_item_auto_linked():
    """``unclassified_count`` handed to the engine must agree with what the loop
    can actually classify.

    ``count_state``'s query can keep counting a row the auto-link pass just
    linked (same tick, write not yet visible). Forwarding that count makes the
    engine pick ``ClassifyBatch`` off the top of the ladder, the loop finds an
    empty batch, and the tick does NOTHING — so a qualifying drift cluster is
    never proposed, tick after tick, with no feed event explaining why."""
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3]), _item(12, kind="pr", links=[4])]
    cluster = NoFitCluster(suggestion="offline-mode",
                           item_numbers=(1, 2, 3, 4, 5), repo="o/r")
    st = TriageState(mode="propose", unclassified_count=2, pending_count=0,
                     no_fit_clusters=(cluster,), stale_features=(),
                     day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st, autolink_pred=lambda i: True)
    await loop.tick()
    assert rec["classified"] == []
    assert rec["map_diffs"] == [("offline-mode", (1, 2, 3, 4, 5), "o/r")]


# --------------------------------------------------------------------------- #
# Feed hygiene: failures are visible, the ceiling does not evict the feed       #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_the_ceiling_event_is_emitted_once_per_capped_run_not_per_tick():
    """``AutomationContext`` keeps 500 events and the UI reads 100, so a ceiling
    re-emitted every 60 s tick evicts every record of what this propose-only
    automation actually did. Emit on the TRANSITION into the capped state."""
    ctx = Ctx()
    st = TriageState(mode="propose", unclassified_count=5, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=8, day_cap=8)
    loop, _rec = _loop(ctx, pending=[_item(1)], state=st)
    await loop.tick()
    await loop.tick()
    assert sum(1 for _, _, kw in ctx.events if kw.get("kind") == "ceiling") == 1


@pytest.mark.asyncio
async def test_a_failing_auto_link_warns_and_the_tick_continues():
    """The auto-link pass is per-item and independent: one GitHub 404 on item 2
    must not abandon classify/drift/stale for the whole tick, and it must reach
    the feed as ``warn`` (the notification route fires on error/warn only)."""
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3]), _item(11, kind="pr", links=[4]),
               _item(12, kind="issue")]
    st = TriageState(mode="propose", unclassified_count=3, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)

    def pred(item):
        if item.number == 11:
            raise RuntimeError("gh 404")
        return item.number == 10

    loop, rec = _loop(ctx, pending=pending, state=st, autolink_pred=pred)
    await loop.tick()
    warns = [kw for lvl, _, kw in ctx.events if lvl == "warn"]
    assert any(kw.get("target") == "o/r#11" for kw in warns)
    assert rec["autolinked"] == [10]
    # The failed item stays in the queue and the tick still classifies.
    assert rec["classified"] == [[11, 12]]


@pytest.mark.asyncio
async def test_a_tick_level_failure_reaches_the_feed_as_warn():
    ctx = Ctx()

    async def boom():
        raise RuntimeError("nope")

    loop, _rec = _loop(ctx, pending=[_item(1)], list_pending=boom)
    await loop.tick()
    assert any(lvl == "warn" for lvl, _, _ in ctx.events)


@pytest.mark.asyncio
async def test_status_drift_count_excludes_latched_suggestions():
    """The status card and the engine must agree on which clusters qualify:
    ``no_fit`` clusters never shrink, so a re-implemented count that ignores the
    latch reports "N drift" (and ``level="active"``) forever after every cluster
    has been proposed."""
    ctx = Ctx()
    cluster = NoFitCluster(suggestion="offline-mode",
                           item_numbers=(1, 2, 3, 4, 5), repo="o/r")
    st = TriageState(mode="propose", unclassified_count=0, pending_count=0,
                     no_fit_clusters=(cluster,), stale_features=(),
                     day_spawns=0, day_cap=8,
                     open_map_diff_suggestions=("offline-mode",))
    loop, rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert rec["map_diffs"] == []  # latched — the engine is Idle
    assert ctx.pm_triage_counts == {"unclassified": 0, "pending": 0, "drift": 0}


@pytest.mark.asyncio
async def test_a_pr_the_auto_link_seam_declines_still_reaches_the_classifier():
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3])]
    st = TriageState(mode="propose", unclassified_count=1, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      autolink_pred=lambda i: False)
    await loop.tick()
    assert rec["autolinked"] == []
    assert rec["classified"] == [[10]]


@pytest.mark.asyncio
async def test_state_mode_off_needs_human_and_does_no_work():
    """The stored mode (read by ``count_state``) is what the engine judges; a
    disagreement with the config read is reported, never worked around."""
    ctx = Ctx()
    st = TriageState(mode="off", unclassified_count=5, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=[_item(1)], state=st)
    await loop.tick()
    assert rec["classified"] == [] and rec["map_diffs"] == [] and rec["stale"] == []
    assert any(kw.get("kind") == "needs_human" for _, _, kw in ctx.events)


@pytest.mark.asyncio
async def test_status_counts_are_stashed_on_the_context():
    ctx = Ctx()
    cluster = NoFitCluster(suggestion="offline-mode",
                           item_numbers=(1, 2, 3, 4, 5), repo="o/r")
    st = TriageState(mode="propose", unclassified_count=0, pending_count=3,
                     no_fit_clusters=(cluster,), stale_features=(),
                     day_spawns=0, day_cap=8)
    loop, _rec = _loop(ctx, pending=[], state=st)
    await loop.tick()
    assert ctx.pm_triage_counts == {"unclassified": 0, "pending": 3, "drift": 1}


@pytest.mark.asyncio
async def test_off_mode_does_not_fabricate_observation():
    ctx = Ctx()
    loop, _rec = _loop(ctx, mode="off", pending=[_item(1)])
    await loop.tick()
    assert not hasattr(ctx, "pm_triage_counts")
    assert not hasattr(ctx, "observed_at")


@pytest.mark.asyncio
async def test_tick_swallows_a_seam_failure():
    """Best-effort like every other loop: a failing seam never crashes the
    supervising driver."""
    ctx = Ctx()

    async def boom():
        raise RuntimeError("nope")

    loop, _rec = _loop(ctx, pending=[_item(1)], list_pending=boom)
    await loop.tick()  # must not raise


# --------------------------------------------------------------------------- #
# The Automation contract surface                                              #
# --------------------------------------------------------------------------- #

def test_pm_triage_is_registered_propose_only():
    """Modes are ("off", "propose") — there is NO ``auto``: a human accept is the
    gate (design §6, constitution)."""
    from server.automations.registry import registry

    a = registry.get("pm-triage")
    assert a.modes == ("off", "propose")
    assert "auto" not in a.modes


def test_pm_triage_config_schema_defaults():
    from server.automations.pm_triage import PmTriage

    defaults = {f.key: f.default for f in PmTriage.config_schema}
    assert defaults == {"batch_size": 50, "max_spawns_per_day": 8,
                        "drift_threshold": 5, "stale_days": 14}


def test_pm_triage_status_idle_without_counts():
    from server.automations.pm_triage import PmTriage

    assert PmTriage().status(None).level == "idle"


@pytest.mark.asyncio
async def test_labelled_issues_are_offered_to_auto_link_and_leave_the_queue():
    ctx = Ctx()
    flaky = PendingItemView(repo="o/r", number=7, kind="issue", title="[flaky] x", body="",
                            labels=("flaky",), linked_issue_numbers=())
    plain = _item(8)
    loop, rec = _loop(ctx, pending=[flaky, plain],
                      autolink_pred=lambda item: "flaky" in item.labels)
    await loop.tick()
    assert rec["autolinked"] == [7]
    assert rec["classified"] == [[8]]          # only the unrouted item reaches the classifier


# --------------------------------------------------------------------------- #
# The auto-accept pass (spec §8) — policy-gated, capped, fail-safe             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_accepts_qualifying_proposals_for_an_opted_in_product():
    """Tier ``high`` accepts the high-confidence proposal and leaves the medium
    one for a human — the gate is the shared ``auto_accept_eligible``."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")},
                      candidates=[_cand("p1", level="high"),
                                  _cand("p2", level="medium", number=2)])
    await loop.tick()
    assert rec["auto_accepted"] == ["p1"]
    kinds = [e[2].get("kind") for e in ctx.events]
    assert kinds.count("auto_accepted") == 1


@pytest.mark.asyncio
async def test_product_with_auto_accept_off_is_untouched():
    """``off`` is the default: opting in is an explicit product decision."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("off")},
                      candidates=[_cand("p1", level="high")])
    await loop.tick()
    assert rec["auto_accepted"] == []
    assert [e[2].get("kind") for e in ctx.events].count("auto_accepted") == 0


@pytest.mark.asyncio
async def test_a_product_with_no_policy_is_off():
    """No policy row at all reads as ``off`` — never as "anything goes"."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={}, candidates=[_cand("p1", level="high")])
    await loop.tick()
    assert rec["auto_accepted"] == []


@pytest.mark.asyncio
async def test_no_fit_and_proposed_capability_and_undone_are_skipped():
    """Three hard exclusions, even at a tier the confidence clears: a no-fit
    (creating a capability is a human call), a target capability still
    ``proposed`` (its doc is unmerged), and a proposal a human already undid."""
    ctx = Ctx()
    loop, rec = _loop(
        ctx, policies={"viberpit": _policy("medium")},
        candidates=[_cand("no-fit", feature=None, state=None),
                    _cand("proposed-cap", state="proposed", number=2),
                    _cand("undone", undone=True, number=3)])
    await loop.tick()
    assert rec["auto_accepted"] == []


@pytest.mark.asyncio
async def test_per_product_cap_per_tick():
    """The cap is PER PRODUCT per tick: viberpit stops at
    ``MAX_AUTO_ACCEPTS_PER_TICK`` while kindash's single proposal still lands,
    and the cap is announced once for the product that hit it."""
    ctx = Ctx()
    viberpit = [_cand(f"v{n}", number=n) for n in range(30)]
    kindash = [_cand("k1", product="kindash", number=99)]
    loop, rec = _loop(ctx, policies={"viberpit": _policy("medium"),
                                     "kindash": _policy("medium")},
                      candidates=viberpit + kindash)
    await loop.tick()
    assert rec["auto_accepted"] == [f"v{n}" for n in range(MAX_AUTO_ACCEPTS_PER_TICK)] + ["k1"]
    capped = [e for e in ctx.events if e[2].get("kind") == "auto_accept_capped"]
    assert len(capped) == 1 and capped[0][2].get("target") == "viberpit"


@pytest.mark.asyncio
async def test_a_failed_policy_read_skips_the_pass_but_the_tick_completes():
    """Fail-safe: a policy read that raises accepts NOTHING this tick (``{}``
    would read as "no product opted in" — same outcome, no warning), and the
    rest of the tick still runs."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies=None, candidates=[_cand("p1")])
    await loop.tick()
    assert rec["auto_accepted"] == []
    assert ctx.pm_triage_counts == {"unclassified": 0, "pending": 0, "drift": 0}
    warns = [e for e in ctx.events if e[2].get("kind") == "auto_accept_skipped"]
    assert len(warns) == 1 and warns[0][0] == "warn"


@pytest.mark.asyncio
async def test_a_failed_candidate_read_skips_the_pass_but_the_tick_completes():
    """R2-7, the twin of the policy-read guard: the candidate listing is the
    pass's OTHER DB read, and an unguarded failure there took the whole tick
    down with it — including ``_stash``, which is what the status card reads, so
    one transient DB blip showed the operator "status unknown" instead of the
    counts the tick had already computed."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")},
                      candidates=[_cand("p1")], candidate_list_fails=True)
    await loop.tick()
    assert rec["auto_accepted"] == []
    warns = [e for e in ctx.events if e[2].get("kind") == "auto_accept_skipped"]
    assert len(warns) == 1 and warns[0][0] == "warn"
    assert "candidate read failed" in warns[0][1]
    # the rest of the tick still ran: the counts the status card reads are set
    assert ctx.pm_triage_counts == {"unclassified": 0, "pending": 0, "drift": 0}


@pytest.mark.asyncio
async def test_one_failing_accept_does_not_abort_the_others():
    """Per-proposal guard: one accept blowing up is logged and skipped, never
    the pass (the next proposal is independent work)."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")},
                      candidates=[_cand("p1", number=1), _cand("p2", number=2)],
                      accept_fails=("p1",))
    await loop.tick()
    assert rec["auto_accepted"] == ["p2"]
    failed = [e for e in ctx.events if e[2].get("kind") == "auto_accept_failed"]
    assert len(failed) == 1
    assert failed[0][0] == "warn" and failed[0][2].get("target") == "o/r#1"


@pytest.mark.asyncio
async def test_a_proposal_a_human_resolved_first_is_not_reported_as_accepted():
    """The seam answers ``None`` when the proposal stopped being pending between
    the list and the accept — a race a human won, not an error."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")},
                      candidates=[_cand("p1")], accept_none=("p1",))
    await loop.tick()
    assert rec["auto_accepted"] == []
    kinds = [e[2].get("kind") for e in ctx.events]
    assert "auto_accepted" not in kinds and "auto_accept_failed" not in kinds


@pytest.mark.asyncio
async def test_dispatch_off_flag_accepts_low_confidence_only_while_dispatch_is_off():
    """#2683: the flag waives the confidence bar for a product whose Dispatch is off."""
    from server.automations.product_policy import effective_policy
    flagged = {**effective_policy(None), "triage_auto_accept_dispatch_off": True}
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": flagged},
                      candidates=[_cand("p1", level="low"),
                                  _cand("p2", level="low", feature=None, number=2)])
    await loop.tick()
    assert rec["auto_accepted"] == ["p1"]          # a no-fit still waits for a human

    ctx = Ctx()
    live = {**flagged, "dispatch": "auto"}
    loop, rec = _loop(ctx, policies={"viberpit": live}, candidates=[_cand("p1", level="low")])
    await loop.tick()
    assert rec["auto_accepted"] == []


@pytest.mark.asyncio
async def test_bot_artifacts_are_dismissed_unless_the_product_opts_out():
    """Dismissal is ON by default (bot output never belongs in triage; it is
    undoable and parks nothing) and a product may still switch it off."""
    from server.automations.product_policy import effective_policy
    cands = [_cand("d1", title="UI Maintenance Cost & Architecture ROI Digest", level="low"),
             _cand("d2", title="docs(fix-patterns): regenerate index", number=2),
             _cand("d3", title="Bump lodash", author="dependabot[bot]", level="low", number=4),
             _cand("real", title="Fix the login bug", level="low", number=3)]
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": effective_policy(None)}, candidates=cands)
    await loop.tick()
    assert rec["auto_dismissed"] == ["d1", "d2", "d3"]
    assert set(rec["reasons"].values()) == {"bot_artifact"}
    assert rec["auto_accepted"] == []

    ctx = Ctx()
    off = {**effective_policy(None), "triage_dismiss_bot_artifacts": False}
    loop, rec = _loop(ctx, policies={"viberpit": off}, candidates=cands)
    await loop.tick()
    assert rec["auto_dismissed"] == []


@pytest.mark.asyncio
async def test_a_products_own_bot_artifact_patterns_are_dismissed():
    pats = {"triage_bot_artifact_patterns": [r"^chore: audit codebase for\b"]}
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("off", triage_dismiss_bot_artifacts=True,
                                                          **pats)},
                      candidates=[_cand("a1", title="chore: audit codebase for X failure mode"),
                                  _cand("keep", title="chore: tidy", number=2)])
    await loop.tick()
    assert rec["auto_dismissed"] == ["a1"]


# --------------------------------------------------------------------------- #
# #2683 follow-up: the repeat rule                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repeat_rule_accepts_onto_a_proposed_capability_humans_keep_accepting():
    """The technical-debt-audits case: low confidence, capability still proposed,
    but humans accepted N onto it already — the loop takes it, names the rule."""
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("medium")},
                      candidates=[_cand("p1", level="low", state="proposed", prior=3),
                                  _cand("p2", level="low", state="proposed", prior=2,
                                        number=2)])
    await loop.tick()
    assert rec["auto_accepted"] == ["p1"]
    assert rec["rules"] == {"p1": "repeat"}
    ev = next(e for e in ctx.events if e[2].get("kind") == "auto_accepted")
    assert "repeat" in ev[1] and "3" in ev[1]
    assert ev[2]["detail"]["rule"] == "repeat"


@pytest.mark.asyncio
async def test_repeat_rule_never_fires_for_a_product_that_never_opted_in():
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("off")},
                      candidates=[_cand("p1", level="low", prior=50)])
    await loop.tick()
    assert rec["auto_accepted"] == []


@pytest.mark.asyncio
async def test_repeat_rule_respects_the_per_tick_cap():
    ctx = Ctx()
    cands = [_cand(f"r{n}", level="low", state="proposed", prior=9, number=n)
             for n in range(MAX_AUTO_ACCEPTS_PER_TICK + 5)]
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")}, candidates=cands)
    await loop.tick()
    assert len(rec["auto_accepted"]) == MAX_AUTO_ACCEPTS_PER_TICK
    assert [e for e in ctx.events if e[2].get("kind") == "auto_accept_capped"]


@pytest.mark.asyncio
async def test_confidence_accepts_are_labelled_with_their_rule():
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": _policy("high")},
                      candidates=[_cand("p1", level="high")])
    await loop.tick()
    assert rec["rules"] == {"p1": "confidence"}


# --------------------------------------------------------------------------- #
# #2683 follow-up: stale proposals (item closed / merged)                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_proposals_for_closed_or_merged_items_are_dismissed_always():
    """Always on — no policy key, no opt-in, not even an owning product: a
    closed item has no decision left to make."""
    from server.automations.product_policy import effective_policy
    ctx = Ctx()
    loop, rec = _loop(ctx, policies={"viberpit": {**effective_policy(None),
                                                  "triage_dismiss_bot_artifacts": False}},
                      candidates=[_cand("c1", item_state="closed"),
                                  _cand("m1", item_state="merged", number=2, product=None),
                                  _cand("open", level="low", number=3),
                                  _cand("undone", item_state="closed", undone=True, number=4)])
    await loop.tick()
    assert rec["auto_dismissed"] == ["c1", "m1"]
    assert rec["reasons"] == {"c1": "item_closed", "m1": "item_closed"}
    assert rec["auto_accepted"] == []
    kinds = [e[2].get("kind") for e in ctx.events]
    assert kinds.count("auto_dismissed") == 2


# --------------------------------------------------------------------------- #
# #2683 follow-up: bot artifacts never reach a classify session                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bot_artifact_items_are_ignored_before_classification():
    pending = [_item(1, title="docs(fix-patterns): regenerate index"),
               _item(2, title="Fix the login bug", author="github-actions"),
               _item(3, title="Fix the login bug"),
               _item(4, title="docs(fix-patterns): regenerate index", product=None)]
    ctx = Ctx()
    loop, rec = _loop(ctx, pending=pending, candidates=[],
                      policies={"viberpit": _policy("off", triage_dismiss_bot_artifacts=True)})
    await loop.tick()
    assert rec["ignored"] == [1, 2]
    assert rec["classified"] == [[3, 4]]          # an unowned item is not judged here
    kinds = [e[2].get("kind") for e in ctx.events]
    assert kinds.count("bot_artifact_ignored") == 2


@pytest.mark.asyncio
async def test_bot_artifact_items_are_classified_when_dismissal_is_off():
    pending = [_item(1, title="docs(fix-patterns): regenerate index")]
    ctx = Ctx()
    loop, rec = _loop(ctx, pending=pending, candidates=[],
                      policies={"viberpit": _policy("off")})
    await loop.tick()
    assert rec["ignored"] == []
    assert rec["classified"] == [[1]]


@pytest.mark.asyncio
async def test_a_failing_ignore_warns_and_still_keeps_the_item_out_of_classify():
    pending = [_item(1, title="docs(fix-patterns): regenerate index"), _item(2)]
    ctx = Ctx()
    loop, rec = _loop(ctx, pending=pending, candidates=[], ignore_fails=(1,),
                      policies={"viberpit": _policy("off", triage_dismiss_bot_artifacts=True)})
    await loop.tick()
    assert rec["classified"] == [[2]]
    assert [e for e in ctx.events if e[2].get("kind") == "bot_artifact_ignore_failed"]
