"""Pure per-stage seed-prompt builders for the SDLC front chain.

Mirrors ``backlog_prompt.build_implement_prompt``: each function returns the first
user prompt for a spawned, non-interactive session that produces one
committed artifact PR. All builders are PURE — no I/O, no model calls — so they
are unit-tested without infrastructure.

The chain (design §3.4): a triage/brainstorm *trigger* seeds ``intent.md``;
``intent.md`` seeds ``spec.md`` (org skills injected as constraints, policy
concerns flagged); ``spec.md`` seeds ``plan.md`` (plan mode). Every session
commits to ``docs/sdlc/<slug>/<kind>.md``, opens a NON-draft PR, and must never
edit the upstream artifacts it reads — v1 is propose-only, so the artifact PR is
the proposal and a human advance is the gate.

Phase 2 (#804) adds the *back* of the loop to this same module rather than a
parallel one: ``build_breach_evidence`` renders a control-band breach, and the
two builders above it turn that evidence into either a read-only diagnosis
session (2σ) or the intake trigger text that becomes ``intent.md`` (3σ).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ccswitch.engines import AgentEngine

from server.sessions.completion import commit_trailer

if TYPE_CHECKING:  # pragma: no cover — typing only, keeps this module import-light
    from server.sdlc.control_bands import Breach

# One place defining the committed-artifact directory (single source of truth).
SDLC_DOCS_DIR = "docs/sdlc"


def artifact_path(slug: str, kind: str) -> str:
    """Repo-relative path of an initiative's committed artifact — the one place
    the intake/synthesizer loops and the prompt builders agree on layout
    (constitution VI: single source of truth)."""
    return f"{SDLC_DOCS_DIR}/{slug}/{kind}.md"


# Internal alias kept for the builders below; ``artifact_path`` is the public name.
_artifact_path = artifact_path


def _common_instructions(slug: str, kind: str, repo: str, default_branch: str,
                         engine: AgentEngine | str = AgentEngine.CLAUDE_CODE) -> str:
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
        f"   `{commit_trailer(engine)}`\n"
    )


def build_intent_prompt(
    trigger: str,
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
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
        + _common_instructions(slug, "intent", repo, default_branch, engine)
    )


def build_spec_prompt(
    intent_md: str,
    skills: list[str],
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
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
        + _common_instructions(slug, "spec", repo, default_branch, engine)
    )


def build_plan_prompt(
    spec_md: str,
    *,
    slug: str,
    repo: str = "",
    default_branch: str = "master",
    engine: AgentEngine | str = AgentEngine.CLAUDE_CODE,
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
        + _common_instructions(slug, "plan", repo, default_branch, engine)
    )


# --------------------------------------------------------------------------- #
# Phase 2 — the Maintain loop (#804)                                            #
#                                                                               #
# A control-band breach is rendered ONCE, by build_breach_evidence, and both     #
# response tiers read that rendering. Two independent descriptions of the same   #
# incident would drift, and the diagnosis a human reads at 2σ has to be the same #
# incident the intent.md at 3σ was written from (constitution VI).               #
# --------------------------------------------------------------------------- #

def build_breach_evidence(breach: "Breach", *, repo: str = "", unit: str = "") -> str:
    """Render a ``control_bands.Breach`` as the evidence block both Maintain-loop
    prompts embed. Pure — every number comes off the breach.

    The rendering states the rule *and* the arithmetic behind it. An agent told
    only "PR cycle time breached 3σ" has to re-derive the detection to know
    whether it agrees; one given the baseline, the σ-distance and the offending
    points can start on the actual question, which is *why*.
    """
    suffix = f" {unit}" if unit else ""
    points = "\n".join(
        f"  - {s.at}: {s.value:.4g}{suffix} "
        f"({breach.baseline.zone(s.value):+.2f}σ)" for s in breach.points)
    return (
        "CONTROL-BAND BREACH (deterministic detection — no model was involved)\n"
        f"- Metric: {breach.metric}{f' in {repo}' if repo else ''}\n"
        f"- Rule: {breach.rule} — breached the {breach.sigma}σ band on the "
        f"{breach.side} side\n"
        f"- Newest observation: {breach.value:.4g}{suffix} at {breach.at} "
        f"({breach.z:+.2f}σ)\n"
        f"- Rolling baseline: mean {breach.baseline.mean:.4g}{suffix}, "
        f"σ {breach.baseline.stddev:.4g}, over n={breach.baseline.n} samples\n"
        f"- Points that fired the rule (oldest first):\n{points}\n"
    )


def build_diagnosis_prompt(
    breach: "Breach",
    *,
    tools: list[str] | tuple[str, ...] = (),
    repo: str = "",
    unit: str = "",
) -> str:
    """Seed prompt for a 2σ **read-only** diagnosis session.

    This tier investigates and reports; it never changes anything. The session's
    transcript IS its deliverable — it is linked to the triage item, the way a
    ``pr_doctor`` session is linked to its PR — so the prompt asks for a
    conclusion in the conversation rather than a file nobody asked for.

    *tools* is the tier's allowlist from ``bands.yaml``, stated as a binding
    constraint. See ``server/sdlc/bands.py`` on what that does and does not
    enforce.
    """
    tools_block = ("\n".join(f"- {t}" for t in tools)
                   if tools else "- (none declared — read-only inspection only)")
    return (
        "You are running the **Maintain** stage of an AI-native SDLC loop, at the "
        "read-only *diagnose* tier. A deterministic control-band monitor detected "
        "the breach below and woke you to explain it. Nobody is watching this "
        "session; finish with a written conclusion.\n\n"
        f"{build_breach_evidence(breach, repo=repo, unit=unit)}\n"
        "Investigate and answer, in this order:\n"
        "1. **Is it real?** Could this be a measurement artifact (a backfill, a\n"
        "   window boundary, one outlier PR) rather than a change in the system?\n"
        "2. **What changed?** Correlate the breach window against merges, releases\n"
        "   and config changes in that period.\n"
        "3. **Most likely cause**, with the evidence for it and your confidence.\n"
        "4. **Recommended response** — and say plainly if the right answer is\n"
        "   \"nothing, this is normal variation\".\n\n"
        "Constraints — this tier is READ-ONLY. You may use only these tools:\n"
        f"{tools_block}\n"
        "- Do NOT edit, create or delete any file.\n"
        "- Do NOT commit, push, open a PR, or comment on GitHub.\n"
        "- Do NOT restart, ship or reconfigure anything.\n"
        "If the fix seems obvious and urgent, say so in your conclusion and stop — "
        "escalating past read-only is a human's call, not yours.\n"
    )


def build_breach_trigger(
    breach: "Breach",
    *,
    repo: str = "",
    unit: str = "",
    title: str = "",
) -> str:
    """The intake **trigger text** for a 3σ breach — the step that closes the loop.

    Deliberately NOT a second intent-writing prompt. It is the ``Trigger.text``
    handed to the existing ``SdlcIntakeLoop``, which already owns the one path
    that turns a trigger into a committed ``intent.md`` PR (constitution VI:
    there is exactly one intent writer, and Phase 2 feeds it rather than forking
    it — ``build_intent_prompt`` wraps this text).

    The regression-eval clause is the design's Phase-3 tie-in (§7: *"each
    incident adds a regression eval"*). It rides on the intent document because
    that is the artifact this incident actually produces — a separate
    eval-writing session would be a second agent reasoning about the same
    incident from less context.
    """
    heading = title or breach.metric
    return (
        f"A control-band monitor watching **{heading}** breached its "
        f"{breach.sigma}σ band and filed this incident automatically. Detection "
        "was deterministic; the judgment is yours.\n\n"
        f"{build_breach_evidence(breach, repo=repo, unit=unit)}\n"
        "Write the intent document for fixing this. Before you do, investigate "
        "the breach window — what merged, what shipped, what changed — and let "
        "what you find drive the Problem section. If your investigation concludes "
        "this is normal variation or a measurement artifact, SAY SO as the "
        "document's Problem and recommend closing the incident: a wrong intent is "
        "more expensive than an honest no-op.\n\n"
        "Additionally, add a regression eval for this incident under "
        "`evals/regressions/` (see `evals/README.md` for the case format: `id`, "
        "`description`, `kind: static`, `check`, `tags`). Its `check` must be a "
        "deterministic shell command that exits non-zero if the condition that "
        "produced this breach can recur. Commit it in the same PR as the intent "
        "document. If no deterministic check is possible for this incident, say "
        "why in the intent's Open questions section instead of inventing a "
        "vacuous one that always passes.\n"
    )
