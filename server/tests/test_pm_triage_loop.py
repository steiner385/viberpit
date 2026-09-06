"""PmTriageLoop tests — auto-link pass, single-action dispatch, day cap, off-mode.

Every I/O seam is injected; the loop delegates the decision to the pure
``pm.engine`` and (for classify) reuses phase-2 ``classification`` via the
injected ``classify`` seam. Mirrors test_pm_sync_loop / test_sdlc_intake_loop.
"""
from __future__ import annotations

import pytest

from server.automations.pm_triage import PendingItemView, PmTriageLoop
from server.pm.engine import NoFitCluster, StaleFeature, TriageState


class Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


class _Sess:
    def __init__(self, sid="s1"):
        self.id = sid


def _item(number, kind="issue", links=()):
    return PendingItemView(
        repo="o/r", number=number, kind=kind, title=f"#{number}", body="",
        labels=(), linked_issue_numbers=tuple(links))


def _loop(ctx, *, mode="propose", config=None, pending=None, state=None,
          autolink_pred=None, clock=None, list_pending=None):
    """Wire a loop over recorder fakes. Returns (loop, rec)."""
    rec = {"classified": [], "map_diffs": [], "stale": [], "autolinked": []}
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
        return _Sess()

    async def propose_map_diff(suggestion, numbers, repo):
        rec["map_diffs"].append((suggestion, numbers, repo))
        return _Sess()

    async def mark_stale(slugs):
        rec["stale"].append(tuple(slugs))

    loop = PmTriageLoop(
        ctx, read_config=read_config,
        list_pending=list_pending or _default_list_pending,
        auto_link=auto_link, count_state=count_state, classify=classify,
        propose_map_diff=propose_map_diff, mark_stale=mark_stale,
        clock=clock or (lambda: 1000.0))
    return loop, rec


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
    assert rec == {"classified": [], "map_diffs": [], "stale": [], "autolinked": []}


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
    mechanical PRs costs zero sessions."""
    ctx = Ctx()
    pending = [_item(10, kind="pr", links=[3]), _item(12, kind="pr", links=[4])]
    st = TriageState(mode="propose", unclassified_count=2, pending_count=0,
                     no_fit_clusters=(), stale_features=(), day_spawns=0, day_cap=8)
    loop, rec = _loop(ctx, pending=pending, state=st,
                      autolink_pred=lambda i: True)
    await loop.tick()
    assert rec["autolinked"] == [10, 12]
    assert rec["classified"] == []


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
async def test_off_mode_zeroes_the_status_counts():
    ctx = Ctx()
    loop, _rec = _loop(ctx, mode="off", pending=[_item(1)])
    await loop.tick()
    assert ctx.pm_triage_counts == {"unclassified": 0, "pending": 0, "drift": 0}


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
