"""Pure classification: prompt builders + structured-output schema/parse
(design 2026-08-31 §5). No I/O, no model calls — the classify/map-synthesis
loops feed views in and get a prompt out, or feed session output in and get
``ProposedClassification`` rows out (mapped to TriageProposal by the seam).

Phase 3's continuous triage loop REUSES this module verbatim — there is exactly
one classification implementation (constitution VI).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from server.db.models import FEATURE_LIFECYCLE_STATES, WORK_ITEM_KINDS

# One place defining the feature-doc directory + path (single source of truth,
# mirrors sdlc.prompts.SDLC_DOCS_DIR / artifact_path). The map-synthesis session
# writes one file per capability under here.
FEATURE_DOCS_DIR = "docs/product/features"

# Open items classified per session (spec §5: ~50/session, structured output).
CLASSIFY_CHUNK_SIZE = 50

_VALID_CONFIDENCE = ("low", "medium", "high")


def feature_doc_path(slug: str) -> str:
    return f"{FEATURE_DOCS_DIR}/{slug}.md"


class ClassificationParseError(ValueError):
    """Raised only when a classify session's output has no parseable JSON array.
    Individual malformed rows are skipped, not fatal (one bad row must never lose
    the whole chunk)."""


@dataclass(frozen=True)
class WorkItemBrief:
    number: int
    kind: str  # WORK_ITEM_KINDS
    title: str
    body: str
    labels: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class FeatureBrief:
    slug: str
    title: str
    summary: str


@dataclass(frozen=True)
class ProposedClassification:
    """One row a classify session emits. ``feature_slug`` is None for a
    ``no_fit`` answer, which instead carries ``no_fit_suggestion`` (the suggested
    new-feature name). Mapped onto TriageProposal by the orchestrator seam."""
    number: int
    kind: str
    feature_slug: str | None
    no_fit_suggestion: str | None
    reach: int | None
    impact: int | None
    effort: int | None
    confidence: int | None
    rationale: str
    confidence_level: str  # low | medium | high


def build_map_synthesis_prompt(
    *,
    slug: str,
    repo: str,
    default_branch: str,
    open_count: int,
    closed_count: int,
    scope: str = "initial",
    suggestion: str | None = None,
    item_numbers: tuple[int, ...] = (),
) -> str:
    """Seed prompt for the map-synthesis session (spec §5 Step 1 / §6 drift).

    Reads the codebase + mirrored backlog (open+closed) + PR history as
    evidence, writes outcome-driven feature docs, and opens ONE non-draft PR —
    NO auto-merge. The merge is the human curation gate.

    ``scope="initial"`` (default) is the whole-repo archaeology prompt.
    ``scope="diff"`` is the diff-scoped drift prompt (phase 3 reuses this one
    builder): it names ``suggestion`` (the proposed new-feature name) and
    ``item_numbers`` (the clustered no-fit items) and asks for a single new/edited
    feature doc plus PR — no full re-archaeology."""
    if scope == "diff":
        nums = ", ".join(f"#{n}" for n in item_numbers) or "(none listed)"
        return (
            "You are proposing a **feature-map update** for an existing project "
            "whose triage flagged a cluster of items that do not fit the current "
            f"map. Suggested new capability: `{suggestion or 'unnamed'}`. "
            f"Driving items{f' in `{repo}`' if repo else ''}: {nums}.\n\n"
            f"Add or edit ONE feature doc under `{FEATURE_DOCS_DIR}/"
            f"{(suggestion or 'new-feature')}.md` using the outcome-driven "
            "template (Problem / Target users / Desired outcome / Success "
            "metrics / Non-goals / Notes / Evidence). Justify it with the "
            "driving item numbers and relevant file paths in the Evidence "
            "section. Do NOT re-run whole-repo archaeology.\n\n"
            "Requirements — propose-only, follow exactly:\n"
            f"1. Branch off `origin/{default_branch}` (never local "
            f"`{default_branch}`).\n"
            f"2. Commit only under `{FEATURE_DOCS_DIR}/`.\n"
            "3. Open exactly ONE NON-draft PR, marked ready for review.\n"
            "4. Do NOT arm auto-merge — the merge is the human curation gate.\n"
            "5. End every commit message with:\n"
            "   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`\n"
        )
    repo_clause = f" in repository `{repo}`" if repo else ""
    return (
        "You are performing **product archaeology** for an existing project. "
        "Read the codebase in this clone, the mirrored backlog, and the PR "
        "history to reconstruct the durable *feature map* — what this product "
        "actually is, separate from any single delivery effort.\n\n"
        f"CONTEXT: project `{slug}`{repo_clause}. The mirror holds ~{open_count} "
        f"open and ~{closed_count} closed items; churn concentration in the PR "
        "history is strong evidence of real capability areas.\n\n"
        f"Write one markdown doc per capability under `{FEATURE_DOCS_DIR}/"
        "<feature-slug>.md`. Each doc uses YAML frontmatter (slug, title, "
        f"state ∈ {list(FEATURE_LIFECYCLE_STATES)}, optional links) followed by "
        "the outcome-driven template:\n"
        "- **Problem** — the concrete pain this capability addresses.\n"
        "- **Target users** — who relies on it.\n"
        "- **Desired outcome** — what good looks like.\n"
        "- **Success metrics** — how you would measure it.\n"
        "- **Non-goals** — what it deliberately excludes.\n"
        "- **Notes** — anything else worth capturing.\n"
        "- **Evidence** — file paths and representative issue/PR numbers that "
        "justify this capability's existence.\n"
        "Fill only what evidence supports; mark gaps explicitly as "
        "`(unknown — needs owner input)`.\n\n"
        "Requirements — this is a propose-only onboarding step, follow exactly:\n"
        f"1. Branch off `origin/{default_branch}` (never local `{default_branch}`).\n"
        f"2. Commit the new docs under `{FEATURE_DOCS_DIR}/` only.\n"
        "3. Open exactly ONE pull request with the whole feature map — a "
        "NON-draft PR, marked ready for review.\n"
        "4. Do NOT arm auto-merge: the merge is the human curation gate; PR "
        "review comments are where the map is argued with.\n"
        "5. End every commit message with:\n"
        "   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`\n"
    )


def build_classification_prompt(
    *,
    items: list[WorkItemBrief],
    features: list[FeatureBrief],
    corrections: list[str] | None = None,
) -> str:
    """Seed prompt for a classify session (spec §5 Step 2 / §6 item 1).

    Presents a chunk of open items and the candidate feature map, and asks for a
    JSON array of classifications with RICE-lite scores + a ``no_fit`` escape
    hatch. Past human corrections (spec §6) are surfaced as ground truth."""
    feature_lines = "\n".join(
        f"- `{f.slug}` — {f.title}: {f.summary}" for f in features
    ) or "- (no features yet — every item is likely no_fit)"
    item_lines = "\n".join(
        f"- #{it.number} [{it.kind}] {it.title}\n"
        f"    labels: {', '.join(it.labels) or '(none)'}\n"
        f"    body: {it.body[:500]}"
        for it in items
    )
    corrections_block = ""
    if corrections:
        joined = "\n".join(f"- {c}" for c in corrections)
        corrections_block = (
            "\nGROUND TRUTH — past human corrections; respect them:\n"
            f"{joined}\n")
    return (
        "You are triaging an existing project's backlog into its feature map. "
        "Assign each work item to the single best-fitting feature, or answer "
        "`no_fit` with a suggested new-feature name when none fits — never force "
        "a bad assignment.\n\n"
        "CANDIDATE FEATURES:\n"
        f"{feature_lines}\n\n"
        "WORK ITEMS TO CLASSIFY:\n"
        f"{item_lines}\n"
        f"{corrections_block}\n"
        "For each item estimate RICE-lite inputs as small integers (1–5): "
        "reach, impact, effort, confidence. Rate your own confidence_level as "
        "one of low|medium|high.\n\n"
        "Return ONLY a JSON array (in a ```json fenced block), one object per "
        "item, with keys: number, kind, feature_slug (or null), "
        "no_fit_suggestion (or null), reach, impact, effort, confidence, "
        "rationale, confidence_level. Do not classify any item not listed above."
    )


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — reject it
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _row_to_classification(row: object) -> ProposedClassification | None:
    if not isinstance(row, dict):
        return None
    number = _coerce_int(row.get("number"))
    if number is None:
        return None
    kind = str(row.get("kind") or "issue").lower()
    if kind not in WORK_ITEM_KINDS:
        kind = "issue"
    feature_slug = row.get("feature_slug")
    feature_slug = str(feature_slug).strip() if feature_slug else None
    no_fit = row.get("no_fit_suggestion")
    no_fit = str(no_fit).strip() if no_fit else None
    level = str(row.get("confidence_level") or "").lower()
    if level not in _VALID_CONFIDENCE:
        level = "low"  # missing/garbage → conservative default
    return ProposedClassification(
        number=number,
        kind=kind,
        feature_slug=feature_slug or None,
        no_fit_suggestion=no_fit or None,
        reach=_coerce_int(row.get("reach")),
        impact=_coerce_int(row.get("impact")),
        effort=_coerce_int(row.get("effort")),
        confidence=_coerce_int(row.get("confidence")),
        rationale=str(row.get("rationale") or ""),
        confidence_level=level,
    )


def parse_classification_output(text: str) -> list[ProposedClassification]:
    """Extract the JSON array of classifications from a session's output.

    Tolerant: strips an optional ```json fence, finds the first top-level array,
    skips rows that lack a ``number``. Raises ``ClassificationParseError`` only
    when no JSON array can be parsed at all."""
    candidate = text
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ClassificationParseError("no JSON array found in output")
        candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ClassificationParseError(f"invalid JSON array: {e}") from e
    if not isinstance(data, list):
        raise ClassificationParseError("parsed JSON is not an array")
    out: list[ProposedClassification] = []
    for row in data:
        parsed = _row_to_classification(row)
        if parsed is not None:
            out.append(parsed)
    return out
