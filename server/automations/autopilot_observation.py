"""Canonical autopilot summary and aggregation of host-owned observations."""
from math import isfinite

from server.automations.base import AutomationStatus
from server.time_iso import parse_iso_utc

# A tick can take minutes while sequential decisions await the helper model.
# Beyond fifteen minutes its old counters cannot describe current activity.
AUTOPILOT_OBSERVATION_STALE_S = 15 * 60


def autopilot_status(counts: dict | None) -> AutomationStatus:
    """Render numeric observations identically for one host and the fleet."""
    if counts is None:
        return AutomationStatus("Runtime telemetry unavailable", level="unknown")
    advancing = counts.get("advancing", 0)
    escalated = counts.get("escalated", 0)
    waiting = counts.get("waiting", 0)
    text = f"{advancing} advancing"
    if escalated:
        text += f", {escalated} escalated"
    if waiting:
        text += f", {waiting} waiting"
    return AutomationStatus(
        text, level="active" if advancing else ("warn" if escalated else "idle"),
        counts=counts, waiting_reason="human" if escalated else None)


def merge_autopilot_statuses(observations: dict[str, dict | None], *, now: float) -> dict:
    """Sum fresh counters; preserve producer evidence and explicitly name gaps.

    A missing host is represented by None. Old counts never become fresh just
    because its daemon answered an RPC. Keep the oldest known producer timestamp
    and any human wait even when that host's counters cannot safely be summed.
    """
    counts: dict[str, int | float] = {}
    unavailable = []
    warnings: dict[str, str] = {}
    observed_times: list[tuple[float, str]] = []
    failed_times: list[tuple[float, str]] = []
    waiting_reason = None
    for host, raw in observations.items():
        part = raw if isinstance(raw, dict) else {}
        reason = part.get("waitingReason")
        if reason == "human" or (waiting_reason is None and isinstance(reason, str)):
            waiting_reason = reason
        stamp = part.get("observedAt")
        observed = parse_iso_utc(stamp) if isinstance(stamp, str) else None
        if observed is not None and isinstance(stamp, str):
            observed_times.append((observed.timestamp(), stamp))
        failed_stamp = part.get("lastRefreshFailedAt")
        failed = parse_iso_utc(failed_stamp) if isinstance(failed_stamp, str) else None
        if failed is not None and isinstance(failed_stamp, str):
            failed_times.append((failed.timestamp(), failed_stamp))
        host_counts = part.get("counts")
        if (observed is None or now - observed.timestamp() > AUTOPILOT_OBSERVATION_STALE_S
                or part.get("lastRefreshFailedAt") or part.get("level") == "unknown"
                or not isinstance(host_counts, dict)):
            unavailable.append(host)
            continue
        for key, value in host_counts.items():
            if type(value) in (int, float) and isfinite(value):
                counts[key] = counts.get(key, 0) + value
        if part.get("level") in ("warn", "error"):
            # observed_status marks partial producer observations as warn without
            # discarding the measured subset. Reconstructing level from counters
            # alone would turn that partial zero into a healthy idle fleet.
            warnings[host] = part["level"]
    status = autopilot_status(counts or None)
    status.waiting_reason = waiting_reason or status.waiting_reason
    if observed_times:
        status.observed_at = min(observed_times)[1]
    if failed_times:
        status.last_refresh_failed_at = max(failed_times)[1]
    if warnings:
        status.level = "error" if "error" in warnings.values() else "warn"
        status.text += "; host observations need attention: " + ", ".join(sorted(warnings))
    if unavailable:
        status.counts = {**counts, "hosts_unavailable": len(unavailable)}
        status.level = "unknown"
        status.text += "; observations unavailable: " + ", ".join(sorted(unavailable))
    return status.to_dict()
