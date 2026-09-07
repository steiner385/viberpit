"""Pure control-band detection — the deterministic half of the SDLC Maintain loop.

Phase 2 of the AI-Native SDLC design (§3.2/§4/§9,
`docs/superpowers/specs/2026-08-29-ai-native-sdlc-design.md`, issue #804). The
playbook's thesis is *"detection deterministic, judgment gated"*: nothing in this
module asks a model anything. It turns a numeric series into a
Western-Electric-style verdict, and only a **breach** is allowed to invoke Claude
(the monitor loop does that, having been handed a `Breach` this module produced).

Like ``server.sdlc.engine`` and ``server.sdlc.governance``, this is a **pure**
module — no DB, no clock, no orchestrator — so every rule is exhaustively
unit-testable. The I/O (reading rows, spawning a session, filing a triage item)
lives in the orchestrator seams the monitor loop injects.

Two things live here:

* ``bucket_daily`` — the shared reducer that turns raw ``(timestamp, value)``
  observations into one sample per UTC day. Every metric source funnels through
  it so two sources cannot disagree about what "a day" is (constitution VI).
* ``evaluate`` — the detector. Baseline statistics over a trailing reference
  window, then the four Western Electric zone rules, most severe first.

Why the rules are anchored on the NEWEST sample
-----------------------------------------------
Textbook WE rules ask "is there *any* run of points in the series that violates
a rule". A monitor asking that would re-fire the same breach on every tick for as
long as the offending points stay inside the window. Every rule here therefore
requires the newest sample to participate: the question is "did the process go
out of control *as of the latest observation*", which is what a control loop
actually needs and which makes re-detection settle by itself once the series
recovers. Cross-tick duplicate suppression is still the monitor's job (a breach
that persists for three days should not file three triage items) — this only
keeps the detector from being noisy by construction.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

# The metric series the monitor knows how to read. bands.yaml validates every
# metric's ``source`` against this tuple, so a typo in the config is a load-time
# error rather than a silently-never-firing band.
#
# Adding a source means adding BOTH an entry here and the orchestrator seam that
# reads it (``_control_band_series_for``) — the loader's check is what makes the
# pairing impossible to half-do.
METRIC_SOURCES: tuple[str, ...] = (
    # Mean hours from PR open to merge, over PRs merged that day (merged_prs).
    "pr_cycle_time_hours",
    # Share of merge-group CI runs that ejected at least one check that day
    # (group_runs / group_failures).
    "ci_failure_rate",
)

# Which side of the mean counts as a problem for a metric.
DIRECTIONS: tuple[str, ...] = ("higher_is_worse", "lower_is_worse", "both")

# How many of the newest samples the rules may look at. The longest rule (WE4)
# needs 8 consecutive points, so this is 8 — not a tunable: making it larger
# would let a rule reach back past its own definition.
EVAL_POINTS = 8

# Rules, in the order they are checked. The FIRST match wins, so the list is
# ordered most-severe first: a point beyond 3σ is a 3σ event even though it is
# also, trivially, beyond 2σ and 1σ.
#
# (rule id, sigma, how many of the last N, N)
_RULES: tuple[tuple[str, int, int, int], ...] = (
    ("we1", 3, 1, 1),   # 1 point beyond 3σ
    ("we2", 2, 2, 3),   # 2 of the last 3 beyond 2σ, same side
    ("we3", 1, 4, 5),   # 4 of the last 5 beyond 1σ, same side
)

# WE4 ("8 consecutive points on the same side of the mean") is a run rule, not a
# zone rule — it has no sigma threshold of its own. It is reported at 1σ, the
# lowest response tier, because it signals a shifted mean rather than an
# excursion: worth logging, never worth waking an agent.
_WE4_RUN = 8
_WE4_SIGMA = 1


@dataclass(frozen=True)
class Sample:
    """One bucketed observation. ``at`` is the bucket label (a UTC date string
    for ``bucket_daily``), kept as text because it is an identity/display value —
    it is never re-parsed for arithmetic, and it round-trips into the triage row
    and the API payload unchanged."""
    at: str
    value: float


@dataclass(frozen=True)
class Baseline:
    """Reference statistics the zone rules are measured against.

    ``stddev`` is the POPULATION standard deviation: the baseline window is the
    full reference period being characterised, not a sample drawn from a larger
    one, and the sample (n-1) form would inflate σ on the short windows a young
    metric has — making a real breach silently unreportable exactly when the
    history is thinnest.
    """
    mean: float
    stddev: float
    n: int

    def zone(self, value: float) -> float:
        """How many σ *value* sits from the mean, signed. Undefined (0.0) for a
        flat baseline — see ``evaluate``'s zero-variance guard."""
        if self.stddev <= 0:
            return 0.0
        return (value - self.mean) / self.stddev


@dataclass(frozen=True)
class Breach:
    """A fired control-band rule. Carries every number a human (or the diagnosis
    prompt) needs to understand the call without re-running the detector.

    ``sigma`` is the rule's zone, which the bands config maps to a response tier
    — the detector deliberately does not name tiers, so statistics and policy
    cannot drift into each other.
    """
    metric: str
    rule: str            # "we1" | "we2" | "we3" | "we4"
    sigma: int           # 3 | 2 | 1
    side: str            # "high" | "low"
    at: str              # bucket label of the newest sample
    value: float         # newest sample's value
    baseline: Baseline
    points: tuple[Sample, ...]   # the samples that fired the rule, oldest first

    @property
    def z(self) -> float:
        """Signed σ-distance of the breaching value from the baseline mean."""
        return self.baseline.zone(self.value)

    def key(self) -> str:
        """Stable identity for cross-tick de-duplication: one open triage item per
        (metric, rule, side). A breach that persists across ticks re-derives the
        same key, so the monitor updates the existing row instead of filing a
        second one; a breach that flips side or escalates a rule is a genuinely
        new signal and gets its own."""
        return f"{self.metric}:{self.rule}:{self.side}"

    def summary(self) -> str:
        """One-line human description, used as the triage item's title and in the
        activity feed."""
        direction = "above" if self.side == "high" else "below"
        return (
            f"{self.metric} {direction} {self.sigma}σ ({self.rule}): "
            f"{self.value:.3g} vs baseline {self.baseline.mean:.3g} "
            f"±{self.baseline.stddev:.3g} (n={self.baseline.n}) at {self.at}"
        )


def bucket_daily(observations: list[tuple[datetime, float]]) -> list[Sample]:
    """Reduce raw ``(when, value)`` observations to one mean-valued sample per UTC
    day, oldest first. Pure + total; an empty input yields an empty list.

    Naive datetimes are read as UTC — every timestamp in this codebase's DB is
    naive UTC by convention (``server/db/clock.py``), so attaching UTC is a
    no-op restatement of the invariant rather than a guess. Days with no
    observation are simply absent: the detector counts *samples*, not calendar
    days, so an honest gap shortens the window instead of injecting a fabricated
    zero that would drag the mean toward it.
    """
    by_day: dict[str, list[float]] = {}
    for when, value in observations:
        at = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
        by_day.setdefault(at.astimezone(timezone.utc).date().isoformat(), []).append(
            float(value))
    return [Sample(at=day, value=statistics.fmean(values))
            for day, values in sorted(by_day.items())]


def _counts(baseline: Baseline, samples: tuple[Sample, ...], sigma: int,
            side: str) -> int:
    """How many of *samples* sit beyond *sigma* on *side* of the baseline."""
    sign = 1.0 if side == "high" else -1.0
    return sum(1 for s in samples if sign * baseline.zone(s.value) > sigma)


def _sides(direction: str) -> tuple[str, ...]:
    """The excursion sides that count as a problem under *direction*.

    ``high`` is checked before ``low`` for ``both`` only to make the outcome
    deterministic when a single tick could satisfy either — it cannot in
    practice (one sample cannot be above and below the mean at once), but a
    documented order beats an incidental one.
    """
    if direction == "higher_is_worse":
        return ("high",)
    if direction == "lower_is_worse":
        return ("low",)
    return ("high", "low")


def compute_baseline(samples: list[Sample], *, baseline_points: int) -> Baseline | None:
    """Reference statistics over the window that PRECEDES the evaluation points.

    The trailing ``EVAL_POINTS`` samples are held out: they are the points under
    test, and letting a breach contribute to the mean and σ it is measured
    against is how a slow drift makes itself invisible. The reference window is
    then capped at the newest *baseline_points* of what remains, so the chart
    tracks a *rolling* baseline rather than being anchored to the metric's
    ancient history.

    Returns ``None`` when the reference window is empty — a caller decides
    whether that is "too young to judge" (it always is).
    """
    reference = samples[:-EVAL_POINTS]
    if baseline_points > 0:
        reference = reference[-baseline_points:]
    if not reference:
        return None
    values = [s.value for s in reference]
    return Baseline(
        mean=statistics.fmean(values),
        stddev=statistics.pstdev(values),
        n=len(values),
    )


def evaluate(
    metric: str,
    samples: list[Sample],
    *,
    direction: str = "higher_is_worse",
    baseline_points: int = 30,
    min_baseline: int = 10,
) -> Breach | None:
    """The detector. Returns the most severe fired rule, or ``None``. Pure + total.

    *samples* must be oldest-first (``bucket_daily`` guarantees it). The series is
    judged only when there is a real reference window: fewer than *min_baseline*
    baseline points, or a perfectly flat baseline (σ = 0), yields ``None``.

    The σ = 0 guard is not a rounding nicety. With zero variance every value that
    is not exactly the mean is infinitely many σ out, so a metric that has been
    constant — a brand-new counter, a rate pinned at 0.0 — would report a 3σ
    emergency the first time it moved by any amount at all. A control chart needs
    variation to have said anything; without it the honest answer is "no signal".
    """
    if direction not in DIRECTIONS:
        raise ValueError(
            f"unknown direction {direction!r}; expected one of {', '.join(DIRECTIONS)}")
    if len(samples) <= EVAL_POINTS:
        return None
    baseline = compute_baseline(samples, baseline_points=baseline_points)
    if baseline is None or baseline.n < min_baseline or baseline.stddev <= 0:
        return None

    recent = tuple(samples[-EVAL_POINTS:])
    newest = recent[-1]

    # Rules outermost, sides innermost: severity has to dominate the side sweep,
    # or under ``direction="both"`` a 1σ excursion on the high side would be
    # reported in preference to a 3σ excursion on the low side.
    for rule, sigma, needed, window in _RULES:
        for side in _sides(direction):
            sign = 1.0 if side == "high" else -1.0
            # Anchored on the newest sample: if it is not itself beyond the zone,
            # this rule did not fire *now* (see the module docstring).
            if sign * baseline.zone(newest.value) <= sigma:
                continue
            window_points = recent[-window:]
            if _counts(baseline, window_points, sigma, side) >= needed:
                return Breach(
                    metric=metric, rule=rule, sigma=sigma, side=side,
                    at=newest.at, value=newest.value, baseline=baseline,
                    points=window_points)

    # WE4 last: a sustained run on one side, with no point required to leave the
    # 1σ zone at all. Checked after the zone rules because it is the weakest
    # signal, and only when there are 8 points to run over.
    for side in _sides(direction):
        sign = 1.0 if side == "high" else -1.0
        if len(recent) < _WE4_RUN:
            continue
        run = recent[-_WE4_RUN:]
        if all(sign * (s.value - baseline.mean) > 0 for s in run):
            return Breach(
                metric=metric, rule="we4", sigma=_WE4_SIGMA, side=side,
                at=newest.at, value=newest.value, baseline=baseline, points=run)
    return None
