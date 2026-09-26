"""Pure classification: prompt builders + structured-output schema/parse
(design 2026-08-31 §5). No I/O, no model calls — the classify/map-synthesis
loops feed views in and get a prompt out, or feed session output in and get
``ProposedClassification`` rows out (mapped to TriageProposal by the seam).

Phase 3's continuous triage loop REUSES this module verbatim — there is exactly
one classification implementation (constitution VI).
"""
from __future__ import annotations

from dataclasses import dataclass

from viberpit import brand
from viberpit.engines import AgentEngine

from server.db.models import (
    FEATURE_EVIDENCE_KINDS, FEATURE_LIFECYCLE_STATES, FEATURE_LINK_KINDS, WORK_ITEM_KINDS)
from server.sessions.completion import commit_trailer

from .fenced_json import FencedJsonError, extract_fenced_json

#: Frontmatter lines for a doc's evidence (#2162), shared by every prompt that
#: writes a feature doc. pm-sync indexes these keys into ``feature_evidence``,
#: resolves each path against the default branch, and roadmap health flags
#: dangling paths, live capabilities with no ``tests:``, and ``code:`` with no
#: ``specs:`` — none of which it can do for a path written in prose.
EVIDENCE_FRONTMATTER_HELP = (
    "  code:  [<repo paths implementing it — files, `dir/`, or globs>]  # optional\n"
    "  tests: [<repo paths of the tests that prove it>]  # optional\n"
    "  specs: [<repo paths of its spec/plan docs>]  # optional\n"
    "  adrs:  [<repo paths of the decision records governing it>]  # optional\n"
)

# One place defining the feature-doc directory + path (single source of truth,
# mirrors sdlc.prompts.SDLC_DOCS_DIR / artifact_path). The map-synthesis session
# writes one file per capability under here.
FEATURE_DOCS_DIR = "docs/product/features"

# Open items classified per session (spec §5: ~50/session, structured output).
CLASSIFY_CHUNK_SIZE = 50

# The one confidence vocabulary, ASCENDING — the auto-accept tier gate
# (server/pm/triage_accept.py) orders by index into this tuple.
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")

# Shared capability-surface list (single source of truth — constitution VI).
# Referenced verbatim by BOTH build_map_synthesis_prompt (Step B) and
# build_coverage_critic_prompt so the two agents always audit the same surfaces.
_CENSUS_SURFACES = (
    "- **CLI / entry points:** `[project.scripts]` / `console_scripts` in "
    "pyproject.toml or setup.py, argparse/click/typer subcommands, bin/ "
    "scripts, Makefile targets, package.json `bin` entries\n"
    "- **Web / API routers:** FastAPI/Flask/Express routers, page/tab "
    "registries, GraphQL schema types and resolvers\n"
    "- **Background automation:** scheduler jobs, worker registries, "
    "daemon loops, cron tasks, background task queues\n"
    "- **Data model:** every DB model/table, migration history, "
    "significant data relationships\n"
    "- **Config / settings surface:** env vars, settings files, "
    "config schemas, feature flags\n"
    "- **Top-level library API:** exported public functions/classes "
    "that callers depend on"
)


def feature_doc_path(slug: str) -> str:
    return f"{FEATURE_DOCS_DIR}/{slug}.md"


def map_branch_name(slug: str) -> str:
    """Deterministic branch a map-synthesis PR pushes to, so the completeness
    critic (phase 2) can find and augment the SAME PR. One map per project."""
    return f"pm/feature-map-{slug}"


def constitution_branch_name(slug: str) -> str:
    """Deterministic branch a constitution-proposal PR pushes to (issue #2259).
    Mirrors ``map_branch_name`` — one proposal per project per tap."""
    return f"pm/constitution-{slug}"


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
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
    branch: str | None = None,
) -> str:
    """Seed prompt for the map-synthesis session (spec §5 Step 1 / §6 drift).

    Reads the codebase + mirrored backlog (open+closed) + PR history as
    evidence, writes outcome-driven feature docs, and opens ONE non-draft PR.

    Auto-merge differs by scope, deliberately:

    * ``scope="diff"`` ARMS auto-merge. Full automation is the default; human
      takeover is the exception, signalled by the agent adding a ``hold:human``
      label when its own confidence is low. The former "do not arm — the merge
      is the human curation gate" instruction described a gate that did not
      exist in the target repo (``main`` requiring zero reviews, plus a sweep
      that arms every open non-draft PR), so it only produced false claims in
      PR bodies while the bot merged the PR unreviewed regardless.
    * ``scope="initial"`` does NOT arm, because a coverage-critic session is
      chained after it to add missing capability docs to the SAME PR. Arming
      here would let the map merge before that anti-omission pass runs — and
      the critic can legitimately never spawn. Changing that is a coupled
      decision with ``build_coverage_critic_prompt``, not a wording change.

    ``scope="initial"`` (default) is the whole-repo archaeology prompt.
    ``scope="diff"`` is the diff-scoped drift prompt (phase 3 reuses this one
    builder): it names ``suggestion`` (the proposed new-feature name) and
    ``item_numbers`` (the clustered no-fit items) and asks for a single new/edited
    feature doc plus PR — no full re-archaeology.

    *branch* is the product's OPEN ViberPit-owned map-changes branch (R8b-9 /
    spec §9): when one exists the drift session commits to it, so the map's
    inline edits and the AI's proposal ride one reviewable PR instead of two.
    Without it the diff session opens its own PR on the historical
    ``pm/feature-map-<slug>`` branch, exactly as before. Ignored for
    ``scope="initial"``, which IS the archaeology PR on that branch."""
    if scope == "diff":
        nums = ", ".join(f"#{n}" for n in item_numbers) or "(none listed)"
        target = branch or map_branch_name(slug)
        if branch:
            # The map-changes PR is human-owned from the roadmap UI (Rebase /
            # Abandon) and may carry the human's own in-progress capability
            # edits, so the session appends to it and never merges it.
            branch_step = (
                f"1. The product has an OPEN pull request on the {brand.NAME}-owned "
                f"branch `{target}`. `git fetch` and check that branch out, and "
                f"commit onto it.\n")
            pr_step = (
                "3. That branch's existing pull request carries this change — do "
                "NOT open another pull request.\n")
            merge_step = (
                "4. Do NOT arm auto-merge on it: a human merges or abandons that "
                "pull request from the roadmap, and it may carry their own "
                "in-progress capability edits.\n")
        else:
            branch_step = (
                f"1. Branch off `origin/{default_branch}` (never local "
                f"`{default_branch}`) into a branch named exactly `{target}`.\n")
            pr_step = "3. Open exactly ONE NON-draft PR, marked ready for review.\n"
            merge_step = (
                "4. Arm auto-merge on the PR (`gh pr merge --auto`). Full "
                "automation is the default here — do not leave it unarmed and do "
                "not claim in the PR body that a human curates the feature map.\n")
        return (
            "You are proposing a **feature-map update** for an existing project "
            "whose triage flagged a cluster of items that do not fit the current "
            f"map. Suggested new capability: `{suggestion or 'unnamed'}`. "
            f"Driving items{f' in `{repo}`' if repo else ''}: {nums}.\n\n"
            f"Add or edit ONE feature doc under `{FEATURE_DOCS_DIR}/"
            f"{(suggestion or 'new-feature')}.md` using the outcome-driven "
            "template (Problem / Target users / Desired outcome / Success "
            "metrics / Non-goals / Notes / Evidence). Justify it with the "
            "driving item numbers in the Evidence section and the relevant file "
            "paths in the `code`/`tests`/`specs`/`adrs` frontmatter. Do NOT re-run whole-repo archaeology.\n\n"
            "The doc uses YAML frontmatter with fields:\n"
            "  slug: <kebab-case-identifier>\n"
            "  title: <human name>\n"
            "  state: <lifecycle state>\n"
            "  parent: <slug of the parent capability; omit on a top-level capability>\n"
            "  labels: [<GitHub labels whose issues ALWAYS belong here, e.g. bot-filed>]  # optional\n"
            "  links: [feature-to-feature only; optional]\n"
            + EVIDENCE_FRONTMATTER_HELP + "\n"
            "Never invent other frontmatter keys (`area`, `owner`, `category`): the "
            "indexer reads slug/title/state/parent/labels/links/"
            + "/".join(FEATURE_EVIDENCE_KINDS) + " and silently ignores "
            "the rest. Cite repo paths in the evidence keys, not in body prose — "
            "only the keys are checked for paths that have since moved. The body starts, before `## Problem`, with ONE sentence stating "
            "the outcome this capability delivers — the roadmap shows that sentence as "
            "the summary.\n\n"
            # NOT "propose-only": in this repo that term means "a human merges"
            # (see the SDLC chain), which is the very claim step 4 retires. The
            # surviving guarantee is one of SCOPE — feature docs only — so say
            # that instead of implying a merge gate that no longer exists.
            "Requirements — docs-only, follow exactly:\n"
            + branch_step
            + f"2. Commit only under `{FEATURE_DOCS_DIR}/`.\n"
            + pr_step
            + merge_step
            + "5. Human takeover is the exception, and you decide when to invoke "
            "it: if your own confidence in this mapping is LOW — the capability "
            "may not be real, or the cluster may belong to a doc that already "
            "exists — add the `hold:human` label so a maintainer can take it "
            "over before it lands.\n"
            "6. End every commit message with:\n"
            f"   `{commit_trailer(engine)}`\n"
        )
    repo_clause = f" in repository `{repo}`" if repo else ""
    return (
        "You are performing **product archaeology** for an existing project. "
        "Your goal is to reconstruct the durable *feature map* — what this "
        "product actually is — by working through the five steps below IN ORDER. "
        "Do NOT skip steps or reorder them.\n\n"
        f"CONTEXT: project `{slug}`{repo_clause}. The mirror holds ~{open_count} "
        f"open and ~{closed_count} closed items.\n\n"
        "**ANTI-BIAS RULE (read before starting):** PR-churn signals activity, "
        "not importance. Stable, rarely-churned code (CLI, auth, storage, "
        "isolation primitives, config) is frequently the most fundamental — "
        "include it. A census surface with near-zero recent churn is a reason "
        "to scrutinize for a MISSING foundational feature, not to skip it.\n\n"
        "---\n\n"
        "## Step A — Read authoritative docs FIRST\n\n"
        "Read these sources before looking at any code:\n"
        "- README.md and any README files in sub-directories\n"
        "- CLAUDE.md, AGENTS.md, or equivalent AI-context files\n"
        "- docs/ directory (design docs, ADRs, constitutions, specs)\n"
        "- Any constitution, principles, or governance files\n\n"
        "Extract from these: the product's stated purpose, every named "
        "capability/principle, and any explicitly called-out non-goals. "
        "This is your ground truth — capabilities stated here exist even if "
        "they appear in zero PRs.\n\n"
        "---\n\n"
        "## Step B — Structural capability census (churn-INDEPENDENT)\n\n"
        "Enumerate EVERY surface in the codebase, independent of PR churn. "
        "Each distinct surface cluster is a candidate capability. Cover ALL of:\n\n"
        f"{_CENSUS_SURFACES}\n\n"
        "Record your census findings before moving to Step C. A surface "
        "present here MUST either appear in a feature doc or be explicitly "
        "noted as 'deliberately not a feature' in the coverage table (Step E).\n\n"
        "---\n\n"
        "## Step C — Churn + backlog enrichment (SECONDARY — prioritize, don't discover)\n\n"
        "Use PR-churn concentration, commit scopes, and the open/closed backlog "
        f"(~{open_count} open, ~{closed_count} closed) to:\n"
        "- Assign lifecycle state to capabilities found in Steps A/B\n"
        "- Identify in-flight work and gaps\n"
        "- Prioritize which capabilities deserve their own doc vs. which are "
        "sub-features of a parent\n\n"
        "Do NOT use churn as the discovery mechanism — it is enrichment only. "
        "A capability absent from churn but present in the census (Step B) or "
        "authoritative docs (Step A) still gets a feature doc.\n\n"
        "---\n\n"
        "## Step D — Write feature docs\n\n"
        f"Write one markdown doc per capability under `{FEATURE_DOCS_DIR}/"
        "<feature-slug>.md`. Each doc uses YAML frontmatter with these fields:\n"
        "  slug: <kebab-case-identifier>\n"
        "  title: <human name>\n"
        f"  state: <one of {list(FEATURE_LIFECYCLE_STATES)}>\n"
        "  parent: <slug of the parent capability; omit on a top-level capability>\n"
        "  labels: [<GitHub labels whose issues ALWAYS belong here, e.g. flaky>]  # optional\n"
        "  links:  # optional — feature-to-feature relationships ONLY\n"
        f"    - kind: <one of {list(FEATURE_LINK_KINDS)}>\n"
        "      project: <repo-slug; omit for a capability of this product>\n"
        "      feature: <feature-slug>\n"
        + EVIDENCE_FRONTMATTER_HELP + "\n"
        "Never invent other frontmatter keys (`area`, `owner`, `category`): the "
        "indexer reads slug/title/state/parent/labels/links/"
        + "/".join(FEATURE_EVIDENCE_KINDS) + " and silently ignores "
        "the rest, so a grouping written anywhere else does not exist.\n"
        "Top-level capabilities are the product's standing areas (a family-facing "
        "feature area, a quality bar such as accessibility or security, or the "
        "delivery pipeline); everything else nests under one of them via `parent`. "
        "`labels` is for bot-filed or audit-filed issues (flaky detectors, deploy "
        "audits, CI failures) so they route mechanically and never cluster into a "
        "proposed capability of their own.\n"
        "The body starts, before `## Problem`, with ONE sentence stating the outcome "
        "this capability delivers — the roadmap shows that sentence as the summary.\n\n"
        "IMPORTANT: do NOT put code paths or PR numbers in `links`. Repo paths "
        "go in the `code`/`tests`/`specs`/`adrs` frontmatter lists — pm-sync "
        "checks each still exists; PR/issue numbers go in the **Evidence** body "
        "section.\n\n"
        "The body uses the outcome-driven template:\n"
        "- **Problem** — the concrete pain this capability addresses.\n"
        "- **Target users** — who relies on it.\n"
        "- **Desired outcome** — what good looks like.\n"
        "- **Success metrics** — how you would measure it.\n"
        "- **Non-goals** — what it deliberately excludes.\n"
        "- **Notes** — anything else worth capturing.\n"
        "- **Evidence** — representative issue/PR numbers that justify this "
        "capability's existence (file paths go in the evidence frontmatter).\n"
        "\n"
        "IMPORTANT — frontmatter `links` rules:\n"
        "  • `links` is ONLY for feature-to-feature relationships. It must be a\n"
        "    YAML LIST of `{kind: powered_by|integrates_with, feature: <slug>}`\n"
        "    mappings. Example:\n"
        "      links:\n"
        "        - kind: powered_by\n"
        "          feature: message-queue\n"
        "  • NEVER put code paths, file paths, or PR numbers in `links`. Those\n"
        "    are EVIDENCE — paths go in the `code`/`tests`/`specs`/`adrs`\n"
        "    frontmatter lists, PR numbers in the body's **Evidence** section.\n"
        "  • If there are no feature-to-feature relationships, omit `links`\n"
        "    entirely (do not write `links: {}` or `links: {code: ..., prs: ...}`).\n"
        "Fill only what evidence supports; mark gaps explicitly as "
        "`(unknown — needs owner input)`.\n\n"
        "---\n\n"
        "## Step E — Coverage audit (anti-omission gate)\n\n"
        "After writing the feature docs, produce a coverage table that maps "
        "EVERY surface enumerated in Step B to the feature doc(s) covering it. "
        "Format it as a markdown table with columns: Surface | Type | "
        "Feature Doc(s) | Status.\n\n"
        "For Status use:\n"
        "- `covered` — a feature doc exists for this surface\n"
        "- `sub-feature` — covered as part of a parent feature doc\n"
        "- `not-a-feature` — deliberately excluded (explain why in Status)\n\n"
        "**Put this coverage table in the PR body.** It is the anti-omission "
        "gate: any census surface missing from the table is a gap that must "
        "be resolved before the PR can be merged.\n\n"
        "---\n\n"
        "Requirements — this is a propose-only onboarding step, follow exactly:\n"
        f"1. Branch off `origin/{default_branch}` (never local `{default_branch}`) "
        f"into a branch named exactly `{map_branch_name(slug)}`, and open ONE "
        f"non-draft PR from it.\n"
        f"2. Commit the new docs under `{FEATURE_DOCS_DIR}/` only.\n"
        "3. Open exactly ONE pull request with the whole feature map — a "
        "NON-draft PR, marked ready for review.\n"
        "4. Do NOT arm auto-merge: the merge is the human curation gate; PR "
        "review comments are where the map is argued with.\n"
        "5. End every commit message with:\n"
        f"   `{commit_trailer(engine)}`\n"
    )


def build_coverage_critic_prompt(
    *,
    slug: str,
    repo: str,
    default_branch: str,
    branch: str,
    open_count: int,
    closed_count: int,
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
) -> str:
    """Independent completeness-critic (design §3.2). A FRESH agent, no synthesis
    context, whose only job is to catch capabilities the map omitted — especially
    low-churn foundations (CLI, auth, storage, isolation, config).

    *engine* only selects the commit-attribution trailer, exactly as it does for
    the synthesis prompt: the critic commits and pushes, so it owes the same
    attribution. It shipped with no trailer line at all, which read as "Claude"
    by omission and left every critic commit unattributed on either engine."""
    return (
        f"You are an independent COVERAGE CRITIC for the product `{slug}` "
        f"(repo `{repo}`). A prior session drafted a feature map on the open PR "
        f"branch `{branch}`. Your ONLY job: find capabilities that have NO feature "
        f"doc, and add them — do not rewrite existing docs.\n\n"
        f"1. `git fetch` and check out `{branch}` (the open map PR). Read the "
        f"drafted docs under `docs/product/features/`.\n"
        f"2. Structural capability CENSUS (churn-independent):\n{_CENSUS_SURFACES}\n"
        f"3. For every census surface with no covering feature doc, ADD a feature "
        f"doc (same outcome template + frontmatter, including `parent:`, optional `labels:` and the leading outcome sentence; `links` = feature-to-feature "
        f"only) OR, if it is genuinely not a feature, record it in the PR's "
        f"coverage-audit table as an explicit not-a-feature line.\n"
        f"4. ANTI-BIAS: PR churn signals activity, not importance. Stable, "
        f"rarely-churned code (CLI, auth, storage, isolation primitives, config) "
        f"is frequently the MOST fundamental — a near-zero-churn surface with no "
        f"doc is a reason to ADD one, not to skip it.\n"
        f"5. Commit your additions and PUSH to `{branch}` to update the SAME PR. "
        f"DO NOT open a new pull request. DO NOT arm auto-merge. Leave the PR for "
        f"human review.\n"
        f"6. End every commit message with:\n"
        f"   `{commit_trailer(engine)}`\n"
    )


# The status-specific opening line, keyed on
# `server.sdlc.constitution_status.classify_document`'s vocabulary. Kept as a
# plain dict rather than importing that module — this one is a display string,
# not the classification itself, and pm/classification.py stays free of a
# server.sdlc import.
_CONSTITUTION_STATUS_OPENING = {
    "missing": "has no governance document at all",
    "template": "still carries the unfilled spec-kit template",
    "no_version_footer": "is missing a parseable Version/Ratified/Last Amended footer",
}


def build_constitution_proposal_prompt(
    *,
    slug: str,
    repo: str,
    default_branch: str,
    constitution_status: str | None = None,
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
) -> str:
    """Seed prompt for the constitution-proposal session (issue #2259, item 2 of
    #2165). Offered from the onboarding wizard once ``Project.constitution_status``
    is non-``None`` (missing / template / no_version_footer) — i.e. there is an
    actual governance gap to propose fixing.

    Drafts ``.specify/memory/constitution.md`` from the repo's own ADRs,
    AGENTS.md (or equivalent AI-context files), and any spec-kit templates
    already in the repo, and opens ONE non-draft PR. Propose-only, the same
    shape as ``build_map_synthesis_prompt``'s ``scope="initial"``: a human
    reviews and merges — auto-merge is never armed, because ratifying a repo's
    own governance document is exactly the kind of change
    ``constitution_gate.check_amendment`` escalates to the owner."""
    repo_clause = f" in repository `{repo}`" if repo else ""
    branch = constitution_branch_name(slug)
    gap = _CONSTITUTION_STATUS_OPENING.get(
        constitution_status or "", "needs review")
    return (
        "You are proposing a **repository constitution** for an existing "
        f"project{repo_clause}. Its governance document at "
        f"`.specify/memory/constitution.md` currently {gap} — draft or "
        "complete one.\n\n"
        "Sources to draw from, in order of authority:\n"
        "- Architecture Decision Records (ADRs) anywhere in the repo "
        "(commonly under `docs/adr/`, `docs/decisions/`, or similar)\n"
        "- CLAUDE.md, AGENTS.md, or equivalent AI-context files — these "
        "usually already state governance rules in prose\n"
        "- Any spec-kit templates already in the repo (e.g. under "
        "`.specify/templates/` or `.specify/memory/`), which give the "
        "expected structure (Principles, Governance, versioning footer)\n\n"
        "Write `.specify/memory/constitution.md` with numbered Principles "
        "(each a short name plus a concrete, testable rule — not vague "
        "aspiration), a Governance section describing how amendments are "
        "reviewed, and a footer of the form `**Version**: X.Y.Z | "
        "**Ratified**: <date> | **Last Amended**: <date>` so "
        "`constitution_gate.parse_footer` can read it. Ground every "
        "principle in something this repo actually does — cite the file "
        "paths or ADRs that justify it. Do not invent principles the repo "
        "has no evidence for.\n\n"
        "Requirements — follow exactly:\n"
        f"1. Branch off `origin/{default_branch}` (never local "
        f"`{default_branch}`) into a branch named exactly `{branch}`.\n"
        "2. Commit only `.specify/memory/constitution.md` (and, if you "
        "formalize one into a principle, an ADR you create to record it).\n"
        "3. Open exactly ONE non-draft PR, marked ready for review.\n"
        "4. Do NOT arm auto-merge: a project's own governance document is a "
        "human decision, and the PR review is where it is argued with.\n"
        "5. End every commit message with:\n"
        f"   `{commit_trailer(engine)}`\n"
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
    if level not in CONFIDENCE_LEVELS:
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

    Tolerant: strips an optional ```json fence, finds the top-level array,
    skips rows that lack a ``number``. Raises ``ClassificationParseError`` only
    when no JSON array can be parsed at all. Extraction is the shared
    ``fenced_json`` helper — the materialization parser reads object payloads
    with the same tolerance (constitution VI)."""
    try:
        data = extract_fenced_json(text, shape="array")
    except FencedJsonError as e:
        raise ClassificationParseError(str(e)) from e
    out: list[ProposedClassification] = []
    for row in data:
        parsed = _row_to_classification(row)
        if parsed is not None:
            out.append(parsed)
    return out
