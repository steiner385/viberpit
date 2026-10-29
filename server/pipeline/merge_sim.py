"""Analytic multi-train merge-ETA simulator (SPEC §1 — mergeEtaSim).

Pure port of plotroom@5166b52 ``server/estimator/queue.ts:107-167`` +
``server/math.ts:6-8`` (the ``percentile`` pick). No DB imports — the per-frame
store reads (last-20 durations, 7-day run/eject counts) happen once per repo in
``server.api.pipeline._build_state`` and are threaded in as plain values.

Estimator, not Monte-Carlo. For a WAITING queue entry, given the observed
whole-train durations (``group_runs`` last 20) and the 7-day eject probability::

    trains    = ceil((queuedAhead + 1) / batchSize)      # future trains incl. mine
    p50Secs   = currentTrainEta + trains * dur50
    p90Secs   = currentTrainEta + (trains + bump) * dur90 # bump=1 when ejectProb>15%
    trainsAhead = trains - 1 + (1 when a train is building ahead)

ViberPit extension (#3196): the analytic p90 assumes every group ahead passes,
so it grows only by one train-duration per batch. A poisoned queue head fails
every group behind it, and real waits ran 7.5h p50 against a "~2h" estimate.
When the repo has enough measured enqueue→merge history, the p90 is floored at
that history's p90 minus the time this entry has already waited
(``history_wait_p90``), so the pessimistic bound tracks what the queue actually
delivers rather than per-slot CI time.

Percentile indexing (SPEC §1 governs, citing queue.ts:155-156 → math.ts:6-8):
``percentile(sorted, p) = sorted[min(len-1, ceil(p*len) - 1)]``. This is the
plotroom formula verbatim, NOT the brief's ``s[len//2]`` / ``s[min(len-1,
int(len*0.9))]`` sketch (which disagrees; SPEC §1 wins). Pinned by
``test_plotroom_percentile_pick_on_fixed_20_sample_array``: for sorted [1..20],
dur50 = sorted[9] = 10 and dur90 = sorted[17] = 18.
"""
from __future__ import annotations

import math

# ejectProb above this bumps p90 by one extra train (queue.ts:9, EJECT_BUMP_MIN_PROB).
EJECT_BUMP_MIN_PROB = 0.15
# Minimum 7-day (runs + ejects) samples before ejectProb is trusted (else 0).
EJECT_PROB_MIN_SAMPLES = 5
# Minimum merged PRs with a known enqueue→merge wait before that history is
# trusted as a p90 floor (else the analytic p90 stands alone).
HISTORY_WAIT_MIN_SAMPLES = 5


def eject_probability(runs_7d: int, ejects_7d: int) -> float:
    """Observed 7-day eject probability = ejects / (runs + ejects).

    Returns 0.0 under ``EJECT_PROB_MIN_SAMPLES`` total samples — one bad
    afternoon must not double every p90 forever (queue.ts:107-111).
    """
    total = runs_7d + ejects_7d
    if total < EJECT_PROB_MIN_SAMPLES:
        return 0.0
    return ejects_7d / total


def _percentile(sorted_samples: list[int], p: float) -> int:
    """p-th percentile of an already-sorted list (plotroom math.ts:6-8).

    ``sorted[min(len-1, ceil(p*len) - 1)]`` — nearest-rank, no interpolation.
    """
    idx = min(len(sorted_samples) - 1, math.ceil(p * len(sorted_samples)) - 1)
    return sorted_samples[idx]


def history_wait_p90(wait_samples: list[int]) -> int | None:
    """p90 of measured enqueue→merge waits, or None under the sample floor."""
    samples = sorted(w for w in wait_samples if math.isfinite(w) and w > 0)
    if len(samples) < HISTORY_WAIT_MIN_SAMPLES:
        return None
    return _percentile(samples, 0.9)


def simulate_merge_eta(
    queued_ahead: int,
    batch_size: int,
    duration_samples: list[int],
    eject_prob: float,
    current_train_eta_secs: int | None,
    history_p90_secs: int | None = None,
    waited_secs: int = 0,
) -> dict | None:
    """Plotroom ``simulateMergeEta``: analytic multi-train ETA for a WAITING entry.

    Returns a ``MergeEtaSimulation`` dict, or ``None`` when there are no usable
    duration samples (callers fall back to the single-number stage ETA).
    ``history_p90_secs`` (measured enqueue→merge p90) floors the p90 at what is
    left of it after ``waited_secs`` in the queue; ``historyP90Secs`` echoes it.
    """
    samples = sorted(d for d in duration_samples if math.isfinite(d) and d > 0)
    if not samples:
        return None
    dur50 = _percentile(samples, 0.5)
    dur90 = _percentile(samples, 0.9)
    batch = max(1, batch_size)
    trains = math.ceil((max(0, queued_ahead) + 1) / batch)
    current_eta = max(0, current_train_eta_secs or 0)
    assumes_ejects = eject_prob > EJECT_BUMP_MIN_PROB
    bump = 1 if assumes_ejects else 0
    p90 = current_eta + (trains + bump) * dur90
    if history_p90_secs is not None:
        p90 = max(p90, history_p90_secs - max(0, waited_secs))
    return {
        "p50Secs": round(current_eta + trains * dur50),
        "p90Secs": round(p90),
        "historyP90Secs": history_p90_secs,
        "trainsAhead": trains - 1 + (1 if current_train_eta_secs is not None else 0),
        "assumesEjects": assumes_ejects,
    }
