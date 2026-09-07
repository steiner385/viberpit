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

``resolve_model``/``assess_model`` answer in Claude catalog aliases, so they
apply ONLY to a Claude-engine spawn — handing a Codex runner ``"opus"`` is a
mis-route ``sanitize_launch_for_engine`` has to drop downstream. For a Codex
spawn, ``resolve_codex_model``/``assess_codex_effort`` reuse the SAME Haiku
complexity judgment (deciding how hard a job is does not depend on which
engine will run it) but map the picked tier onto the account's live Codex
model/effort catalog instead — reasoning effort is Codex's own
complexity/cost dial, the direct counterpart of a Claude tier alias. Neither
function ever returns a value the caller did not hand it in ``efforts``, so a
Claude alias structurally cannot reach a Codex dispatch through this module.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from server.sessions.account_auth import helper_env
from server.sessions.claude_bin import cli_path_kwargs
from server.sessions.helper_call import pick_helper_config_dir, run_helper_call
from server.sessions.model_catalog import ALIASES, TIER_ORDER

log = logging.getLogger("ccswitch.automations")

# Tiers "auto" chooses between, cheapest first. Derived from the model-catalog
# ordering so there is exactly one tier ranking in the codebase.
AUTO_MODEL_POOL: list[str] = TIER_ORDER

# The one-shot assessment is a network SDK call with no inherent timeout, and an
# automation spawn runs it INLINE inside the (bounded) edge→daemon RPC. A wedged
# or pathologically slow assessment must fail open to the account default, never
# hang the spawn (issue #567). Override via env for ops tuning.
ASSESS_MODEL_TIMEOUT_S: float = float(
    os.environ.get("CCSWITCH_ASSESS_MODEL_TIMEOUT_S", "30"))

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
    from claude_agent_sdk import query  # lazy: resolved off the package at call
    # time, which lets server/tests/conftest.py stub every helper door at once (#1383)
    return query


async def assess_model(
    task_summary: str,
    *,
    _query_fn=None,
    _config_dir: str | None = None,
    _cwd: str | None = None,
    _timeout: float | None = None,
) -> str | None:
    """One-shot Haiku query mapping *task_summary* to a tier in AUTO_MODEL_POOL.

    Mirrors ``backlog_grouping.propose_groups``: tool-less one-shot query with an
    injected ``_query_fn`` seam so tests never call the real model. Returns the
    picked alias, or ``None`` on ANY failure (exception, timeout, empty or
    out-of-pool reply) so the caller falls back to the account default.

    The assessment is bounded by ``_timeout`` (default ``ASSESS_MODEL_TIMEOUT_S``)
    so a wedged model call can never hang the inline spawn pipeline.
    """
    if not task_summary.strip():
        return None
    timeout = _timeout if _timeout is not None else ASSESS_MODEL_TIMEOUT_S

    body = (
        f"Job summary:\n{task_summary}\n\n"
        f"Reply with EXACTLY one of: {', '.join(AUTO_MODEL_POOL)}. "
        f"No explanation, no punctuation, just the tier name."
    )

    config_dir = _config_dir or pick_helper_config_dir() or os.environ.get("CLAUDE_CONFIG_DIR", "")
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
            env=helper_env(config_dir),
            allowed_tools=[],
            setting_sources=[],
            **cli_path_kwargs(),
        )
    except Exception as exc:
        log.warning("assess_model: model call failed: %s", exc)
        return None

    # retries=0: this assessment runs INLINE inside the bounded edge→daemon spawn
    # RPC (#567/#568), so backoff retries would reintroduce the hang it guards
    # against. Keep the single timeout-bounded attempt; the per-account breaker
    # still skips calls against a cooled-down account.
    outcome = await run_helper_call(
        lambda: query_fn(prompt=body, options=options),
        subsystem="assess_model", account_key=config_dir,
        per_attempt_timeout=timeout, retries=0)
    if outcome.skipped:
        return None
    if outcome.error is not None:
        if outcome.failure_kind == "timeout":
            log.warning("assess_model: assessment exceeded %.0fs — falling back to default", timeout)
        else:
            log.warning("assess_model: model call failed: %s", outcome.error)
        return None
    text_parts: list[str] = []
    for msg in outcome.messages:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                text_parts.append(t)
    raw = "".join(text_parts).strip()

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


async def assess_codex_effort(
    task_summary: str,
    efforts: Sequence[str],
    *,
    _assess=None,
) -> str | None:
    """Map *task_summary* to one of *efforts* — an explicit, caller-supplied
    slice of an OpenAI account's live ``model/list`` catalog for the model a
    Codex spawn will actually dispatch with, ordered cheapest-first the way
    the app-server returns them.

    Reuses ``assess_model``'s Haiku complexity classifier: judging how hard a
    job is does not depend on which engine will run it, only the OUTPUT does —
    the picked ``AUTO_MODEL_POOL`` tier is mapped ordinally onto *efforts*
    rather than returned as a Claude alias, so the result is always a value
    the live catalog actually offers (issue #1289: no Claude alias may reach a
    Codex dispatch).

    Fails open like ``assess_model``: ``None`` when *efforts* is empty (nothing
    to map onto — the model has a fixed effort, or the catalog could not be
    read) or the assessment itself fails, so the caller falls back to the
    account/engine default rather than blocking the spawn.
    """
    if not efforts:
        return None
    assess = _assess or assess_model
    tier = await assess(task_summary)
    if tier is None:
        return None
    tier_idx = AUTO_MODEL_POOL.index(tier)
    scaled = min(len(efforts) - 1, (tier_idx * len(efforts)) // len(AUTO_MODEL_POOL))
    return efforts[scaled]


async def resolve_codex_model(
    config: dict,
    task_summary: str,
    efforts: Sequence[str],
    *,
    _assess=None,
) -> tuple[str | None, str | None]:
    """Codex counterpart to ``resolve_model``: maps ``default_model`` to a
    ``(reasoning_effort, source)`` pair for a Codex spawn.

    Only ``"auto"`` does anything here. A pinned Claude alias (``"opus"`` etc.)
    or ``"default"`` means Codex has nothing to apply — an operator pins a
    Codex model/effort directly via that engine's own ``codex_model`` /
    ``codex_reasoning_effort`` config fields (applied downstream by
    ``sanitize_launch_for_engine``), never through this Claude-alias field.
    Both resolve to ``(None, None)`` — the engine/account default — so a Claude
    alias can never reach this function's return value.
    """
    value = config.get("default_model")
    value = value if isinstance(value, str) else "default"
    if value != "auto":
        return None, None
    assess = _assess or assess_codex_effort
    effort = await assess(task_summary, efforts)
    if effort is None:
        return None, None
    return effort, "auto"
