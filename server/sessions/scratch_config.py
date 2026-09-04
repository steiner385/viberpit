"""Host-local scratch ``CLAUDE_CONFIG_DIR`` for one-shot LLM helper spawns (#1005).

Why this exists
---------------
The account store (``~/.claude-accounts``) is canonical on dobby and reaches every other
fleet host over sshfs. Claude Code rewrites ``.claude.json`` atomically (temp file +
``rename(2)``), so dobby's live sessions change the file's inode and size continuously.
A reading host that has the name/attributes cached therefore gets a *truncated* body —
and Claude Code's config loader treats an unparseable config as **corruption**: it moves
the file aside into ``backups/.claude.json.corrupted.<ts>``, starts from a **default**
config, and writes that default back. Through the mount, that write lands on dobby's
disk. From 2026-09-01 rocky did this ~1,100x/day, repeatedly resetting project trust,
``projects`` history, onboarding flags and cached identity for all three accounts.

``deploy/bin-ccswitch-mounts.sh`` fixes the *cause* (the account store no longer gets a
600s attribute cache). This module removes the *blast radius*, which is the half that
does not depend on every host's mount being right: a one-shot helper spawn never points
``CLAUDE_CONFIG_DIR`` at the shared account dir at all. It gets a host-local copy, so a
false "corrupted" verdict can only reset a scratch directory this host owns.

What it does
------------
``helper_config_dir(config_dir)`` returns a path under ``~/.cache/ccswitch/helper-cfg``
holding:

* ``.claude.json`` / ``.credentials.json`` — **copies**, refreshed from the account dir
  whenever the source's ``(size, mtime_ns)`` changes and the bytes parse as JSON.
* the ``configdir.SHARE_ALLOWLIST`` entries — **symlinks** back into the account dir
  (which are themselves symlinks to the shared base), so plugins/commands/settings stay
  a single source of truth rather than being duplicated per host.

Sessions are deliberately NOT routed through here: an interactive session must read and
write the real account dir (that is where its history and token refreshes belong). Only
the stateless, tool-denied one-shot helpers — the nine ``account_auth.helper_env`` call
sites plus the suggestion/summary factories — are isolated.

Torn reads are evidence, not noise
----------------------------------
When the source ``.claude.json`` does not parse, the previous good copy is kept (the
helper runs against last-known-good config) and a host-local marker is dropped in
``torn-reads/``. :func:`recent_torn_reads` counts those over a window; the watchdog
(``scripts/ccswitch-health-check.sh``) turns the count into ``ALERT[config-torn-read]``,
which is how "rocky is reading a stale view of dobby's config" becomes visible instead
of silently rewriting the shared store. Markers are per-host by construction — the
scratch root is local disk, never the mount.

Fail-open everywhere: any error returns *config_dir* unchanged, which is exactly today's
behaviour. A helper call is best-effort and must never be crashed by this optimisation.
Kill switch: ``CCSWITCH_HELPER_SCRATCH_CONFIG=false``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from ccswitch import paths as _paths
from ccswitch.configdir import CLAUDE_JSON_NAME, CREDENTIALS_NAME, SHARE_ALLOWLIST

log = logging.getLogger(__name__)

# Real files copied into the scratch dir (never symlinked): these are the two the config
# loader rewrites, and a symlink would let a "repair" write straight back to the store.
_COPIED = (CLAUDE_JSON_NAME, CREDENTIALS_NAME)

_TORN_READS_DIRNAME = "torn-reads"
# Cap the marker directory so an unfixed mount cannot fill the disk. The watchdog only
# ever looks at the last hour, so a few hundred is plenty of history.
_MAX_TORN_READ_MARKERS = 500
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _enabled() -> bool:
    return os.environ.get("CCSWITCH_HELPER_SCRATCH_CONFIG", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def scratch_root() -> Path:
    """Host-local root for scratch config dirs.

    ``CCSWITCH_HELPER_SCRATCH_ROOT`` overrides (tests, and hosts whose ``~/.cache`` is
    itself shared). Must resolve OUTSIDE the account store, or the whole point is lost.
    """
    env = os.environ.get("CCSWITCH_HELPER_SCRATCH_ROOT", "").strip()
    return Path(env) if env else Path.home() / ".cache" / "ccswitch" / "helper-cfg"


def _root_is_safe(root: Path) -> bool:
    """False when *root* lives inside the account store (a scratch dir there is a no-op)."""
    try:
        root.resolve().relative_to(_paths.cfg_root().resolve())
    except ValueError:
        return True
    except OSError:
        return False
    return False


def _scratch_dir_for(src: Path, root: Path, *, suffix: str = "") -> Path:
    """Stable per-source scratch path: ``<root>/<account-name>-<hash8><suffix>``.

    The hash of the absolute source path keeps two accounts that happen to share a
    basename (e.g. a test store and the real one) from colliding on the same scratch dir.
    *suffix* separates the two VARIANTS of one account's scratch dir (with and without
    the share-allowlist links) so they cannot fight over the same directory.
    """
    digest = hashlib.sha256(str(src).encode("utf-8")).hexdigest()[:8]
    return root / f"{_SAFE_NAME_RE.sub('_', src.name) or 'account'}-{digest}{suffix}"


def _stamp_path(dst: Path, name: str) -> Path:
    return dst / f".ccswitch-src-{name}.stamp"


def _sync_file(src_file: Path, dst: Path, name: str) -> bool:
    """Refresh ``dst/name`` from *src_file*. Returns True if the source read was torn.

    Skips the copy when the source's ``(size, mtime_ns)`` is unchanged since the last
    successful sync. A source that does not parse as JSON is a torn read: the existing
    copy (last-known-good) is left in place and the caller records the event. A source
    that is simply absent is not a tear — nothing to copy, no alarm.
    """
    try:
        st = src_file.stat()
    except FileNotFoundError:
        return False
    stamp = _stamp_path(dst, name)
    fingerprint = f"{st.st_size}:{st.st_mtime_ns}"
    try:
        if stamp.read_text() == fingerprint and (dst / name).exists():
            return False
    except OSError:
        pass

    raw = src_file.read_bytes()
    try:
        json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # THE bug's signature. Keep the previous copy and let the caller alert; do not
        # stamp, so the next call retries the read rather than trusting a torn snapshot.
        return True

    # Same temp-file + rename discipline creds.write_json_secure uses: a helper spawning
    # concurrently must never observe a half-written scratch config.
    tmp = dst / f".{name}.tmp.{os.getpid()}"
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst / name)
    stamp.write_text(fingerprint)
    return False


def _sync_links(src: Path, dst: Path, *, share_links: bool = True) -> None:
    """Point the share-allowlist names at the account dir's own entries.

    Symlinks, not copies: plugins/commands/settings are read-only shared assets and
    duplicating them per host is exactly the parallel-copy problem SHARE_ALLOWLIST exists
    to avoid. Stale links (source removed) are pruned.

    ``share_links=False`` prunes them ALL instead: an assistant session (spec §4.1) must
    not inherit the operator's plugins, custom commands, hooks or settings. Pruning
    rather than merely not-creating keeps the directory correct when a dir built by an
    earlier (or a differently-parameterised) call is reused.
    """
    for name in SHARE_ALLOWLIST:
        link, source = dst / name, src / name
        if not share_links or not source.exists():
            if link.is_symlink():
                link.unlink()
            continue
        if link.is_symlink():
            if Path(os.readlink(link)) == source:
                continue
            link.unlink()
        elif link.exists():
            continue  # a real file we did not create — leave it alone
        link.symlink_to(source)


def _record_torn_read(root: Path, src: Path) -> None:
    """Drop a host-local, timestamped marker for one torn read of *src*."""
    d = root / _TORN_READS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{time.time():.6f}-{os.getpid()}").write_text(str(src))
    markers = sorted(d.iterdir(), key=lambda p: p.name)
    for stale in markers[:-_MAX_TORN_READ_MARKERS]:
        try:
            stale.unlink()
        except OSError:
            pass


def recent_torn_reads(window_s: float = 3600.0, *, _now: float | None = None) -> int:
    """How many torn config reads this host recorded in the last *window_s* seconds.

    Read by ``scripts/ccswitch-health-check.sh`` (section 6). Zero on any error — the
    watchdog must not go red because the marker directory is missing.
    """
    now = time.time() if _now is None else _now
    try:
        d = scratch_root() / _TORN_READS_DIRNAME
        return sum(1 for m in d.iterdir() if now - m.stat().st_mtime <= window_s)
    except (OSError, ValueError):
        return 0


def helper_config_dir(config_dir: str, *, share_links: bool = True) -> str:
    """Return a host-local scratch mirror of *config_dir*, or *config_dir* unchanged.

    Unchanged when the kill switch is off, when *config_dir* is empty or missing, when
    the scratch root would land inside the account store, or on any error — including a
    torn source read with no last-known-good copy to fall back on, where reading the
    shared file directly is no worse than today.

    ``share_links=False`` returns a variant that OMITS the ``SHARE_ALLOWLIST`` symlinks
    (plugins, commands, settings.json, settings.local.json, projects) — the assistant
    session's config dir (spec §4.1). It is a separate directory from the shared-link
    mirror, so the two variants of one account never overwrite each other's links.

    Note what a config dir CANNOT isolate: ``$HOME/.claude/CLAUDE.md`` is resolved from
    HOME, not from ``CLAUDE_CONFIG_DIR``, so user memory still loads. The assistant's
    system prompt states that its role overrides those instructions rather than HOME
    being repointed (which would also move credentials, caches and git config).
    """
    if not config_dir or not _enabled():
        return config_dir
    try:
        src = Path(config_dir)
        if not src.is_dir():
            return config_dir
        root = scratch_root()
        if not _root_is_safe(root):
            log.debug("scratch_config: root %s is inside the account store — not isolating", root)
            return config_dir

        dst = _scratch_dir_for(src, root, suffix="" if share_links else "-nolinks")
        dst.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        os.chmod(dst, 0o700)

        torn = [name for name in _COPIED if _sync_file(src / name, dst, name)]
        if torn:
            _record_torn_read(root, src)
            log.warning(
                "scratch_config: torn read of %s from %s — reusing last-known-good copy "
                "(stale sshfs attr cache? see #1005)", ", ".join(torn), src,
            )
        if not (dst / CLAUDE_JSON_NAME).exists():
            # First-ever sync and the source did not parse: no last-known-good to serve.
            # Degrade to the shared dir rather than handing the helper an empty config.
            return config_dir
        _sync_links(src, dst, share_links=share_links)
        return str(dst)
    except Exception as exc:  # noqa: BLE001 — best-effort isolation, never a helper's problem
        log.debug("scratch_config: falling back to %s: %s", config_dir, exc)
        return config_dir
