"""The runner-side cache of the shared MCP store's public view (#3287).

The refresh authority (the edge) is the only writer of ``store/mcp.shared.json`` and the only
holder of MCP refresh tokens. A runner never reads that file over a mount once the edge pushes
the store's *public view* (``creds.public_view`` of every bundle: everything but the refresh
token) beside the Claude envelopes on the ``creds_sync`` RPC it already receives every 30 s.
This module is where that push lands: :class:`McpViewStore` keeps the pushed bundles and the
grant reconcile's live purge keys, host-locally, so a daemon restart does not need the edge to
be up before a worker's ``creds.read_shared_mcp`` can answer.

There is no lease, unlike :class:`viberpit.credential_envelope.EnvelopeStore`: the view holds
access tokens only, and a stale access token in the cache is the same harmless state an account
dir would be in anyway (the CLI gets a 401 and the next publish replaces it). The file does hold
access tokens, hence ``0600`` in a ``0700`` dir, and ``sync`` strips ``refreshToken`` again on
receipt so a mistaken push can never park a refresh token on a runner (constitution II).
"""
from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path

from .credential_envelope import state_dir

_CACHE_FILE = "mcp-view.json"


class McpViewStore:
    """Holds the last pushed public view + purge keys; rewrites its cache only on change."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory
        self._lock = threading.Lock()
        self._bundles: dict[str, dict] | None = None  # None until the first sync
        self._drop: frozenset[str] = frozenset()
        self._load()

    @property
    def _path(self) -> Path:
        return (self._dir or state_dir()) / _CACHE_FILE

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            bundles = raw["bundles"]
            if not isinstance(bundles, dict):
                raise TypeError("bundles is not a mapping")
            self._bundles = {str(k): v for k, v in bundles.items() if isinstance(v, dict)}
            self._drop = frozenset(str(k) for k in raw.get("drop") or [])
        except (OSError, ValueError, KeyError, TypeError):
            self._bundles, self._drop = None, frozenset()

    def _persist(self) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        payload = {"bundles": self._bundles or {}, "drop": sorted(self._drop)}
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)

    def sync(self, bundles: Mapping[str, object], drop: Iterable[str] = ()) -> bool:
        """Take a push from the edge; return True when the cache file was rewritten.

        Non-dict values are ignored and every bundle is reduced to its public view, so the
        cache never holds a refresh token whatever the edge sent.
        """
        from .creds import public_view  # creds imports this module

        incoming = {str(k): public_view(v) for k, v in bundles.items() if isinstance(v, dict)}
        drop_keys = frozenset(str(k) for k in drop)
        with self._lock:
            if self._bundles == incoming and self._drop == drop_keys:
                return False
            self._bundles, self._drop = incoming, drop_keys
            self._persist()
            return True

    def bundles(self) -> dict[str, dict] | None:
        """A deep copy of the pushed view, or None until the first sync (fall back to the file)."""
        with self._lock:
            return None if self._bundles is None else copy.deepcopy(self._bundles)

    def drop_keys(self) -> frozenset[str]:
        """The grant reconcile's live purge keys as of the last push."""
        with self._lock:
            return self._drop


_STORE: McpViewStore | None = None
_STORE_LOCK = threading.Lock()


def runner_view() -> McpViewStore:
    """The process-wide view store (lazy so importing never touches the disk)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = McpViewStore()
        return _STORE


def reset_runner_view() -> None:
    """Drop the process-wide view store (tests)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None
