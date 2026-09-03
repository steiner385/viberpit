from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class DeployError(Exception):
    def __init__(self, phase: str, detail: str):
        super().__init__(f"{phase}: {detail}")
        self.phase = phase
        self.detail = detail


class Phase(str, Enum):
    PREFLIGHT = "preflight"
    SAVE = "save"
    UPDATE = "update"
    REPORT = "report"
    RUNNERS = "runners"     # every fleet daemon: update checkout, restart if daemon-changing, verify
    RESTART = "restart"     # the edge
    RESUME = "resume"


class PhaseState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class DeployStatus:
    target_sha: str
    running: bool = False
    phases: dict[str, str] = field(default_factory=dict)  # phase -> PhaseState value
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    restore: dict = field(default_factory=dict)
    # Per-runner deploy state, host name -> HostProgress.to_wire()
    # ({state, step, error, sha, restore}). One host's failure is reported here and named
    # in `error`; the deploy is not rolled back for it (spec §2, decision 1a).
    hosts: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """The camelCase wire shape — served on /status AND persisted across the
        controller's self-restart. One shape, one source of truth (#865)."""
        return {"running": self.running, "phases": self.phases, "error": self.error,
                "warnings": self.warnings, "targetSha": self.target_sha,
                "restore": self.restore, "hosts": self.hosts}

    @classmethod
    def from_dict(cls, d: dict) -> "DeployStatus":
        return cls(target_sha=str(d.get("targetSha") or ""),
                   running=bool(d.get("running", False)),
                   phases=dict(d.get("phases") or {}),
                   error=d.get("error"),
                   warnings=list(d.get("warnings") or []),
                   restore=dict(d.get("restore") or {}),
                   hosts=dict(d.get("hosts") or {}))
