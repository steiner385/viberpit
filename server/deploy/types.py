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
    RESTART = "restart"
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
