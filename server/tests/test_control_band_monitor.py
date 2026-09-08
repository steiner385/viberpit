"""ControlBandMonitorLoop tests — every seam faked, no DB and no network (#804).

Same shape as the BacklogRunnerLoop / SdlcIntakeLoop tests: the loop's whole job
is to compose the pure detector with the pure bands policy and drive at most one
response per breaching metric, so the fakes here record what it *decided*.
"""
from __future__ import annotations

import textwrap

import pytest

from server.automations.control_band_monitor import (
    ControlBandMonitor,
    ControlBandMonitorLoop,
    FiledItem,
    incident_slug,
    next_disposition,
)
from server.automations.engine_policy import EnginePolicy
from server.sdlc.bands import load_bands_text
from server.sdlc.control_bands import EVAL_POINTS, Sample, evaluate

_POLICY = textwrap.dedent("""
    version: 1
    tiers:
      log: {sigma: 1, response: log}
      diagnose: {sigma: 2, response: diagnose, tools: [Read, Grep]}
      propose: {sigma: 3, response: propose, tools: [Read, Write, Bash]}
    metrics:
      - id: cycle-time
        source: pr_cycle_time_hours
        repo: steiner385/ccswitch
        unit: hours
        min_baseline: 10
""")


def _series(values: list[float]) -> list[Sample]:
    return [Sample(at=f"2026-01-{i + 1:02d}", value=v) for i, v in enumerate(values)]


def _stable(n: int = 30) -> list[float]:
    return [10.0 + (0.5 if i % 2 else -0.5) for i in range(n)]


#: Newest point at +80σ — fires we1 (3σ → propose).
_THREE_SIGMA = _stable(30) + [10.0] * (EVAL_POINTS - 1) + [50.0]
#: Newest point at +2.8σ with a second in the last three — fires we2 (2σ → diagnose).
_TWO_SIGMA = _stable(30) + [10.0] * 5 + [11.4, 10.0, 11.4]
#: All eight above the mean but inside 1σ — fires we4 (1σ → log).
_ONE_SIGMA = _stable(30) + [10.2] * EVAL_POINTS


class FakeCtx:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, level, summary, *, kind=None, target=None,
                   session_id=None, detail=None):
        self.events.append({"level": level, "summary": summary, "kind": kind,
                            "target": target, "session_id": session_id,
                            "detail": detail or {}})

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


class FakeSession:
    def __init__(self, sid: str = "sess-1") -> None:
        self.id = sid


def build_loop(
    *,
    values: list[float],
    mode: str = "propose",
    config: dict | None = None,
    policy: str = _POLICY,
    existing_status: str = "open",
    created: bool = True,
    spawn_returns: object = "session",
    queued_count: int = 0,
    series_raises: bool = False,
    bands_raises: bool = False,
    released: int = 0,
):
    ctx = FakeCtx()
    calls: dict = {"filed": [], "dispositions": [], "spawns": [], "series": [],
                   "released": 0}

    async def read_config():
        return mode, {"enabled_repos": [], "max_diagnoses_per_day": 3,
                      "max_open_incidents": 3, **(config or {})}

    async def read_bands():
        if bands_raises:
            raise ValueError("boom")
        return load_bands_text(policy)

    async def read_series(band):
        calls["series"].append(band.id)
        if series_raises:
            raise RuntimeError("db down")
        return _series(values)

    async def file_item(band, breach, tier, slug):
        calls["filed"].append({"band": band.id, "key": breach.key(), "tier": tier.name,
                               "slug": slug, "sigma": breach.sigma})
        return FiledItem(id="item-1", key=breach.key(), status=existing_status,
                         created=created)

    async def set_disposition(item_id, status, session_id):
        calls["dispositions"].append((item_id, status, session_id))

    async def spawn_diagnosis(item_id, prompt, band, tier):
        calls["spawns"].append({"itemId": item_id, "prompt": prompt,
                                "band": band.id, "tier": tier.name})
        return FakeSession() if spawn_returns == "session" else None

    async def count_queued():
        return queued_count

    async def release_due():
        calls["released"] += 1
        return released

    loop = ControlBandMonitorLoop(
        ctx,
        read_config=read_config, read_bands=read_bands, read_series=read_series,
        file_item=file_item, set_disposition=set_disposition,
        spawn_diagnosis=spawn_diagnosis, count_queued=count_queued,
        release_due=release_due, clock=lambda: 1000.0,
    )
    return loop, ctx, calls


# --------------------------------------------------------------------------- #
# Pure helpers                                                                  #
# --------------------------------------------------------------------------- #

def test_incident_slug_is_stable_for_the_same_breach():
    breach = evaluate("pr_cycle_time_hours", _series(_THREE_SIGMA), min_baseline=10)
    assert breach is not None
    assert incident_slug(breach) == incident_slug(breach)
    assert incident_slug(breach) == "incident-pr-cycle-time-hours-we1-high-2026-01-38"


@pytest.mark.parametrize("response, expected", [
    ("log", None), ("diagnose", "diagnosing"), ("propose", "queued"),
])
def test_next_disposition_from_a_fresh_item(response, expected):
    assert next_disposition("open", response) == expected
    assert next_disposition(None, response) == expected


@pytest.mark.parametrize("current", ["dismissed", "intake", "diagnosing", "queued",
                                     "scheduled"])
def test_next_disposition_never_re_drives_a_dispositioned_item(current):
    """A breach persists for days; without this a dismissed item would reopen and
    a diagnosis session would respawn on every tick."""
    for response in ("log", "diagnose", "propose"):
        assert next_disposition(current, response) is None


# --------------------------------------------------------------------------- #
# Tick: off / no breach / policy failure                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_off_mode_reads_nothing_at_all():
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, mode="off")
    await loop.tick()
    assert calls["series"] == [] and calls["filed"] == []
    assert calls["released"] == 0
    assert ctx.control_band_counts == {"watched": 0, "breaching": 0}


@pytest.mark.asyncio
async def test_scheduled_incidents_are_released_before_detection_runs():
    """Otherwise "snooze until tomorrow" is a black hole: the loop never
    re-drives a dispositioned item."""
    loop, ctx, calls = build_loop(values=_stable(40), released=2)
    await loop.tick()
    assert calls["released"] == 1
    assert "released" in ctx.kinds()


@pytest.mark.asyncio
async def test_an_in_control_series_files_nothing():
    loop, ctx, calls = build_loop(values=_stable(40))
    await loop.tick()
    assert calls["filed"] == []
    assert ctx.control_band_counts == {"watched": 1, "breaching": 0}


@pytest.mark.asyncio
async def test_a_broken_bands_policy_is_loud_and_watches_nothing():
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, bands_raises=True)
    await loop.tick()
    assert "bands_invalid" in ctx.kinds()
    assert ctx.events[0]["level"] == "error"
    assert calls["series"] == []


@pytest.mark.asyncio
async def test_one_unreadable_series_warns_without_stopping_the_tick():
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, series_raises=True)
    await loop.tick()
    assert "series_error" in ctx.kinds()
    assert calls["filed"] == []
    assert ctx.control_band_counts == {"watched": 1, "breaching": 0}


@pytest.mark.asyncio
async def test_a_disabled_metric_is_not_watched():
    policy = _POLICY + "    enabled: false\n"
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, policy=policy)
    await loop.tick()
    assert calls["series"] == []
    assert ctx.control_band_counts == {"watched": 0, "breaching": 0}


@pytest.mark.asyncio
async def test_enabled_repos_scopes_the_policy_to_configured_repositories():
    loop, _ctx, calls = build_loop(
        values=_THREE_SIGMA, config={"enabled_repos": ["other/repo"]})
    await loop.tick()
    assert calls["series"] == []


# --------------------------------------------------------------------------- #
# Tick: the three response tiers                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_one_sigma_files_the_incident_and_does_nothing_else():
    loop, ctx, calls = build_loop(values=_ONE_SIGMA)
    await loop.tick()
    assert [f["tier"] for f in calls["filed"]] == ["log"]
    assert calls["dispositions"] == [] and calls["spawns"] == []
    assert "breach" in ctx.kinds()


@pytest.mark.asyncio
async def test_two_sigma_spawns_one_read_only_diagnosis_and_links_it():
    loop, ctx, calls = build_loop(values=_TWO_SIGMA)
    await loop.tick()
    assert [f["tier"] for f in calls["filed"]] == ["diagnose"]
    assert calls["dispositions"] == [("item-1", "diagnosing", "sess-1")]
    (spawn,) = calls["spawns"]
    assert spawn["itemId"] == "item-1"
    # The tier's tool allowlist and the read-only contract reach the session.
    assert "- Read" in spawn["prompt"] and "- Grep" in spawn["prompt"]
    assert "READ-ONLY" in spawn["prompt"]
    assert "Do NOT commit, push, open a PR" in spawn["prompt"]
    # The evidence carries the arithmetic, not just the verdict.
    assert "Rolling baseline" in spawn["prompt"]
    assert "diagnosing" in ctx.kinds()


@pytest.mark.asyncio
async def test_three_sigma_queues_the_incident_for_intake():
    loop, ctx, calls = build_loop(values=_THREE_SIGMA)
    await loop.tick()
    assert [f["tier"] for f in calls["filed"]] == ["propose"]
    assert calls["dispositions"] == [("item-1", "queued", None)]
    # It must NOT write intent.md itself — SdlcIntake owns that path.
    assert calls["spawns"] == []
    assert "queued" in ctx.kinds()
    queued = [e for e in ctx.events if e["kind"] == "queued"][0]
    assert queued["detail"]["slug"] == calls["filed"][0]["slug"]


@pytest.mark.asyncio
async def test_monitor_mode_files_but_never_acts():
    """The reversible way to switch this on: fill the queue, take no action."""
    for values in (_ONE_SIGMA, _TWO_SIGMA, _THREE_SIGMA):
        loop, _ctx, calls = build_loop(values=values, mode="monitor")
        await loop.tick()
        assert len(calls["filed"]) == 1
        assert calls["dispositions"] == [] and calls["spawns"] == []


@pytest.mark.asyncio
async def test_a_breach_above_every_declared_tier_still_gets_the_highest_one():
    policy = textwrap.dedent("""
        version: 1
        tiers:
          log: {sigma: 1, response: log}
          diagnose: {sigma: 2, response: diagnose, tools: [Read, Grep]}
        metrics:
          - id: cycle-time
            source: pr_cycle_time_hours
            repo: steiner385/ccswitch
            min_baseline: 10
    """)
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, policy=policy)
    await loop.tick()
    assert [f["tier"] for f in calls["filed"]] == ["diagnose"]
    assert "no_tier" not in ctx.kinds()


@pytest.mark.asyncio
async def test_a_breach_below_every_declared_tier_warns_instead_of_guessing():
    policy = textwrap.dedent("""
        version: 1
        tiers:
          propose: {sigma: 3, response: propose, tools: [Read]}
        metrics:
          - id: cycle-time
            source: pr_cycle_time_hours
            repo: steiner385/ccswitch
            min_baseline: 10
    """)
    loop, ctx, calls = build_loop(values=_ONE_SIGMA, policy=policy)
    await loop.tick()
    assert "no_tier" in ctx.kinds()
    assert calls["filed"] == []


# --------------------------------------------------------------------------- #
# Idempotence, ceilings, and spawn refusal                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_re_observed_breach_refreshes_without_re_announcing_it():
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, created=False,
                                  existing_status="queued")
    await loop.tick()
    assert len(calls["filed"]) == 1          # measurements still refreshed
    assert "breach" not in ctx.kinds()       # but not announced a second time
    assert calls["dispositions"] == []       # and not re-queued


@pytest.mark.asyncio
async def test_a_dismissed_incident_is_never_reopened_by_a_later_tick():
    loop, _ctx, calls = build_loop(values=_THREE_SIGMA, created=False,
                                   existing_status="dismissed")
    await loop.tick()
    assert calls["dispositions"] == [] and calls["spawns"] == []


@pytest.mark.asyncio
async def test_the_daily_diagnosis_cap_defers_instead_of_spawning():
    loop, ctx, calls = build_loop(values=_TWO_SIGMA,
                                  config={"max_diagnoses_per_day": 0})
    await loop.tick()
    assert calls["spawns"] == [] and calls["dispositions"] == []
    assert "ceiling" in ctx.kinds()


@pytest.mark.asyncio
async def test_the_queued_incident_ceiling_defers_instead_of_queueing():
    loop, ctx, calls = build_loop(values=_THREE_SIGMA, queued_count=3,
                                  config={"max_open_incidents": 3})
    await loop.tick()
    assert calls["dispositions"] == []
    assert "ceiling" in ctx.kinds()


@pytest.mark.asyncio
async def test_a_refused_spawn_leaves_the_item_open_for_a_later_tick():
    """Marking it 'diagnosing' would park the incident behind a session that
    never existed."""
    loop, _ctx, calls = build_loop(values=_TWO_SIGMA, spawn_returns=None)
    await loop.tick()
    assert calls["spawns"] and calls["dispositions"] == []


@pytest.mark.asyncio
async def test_the_daily_cap_window_rolls_so_yesterdays_spawns_expire():
    loop, _ctx, calls = build_loop(values=_TWO_SIGMA,
                                   config={"max_diagnoses_per_day": 1})
    await loop.tick()
    assert len(calls["spawns"]) == 1
    loop._clock = lambda: 1000.0 + 25 * 60 * 60  # a day later
    await loop.tick()
    assert len(calls["spawns"]) == 2


# --------------------------------------------------------------------------- #
# Automation contract surface                                                   #
# --------------------------------------------------------------------------- #

def test_the_automation_surface_is_claude_only_and_defaults_off():
    a = ControlBandMonitor()
    assert a.id == "control-band-monitor"
    assert a.modes == ("off", "monitor", "propose")
    assert a.engine_policy is EnginePolicy.CLAUDE_ONLY
    assert {f.key for f in a.config_schema} == {
        "enabled_repos", "max_diagnoses_per_day", "max_open_incidents"}


def test_status_reports_watched_and_breaching_counts():
    a = ControlBandMonitor()
    assert a.status(None).level == "idle"

    class Ctx: control_band_counts = {"watched": 2, "breaching": 1}
    st = a.status(Ctx())
    assert st.level == "warn" and "2 bands watched" in st.text and "1 breaching" in st.text

    class Quiet: control_band_counts = {"watched": 1, "breaching": 0}
    assert a.status(Quiet()).level == "idle"
    assert "1 band watched" in a.status(Quiet()).text
