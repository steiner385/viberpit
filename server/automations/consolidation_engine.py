# server/automations/consolidation_engine.py
"""Pure consolidation engine: derive candidate metadata and suggest low-risk
clusters of open PRs to combine into one. No I/O — trivially testable."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Conventional-commit scope: `type(scope): …` or `type(scope)!: …`
_SCOPE_RE = re.compile(r"^\s*[a-zA-Z]+\(([^)]+)\)!?:")


def derive_scope(title: str) -> str:
    """Conventional-commit scope from a PR title, lowercased ('' if none)."""
    m = _SCOPE_RE.match(title or "")
    return m.group(1).strip().lower() if m else ""


@dataclass(frozen=True)
class ConsolidationCandidate:
    number: int
    title: str
    url: str
    checks: str          # "passing" | "failing" | "pending" | "none"
    mergeable: str       # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
    updated_at: str
    scope: str           # derive_scope(title)

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title, "url": self.url,
                "checks": self.checks, "mergeable": self.mergeable,
                "updated_at": self.updated_at, "scope": self.scope}


@dataclass(frozen=True)
class Cluster:
    numbers: tuple[int, ...]   # sorted, len >= 2
    rationale: str
    risk: str                  # "low" | "risky"

    def to_dict(self) -> dict:
        return {"numbers": list(self.numbers), "rationale": self.rationale,
                "risk": self.risk}


def _is_risky(c: ConsolidationCandidate) -> bool:
    return c.checks == "failing" or c.mergeable == "CONFLICTING"


def suggest_clusters(candidates: list[ConsolidationCandidate]) -> list[Cluster]:
    """Group candidates sharing a non-empty conventional-commit scope into
    clusters of >=2. A cluster is 'risky' if any member has failing CI or a
    conflict, else 'low'. Largest clusters first.

    [USER CONTRIBUTION POINT] The weighting/threshold here defines the feature's
    taste — v1 keys off scope + all-green; enrich with files/author in v1.5."""
    by_scope: dict[str, list[ConsolidationCandidate]] = {}
    for c in candidates:
        if c.scope:
            by_scope.setdefault(c.scope, []).append(c)
    clusters: list[Cluster] = []
    for scope, members in by_scope.items():
        if len(members) < 2:
            continue
        nums = tuple(sorted(m.number for m in members))
        risky = any(_is_risky(m) for m in members)
        rationale = (f"{len(members)} PRs share scope `{scope}`"
                     + ("" if risky else ", all green"))
        clusters.append(Cluster(numbers=nums, rationale=rationale,
                                risk="risky" if risky else "low"))
    clusters.sort(key=lambda cl: (-len(cl.numbers), cl.numbers))
    return clusters


def consolidation_target(repo: str, numbers: list[int]) -> str:
    """Session origin_ref/dedupe target for a consolidation, e.g.
    'consolidate:o/r#1+2'. Numbers are de-duped and sorted so the same PR set
    always maps to the same target."""
    want = sorted({int(n) for n in numbers})
    return f"consolidate:{repo}#{'+'.join(str(n) for n in want)}"


def parse_consolidation_target(ref: str) -> tuple[str, list[int]] | None:
    """Inverse of consolidation_target: (repo, sorted numbers), or None for a
    foreign or malformed ref."""
    if not ref.startswith("consolidate:"):
        return None
    repo, sep, nums = ref[len("consolidate:"):].rpartition("#")
    if not sep or not repo or not nums:
        return None
    try:
        return repo, sorted({int(n) for n in nums.split("+")})
    except ValueError:
        return None


def validate_selection(candidates: list[ConsolidationCandidate]) -> str | None:
    """Return an error status ('too_few') or None if consolidatable. Distinct
    numbers only; the caller pre-filters to open/non-draft PRs."""
    distinct = {c.number for c in candidates}
    if len(distinct) < 2:
        return "too_few"
    return None
