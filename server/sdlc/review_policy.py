"""Pure SDLC review-policy engine — no I/O, no DB, no orchestrator.

Phase 4 of the AI-Native SDLC design (§9, issue #806). Mirrors the purity of
``server.sdlc.governance``: all decisions are total functions over plain values,
exhaustively testable without infrastructure. The orchestrator injects I/O
(load the file, persist findings, open CLAUDE.md PRs).

Three concerns:

1. **Policy** — ``ReviewPolicy`` captures the parsed ``.claude/REVIEW.md``
   content: allowed categories, severity tags, nit cap, excluded file patterns.
   ``parse_review_md`` produces one from the raw file text; ``load_review_policy``
   reads the file from disk (the one I/O-touching call in this module — kept here
   so callers need not care about the path).

2. **Finding classification** — ``classify_severity`` tags a finding string with
   the policy's severity vocabulary; ``is_excluded_path`` gates generated/CI-
   enforced files.

3. **Comment-command detection** — ``extract_at_claude_command`` returns the
   directive from an ``@claude <verb> <text>`` review comment, or ``None``.
   ``is_initiative_pr`` decides whether a PR is an Initiative artifact PR (and
   therefore subject to this policy).

4. **CLAUDE.md seeding** — ``should_seed_claude_md`` decides when a recurring
   pattern has been seen often enough to warrant an entry in ``CLAUDE.md``.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Policy constants                                                              #
# --------------------------------------------------------------------------- #

REVIEW_MD_PATH = ".claude/REVIEW.md"

ALLOWED_CATEGORIES: tuple[str, ...] = ("bug", "security", "compliance")
SEVERITY_TAGS: tuple[str, ...] = ("critical", "major", "minor", "nit")
# Findings at this severity or above count toward the CLAUDE.md seeding threshold.
SEED_MIN_SEVERITY: str = "major"
DEFAULT_NIT_CAP: int = 3
# A pattern seen this many times in distinct PRs triggers a CLAUDE.md entry.
CLAUDE_MD_SEED_THRESHOLD: int = 2

# File globs that are excluded from review (generated or CI-enforced). The
# orchestrator may supply repo-specific extras via ``ReviewPolicy.exclude_patterns``.
_DEFAULT_EXCLUDES: tuple[str, ...] = (
    "web/src/components/ui/*",
    "web/dist/*",
    ".venv/*",
    "node_modules/*",
    "*.lock",
    "alembic/versions/*",
)

# Regex that matches an ``@claude <verb> <rest>`` comment (case-insensitive,
# tolerates leading/trailing whitespace and markdown).
_AT_CLAUDE_RE = re.compile(
    r"(?:^|\n)\s*@claude\s+(\w+)\s*(.*?)(?:\n|$)",
    re.IGNORECASE | re.DOTALL,
)

# Labels and title fragments that mark a PR as part of an Initiative chain.
_INITIATIVE_LABELS: frozenset[str] = frozenset({"sdlc", "sdlc-artifact", "initiative"})
_INITIATIVE_TITLE_PREFIXES: tuple[str, ...] = (
    "intent:",
    "spec:",
    "plan:",
    "sdlc(",
    "[sdlc]",
    "[intent]",
    "[spec]",
    "[plan]",
)
_INITIATIVE_PATH_RE = re.compile(r"\bdocs/sdlc/[^/]+/(?:intent|spec|plan)\.md\b")


# --------------------------------------------------------------------------- #
# ReviewPolicy dataclass                                                        #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReviewPolicy:
    """Parsed content of ``.claude/REVIEW.md``. Immutable; produced by
    ``parse_review_md``; the orchestrator may extend ``exclude_patterns``."""
    categories: tuple[str, ...] = ALLOWED_CATEGORIES
    severity_tags: tuple[str, ...] = SEVERITY_TAGS
    nit_cap: int = DEFAULT_NIT_CAP
    exclude_patterns: tuple[str, ...] = _DEFAULT_EXCLUDES


# --------------------------------------------------------------------------- #
# Policy loading (the one I/O seam)                                            #
# --------------------------------------------------------------------------- #

def _default_policy() -> ReviewPolicy:
    return ReviewPolicy()


def parse_review_md(text: str) -> ReviewPolicy:
    """Parse a ``REVIEW.md`` file and return a ``ReviewPolicy``.

    Pure + total. Unknown content is ignored gracefully — if the file does not
    specify a nit cap we use the policy default. The parser extracts only the
    nit-cap integer (from a line like ``**3 `[nit]`**``); categories and severity
    tags are defined by this module's constants (the file documents them for
    humans; the code owns the authoritative list).
    """
    nit_cap = DEFAULT_NIT_CAP
    # Look for a line that starts with ``**<number>`` and contains ``nit`` — the
    # canonical nit-cap sentence from REVIEW.md.
    for line in text.splitlines():
        m = re.search(r"\*\*(\d+)\s+`?\[nit\]`?", line)
        if m:
            nit_cap = int(m.group(1))
            break
    return ReviewPolicy(nit_cap=nit_cap)


def load_review_policy(repo_root: str) -> ReviewPolicy:
    """Load ``REVIEW.md`` from *repo_root* and return a ``ReviewPolicy``.

    Returns the default policy when the file is absent or unreadable — reviews
    still run, using the policy constants baked into this module.
    """
    path = os.path.join(repo_root, REVIEW_MD_PATH)
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_review_md(fh.read())
    except OSError:
        return _default_policy()


# --------------------------------------------------------------------------- #
# Finding classification                                                        #
# --------------------------------------------------------------------------- #

def classify_severity(finding_text: str) -> str:
    """Return the severity tag embedded in *finding_text*, or ``"minor"`` as the
    safe default when none is found.

    The policy mandates tags like ``[critical]``, ``[major]``, ``[minor]``,
    ``[nit]``; this extractor is tolerant — it matches the first valid tag
    (bracketed or bare) in the text.
    """
    pattern = r"\[?(" + "|".join(SEVERITY_TAGS) + r")\]?"
    m = re.search(pattern, finding_text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return "minor"


def is_excluded_path(path: str, policy: ReviewPolicy) -> bool:
    """Return True when *path* matches any of *policy*'s exclude patterns.

    Uses ``fnmatch`` so glob patterns like ``web/src/components/ui/*`` work
    across OS path separators.
    """
    for pattern in policy.exclude_patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


# --------------------------------------------------------------------------- #
# Severity ordering helpers                                                     #
# --------------------------------------------------------------------------- #

_SEVERITY_RANK: dict[str, int] = {tag: i for i, tag in enumerate(SEVERITY_TAGS)}


def severity_gte(a: str, b: str) -> bool:
    """Return True when severity *a* is at least as severe as *b*.

    ``critical`` > ``major`` > ``minor`` > ``nit``.  Unknown tags are treated as
    ``minor`` (safe-default position).
    """
    rank_a = _SEVERITY_RANK.get(a, _SEVERITY_RANK["minor"])
    rank_b = _SEVERITY_RANK.get(b, _SEVERITY_RANK["minor"])
    return rank_a <= rank_b  # lower index == more severe


# --------------------------------------------------------------------------- #
# Comment-command detection                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AtClaudeCommand:
    """A parsed ``@claude <verb> <text>`` review comment."""
    verb: str        # e.g. "fix"
    text: str        # the rest of the comment after the verb
    raw: str         # the full original comment body


def extract_at_claude_command(comment_body: str) -> AtClaudeCommand | None:
    """Return the first ``@claude <verb> <text>`` directive in *comment_body*, or
    ``None`` if the comment contains no such directive.

    Pure. Verb is lowercased; text is stripped. Tolerates markdown fences,
    leading/trailing whitespace, and multi-line comments.
    """
    m = _AT_CLAUDE_RE.search(comment_body)
    if m is None:
        return None
    verb = m.group(1).lower()
    text = (m.group(2) or "").strip()
    return AtClaudeCommand(verb=verb, text=text, raw=comment_body)


# --------------------------------------------------------------------------- #
# Initiative PR detection                                                       #
# --------------------------------------------------------------------------- #

def is_initiative_pr(
    *,
    title: str = "",
    body: str = "",
    labels: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> bool:
    """Return True when a PR is an Initiative artifact PR and subject to this policy.

    Detection order (first match wins):
    1. Any label in ``_INITIATIVE_LABELS``.
    2. Title starts with a known Initiative prefix (case-insensitive).
    3. Changed files include a ``docs/sdlc/<slug>/<kind>.md`` path.

    Pure + total. Unknown / missing fields fall through safely.
    """
    for lbl in (labels or []):
        if lbl.lower() in _INITIATIVE_LABELS:
            return True
    title_lower = title.lower().lstrip()
    for prefix in _INITIATIVE_TITLE_PREFIXES:
        if title_lower.startswith(prefix.lower()):
            return True
    for f in (changed_files or []):
        if _INITIATIVE_PATH_RE.search(f):
            return True
    return False


# --------------------------------------------------------------------------- #
# CLAUDE.md seeding decision                                                    #
# --------------------------------------------------------------------------- #

@dataclass
class FindingRecord:
    """Tracks occurrences of a named pattern across PRs."""
    pattern: str
    pr_numbers: list[int] = field(default_factory=list)

    def record(self, pr_number: int) -> None:
        if pr_number not in self.pr_numbers:
            self.pr_numbers.append(pr_number)

    @property
    def occurrence_count(self) -> int:
        return len(self.pr_numbers)


def should_seed_claude_md(record: FindingRecord, severity: str = "major") -> bool:
    """Return True when a finding pattern is seen often enough and severe enough
    to warrant an entry in ``CLAUDE.md``.

    Criteria (both must hold):
    - ``severity`` is ``SEED_MIN_SEVERITY`` (``major``) or worse (``critical``).
    - The pattern has been seen in ``CLAUDE_MD_SEED_THRESHOLD`` or more distinct PRs.
    """
    if not severity_gte(severity, SEED_MIN_SEVERITY):
        return False
    return record.occurrence_count >= CLAUDE_MD_SEED_THRESHOLD


def build_claude_md_entry(record: FindingRecord, description: str, fix: str) -> str:
    """Render a ``CLAUDE.md`` ``## Correction`` entry for *record*.

    Pure. Callers pass the human-readable description and fix text; this module
    produces the formatted entry (single source of truth for the format documented
    in ``.claude/REVIEW.md``).
    """
    seen = ", ".join(f"PR #{n}" for n in record.pr_numbers)
    return (
        f"- **[{record.pattern}]** — {description}\n"
        f"  Seen in: {seen}.\n"
        f"  Fix: {fix}."
    )
