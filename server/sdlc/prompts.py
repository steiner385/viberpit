"""Pure per-stage seed-prompt builders for the SDLC front chain.

Mirrors ``backlog_prompt.build_implement_prompt``: each function returns the first
user prompt for a spawned, non-interactive Claude session that produces one
committed artifact PR. All builders are PURE — no I/O, no model calls — so they
are unit-tested without infrastructure.

The chain (design §3.4): a triage/brainstorm *trigger* seeds ``intent.md``;
``intent.md`` seeds ``spec.md`` (org skills injected as constraints, policy
concerns flagged); ``spec.md`` seeds ``plan.md`` (plan mode). Every session
commits to ``docs/sdlc/<slug>/<kind>.md``, opens a NON-draft PR, and must never
edit the upstream artifacts it reads — v1 is propose-only, so the artifact PR is
the proposal and a human advance is the gate.
"""
from __future__ import annotations

# One place defining the committed-artifact directory (single source of truth).
SDLC_DOCS_DIR = "docs/sdlc"


def _artifact_path(slug: str, kind: str) -> str:
    return f"{SDLC_DOCS_DIR}/{slug}/{kind}.md"


def _common_instructions(slug: str, kind: str, repo: str, default_branch: str) -> str:
    """The shared trailer every stage session must obey. Keeping it in one helper
    avoids N parallel copies drifting apart (constitution VI)."""
    path = _artifact_path(slug, kind)
    repo_clause = f" in repository `{repo}`" if repo else ""
    return (
        "Requirements — this is a propose-only SDLC stage, follow them exactly:\n"
        f"1. Write the artifact to `{path}`{repo_clause} and commit only that file.\n"
        f"2. Branch off `origin/{default_branch}` (never local `{default_branch}`).\n"
        "3. Open ONE pull request for the artifact — a NON-draft PR, marked ready for review.\n"
        "4. Do NOT arm auto-merge: v1 is propose-only, so a human advance is the gate that\n"
        "   merges this artifact and unlocks the next stage.\n"
        "5. NEVER edit the upstream artifacts you were given — they are read-only inputs.\n"
        "   Add a new file; do not modify intent.md/spec.md from an earlier stage.\n"
        "6. End every commit message with:\n"
        "   `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`\n"
    )


def build_intent_prompt(
    trigger: str,
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
) -> str:
    """Seed prompt for the Intake session: a brainstorm/triage *trigger* → ``intent.md``.

    Instructs the agent to capture the problem, desired outcome, affected systems,
    constraints, and open questions.
    """
    return (
        "You are running the **Plan** stage of an AI-native SDLC loop. Turn the "
        "trigger below into a crisp intent document — the shared currency every "
        "later stage reads.\n\n"
        "TRIGGER (a brainstorm or triage item):\n"
        f"{trigger}\n\n"
        f"Produce `{_artifact_path(slug, 'intent')}` with these sections:\n"
        "- **Problem** — the concrete pain, in the user's terms.\n"
        "- **Desired outcome** — what \"done\" looks like and how we would know.\n"
        "- **Affected systems** — components/services this likely touches.\n"
        "- **Constraints** — governance, security, compatibility, scope limits.\n"
        "- **Open questions** — what must be resolved before a spec is written.\n\n"
        + _common_instructions(slug, "intent", repo, default_branch)
    )


def build_spec_prompt(
    intent_md: str,
    skills: list[str],
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
) -> str:
    """Seed prompt for the Synthesizer (Design): ``intent.md`` → ``spec.md``.

    The org ``skills`` are injected as binding constraints, and the session must
    explicitly flag any policy concerns as spec constraints.
    """
    if skills:
        skills_block = "\n".join(f"- {name}" for name in skills)
    else:
        skills_block = "- (no org skills supplied — apply the repository constitution)"
    return (
        "You are running the **Design** stage of an AI-native SDLC loop. Turn the "
        "approved intent below into a specification.\n\n"
        "UPSTREAM INTENT (read-only — do not edit intent.md):\n"
        "-----8<----- intent.md -----8<-----\n"
        f"{intent_md}\n"
        "-----8<----------------------8<-----\n\n"
        "ORG SKILLS — treat each as a binding constraint the spec must satisfy:\n"
        f"{skills_block}\n\n"
        f"Produce `{_artifact_path(slug, 'spec')}` covering: functional behavior, "
        "acceptance criteria, data/API surface, and non-goals. Explicitly **flag "
        "any policy concerns** (security, governance, ToS, data handling) as called-"
        "out spec constraints so a human can resolve them at the gate.\n\n"
        + _common_instructions(slug, "spec", repo, default_branch)
    )


def build_plan_prompt(
    spec_md: str,
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
) -> str:
    """Seed prompt for the Synthesizer (Plan, plan mode): ``spec.md`` → ``plan.md``.

    The plan names the files to touch, the order of work, the tests, and the risks.
    """
    return (
        "You are running the **Plan-mode** step of an AI-native SDLC loop. Turn the "
        "approved spec below into an implementation plan. Use plan mode: reason about "
        "the change without editing product code.\n\n"
        "UPSTREAM SPEC (read-only — do not edit spec.md or intent.md):\n"
        "-----8<----- spec.md -----8<-----\n"
        f"{spec_md}\n"
        "-----8<--------------------8<-----\n\n"
        f"Produce `{_artifact_path(slug, 'plan')}` with:\n"
        "- **Files** — every file to add/change, with a one-line purpose each.\n"
        "- **Order** — the sequence of steps (what must land before what).\n"
        "- **Tests** — the tests that prove each step, TDD-first where possible.\n"
        "- **Risks** — what could go wrong and the mitigation for each.\n\n"
        + _common_instructions(slug, "plan", repo, default_branch)
    )
