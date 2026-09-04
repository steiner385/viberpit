"""Amendment gate for ``.specify/memory/constitution.md`` (constitution Governance).

``MANAGED_DENY`` and the ``PreToolUse`` hook enforce *authorship method* — which
tool or shell command touched the file. Method is not enumerable: every new tool,
shell idiom, or agent is a fresh hole, and neither layer applies to a human's
editor or a web commit.

This module enforces *properties of the resulting change* instead, which is
tool-agnostic and cannot be routed around:

* the version was bumped,
* the Sync Impact Report records the transition,
* ``Last Amended`` moved,
* the pull request carries an explicit owner acknowledgment.

The Governance section requires all four of an amendment; before this gate they
were convention, checked only by whoever reviewed the PR — and #1098 auto-merged
before review comments landed at all.

Pure by design: I/O lives in ``scripts/check_constitution_amendment.py``.
"""
from __future__ import annotations

import re

__all__ = [
    "ACKNOWLEDGMENT_MARKER",
    "check_amendment",
    "parse_footer",
]

# The deliberate, human signal that an amendment was approved by the owner. The
# Governance section requires acknowledgement in the PR description for any change
# touching a principle's meaning; we require it for every constitution edit,
# because "does this change a principle's meaning" is not machine-decidable.
ACKNOWLEDGMENT_MARKER = "owner acknowledgment:"

_VERSION_RE = re.compile(
    r"\*\*Version\*\*:\s*(?P<version>\d+\.\d+\.\d+)\s*\|"
    r"\s*\*\*Ratified\*\*:\s*(?P<ratified>[\d-]+)\s*\|"
    r"\s*\*\*Last Amended\*\*:\s*(?P<amended>[\d-]+)"
)


def parse_footer(text: str) -> dict[str, str] | None:
    """The ``Version`` / ``Ratified`` / ``Last Amended`` footer, or None."""
    match = _VERSION_RE.search(text)
    return match.groupdict() if match else None


def _as_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def check_amendment(base_text: str, head_text: str, pr_body: str) -> list[str]:
    """Violations for an amendment; empty means the change may merge.

    *base_text* is the constitution on the target branch, *head_text* the proposed
    version, *pr_body* the pull request description. Callers only invoke this when
    the file actually changed.
    """
    violations: list[str] = []

    base = parse_footer(base_text)
    head = parse_footer(head_text)
    if head is None:
        return ["the Version / Ratified / Last Amended footer is missing or malformed"]
    if base is None:
        # A base without a parseable footer cannot be compared; require the rest.
        base = {"version": "0.0.0", "amended": ""}

    # 1. The version must move forward.
    try:
        if _as_tuple(head["version"]) <= _as_tuple(base["version"]):
            violations.append(
                f"version not bumped: base {base['version']}, head {head['version']} — "
                "MAJOR for a principle removal/redefinition, MINOR for a new principle "
                "or materially expanded guidance, PATCH for clarifications"
            )
    except ValueError:
        violations.append(f"version {head['version']!r} is not semantic (X.Y.Z)")

    # 2. The Sync Impact Report must record the transition, so the reasoning for
    #    the chosen bump is written down rather than inferred from the diff.
    transition = f"{base['version']} → {head['version']}"
    if transition not in head_text:
        violations.append(
            f"Sync Impact Report has no {transition!r} entry — record the version "
            "change and the rationale for MAJOR vs MINOR vs PATCH"
        )

    # 3. Last Amended must move; a stale date makes the history unreadable.
    if head["amended"] == base["amended"]:
        violations.append(
            f"Last Amended is unchanged ({head['amended']}) — set it to the amendment date"
        )

    # 4. A human must acknowledge the amendment in the PR description.
    if ACKNOWLEDGMENT_MARKER not in (pr_body or "").lower():
        violations.append(
            "the pull request description carries no owner acknowledgment — add a line "
            f"beginning {ACKNOWLEDGMENT_MARKER!r} stating what is approved"
        )

    return violations
