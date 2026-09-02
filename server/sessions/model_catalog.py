"""Model list for the session model selector — a 1-to-1 mirror of the `claude`
binary's `/model` picker.

The claude-agent-sdk exposes no model catalog (set_model is an opaque pass-through
to the `claude` binary). The stable contract is the binary's `/model` aliases, which
auto-resolve to the latest and survive binary version bumps (pinned ids churn every
release). This module surfaces EVERY alias the picker offers so ccswitch exposes
exactly what the binary does — no more, no less.

**How the list is built (in order of preference):**

1. *Binary probe* — at first call to ``catalog()``, ``_discover_from_binary()``
   reads the running ``claude`` binary as bytes and locates the alias JSON array
   in the embedded Bun JS bundle (anchored on ``"opusplan"``, a distinctive string
   that only appears in the picker array).  The probe result is cached for the
   process lifetime; ``refresh()`` clears the cache (e.g. after a binary upgrade).

2. *Static fallback* (``ALIASES``) — used when the binary cannot be found or the
   array pattern is not present (e.g. a future Bun format change).  Keep this list
   in sync after major binary upgrades so the fallback is never stale.

Pinned ids (e.g. "claude-haiku-4-5") remain accepted as free-text input; they are
simply not part of the alias list. Account authorization (Max/plan gating, 1M-context
beta, etc.) is enforced by the binary/account, not here.

Static fallback verified against the real binary (v2.1.197): the picker's alias array
is ``["sonnet","opus","haiku","fable","best","sonnet[1m]","opus[1m]","fable[1m]","opusplan"]``
plus the special ``default`` row.  Order matches the picker.
"""
from __future__ import annotations

import json
import logging
import re

from .claude_bin import resolve_claude_bin

log = logging.getLogger("ccswitch.model_catalog")

# Static fallback: every alias the `claude` /model picker accepted as of v2.1.197,
# in picker order. catalog() prefers the live binary's list over this.
ALIASES: list[str] = [
    "default",
    "sonnet",
    "opus",
    "haiku",
    "fable",
    "best",
    "sonnet[1m]",
    "opus[1m]",
    "fable[1m]",
    "opusplan",
]

_LABELS: dict[str, str] = {
    "default": "Default",
    "sonnet": "Sonnet",
    "opus": "Opus",
    "haiku": "Haiku",
    "fable": "Fable",
    "best": "Best",
    "sonnet[1m]": "Sonnet (1M context)",
    "opus[1m]": "Opus (1M context)",
    "fable[1m]": "Fable (1M context)",
    "opusplan": "Opus Plan",
}

# Module-level cache populated on first catalog() call; cleared by refresh().
_cache: list[str] | None = None


def _label_for(alias: str) -> str:
    """Return the display label for *alias*, deriving it for aliases not in _LABELS.

    Known aliases use the curated label. Unknown future aliases (e.g. a new
    "nova" tier) get a title-cased name with "(1M context)" appended for the
    ``[1m]`` variant, so new binary aliases surface usefully without a code change.
    """
    if alias in _LABELS:
        return _LABELS[alias]
    base = re.sub(r"\[1m\]$", "", alias, flags=re.IGNORECASE)
    label = base.title()
    if base != alias:
        label += " (1M context)"
    return label


def _discover_from_binary(binary_path: str) -> list[str] | None:
    """Extract the model alias list from the claude binary's embedded JS bundle.

    The Bun-compiled binary packs its JS as readable text.  The picker's alias
    array appears as a JSON array literal; ``"opusplan"`` is used as a unique
    anchor because it only occurs in that array.  ``"default"`` is a special UI
    row injected by the picker code (not in the JS array itself) and is prepended
    here if absent.

    Returns the alias list on success, or ``None`` if the binary cannot be read
    or the pattern is not found.
    """
    try:
        with open(binary_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        text = data.decode("latin-1", errors="ignore")
        # Match JSON arrays whose first element is a known alias tier.
        # "opusplan" is our discriminating anchor — it only appears in the
        # picker alias array, not in the many other alias-like contexts.
        for m in re.finditer(
            r'\["(?:default|sonnet|opus|haiku|fable|best)[^"]*"(?:,"[^"]+")*\]',
            text,
        ):
            raw = m.group()
            if "opusplan" not in raw:
                continue
            try:
                aliases: list = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(aliases, list) or not all(
                isinstance(a, str) for a in aliases
            ):
                continue
            # "default" is a special UI row not stored in the JS array itself.
            if "default" not in aliases:
                aliases = ["default"] + aliases
            return aliases
    except Exception as exc:
        log.debug("model_catalog: binary probe error: %s", exc)
    return None


def _live_aliases() -> list[str]:
    """Return the alias list from the running binary, falling back to ``ALIASES``."""
    global _cache
    if _cache is None:
        # Probe the SAME binary the SDK session factory runs (resolve_claude_bin,
        # the single source of truth) so the picker and the runtime can't diverge.
        path = resolve_claude_bin()
        discovered = _discover_from_binary(path) if path else None
        if discovered is not None:
            log.debug("model_catalog: probed %d aliases from %s", len(discovered), path)
            _cache = discovered
        else:
            log.debug("model_catalog: binary probe returned nothing; using static fallback")
            _cache = ALIASES
    return _cache


def refresh() -> None:
    """Clear the cached alias list so the next ``catalog()`` call re-probes the binary.

    Call this after a binary upgrade or via ``GET /api/models?refresh=1``.
    """
    global _cache
    _cache = None


def catalog() -> list[dict]:
    """The full alias list as ``[{"id", "label"}]``, in picker order."""
    return [{"id": a, "label": _label_for(a)} for a in _live_aliases()]


def is_known_alias(name: str) -> bool:
    """True if *name* is in the static alias list.

    Uses the static ``ALIASES`` (not the live binary list) for deterministic
    validation — a value that passed validation must remain valid across restarts
    regardless of which binary version is running.
    """
    return name in ALIASES


# Canonical model tiers, CHEAPEST FIRST. The single ordered source of truth: the
# model-approval policy matches against it, the self-directed switch rule compares
# ranks with it, and automations/model_pick.AUTO_MODEL_POOL derives from it.
# Server mirror of web/src/session/modelName.ts TIERS (constitution VI: one tier
# definition, not four).
TIER_ORDER: list[str] = ["haiku", "sonnet", "opus", "fable"]

_TIER_ALIASES: frozenset[str] = frozenset(TIER_ORDER)


def model_tier(model: str | None) -> str | None:
    """The tier of a model alias or pinned id, or None when it has no fixed tier
    (None / "" / "default" / "best" / unknown ids)."""
    if not model:
        return None
    base = model.strip().lower().split("[", 1)[0]  # drop context tag like [1m]
    m = re.match(r"^claude-(opus|sonnet|haiku|fable)-", base)
    if m:
        return m.group(1)
    if base in _TIER_ALIASES:
        return base
    if base == "opusplan":
        return "opus"
    return None


def tier_rank(model: str | None) -> int | None:
    """Cost rank of *model* within TIER_ORDER (0 = cheapest), or None when the
    model has no fixed tier (None / "" / "default" / "best" / unknown ids).

    A None rank means no honest cheaper/costlier comparison exists — callers must
    treat it as indeterminate rather than defaulting a direction.
    """
    tier = model_tier(model)
    return TIER_ORDER.index(tier) if tier is not None else None
