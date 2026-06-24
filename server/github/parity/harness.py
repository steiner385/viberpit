"""Diff ccswitch's PR snapshot (candidate) against Plotroom's /api/state (oracle).

`diff_snapshots` is pure. The I/O wrapper (fetch + compute candidate) lives in the
CLI (__main__) so this module stays unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from server.github.pr_status import PullRequestInfo
from server.github.parity.coverage import covered_fields
from server.github.parity.normalize import OraclePr


@dataclass(frozen=True)
class FieldMismatch:
    key: tuple[str, int]
    field: str
    candidate: object
    oracle: object


@dataclass(frozen=True)
class ParityReport:
    matched: int
    mismatches: list = field(default_factory=list)
    oracle_only: list = field(default_factory=list)
    candidate_only: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Parity holds iff every COVERED field agrees on the PRs BOTH sides see.

        Population differences (``oracle_only``/``candidate_only``) are reported but do
        NOT fail parity: Plotroom tracks all open PRs while ccswitch's candidate can
        differ in coverage or poll timing. The parity question is whether the covered
        fields agree where both observe the same PR — i.e. zero field mismatches.
        """
        return not self.mismatches


def _candidate_value(pr: PullRequestInfo, name: str) -> object:
    if name == "checks_detail":
        return sorted((c.name, c.conclusion) for c in pr.checks_detail)
    return getattr(pr, name)


def _oracle_value(pr: OraclePr, name: str) -> object:
    if name == "checks_detail":
        return sorted((c["name"], c["conclusion"]) for c in pr.fields.get("checks_detail", []))
    return pr.fields.get(name)


def diff_snapshots(
    candidate: dict[tuple[str, int], PullRequestInfo],
    oracle: dict[tuple[str, int], OraclePr],
    phase: int,
) -> ParityReport:
    fields = covered_fields(phase)
    mismatches: list[FieldMismatch] = []
    matched = 0
    shared = candidate.keys() & oracle.keys()
    for key in shared:
        cand, orc = candidate[key], oracle[key]
        row_ok = True
        for name in fields:
            # Compare only when the ORACLE actually carries the field (oracle-missing
            # is treated as "not under test for this row", not a mismatch).
            if name not in orc.fields:
                continue
            cv, ov = _candidate_value(cand, name), _oracle_value(orc, name)
            if cv != ov:
                mismatches.append(FieldMismatch(key=key, field=name, candidate=cv, oracle=ov))
                row_ok = False
        if row_ok:
            matched += 1
    return ParityReport(
        matched=matched,
        mismatches=mismatches,
        oracle_only=sorted(oracle.keys() - candidate.keys()),
        candidate_only=sorted(candidate.keys() - oracle.keys()),
    )
