from __future__ import annotations

from dataclasses import dataclass

from server.github.merge_queue import QueueEntry


@dataclass(frozen=True)
class Observation:
    captured_at: int
    entries: tuple[QueueEntry, ...]


@dataclass(frozen=True)
class TrainBuild:
    pr_number: int
    percent: int | None
    eta_seconds: int | None

    def to_dict(self) -> dict:
        return {"pr_number": self.pr_number, "percent": self.percent, "eta_seconds": self.eta_seconds}

    @classmethod
    def from_dict(cls, d: dict) -> "TrainBuild":
        return cls(pr_number=d["pr_number"], percent=d.get("percent"), eta_seconds=d.get("eta_seconds"))


@dataclass(frozen=True)
class MergeTrainState:
    repo: str
    depth: int
    trains_per_hour: float | None
    batch_success_pct: float | None
    ejects_24h: int
    oldest_wait_seconds: int | None
    failing: tuple[int, ...]
    building: TrainBuild | None
    blocked: tuple[int, ...]
    awaiting_qa: int
    awaiting_prod: int
    merges_next: TrainBuild | None

    def is_idle(self) -> bool:
        """No queue activity and no recent history → the widget renders nothing.

        Note: the canonical render gate is ``isIdle`` in MergeTrainWidget.tsx — this Python
        method is a convenience mirror used in tests. Keep both in sync if the logic changes.
        """
        return (
            self.depth == 0 and self.ejects_24h == 0 and not self.failing
            and not self.blocked and not self.building
            and not self.trains_per_hour
        )

    def to_dict(self) -> dict:
        return {
            "repo": self.repo, "depth": self.depth,
            "trains_per_hour": self.trains_per_hour,
            "batch_success_pct": self.batch_success_pct, "ejects_24h": self.ejects_24h,
            "oldest_wait_seconds": self.oldest_wait_seconds,
            "failing": list(self.failing),
            "building": self.building.to_dict() if self.building else None,
            "blocked": list(self.blocked),
            "awaiting_qa": self.awaiting_qa, "awaiting_prod": self.awaiting_prod,
            "merges_next": self.merges_next.to_dict() if self.merges_next else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MergeTrainState":
        b = d.get("building")
        n = d.get("merges_next")
        return cls(
            repo=d["repo"], depth=d["depth"], trains_per_hour=d.get("trains_per_hour"),
            batch_success_pct=d.get("batch_success_pct"), ejects_24h=d.get("ejects_24h", 0),
            oldest_wait_seconds=d.get("oldest_wait_seconds"),
            failing=tuple(d.get("failing", [])),
            building=TrainBuild.from_dict(b) if b else None,
            blocked=tuple(d.get("blocked", [])),
            awaiting_qa=d.get("awaiting_qa", 0), awaiting_prod=d.get("awaiting_prod", 0),
            merges_next=TrainBuild.from_dict(n) if n else None,
        )


_BIG = 1 << 30


def _percent(e: "QueueEntry") -> int | None:
    if e.checks_total and e.checks_done is not None and e.checks_total > 0:
        return round(100 * e.checks_done / e.checks_total)
    return None


def _build(e: "QueueEntry") -> "TrainBuild":
    return TrainBuild(pr_number=e.pr_number, percent=_percent(e), eta_seconds=e.eta_seconds)


def derive_live(latest: "Observation", now: int) -> dict:
    entries = latest.entries
    enq = [e.enqueued_at for e in entries if e.enqueued_at is not None]
    head = min(
        entries,
        key=lambda e: e.position if e.position is not None else _BIG,
        default=None,
    ) if entries else None
    building = _build(head) if (head and head.group_checks == "pending") else None
    return {
        "depth": len(entries),
        "oldest_wait_seconds": (now - min(enq)) if enq else None,
        "failing": tuple(e.pr_number for e in entries if e.group_checks == "failing"),
        "blocked": tuple(e.pr_number for e in entries if e.state == "UNMERGEABLE"),
        "building": building,
        "merges_next": _build(head) if head else None,
    }


def derive_history(observations: list["Observation"], now: int, window_seconds: int = 86400) -> dict:
    cutoff = now - window_seconds
    obs = sorted((o for o in observations if o.captured_at >= cutoff), key=lambda o: o.captured_at)
    ejects = 0
    merged: set[str] = set()
    failed: set[str] = set()
    for prev, cur in zip(obs, obs[1:]):
        present = {e.pr_number for e in cur.entries}
        for e in prev.entries:
            if e.pr_number in present:
                continue
            if e.state == "UNMERGEABLE" or e.group_checks == "failing":
                ejects += 1
                if e.batch_head_oid:
                    failed.add(e.batch_head_oid)
            elif e.group_checks == "passing" and e.batch_head_oid:
                merged.add(e.batch_head_oid)
            # NOTE: a PR that completes AND merges within one poll interval departs while
            # last-seen "pending" — counted as neither merged nor eject (undercounts trains/hr;
            # never inflates ejects). Inherent to snapshot polling.
    merged -= failed                       # a batch that ever failed is counted failed
    completed = len(merged) + len(failed)
    success_pct = (len(merged) / completed * 100) if completed else None
    span_h = ((obs[-1].captured_at - obs[0].captured_at) / 3600) if len(obs) >= 2 else 0.0
    trains_per_hour = (completed / span_h) if (completed and span_h > 0) else None
    return {"ejects_24h": ejects, "batch_success_pct": success_pct,
            "trains_per_hour": trains_per_hour}


def derive_merge_train_state(
    repo: str, observations: list["Observation"], now: int,
    awaiting_qa: int, awaiting_prod: int, window_seconds: int = 86400,
) -> MergeTrainState:
    latest = max(observations, key=lambda o: o.captured_at, default=None)
    live = derive_live(latest, now) if latest else {
        "depth": 0, "oldest_wait_seconds": None, "failing": (), "building": None,
        "blocked": (), "merges_next": None}
    hist = derive_history(observations, now, window_seconds)
    return MergeTrainState(
        repo=repo, depth=live["depth"], trains_per_hour=hist["trains_per_hour"],
        batch_success_pct=hist["batch_success_pct"], ejects_24h=hist["ejects_24h"],
        oldest_wait_seconds=live["oldest_wait_seconds"], failing=live["failing"],
        building=live["building"], blocked=live["blocked"],
        awaiting_qa=awaiting_qa, awaiting_prod=awaiting_prod,
        merges_next=live["merges_next"],
    )
