"""Per-automation session-model selection: the `default_model` config contract
plus the "auto" complexity assessor.

Spawning automations (backlog-runner, pr-doctor) carry a `default_model` enum
config whose choices are ``AUTOMATION_MODEL_CHOICES`` — the model-catalog
aliases (the binary's own `/model` picker list, single source of truth) with
``"auto"`` prepended. ``resolve_model`` maps the persisted value to the
``model=`` kwarg a spawn passes to ``create_session``:

- ``"default"`` → ``None`` (account default — pre-feature behavior)
- any catalog alias → passed through as-is
- ``"auto"`` → one-shot Haiku assessment of the job summary picks a tier from
  ``AUTO_MODEL_POOL``

Fail-open invariant: the assessor can only ever REFINE a spawn, never block or
fail one. Any exception, empty reply, or out-of-pool reply resolves to the
account default.
"""
from __future__ import annotations

import logging
import os

from server.sessions.model_catalog import ALIASES

log = logging.getLogger("ccswitch.automations")

# Tiers "auto" chooses between, cheapest first (user-confirmed pool).
AUTO_MODEL_POOL: list[str] = ["haiku", "sonnet", "opus", "fable"]

# The `default_model` dropdown contents: "auto" + every alias the binary's
# /model picker offers (ALIASES already leads with "default"). Deriving from
# the catalog keeps one source of truth — a new alias there appears in every
# automation dropdown automatically.
AUTOMATION_MODEL_CHOICES: list[str] = ["auto", *ALIASES]

_ASSESS_SYSTEM = (
    "You are a task-complexity assessor for a coding-agent fleet. You receive "
    "a short summary of a job about to be dispatched to a Claude coding "
    "session and reply with the cheapest model tier that can complete it "
    "reliably:\n"
    "- haiku: trivial/mechanical work — typo or copy fixes, config bumps, "
    "single-line changes, dependency pins.\n"
    "- sonnet: a typical single-concern change — one feature or bugfix with "
    "its tests, limited blast radius.\n"
    "- opus: hard multi-file work — cross-module changes, tricky debugging, "
    "CI failures with unclear cause, merge-conflict resolution.\n"
    "- fable: the hardest architectural or cross-cutting work — large "
    "refactors, subtle concurrency/data-integrity issues, multi-PR "
    "consolidation."
)


def _default_sdk_query_fn():
    from claude_agent_sdk import query  # lazy import — not available in tests
    return query


async def assess_model(
    task_summary: str,
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
) -> str | None:
    """One-shot Haiku query mapping *task_summary* to a tier in AUTO_MODEL_POOL.

    Mirrors ``backlog_runner.rank_issues``: tool-less one-shot query with an
    injected ``_query_fn`` seam so tests never call the real model. Returns the
    picked alias, or ``None`` on ANY failure (exception, empty or out-of-pool
    reply) so the caller falls back to the account default.
    """
    if not task_summary.strip():
        return None

    body = (
        f"Job summary:\n{task_summary}\n\n"
        f"Reply with EXACTLY one of: {', '.join(AUTO_MODEL_POOL)}. "
        f"No explanation, no punctuation, just the tier name."
    )

    config_dir = _config_dir or os.environ.get("CLAUDE_CONFIG_DIR", "")
    cwd = _cwd or os.getcwd()

    try:
        # SDK resolution stays INSIDE the try: an unimportable SDK must degrade
        # to the account default, never fail the spawn.
        query_fn = _query_fn or _default_sdk_query_fn()
        from claude_agent_sdk import ClaudeAgentOptions

        options = ClaudeAgentOptions(
            model="haiku",
            system_prompt=_ASSESS_SYSTEM,
            cwd=cwd,
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            allowed_tools=[],
            setting_sources=[],
        )
        text_parts: list[str] = []
        async for msg in query_fn(prompt=body, options=options):
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                t = getattr(block, "text", None)
                if isinstance(t, str):
                    text_parts.append(t)
        raw = "".join(text_parts).strip()
    except Exception as exc:
        log.warning("assess_model: model call failed: %s", exc)
        return None

    pick = raw.strip().strip(".").lower()
    if pick in AUTO_MODEL_POOL:
        return pick
    log.warning("assess_model: out-of-pool reply %r — falling back to default", raw[:80])
    return None


async def resolve_model(
    config: dict,
    task_summary: str,
    *,
    _assess=None,
) -> tuple[str | None, str | None]:
    """Map an automation's ``default_model`` config to ``(model, source)``.

    ``model`` is the value to pass to ``create_session`` (``None`` = account
    default). ``source`` is ``"auto"`` / ``"pinned"`` / ``None`` for the
    spawned-event detail, so the activity feed shows why a session got its
    model. ``_assess`` is a test seam replacing ``assess_model``.
    """
    value = config.get("default_model")
    value = value if isinstance(value, str) else "default"
    if value not in AUTOMATION_MODEL_CHOICES:
        # Defensive: a persisted value no longer in the catalog (validate_config
        # rejects writes, but old rows survive alias removals).
        log.warning("resolve_model: unknown default_model %r — using account default", value)
        return None, None
    if value == "default":
        return None, None
    if value == "auto":
        assess = _assess or assess_model
        picked = await assess(task_summary)
        if picked is None:
            return None, None
        return picked, "auto"
    return value, "pinned"
