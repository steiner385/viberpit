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
   process lifetime and persisted under ``~/.cache/viberpit/`` keyed by the
   binary's ``(realpath, size, mtime_ns)``, so a restart against an unchanged
   binary skips the scan; ``refresh()`` clears both (e.g. after a binary upgrade).

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

import io
import json
import logging
import os
import re
import time
from pathlib import Path

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
# Bytes per readinto() + in-memory find(); the binary is ~340 MB, so ~85 yields per scan.
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

    Callers on the event loop must go through ``asyncio.to_thread`` (see
    ``api/models.py``), but off-loop alone is NOT enough: the thread only helps
    the loop while it does not hold the GIL. History of the ~340 MB scan:

    - #3150: reading the whole file and latin-1 decoding it took 11.6 s.
    - #3300: an ``mmap`` + whole-file ``re.finditer`` held the GIL 5–6 s.
    - #3510: a chunked ``mmap.find`` still stalled the loop 5–10 s after every
      restart, because ``find`` faults cold pages in with the GIL held — and a
      deploy leaves the disk saturated, so one 4 MiB chunk took seconds.

    So ``_scan_aliases`` reads the file with ``readinto`` (a syscall that drops
    the GIL while it blocks on disk) and ``find``s only within that in-memory
    buffer, which is pure CPU. ``_live_aliases`` also skips the scan entirely
    when the binary is unchanged since the last probe (``_load_persisted``).
    """
    try:
        with open(binary_path, "rb") as f:
            return _scan_aliases(f)
    except OSError:
        return None
    except Exception as exc:
        log.debug("model_catalog: binary probe error: %s", exc)
    return None


def _read_at(f: io.BufferedIOBase, offset: int, buf: memoryview) -> int:
    """Fill ``buf`` from ``f`` at ``offset``; the byte count read (short at EOF).

    ``readinto`` releases the GIL while it blocks on disk; a short read before
    EOF (raw files may return one) just loops.
    """
    f.seek(offset)
    got = 0
    while got < len(buf):
        n = f.readinto(buf[got:])
        if not n:
            break
        got += n
    return got


def _scan_aliases(src: io.BufferedIOBase | bytes) -> list[str] | None:
    """The picker alias array in ``src`` (a seekable binary file, or bytes), or None.

    ``"opusplan"`` is the discriminating anchor — it only appears in the picker
    alias array, not in the many other alias-like contexts. The file is read one
    ``_SCAN_CHUNK`` at a time into a reused buffer; disk waits happen inside
    ``readinto`` with the GIL released, and the ``find`` over the buffer never
    touches the disk. ``time.sleep(0)`` between chunks hands the GIL to the
    event loop thread (#3300, #3510).
    """
    f: io.BufferedIOBase = io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src
    # Overlap by len(anchor)-1 so an anchor straddling a chunk edge is found; an
    # anchor starting inside the overlap cannot fit, so no hit is seen twice.
    buf = bytearray(_SCAN_CHUNK + len(_ANCHOR) - 1)
    view = memoryview(buf)
    pos = 0
    while True:
        n = _read_at(f, pos, view)
        hit = buf.find(_ANCHOR, 0, n)
        while hit >= 0:
            aliases = _array_around(f, pos + hit)
            if aliases is not None:
                return aliases
            hit = buf.find(_ANCHOR, hit + 1, n)
        if n < len(buf):
            return None
        pos += _SCAN_CHUNK
        time.sleep(0)


def _array_around(f: io.BufferedIOBase, hit: int) -> list[str] | None:
    """The alias array containing the anchor at file offset ``hit``, or None.

    Regexes a window around the hit rather than ``rfind``-ing the opening ``[``:
    ``"sonnet[1m]"`` puts brackets inside the array itself. The window grows
    while no match spans the hit, so an array longer than the first window (the
    picker list only grows) is still found. Each window is re-read from ``f``, so
    the hit may sit anywhere relative to the scan buffer.
    """
    back = _WINDOW
    while True:
        start = max(0, hit - back)
        window = bytearray(hit + len(_ANCHOR) + _WINDOW - start)
        got = _read_at(f, start, memoryview(window))
        for m in _ALIAS_ARRAY_RE.finditer(window, 0, got):
            if start + m.start() <= hit and start + m.end() >= hit + len(_ANCHOR):
                return _parse_alias_array(m.group())
        if start == 0 or back >= _MAX_WINDOW:
            return None
        back *= 4


def _persist_path() -> Path:
    """Where the last probe result lives across restarts (#3510)."""
    return Path.home() / ".cache" / "viberpit" / "model-catalog.json"


def _binary_key(binary_path: str) -> list | None:
    """``[realpath, st_size, st_mtime_ns]`` of the binary — the persisted cache key."""
    try:
        real = os.path.realpath(binary_path)
        st = os.stat(real)
    except OSError:
        return None
    return [real, st.st_size, st.st_mtime_ns]


def _load_persisted(key: list) -> list[str] | None:
    """The alias list persisted for exactly this binary ``key``, or None.

    Most edge restarts are ViberPit deploys against the same ``claude`` binary,
    so the cold scan only has to run after a CLI upgrade (#3510).
    """
    try:
        data = json.loads(_persist_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    aliases = data.get("aliases")
    if not isinstance(aliases, list) or not aliases \
            or not all(isinstance(a, str) for a in aliases):
        return None
    return aliases


def _store_persisted(key: list, aliases: list[str]) -> None:
    """Persist ``aliases`` for ``key`` (atomic replace; failures only cost a rescan)."""
    path = _persist_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"key": key, "aliases": aliases}))
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("model_catalog: could not persist probe result: %s", exc)


def _probe(binary_path: str) -> list[str] | None:
    """The binary's alias list: the persisted result if the binary is unchanged,
    else a fresh scan (persisted on success)."""
    key = _binary_key(binary_path)
    if key is not None:
        cached = _load_persisted(key)
        if cached is not None:
            return cached
    discovered = _discover_from_binary(binary_path)
    if discovered is not None and key is not None:
        _store_persisted(key, discovered)
    return discovered


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
        discovered = _probe(path) if path else None
        if discovered is not None:
            log.debug("model_catalog: probed %d aliases from %s", len(discovered), path)
            _cache = discovered
        else:
            log.debug("model_catalog: binary probe returned nothing; using static fallback")
            _cache = ALIASES
    return _cache


def refresh() -> None:
    """Clear the cached alias list so the next ``catalog()`` call re-probes the binary.

    Call this after a binary upgrade or via ``GET /api/models?refresh=1``. Also
    drops the persisted probe result, so the re-probe really rescans the binary.
    """
    global _cache
    _cache = None
    try:
        _persist_path().unlink(missing_ok=True)
    except OSError as exc:
        log.debug("model_catalog: could not drop persisted probe result: %s", exc)


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
