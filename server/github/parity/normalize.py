"""Adapter: Plotroom GET /api/state -> comparable snapshot in ccswitch vocabulary.

This is the ONE place that encodes Plotroom's JSON shape. A Plotroom schema change
touches only this file. Tolerances are documented here, not scattered in the harness.

Real Plotroom /api/state shape (verified 2026-06-24):
  repos[]:
    .repo            — "owner/name"  (no nameWithOwner)
    .prs[]:
      .number        — int
      .url           — str
      .stage.stage   — "merged" | "ready" | "open" | "draft" | ...
      .checks[]      — list of {name, status, conclusion, ...}
      (no headSha, headRefName, isDraft, checksRollup at top level)
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Timestamps (when later phases compare them) are matched at second granularity:
TIMESTAMP_GRANULARITY_SECS = 1

# Plotroom stage → ccswitch state vocabulary
_STAGE_MAP = {
    "merged": "merged",
    "closed": "closed",
    "draft": "draft",
    "ready": "open",
    "open": "open",
    "waiting": "open",
    "blocked": "open",
    "running": "open",
}

# Legacy/assumed-schema state field (plan test uses these values)
_STATE_MAP = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}

_CHECKS_MAP: dict[str | None, str] = {
    "SUCCESS": "passing",
    "FAILURE": "failing",
    "ERROR": "failing",
    "PENDING": "pending",
    "EXPECTED": "pending",
    None: "none",
    "": "none",
}


@dataclass(frozen=True)
class OraclePr:
    repo: str
    number: int
    branch: str
    fields: dict = field(default_factory=dict)


def _map_state_from_stage(stage_obj: dict | None, is_draft: bool) -> str:
    """Map Plotroom's stage object to ccswitch state vocabulary."""
    if stage_obj is None:
        return "open"
    stage = (stage_obj.get("stage") or "").lower()
    if stage == "draft" or (stage in ("ready", "open") and is_draft):
        return "draft"
    return _STAGE_MAP.get(stage, stage)


def _map_state_legacy(raw_state: str, is_draft: bool) -> str:
    """Map legacy assumed-schema state field."""
    if raw_state == "OPEN" and is_draft:
        return "draft"
    return _STATE_MAP.get(raw_state, raw_state.lower())


def _derive_checks_rollup(checks: list[dict]) -> str:
    """Derive an overall check status from the checks list."""
    if not checks:
        return "none"
    conclusions = {c.get("conclusion") for c in checks}
    if "FAILURE" in conclusions or "ERROR" in conclusions:
        return "failing"
    if "PENDING" in conclusions or None in conclusions:
        return "pending"
    if all(c.get("conclusion") == "SUCCESS" for c in checks):
        return "passing"
    return "none"


def normalize_state(state_json: dict) -> dict[tuple[str, int], OraclePr]:
    out: dict[tuple[str, int], OraclePr] = {}
    for repo in (state_json or {}).get("repos", []):
        # Support both real Plotroom shape (.repo) and plan's assumed shape (.nameWithOwner)
        name = repo.get("nameWithOwner") or repo.get("repo") or ""
        for pr in repo.get("prs", []):
            number = pr.get("number")
            if number is None:
                continue
            is_draft = bool(pr.get("isDraft"))

            # State: real Plotroom uses stage.stage; assumed schema uses top-level state
            if "stage" in pr and isinstance(pr["stage"], dict):
                state = _map_state_from_stage(pr.get("stage"), is_draft)
            else:
                state = _map_state_legacy(pr.get("state", ""), is_draft)

            # Checks detail: real Plotroom checks[] have name+conclusion directly
            checks_list = pr.get("checks", [])
            checks_detail = [
                {
                    "name": c.get("name", ""),
                    "conclusion": (c.get("conclusion") or "pending").lower(),
                }
                for c in checks_list
            ]

            # Checks rollup: use explicit checksRollup if present (assumed schema),
            # otherwise derive from the checks list (real Plotroom schema)
            if "checksRollup" in pr:
                checks_rollup = _CHECKS_MAP.get(pr.get("checksRollup"), "none")
            else:
                checks_rollup = _derive_checks_rollup(checks_list)

            # head_sha: real Plotroom has no headSha; assumed schema uses headSha
            head_sha = pr.get("headSha", "")

            # branch: real Plotroom has no headRefName; assumed schema uses headRefName
            branch = pr.get("headRefName", "")

            fields = {
                "number": number,
                "url": pr.get("url", ""),
                "state": state,
                "is_draft": is_draft or state == "draft",
                "merged": state == "merged",
                "head_sha": head_sha,
                "checks": checks_rollup,
                "checks_detail": checks_detail,
            }
            out[(name, number)] = OraclePr(
                repo=name, number=number, branch=branch, fields=fields
            )
    return out
