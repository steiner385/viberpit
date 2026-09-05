"""The one place that decides which agent engine an automation may launch.

Phase 7 of the native-Codex plan makes automations provider-neutral. The risk it
has to contain is narrow and specific: an automation session runs unattended, in
``full-auto``, with git/gh/pytest — so pointing one at an engine whose prompt
contract, completion markers, approval semantics and quota handling have not been
exercised end-to-end is not a degraded experience, it is an unsupervised agent
acting on a contract nobody verified. Hence a per-origin **ceiling**:

``claude_only``    the origin has no Codex acceptance evidence (every origin, today)
``codex_allowed``  the origin passed its Codex acceptance suite; an operator may opt in
``codex_only``     the origin is audited for Codex ALONE

There is deliberately no implicit ``any``. :func:`policy_for` answers
``claude_only`` for an automation that forgot to declare one, so the failure mode
of an oversight is "runs Claude", never "runs an unaudited engine".

**Two gates, not one.** The ceiling is declared in code (a class attribute, so
widening it is a reviewable diff in the automation that earned it) and the
operator's opt-in lives in the automation's ordinary ``config`` JSON:

1. :func:`engine_config_fields` gives a ``claude_only`` origin *no* ``engine``
   field at all, so ``validate_config`` rejects ``{"engine": "codex"}`` as an
   unknown key and the API answers 422. The UI renders the selector only where it
   exists, from the same schema.
2. :func:`resolve_engine` clamps again at spawn time, because a config row can be
   written by something other than the API (a migration, psql, an older build).

Storing the opt-in in the existing config JSON — rather than a new ``Automation``
column — is deliberate: that JSON *is* the automation configuration row, it is
already validated, already patched through one endpoint, and already rendered
generically by the browser. A parallel persisted ceiling beside the code-declared
one would be two sources of truth for one concept.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from ccswitch.engines import AgentEngine, InvalidAgentEngine, parse_agent_engine

from .base import ConfigField, ConfigSchema

log = logging.getLogger("ccswitch.automations")

#: Config key holding the operator's engine opt-in for an origin whose ceiling
#: admits more than one engine.
ENGINE_CONFIG_KEY = "engine"

#: Codex launch controls. A model and its reasoning effort are ONE selection that
#: only the live per-account catalog can validate (``server/codex/catalog.py``),
#: so these are free-text rather than a frozen enum that would go stale. Blank
#: means "let the engine pick its own default".
CODEX_MODEL_CONFIG_KEY = "codex_model"
CODEX_EFFORT_CONFIG_KEY = "codex_reasoning_effort"


class EnginePolicy(StrEnum):
    """How far one automation origin's audited engine ceiling reaches."""

    CLAUDE_ONLY = "claude_only"
    CODEX_ALLOWED = "codex_allowed"
    CODEX_ONLY = "codex_only"


DEFAULT_ENGINE_POLICY = EnginePolicy.CLAUDE_ONLY


class EnginePolicyViolation(RuntimeError):
    """The only engine this origin is audited for cannot be launched right now.

    Raised instead of falling back, because every fallback available at that
    point runs an engine this origin has no acceptance evidence for.
    """

    def __init__(self, policy: EnginePolicy, reason: str) -> None:
        self.policy = policy
        self.reason = reason
        super().__init__(f"engine policy {policy.value}: {reason}")


def parse_engine_policy(value: EnginePolicy | str | None) -> EnginePolicy:
    """Parse a policy name. Raises ``ValueError`` on anything unrecognized —
    there is no permissive default at a parse boundary."""
    if isinstance(value, EnginePolicy):
        return value
    if not isinstance(value, str):
        raise ValueError(f"unknown engine policy: {value!r}")
    try:
        return EnginePolicy(value)
    except ValueError:
        raise ValueError(f"unknown engine policy: {value!r}") from None


def policy_for(automation: Any) -> EnginePolicy:
    """The declared ceiling of *automation*, defaulting to ``claude_only``.

    An automation that never declared one is unaudited by definition, so the
    default is the narrowest policy rather than an error: a new automation must
    not be able to fail *open*, and it must not fail to load either.
    """
    declared = getattr(automation, "engine_policy", None)
    if declared is None:
        return DEFAULT_ENGINE_POLICY
    try:
        return parse_engine_policy(declared)
    except ValueError:
        log.warning("automation %s declares an unknown engine_policy %r; "
                    "treating it as %s", getattr(automation, "id", "?"), declared,
                    DEFAULT_ENGINE_POLICY.value)
        return DEFAULT_ENGINE_POLICY


def audited_policy_map() -> dict[str, EnginePolicy]:
    """``{automation_id: policy}`` for every registered origin.

    The reviewable answer to "which origins may run Codex?" — one call, asserted
    whole in ``test_automation_engine_policy.py`` so widening any origin's
    ceiling cannot land without showing up in that assertion.
    """
    from .registry import registry

    return {a.id: policy_for(a) for a in registry.all()}


def allowed_engines(policy: EnginePolicy) -> tuple[AgentEngine, ...]:
    """The engines *policy* admits, in the order they are offered."""
    if policy is EnginePolicy.CODEX_ONLY:
        return (AgentEngine.CODEX,)
    if policy is EnginePolicy.CODEX_ALLOWED:
        return (AgentEngine.CLAUDE_CODE, AgentEngine.CODEX)
    return (AgentEngine.CLAUDE_CODE,)


def default_engine(policy: EnginePolicy) -> AgentEngine:
    """The engine an origin runs when its config says nothing.

    ``codex_allowed`` still defaults to Claude: passing an acceptance suite
    earns an origin the *option*, and moving its live traffic onto a new engine
    is a separate, explicit operator decision.
    """
    return allowed_engines(policy)[0]


def engine_config_fields(policy: EnginePolicy) -> list[ConfigField]:
    """The engine-selection config fields an origin with *policy* offers.

    Empty for ``claude_only`` — there is nothing to choose, and the absence is
    itself the enforcement (``validate_config`` rejects unknown keys).
    """
    engines = allowed_engines(policy)
    if len(engines) == 1 and engines[0] is AgentEngine.CLAUDE_CODE:
        return []
    return [
        ConfigField(
            key=ENGINE_CONFIG_KEY, label="Agent engine", type="enum",
            default=default_engine(policy).value,
            choices=[e.value for e in engines],
            help="Which native agent runs this automation's unattended sessions. "
                 "Only engines this origin has passed an acceptance suite for are "
                 "offered."),
        ConfigField(
            key=CODEX_MODEL_CONFIG_KEY, label="Codex model", type="string", default="",
            help="Model slug for Codex spawns (blank = the account's own default). "
                 "Ignored when the engine is Claude."),
        ConfigField(
            key=CODEX_EFFORT_CONFIG_KEY, label="Codex reasoning effort",
            type="string", default="",
            help="Reasoning effort for Codex spawns (blank = the model's default). "
                 "The accepted pairs come from the account's live model catalog."),
    ]


def effective_config_schema(automation: Any) -> ConfigSchema:
    """*automation*'s own schema plus whatever its engine ceiling adds.

    THE schema for an automation — the API validates against it, the API
    publishes it, and ``read_automation_config`` reads through it — so a
    persisted ``engine`` key can never be valid in one of those and unknown in
    another.
    """
    return [*automation.config_schema, *engine_config_fields(policy_for(automation))]


def resolve_engine(policy: EnginePolicy, config: dict, *,
                   automations_enabled: bool) -> AgentEngine:
    """The engine an origin with *policy* and *config* launches, clamped.

    *automations_enabled* is ``CCSWITCH_CODEX_AUTOMATIONS_ENABLED``: the global
    kill switch, so an operator can stop every Codex automation without unwinding
    each origin's config. For ``codex_only`` the switch cannot downgrade — it
    raises, and the spawn is refused (see :class:`EnginePolicyViolation`).

    Every out-of-policy or unparseable request resolves to the policy default
    rather than raising, so a bad config row degrades a spawn instead of wedging
    a control loop.
    """
    allowed = allowed_engines(policy)
    requested_raw = config.get(ENGINE_CONFIG_KEY)
    requested: AgentEngine | None = None
    if requested_raw is not None:
        try:
            requested = parse_agent_engine(requested_raw)
        except InvalidAgentEngine:
            log.warning("automation config names an unknown engine %r; using %s",
                        requested_raw, default_engine(policy).value)
    if requested is not None and requested not in allowed:
        if policy is EnginePolicy.CODEX_ONLY:
            raise EnginePolicyViolation(
                policy, f"config requests {requested.value}, which this origin is "
                        "not audited for")
        log.warning("automation config requests engine %s outside policy %s; using %s",
                    requested.value, policy.value, default_engine(policy).value)
        requested = None
    engine = requested or default_engine(policy)
    if engine is AgentEngine.CODEX and not automations_enabled:
        if policy is EnginePolicy.CODEX_ONLY:
            raise EnginePolicyViolation(
                policy, "CCSWITCH_CODEX_AUTOMATIONS_ENABLED is false and this "
                        "origin has no Claude acceptance evidence to fall back on")
        return AgentEngine.CLAUDE_CODE
    return engine


def unattended_launch_blocker(engine: AgentEngine, permission_mode: str) -> str | None:
    """Why an unattended *engine* session in *permission_mode* cannot finish an
    automation's own contract — or ``None`` when nothing blocks it.

    Every automation origin ends its seed prompt the same way: branch, commit,
    **push**, open a PR. That is not one automation's style, it is what an
    automation *is* here — a control loop whose only durable output is a PR. So a
    launch whose sandbox cannot reach the network is not a degraded run, it is a
    run that is guaranteed to do all the work and then fail at the last step,
    having consumed quota, a concurrency slot and an attempt.

    Codex's sandbox is network-isolated in every mode except ``bypassPermissions``
    (see ``server.codex.permissions.permits_network_egress`` — a fact about the
    pinned app-server contract, not a ccswitch setting). Claude has no sandbox at
    all, so nothing blocks it and this is a no-op for the engine running today.

    Checked at launch rather than encoded as "automations must use mode X"
    because the mode is resolved from the operator's own account default
    (:func:`~server.automations.context.resolve_permission_mode`); the two can
    disagree, and the disagreement has to be reported, not assumed away.
    """
    if engine is not AgentEngine.CODEX:
        return None
    from server.codex.permissions import (
        UnsupportedCodexPermissionMode,
        permits_network_egress,
    )

    try:
        if permits_network_egress(permission_mode):
            return None
    except UnsupportedCodexPermissionMode:
        return (f"permission mode {permission_mode!r} has no Codex sandbox "
                "profile, so the authority an unattended session would run with "
                "is undefined")
    return (f"Codex in permission mode {permission_mode!r} runs in a "
            "network-isolated sandbox, so the session could not push a branch or "
            "open a pull request — the only output an automation has. Grant a "
            "mode whose sandbox permits network egress, or leave this origin on "
            "Claude.")


def _default_provider_of(name: str) -> str | None:
    from ccswitch import accounts as _accts

    for account_name, provider in _accts.all_accounts():
        if account_name == name:
            return provider
    return None


def sanitize_launch_for_engine(
    engine: AgentEngine,
    create_kwargs: dict,
    *,
    config: dict,
    provider_of: Callable[[str], str | None] | None = None,
) -> tuple[dict, tuple[str, ...]]:
    """Strip engine-incompatible launch inputs and apply the engine's own.

    Returns ``(kwargs, dropped_keys)``; *kwargs* is mutated in place and also
    returned so callers can chain.

    The mis-route this exists to stop is quiet and plausible: automation spawn
    sites resolve a *Claude* account (``_backlog_healthy_account``) and a *Claude*
    model alias (``model_pick.resolve_model``, a Haiku complexity assessor) before
    the engine is known. Handing either to a Codex runner would pin a Codex
    session to an Anthropic credential, or ask the app-server for ``"opus"``.
    Task 7.1 requires the audit be explicit, so the drop is explicit and reported
    rather than tolerated downstream.

    Fails closed on an unreadable account provider: an account we cannot classify
    is not assumed to be an OpenAI one.
    """
    if engine is not AgentEngine.CODEX:
        return create_kwargs, ()

    dropped: list[str] = []
    lookup = provider_of or _default_provider_of
    account = create_kwargs.get("account")
    if account:
        try:
            provider = lookup(str(account))
        except Exception:
            log.warning("automation: account-provider lookup failed for %s; "
                        "dropping it from a Codex launch", account, exc_info=True)
            provider = None
        if provider != "openai":
            create_kwargs["account"] = None
            dropped.append("account")

    if create_kwargs.pop("model", None) is not None:
        dropped.append("model")
    codex_model = str(config.get(CODEX_MODEL_CONFIG_KEY) or "").strip()
    if codex_model:
        create_kwargs["model"] = codex_model
    codex_effort = str(config.get(CODEX_EFFORT_CONFIG_KEY) or "").strip()
    if codex_effort:
        create_kwargs["reasoning_effort"] = codex_effort
    return create_kwargs, tuple(dropped)
