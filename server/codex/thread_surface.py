"""Which declared surface a Codex thread STARTED with — so a reopen can resume it.

codex-cli 0.153.2 keeps the ``baseInstructions`` / ``developerInstructions`` and
``dynamicTools`` a thread declared on ``thread/start`` for the thread's whole life:
``thread/resume`` restores them and has no field to change them
(docs/superpowers/spikes/2026-09-06-codex-resume-dynamic-tools.md, 2026-10-05
addendum). Resuming is therefore safe exactly when the surface ViberPit would
declare today is the one the thread already has. The assistant drawer's surface
depends on the operator tier (``operator_tools_enabled``), so it can change between
opens; a thread resumed across that change would describe tools it no longer has.

This module records a fingerprint of the declared surface when a thread starts and
answers "does it still match?" on the next open (#2714). The fingerprint lives in a
ViberPit-owned sidecar next to the item-times journal — ``<root>/<thread id>.surface``
— never in ``CODEX_HOME``, and host-local for the same reason as that journal: only
the daemon that owns the thread's app-server can resume it. No sidecar (a thread
from before this landed, a moved session, an unreadable file) means "unknown", and
unknown means start fresh: the safe side, and what every reopen did before.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from server.codex import schema
from server.codex.item_times import thread_sidecar_path

log = logging.getLogger("server.codex.thread_surface")

SURFACE_SUFFIX = ".surface"

#: The `thread/start` fields a resume cannot change — what the fingerprint covers.
#: cwd, sandbox, approval policy and model are re-sent on `thread/resume` itself.
_SURFACE_FIELDS = ("baseInstructions", "developerInstructions", "dynamicTools")


def surface_fingerprint(params: schema.ThreadStartParams) -> str:
    """A stable digest of the surface *params* declares (see ``_SURFACE_FIELDS``)."""
    wire = params.to_wire()
    surface = {key: wire.get(key) for key in _SURFACE_FIELDS}
    canonical = json.dumps(surface, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ThreadSurfaceStore:
    """Records and checks per-thread surface fingerprints under *root*.

    Best-effort both ways: a failed write is logged and costs only the next
    reopen's resume (it starts fresh), and a failed read answers "no match".
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def record(self, thread_id: str, fingerprint: str) -> None:
        path = thread_sidecar_path(self._root, thread_id, SURFACE_SUFFIX)
        if path is None:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(fingerprint, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("codex.thread_surface: could not record thread %s at %s: %s — "
                        "its next reopen will start a fresh thread", thread_id, path, exc)

    def matches(self, thread_id: str, fingerprint: str) -> bool:
        path = thread_sidecar_path(self._root, thread_id, SURFACE_SUFFIX)
        if path is None:
            return False
        try:
            return path.read_text(encoding="utf-8").strip() == fingerprint
        except OSError:
            return False
