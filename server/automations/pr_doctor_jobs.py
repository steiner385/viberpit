"""In-memory job registry for async pr-doctor manual triggers (issue #668).

The manual "Fix CI" / "Resolve conflicts" actions run a clone → model-assess →
session-spawn pipeline that takes 12–41 s (8 instances observed in prod logs).
Rather than block the edge→daemon RPC for that entire window — it was only
surviving because ``AUTOMATION_RPC_TIMEOUT`` had been stretched to 90 s (PR #568)
— the trigger creates a ``Job`` here, returns its id immediately, and a
background task advances the job through its phases. The UI polls
``GET /automations/pr-doctor/jobs/{job_id}`` to show the live phase and, on
failure, which phase failed.

Deliberately in-memory and process-local (mirrors ``_consolidation_inflight``):
these jobs live for seconds and carry no state worth a migration. A daemon
restart mid-job orphans the spawn regardless; a stale id polled afterwards just
404s, which the UI treats as "job gone".
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

# Ordered lifecycle. ``queued`` is the pre-run state; ``cloning`` → ``assessing``
# → ``spawning`` mirror the pipeline stages; ``ready`` is terminal-success. A job
# that errors keeps ``error_phase`` set to the stage it died in.
PHASES = ("queued", "cloning", "assessing", "spawning", "ready")

# Terminal job status per raw session-fix status string (the value
# ``_pr_doctor_session_fix`` returns). Anything unrecognised is treated as an
# error so a new failure mode never masquerades as success.
_TERMINAL = {
    "spawned": "ready", "sent": "ready",
    "ceiling": "deferred", "transient": "deferred",
    "clone_failed": "error", "spawn_failed": "error", "failed": "error",
}
# Which pipeline phase a given failure status belongs to, so the UI can say
# "clone failed" vs "spawn failed" rather than a generic error.
_ERROR_PHASE = {"clone_failed": "cloning", "spawn_failed": "spawning", "failed": "spawning"}


@dataclass
class Job:
    job_id: str
    target: str           # "owner/repo#123"
    kind: str             # "failing_ci" | "merge_conflict"
    status: str = "queued"    # queued | running | ready | error | deferred
    phase: str = "queued"     # last PHASES entry entered
    session_id: str | None = None
    error_phase: str | None = None
    message: str = ""
    level: str = "ok"         # ok | warn | error (mirrors TriggerResult)

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "target": self.target, "kind": self.kind,
                "status": self.status, "phase": self.phase,
                "session_id": self.session_id, "error_phase": self.error_phase,
                "message": self.message, "level": self.level}


class JobStore:
    """A bounded, insertion-ordered map of job id → ``Job``.

    Not thread-safe by design: every caller runs on the daemon event loop, so a
    plain dict needs no lock. Bounded to ``cap`` entries — the oldest are evicted
    on create so a long-lived daemon never accumulates jobs without limit.
    """

    def __init__(self, *, cap: int = 200) -> None:
        self._jobs: dict[str, Job] = {}
        self._cap = cap

    def create(self, *, target: str, kind: str) -> Job:
        job = Job(job_id=f"prd-{secrets.token_hex(4)}", target=target, kind=kind)
        self._jobs[job.job_id] = job
        while len(self._jobs) > self._cap:
            # dicts preserve insertion order → the first key is the oldest.
            oldest = next(iter(self._jobs))
            del self._jobs[oldest]
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def advance(self, job: Job, phase: str) -> None:
        """Mark the job as running and entering ``phase``."""
        job.status = "running"
        job.phase = phase


def finalize(job: Job, raw_status: str, *, level: str, message: str,
             session_id: str | None = None) -> None:
    """Settle a job from the raw ``_pr_doctor_session_fix`` status string.

    Maps the status to a terminal job status (ready / deferred / error), copies
    the user-facing level+message, and — on error — records which phase failed.
    """
    job.status = _TERMINAL.get(raw_status, "error")
    job.level = level
    job.message = message
    job.session_id = session_id
    if job.status == "ready":
        job.phase = "ready"
    elif job.status == "error":
        job.error_phase = _ERROR_PHASE.get(raw_status, job.phase)
