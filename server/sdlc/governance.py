"""Pure SDLC governance core — the control plane that must precede autonomy.

Phase 3 of the AI-Native SDLC design (§7/§9,
`docs/superpowers/specs/2026-08-29-ai-native-sdlc-design.md`). Like
``server.sdlc.engine``, this module is **pure** — no I/O, no DB, no orchestrator —
so every rule is exhaustively unit-testable. The enforcement wiring lives outside
it: ``.claude/settings.json`` hooks call ``evaluate_release``; CI evals assert the
managed non-negotiables are present; the session spawner reads ``permission_tier``.

Three governance surfaces, one per issue #805 checklist item:

1. **Permission tiers by environment** — ``permission_tier`` maps an environment
   to an existing ccswitch session mode (dev = ``full-auto`` · staging =
   ``acceptEdits`` · prod = ``interactive``). It does NOT invent a new permission
   engine: each returned mode is a real ``VALID_SESSION_MODES`` value already
   understood by ``PermissionRegistry.request`` (constitution VI).
2. **Approval gate** — ``evaluate_release`` blocks a prod release unless
   ``RELEASE_APPROVAL`` is set AND the approver differs from the change author
   (separation of duties, design §4 step 6). It returns a clear reason and the
   approval route so a hook can surface both.
3. **Managed non-negotiables** — ``MANAGED_DENY`` is the version-controlled set of
   deny rules that ``.claude/managed-settings.json`` must always carry (constitution
   IV); ``missing_managed_rules`` lets CI prove none were dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from server.sessions.types import VALID_SESSION_MODES

# --------------------------------------------------------------------------- #
# Environments                                                                  #
# --------------------------------------------------------------------------- #

Environment = Literal["dev", "staging", "prod"]

# Ordered least- to most-restricted. ``prod`` is the only gated tier.
ENVIRONMENTS: tuple[Environment, ...] = ("dev", "staging", "prod")

# The permission-tier ladder. Each value is a real ccswitch session mode (see
# server.sessions.types.VALID_SESSION_MODES) — this is a *policy* over the
# existing permission engine, not a new one:
#   dev         → full-auto   (approve every tool, incl. shell — fast local loop)
#   staging     → acceptEdits (auto-accept file edits; shell/network still gated)
#   prod        → interactive (ask a human for every tool — nothing is automatic)
_TIER_BY_ENV: dict[str, str] = {
    "dev": "full-auto",
    "staging": "acceptEdits",
    "prod": "interactive",
}


def permission_tier(environment: str) -> str:
    """The ccswitch session mode for *environment*'s permission tier.

    Pure + total. Raises ``ValueError`` on an unknown environment so a typo
    surfaces loudly instead of silently falling through to a permissive default.
    The returned mode is guaranteed to be in ``VALID_SESSION_MODES`` — the tier
    ladder can never drift into a mode the permission engine doesn't understand.
    """
    try:
        mode = _TIER_BY_ENV[environment]
    except KeyError:
        raise ValueError(
            f"unknown environment {environment!r}; expected one of {', '.join(ENVIRONMENTS)}"
        ) from None
    # Invariant guard: keep the ladder honest against the canonical vocabulary.
    assert mode in VALID_SESSION_MODES, f"tier {mode!r} not a valid session mode"
    return mode


def describe_tiers() -> dict[str, str]:
    """A copy of the full environment→mode ladder (for API/UX display)."""
    return dict(_TIER_BY_ENV)


# --------------------------------------------------------------------------- #
# Approval gate (prod release)                                                  #
# --------------------------------------------------------------------------- #

# The one place the approval-route guidance is phrased, so the hook, the API, and
# the tests never drift on what a human must do to unblock a prod release.
APPROVAL_ROUTE = (
    "A human must approve the release, then set RELEASE_APPROVAL=<change/incident ref> "
    "in the deploy environment. The approver MUST be a different identity than the "
    "agent/author that produced the change (separation of duties)."
)


@dataclass(frozen=True)
class ReleaseDecision:
    """Outcome of an approval-gate check. ``reason`` explains the decision;
    ``route`` is the (non-empty only when blocked) path to unblock it."""
    allowed: bool
    reason: str
    route: str = ""


def evaluate_release(
    *,
    environment: str,
    release_approval: str | None,
    author: str | None = None,
    approver: str | None = None,
) -> ReleaseDecision:
    """Decide whether a release to *environment* may proceed. Pure + total.

    Non-prod environments are never gated. A prod release is allowed only when:

    - ``release_approval`` is a non-empty token (``RELEASE_APPROVAL`` was set), and
    - the ``approver`` is not the same identity as the change ``author``
      (separation of duties — the agent that wrote the change cannot authorize its
      own release, design §4 step 6).

    When ``author``/``approver`` are unknown (``None``), the separation-of-duties
    check is skipped — a bare ``RELEASE_APPROVAL`` still passes so a human running a
    manual deploy is not blocked, but the hook that has identity context enforces it.
    """
    if environment != "prod":
        return ReleaseDecision(
            allowed=True,
            reason=f"{environment} is not a gated environment",
        )
    if not (release_approval or "").strip():
        return ReleaseDecision(
            allowed=False,
            reason="prod release requires RELEASE_APPROVAL (unset or empty)",
            route=APPROVAL_ROUTE,
        )
    if author is not None and approver is not None and author == approver:
        return ReleaseDecision(
            allowed=False,
            reason=(
                f"separation of duties: the release author ({author!r}) cannot also "
                "approve their own prod release"
            ),
            route=APPROVAL_ROUTE,
        )
    return ReleaseDecision(
        allowed=True,
        reason="prod release approved (RELEASE_APPROVAL set; approver distinct from author)",
    )


# --------------------------------------------------------------------------- #
# Managed non-negotiables (constitution IV)                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ManagedRule:
    """A non-overridable deny rule. ``pattern`` is a Claude-Code permission
    pattern (e.g. ``Bash(git push --force*)``); ``principle`` cites the
    constitution clause it enforces; ``why`` is the human-readable rationale."""
    pattern: str
    principle: str
    why: str


# Repo-relative paths no agent may edit, by ANY route: a native edit tool, a
# Codex ``apply_patch`` envelope, or a shell command. This tuple is the single
# source of truth — ``MANAGED_DENY`` derives its ``Write``/``Edit`` patterns from
# it, and ``.claude/hooks/prod-deploy-gate.py`` imports it to gate ``apply_patch``
# and ``Bash``. Adding a path here extends every enforcement layer at once.
PROTECTED_EDIT_PATHS: tuple[str, ...] = (
    ".specify/memory/constitution.md",
    ".claude/managed-settings.json",
)

# Why each protected path is protected, cited back to the constitution.
_PROTECTED_PATH_RATIONALE: dict[str, tuple[str, str]] = {
    ".specify/memory/constitution.md": (
        "constitution IV/VI",
        "the constitution is amended by deliberate human process, not an agent edit.",
    ),
    ".claude/managed-settings.json": (
        "constitution IV",
        "managed non-negotiables are non-overridable; an agent cannot rewrite its own guardrails.",
    ),
}


def _protected_path_rules() -> tuple[ManagedRule, ...]:
    """One ``Write``/``Edit`` deny rule per protected path, in declaration order."""
    rules: list[ManagedRule] = []
    for path in PROTECTED_EDIT_PATHS:
        principle, why = _PROTECTED_PATH_RATIONALE[path]
        rules.extend(
            ManagedRule(pattern=f"{tool}({path})", principle=principle, why=why)
            for tool in ("Write", "Edit")
        )
    return tuple(rules)


# The non-negotiables a managed (non-overridable) settings file must always deny.
# These encode constitution Principles I (never fork/patch a native agent binary),
# IV (security-first) and the git-safety working norm. CI asserts every pattern
# is present in ``.claude/managed-settings.json`` so a local settings.json can
# never quietly relax them.
#
# NOTE: these patterns match Claude Code's *tool* invocations only. A shell write
# to the same path (``sed -i``, ``cat >``, ``tee``) matches no pattern here and is
# not expressible as one — the enumeration of shell idioms is unbounded. That
# route is gated by ``.claude/hooks/prod-deploy-gate.py`` instead, which sees the
# whole command, and ultimately by the constitution CI gate, which inspects the
# resulting diff rather than guessing how it was produced.
MANAGED_DENY: tuple[ManagedRule, ...] = (
    ManagedRule(
        pattern="Bash(git push --force:*)",
        principle="working-norm: git safety",
        why="force-push can clobber a sibling worktree's pushed commits; use --force-with-lease.",
    ),
    ManagedRule(
        pattern="Bash(git push -f:*)",
        principle="working-norm: git safety",
        why="short form of --force; same clobber risk.",
    ),
    *_protected_path_rules(),
)


def managed_deny_patterns() -> list[str]:
    """The ordered list of deny patterns a managed settings file must carry."""
    return [rule.pattern for rule in MANAGED_DENY]


def missing_managed_rules(settings: dict) -> list[str]:
    """Managed deny patterns absent from *settings* (a parsed settings.json dict).

    ``settings["permissions"]["deny"]`` is the list Claude Code enforces. Any
    ``MANAGED_DENY`` pattern not present is returned — an empty list means the
    file honours every non-negotiable. Pure; used by the CI drift guard.
    """
    deny = set(((settings.get("permissions") or {}).get("deny")) or [])
    return [p for p in managed_deny_patterns() if p not in deny]
