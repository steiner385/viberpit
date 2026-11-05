"""Model list for the session model selector — a 1-to-1 mirror of the `claude`
binary's `/model` picker.

The claude-agent-sdk exposes no model catalog (set_model is an opaque pass-through
to the `claude` binary). The stable contract is the binary's `/model` aliases, which
auto-resolve to the latest and survive binary version bumps (pinned ids churn every
release). This module surfaces EVERY alias the picker offers so ViberPit exposes
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
import mmap
import re
import time

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

# Match JSON arrays whose first element is a known alias tier.
_ALIAS_ARRAY_RE = re.compile(
    rb'\["(?:default|sonnet|opus|haiku|fable|best)[^"]*"(?:,"[^"]+")*\]')
_ANCHOR = b'"opusplan"'
# Bytes per GIL-holding find(); the binary is ~340 MB, so ~85 yields per scan.
_SCAN_CHUNK = 4 * 1024 * 1024
# Regex window either side of an anchor hit; grows ×4 up to _MAX_WINDOW behind it.
_WINDOW = 4 * 1024
_MAX_WINDOW = 1024 * 1024


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

    Scans a read-only ``mmap`` rather than reading the ~340 MB binary into memory
    and latin-1 decoding it into a str: under memory pressure the read+decode took
    11.6 s (#3150). Callers on the event loop must still go through
    ``asyncio.to_thread`` (see ``api/models.py``), but off-loop alone is NOT
    enough: a thread only helps if it releases the GIL, and ``re`` never does. A
    whole-file ``finditer`` stalled the loop 5–6 s after every edge restart
    (#3300), so ``_scan_aliases`` locates the anchor chunk by chunk, yielding the
    GIL between chunks, and runs the regex only on a small window around each hit.
    """
    try:
        with open(binary_path, "rb") as f, \
                mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _scan_aliases(data)
    except (OSError, ValueError):  # ValueError: mmap of an empty file
        return None
    except Exception as exc:
        log.debug("model_catalog: binary probe error: %s", exc)
    return None


def _scan_aliases(data) -> list[str] | None:
    """The picker alias array in ``data`` (bytes-like: ``bytes`` or ``mmap``), or None.

    ``"opusplan"`` is the discriminating anchor — it only appears in the picker
    alias array, not in the many other alias-like contexts. Each ``find`` covers
    one ``_SCAN_CHUNK`` (≈1 ms even on cold pages) and ``time.sleep(0)`` between
    chunks hands the GIL to the event loop thread (#3300).
    """
    size = len(data)
    pos = 0
    while pos < size:
        # Overlap by len(anchor)-1 so an anchor straddling a chunk edge is found.
        end = min(size, pos + _SCAN_CHUNK + len(_ANCHOR) - 1)
        hit = data.find(_ANCHOR, pos, end)
        if hit < 0:
            pos += _SCAN_CHUNK
            time.sleep(0)
            continue
        aliases = _array_around(data, hit)
        if aliases is not None:
            return aliases
        pos = hit + 1
    return None


def _array_around(data, hit: int) -> list[str] | None:
    """The alias array containing the anchor at ``hit``, or None.

    Regexes a window around the hit rather than ``rfind``-ing the opening ``[``:
    ``"sonnet[1m]"`` puts brackets inside the array itself. The window grows
    while no match spans the hit, so an array longer than the first window (the
    picker list only grows) is still found.
    """
    back = _WINDOW
    while True:
        start = max(0, hit - back)
        stop = min(len(data), hit + len(_ANCHOR) + _WINDOW)
        for m in _ALIAS_ARRAY_RE.finditer(data[start:stop]):
            if start + m.start() <= hit and start + m.end() >= hit + len(_ANCHOR):
                return _parse_alias_array(m.group())
        if start == 0 or back >= _MAX_WINDOW:
            return None
        back *= 4


def _parse_alias_array(match: bytes) -> list[str] | None:
    raw = match.decode("latin-1")
    if "opusplan" not in raw:
        return None
    try:
        aliases: list = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
        return None
    # "default" is a special UI row not stored in the JS array itself.
    if "default" not in aliases:
        aliases = ["default"] + aliases
    return aliases


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


def normalized_alias(model: str | None) -> str | None:
    """*model* lowercased with its context tag stripped (``fable[1m]`` → ``fable``),
    or None when no model is given.

    The single normalization used by both ``model_tier`` and the approval policy's
    denylist matching, so an untiered alias like ``best`` is matched the same way a
    tiered one is.
    """
    if not model:
        return None
    return model.strip().lower().split("[", 1)[0]


def model_tier(model: str | None) -> str | None:
    """The tier of a model alias or pinned id, or None when it has no fixed tier
    (None / "" / "default" / "best" / unknown ids).

    A None tier does NOT mean "ungated" — the approval policy also matches the bare
    alias, so `best` and `default` can be put on the denylist by name."""
    base = normalized_alias(model)
    if base is None:
        return None
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
