"""Claude credential envelopes — what a runner holds instead of a refresh token (#2629).

The primary daemon on dobby is the one process that rotates a Claude account's token family
(``accounts.is_refresh_authority``). It publishes a per-account *envelope*; a runner stores it and
launches ``claude`` with the envelope token in ``CLAUDE_CODE_OAUTH_TOKEN``. A launch in that mode
holds no refresh token, so the CLI has nothing to rotate (verified against claude 2.1.183, see
``docs/superpowers/specs/2026-10-02-claude-credential-broker-design.md``).

Two halves live here:

* the pure rules — :class:`Envelope` and :func:`accepts` (generation monotonicity);
* :class:`EnvelopeStore` — the runner-side lease + host-local ``0600`` cache, so a daemon restart
  does not need the edge to be up.

The lease mirrors ``AuthorizationSync``: envelopes expire ``lease_s`` after the last sync, so a
silent edge fails closed. A lapsed lease stops NEW launches; it never touches a running session.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SETUP_TOKEN = "setup_token"
OAUTH_ACCESS = "oauth_access"
KINDS = (SETUP_TOKEN, OAUTH_ACCESS)

_CACHE_FILE = "envelopes.json"


@dataclass(frozen=True)
class Envelope:
    account: str
    kind: str
    token: str
    expires_at: float | None  # epoch seconds; None for a setup token with no known expiry
    generation: int

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown envelope kind {self.kind!r}")
        if self.generation < 0:
            raise ValueError("envelope generation must be >= 0")

    def __repr__(self) -> str:  # never print the secret
        return (f"Envelope(account={self.account!r}, kind={self.kind!r}, "
                f"expires_at={self.expires_at!r}, generation={self.generation})")

    @classmethod
    def from_dict(cls, d: dict) -> "Envelope":
        exp = d.get("expires_at")
        return cls(account=str(d["account"]), kind=str(d["kind"]), token=str(d["token"]),
                   expires_at=None if exp is None else float(exp), generation=int(d["generation"]))


def accepts(held: Envelope | None, incoming: Envelope) -> bool:
    """A runner never goes back: *incoming* is taken iff its generation is >= the one held.

    Equal is accepted so the periodic re-push refreshes the lease; a lower generation is a
    superseded rotation and is refused.
    """
    return held is None or incoming.generation >= held.generation


def state_dir() -> Path:
    """Host-local cache dir (same locality rule as Codex homes — never on a shared mount)."""
    override = os.environ.get("VIBERPIT_CLAUDE_CREDS_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "viberpit" / "state" / "claude-creds"


class EnvelopeStore:
    """Runner-side envelope holder: monotonic generations, a lease, a restart-safe cache."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory
        self._lock = threading.Lock()
        self._held: dict[str, Envelope] = {}
        self._synced_at: float | None = None
        self._lease_s: float = 0.0
        self._load()

    @property
    def _path(self) -> Path:
        return (self._dir or state_dir()) / _CACHE_FILE

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
            self._held = {a: Envelope.from_dict(e) for a, e in raw["envelopes"].items()}
            self._synced_at = float(raw["synced_at"])
            self._lease_s = float(raw["lease_s"])
        except (OSError, ValueError, KeyError, TypeError):
            self._held, self._synced_at, self._lease_s = {}, None, 0.0

    def _persist(self) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        payload = {"synced_at": self._synced_at, "lease_s": self._lease_s,
                   "envelopes": {a: asdict(e) for a, e in self._held.items()}}
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)

    def sync(self, envelopes: list[Envelope], lease_s: float,
             now: float | None = None) -> dict[str, int]:
        """Take a push from the edge; return the highest generation held per account (the ack).

        Accounts absent from the push are dropped: the authority no longer vends them (disabled
        or removed), and a runner must not keep serving them.
        """
        now = time.time() if now is None else now
        with self._lock:
            kept: dict[str, Envelope] = {}
            for env in envelopes:
                held = self._held.get(env.account)
                kept[env.account] = env if accepts(held, env) else held  # type: ignore[assignment]
            self._held, self._synced_at, self._lease_s = kept, now, float(lease_s)
            self._persist()
            return {a: e.generation for a, e in self._held.items()}

    def holds(self, account: str) -> bool:
        """True once the broker has ever vended *account* here — even if the lease has lapsed."""
        with self._lock:
            return account in self._held

    def lease_valid(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return self._synced_at is not None and now < self._synced_at + self._lease_s

    def launch_token(self, account: str, *, skew_s: float = 0.0,
                     now: float | None = None) -> str | None:
        """The token a NEW session may launch with, or None (fails closed).

        None when nothing is held, the lease has lapsed, or an ``oauth_access`` envelope is within
        *skew_s* of expiry. A running session is unaffected — it already holds its token.
        """
        now = time.time() if now is None else now
        if not self.lease_valid(now):
            return None
        with self._lock:
            env = self._held.get(account)
        if env is None:
            return None
        if env.kind == OAUTH_ACCESS and (env.expires_at is None or env.expires_at - now <= skew_s):
            return None
        return env.token


_STORE: EnvelopeStore | None = None
_STORE_LOCK = threading.Lock()


def runner_store() -> EnvelopeStore:
    """The process-wide store (lazy so importing never touches the disk)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = EnvelopeStore()
        return _STORE


def reset_runner_store() -> None:
    """Drop the process-wide store (tests)."""
    global _STORE
    with _STORE_LOCK:
        _STORE = None

