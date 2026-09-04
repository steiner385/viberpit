"""Single source of truth for *which* `claude` executable ccswitch runs.

Used by BOTH the model picker (``model_catalog`` probes it for the alias list)
and the SDK session factory (``sdk_factory`` pins it as the client's ``cli_path``),
so the models the picker advertises and the binary that actually runs a session
can never diverge (constitution VI).

**Why this exists:** ``claude-agent-sdk >= 0.2.x`` bundles its own pinned
``claude`` CLI under ``claude_agent_sdk/_bundled/`` and its ``_find_cli()`` prefers
that bundle over the system binary. The bundle freezes at whatever CLI version the
SDK shipped (e.g. SDK 0.2.105 → CLI 2.1.183) and falls behind the Anthropic API's
minimum supported version, so every turn 400s with "Claude Code <old> does not
support this model; version <new> or newer is required". Pinning the SDK's
``cli_path`` to the auto-updating system ``claude`` keeps sessions current and in
lockstep with the picker's probe.

Resolution mirrors ``ccswitch.launcher.claude_argv``: honor ``CCSWITCH_CLAUDE_BIN``
(a full command whose first token is the executable), else the bare name
``"claude"`` resolved on ``PATH`` — and then, unlike the launcher, the native
installer's well-known locations (``~/.local/bin/claude``, the newest
``~/.local/share/claude/versions/*``). The daemon runs under systemd with a minimal
PATH that lacks ``~/.local/bin``; without the fallback, every host needs a hand-added
``PATH``/``CCSWITCH_CLAUDE_BIN`` line in its unit env file, and the one host that
lacks it silently runs the stale bundled CLI (dobby, 2026-09-03).

**Failure modes made loud, not silent.** Two ways the pin silently degraded to the
stale bundled CLI — and 400d sessions "from time to time" — are guarded here:

1. *Nothing resolves* (``claude`` not on the daemon's ``PATH``, ``CCSWITCH_CLAUDE_BIN``
   unset/invalid) → we log a one-shot WARNING instead of returning ``None`` quietly.
2. *A resolvable-but-stale binary* (e.g. a pinned ``~/.local/bin/claude`` older than
   the SDK's own bundled CLI) → pinning it is strictly worse than the fallback, so we
   refuse it (return ``None`` → SDK uses its newer bundled CLI) and log an ERROR. The
   floor is the bundled CLI's own version, so it self-maintains as the SDK upgrades;
   ``CCSWITCH_CLAUDE_MIN_VERSION`` overrides it when the API needs higher than both.

An *unprobeable* version is never rejected — that preserves today's "trust whatever
resolves" behavior for a binary that may well be current.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess

log = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# One-shot dedupe so the loud logs above don't spam the daemon's frequent
# automation ticks (each of which builds a ``ClaudeAgentOptions``).
_warned: set[str] = set()

Version = tuple[int, int, int]


def _warn_once(key: str, level: int, msg: str, *args: object) -> None:
    if key in _warned:
        log.debug(msg, *args)
        return
    _warned.add(key)
    log.log(level, msg, *args)


def _parse_version(text: str) -> Version | None:
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _probe_version(path: str) -> Version | None:
    """Best-effort ``<path> --version`` → ``(major, minor, patch)``, else ``None``.

    Any failure (missing file, timeout, unparseable output) yields ``None`` so the
    caller trusts the binary rather than rejecting one whose version it can't read.
    """
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # FileNotFoundError, TimeoutExpired, PermissionError…
        log.debug("claude_bin: version probe failed for %s: %s", path, exc)
        return None
    return _parse_version((out.stdout or "") + (out.stderr or ""))


def _bundled_version() -> Version | None:
    """Version of the SDK's vendored ``_bundled/claude``, the natural minimum floor."""
    try:
        import claude_agent_sdk

        bundled = os.path.join(
            os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude"
        )
        if os.path.isfile(bundled):
            return _probe_version(bundled)
    except Exception as exc:  # SDK import/layout changed
        log.debug("claude_bin: could not locate bundled CLI: %s", exc)
    return None


def _min_version() -> Version | None:
    """The floor a pinned binary must meet: ``CCSWITCH_CLAUDE_MIN_VERSION`` if set
    and parseable, else the SDK's bundled CLI version (self-maintaining)."""
    raw = os.environ.get("CCSWITCH_CLAUDE_MIN_VERSION")
    if raw:
        parsed = _parse_version(raw)
        if parsed:
            return parsed
    return _bundled_version()


def _is_exe(path: str) -> bool:
    # isfile() follows symlinks, so a dangling ~/.local/bin/claude is skipped.
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _well_known_candidates() -> list[str]:
    """Where the native ``claude`` installer puts the binary, best first.

    A systemd unit runs with a minimal PATH that lacks ``~/.local/bin``, so a bare
    ``which("claude")`` misses a perfectly good, auto-updating install and the SDK
    silently falls back to its stale bundled CLI. Scanning these fixed locations
    makes resolution independent of per-host ``PATH`` lines in the unit's env file.

    1. ``~/.local/bin/claude`` — the symlink ``claude update`` maintains.
    2. Newest entry under ``~/.local/share/claude/versions/`` — the installer's
       version store, in case the symlink is missing or dangling. Sorted by parsed
       version, not lexically (``2.1.9`` must lose to ``2.1.259``).
    3. ``~/.claude/local/claude`` — the older ``claude migrate-installer`` layout.
    """
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".local", "bin", "claude")]

    versions_dir = os.path.join(home, ".local", "share", "claude", "versions")
    try:
        entries = os.listdir(versions_dir)
    except OSError:
        entries = []
    versioned = [(v, name) for name in entries if (v := _parse_version(name)) is not None]
    for _, name in sorted(versioned, reverse=True):
        candidates.append(os.path.join(versions_dir, name))

    candidates.append(os.path.join(home, ".claude", "local", "claude"))
    return candidates


def resolve_claude_bin() -> str | None:
    """Absolute path to the system ``claude`` executable, or ``None`` if none is
    usable.

    Resolution order: ``CCSWITCH_CLAUDE_BIN`` (first token) → ``claude`` on PATH →
    the native installer's well-known locations (``_well_known_candidates``). The
    explicit/PATH forms stay authoritative so an operator override is never shadowed.

    Returns ``None`` (rather than a bare name, bogus path, or a binary older than the
    SDK's bundled CLI) when nothing usable resolves, so callers fall back to the SDK's
    own — newer — CLI discovery instead of crashing or pinning a stale binary. Both
    ``None`` paths log loudly (once) so the degradation is visible before a session
    400s mid-turn.
    """
    raw = os.environ.get("CCSWITCH_CLAUDE_BIN") or "claude"
    parts = shlex.split(raw)
    exe = parts[0] if parts else "claude"
    candidate = shutil.which(exe)
    # An absolute/relative path that exists and is executable but which() misses
    # (e.g. not on PATH). Accept it directly.
    if not candidate and _is_exe(exe):
        candidate = exe

    if not candidate:
        for path in _well_known_candidates():
            if _is_exe(path):
                log.debug("claude_bin: `%s` not on PATH; using well-known %s", exe, path)
                candidate = path
                break

    if not candidate:
        _warn_once(
            "not-found",
            logging.WARNING,
            "claude_bin: no system `claude` resolved (CCSWITCH_CLAUDE_BIN unset/invalid, "
            "`claude` not on PATH, and none of the native installer's well-known "
            "locations exist); the SDK will fall back to its stale bundled CLI, which "
            "may 400 on newer models. Install claude or set CCSWITCH_CLAUDE_BIN.",
        )
        return None

    # Refuse to pin a binary older than the floor — doing so is strictly worse than
    # the SDK's bundled fallback. Unknown version → trust it (never reject on an
    # unprobeable binary).
    version = _probe_version(candidate)
    if version is not None:
        floor = _min_version()
        if floor is not None and version < floor:
            _warn_once(
                f"below-floor:{candidate}",
                logging.ERROR,
                "claude_bin: resolved %s is version %s, below the required floor %s; "
                "refusing to pin it (the SDK's newer bundled CLI will be used instead). "
                "Run `claude update` or point CCSWITCH_CLAUDE_BIN at a current binary.",
                candidate,
                ".".join(map(str, version)),
                ".".join(map(str, floor)),
            )
            return None

    return candidate


def cli_path_kwargs() -> dict:
    """``{"cli_path": <system claude>}`` for ``ClaudeAgentOptions``, or ``{}``.

    Splat this into *every* ``ClaudeAgentOptions(...)`` in the codebase so no SDK
    client silently falls back to the stale bundled CLI. ``{}`` when the binary
    can't be resolved leaves ``cli_path`` at its default (None) → the SDK uses its
    own discovery rather than crashing on a bogus path.
    """
    binary = resolve_claude_bin()
    return {"cli_path": binary} if binary else {}
