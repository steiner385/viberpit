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
        """No queue activity and no recent history → the widget renders nothing."""
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
