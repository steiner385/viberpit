"""THE account registry: a versioned, non-secret ``accounts.json`` manifest.

Before this module, "which accounts exist and what are they?" was answered by
globbing the credential store: an ``*.cred.json`` file meant an Anthropic account,
a ``store/copilot/<name>/`` directory meant a Copilot one, and — the actual bug —
``provider_of()`` returned ``"anthropic"`` for ANY name it did not recognise. A
typo, a stale session row or a future provider therefore silently resolved to the
Anthropic launch path.

The manifest replaces that inference with a declaration.

Writer rule
-----------
**One writer.** Dobby/the primary host owns ``accounts.json``; a runner
(``CCSWITCH_DAEMON_ROLE=worker``) only ever READS the snapshot. This is the
opposite of ``ledger.json``, which every host writes and whose known failure mode
is a lost update across an sshfs/NFS mount — acceptable for a usage cache, not for
identity. A worker that finds no manifest imports one **in memory** and logs; it
never persists. Writes on the primary use the same ``flock`` + temp-file +
``os.replace`` discipline ``ledger.locked_ledger`` uses, so a torn read is
impossible even mid-write.

**No secrets, ever.** The manifest records how an account authenticates, never any
material it authenticates WITH. Tokens, refresh tokens, client secrets and device
codes stay in the credential store (0600 slots) and in the Codex auth authority.
``assert_no_secrets`` is asserted by a test on every write path.

Vocabularies
------------
``AccountProvider`` / ``UserAuthMode`` / ``RunnerAuthMode`` come from the Codex
integration plan's "Fixed contracts" block and are defined ONCE, here.

``engine`` is deliberately **nullable** and is not re-declared here. The engine
vocabulary's single definition is ``ccswitch.engines.AgentEngine``; the server
driver registry re-exports that same enum. An entry stores ``engine=None`` to
mean "resolve through the engine registry", which
``parse_agent_engine(None, legacy_default=True)`` answers as ``claude_code``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from . import paths
from .account_names import aliases_store_root

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1

# --- Fixed contracts (plan §"Fixed contracts") -------------------------------
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_COPILOT = "copilot"
PROVIDER_OPENAI = "openai"
ACCOUNT_PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_COPILOT, PROVIDER_OPENAI)

USER_AUTH_CLAUDE_OAUTH = "claude_oauth"
USER_AUTH_GITHUB_DEVICE = "github_device"
USER_AUTH_CHATGPT_DEVICE = "chatgpt_device"
USER_AUTH_MODES = (USER_AUTH_CLAUDE_OAUTH, USER_AUTH_GITHUB_DEVICE,
                   USER_AUTH_CHATGPT_DEVICE)

RUNNER_AUTH_CONFIG_DIR = "config_dir"
RUNNER_AUTH_COPILOT_PROXY = "copilot_proxy"
RUNNER_AUTH_CHATGPT_EXTERNAL_TOKENS = "chatgpt_external_tokens"
RUNNER_AUTH_MODES = (RUNNER_AUTH_CONFIG_DIR, RUNNER_AUTH_COPILOT_PROXY,
                     RUNNER_AUTH_CHATGPT_EXTERNAL_TOKENS)

# Who holds the refresh credential for an account. "local" = this host's own
# credential store (Anthropic slots, Copilot token dirs). Codex/ChatGPT accounts
# name the dobby auth-authority service instead, which is the ONLY holder of a
# ChatGPT refresh token (plan §"No shared Codex runtime home").
AUTHORITY_LOCAL = "local"
AUTHORITY_CODEX_AUTH = "codex_auth_service"

# The default (provider -> auth modes) wiring for an imported account. One table,
# so the importer and any future add-account path cannot disagree.
_PROVIDER_DEFAULTS: dict[str, tuple[str, str, str]] = {
    # provider: (user_auth_mode, runner_auth_mode, credential_authority)
    PROVIDER_ANTHROPIC: (USER_AUTH_CLAUDE_OAUTH, RUNNER_AUTH_CONFIG_DIR, AUTHORITY_LOCAL),
    PROVIDER_COPILOT: (USER_AUTH_GITHUB_DEVICE, RUNNER_AUTH_COPILOT_PROXY, AUTHORITY_LOCAL),
    PROVIDER_OPENAI: (USER_AUTH_CHATGPT_DEVICE, RUNNER_AUTH_CHATGPT_EXTERNAL_TOKENS,
                      AUTHORITY_CODEX_AUTH),
}

# Substrings that must never appear as a manifest KEY. Belt-and-braces against a
# future field that quietly carries credential material into a world-readable,
# fleet-replicated file. Asserted on every write and by a dedicated test.
_SECRET_KEY_MARKERS = ("token", "secret", "password", "credential_value",
                       "refresh", "access_key", "apikey", "api_key", "cookie",
                       "private")
# ``credentialAuthority`` names WHO holds the credential; it is not one.
_SECRET_KEY_ALLOWLIST = frozenset({"credentialAuthority"})


class AccountRegistryError(Exception):
    """Base class for manifest faults."""


class UnknownAccountError(AccountRegistryError):
    """A name that is not in the manifest.

    Raised instead of guessing a provider. The predecessor returned
    ``"anthropic"`` for every unrecognised name, which routed an unknown (or
    mistyped, or newly-added-but-not-imported) account down the Claude OAuth
    launch path with someone else's credential directory.
    """

    def __init__(self, name: str):
        super().__init__(
            f"account {name!r} is not in the account manifest "
            f"({paths.accounts_manifest_path()}). Run `ccswitch accounts import` "
            f"on the primary host to register credential slots found on disk.")
        self.name = name


class ManifestNotWritable(AccountRegistryError):
    """A write was attempted on a host that is not the manifest's writer."""


# Keys already warned about, so a per-call fault (an unreadable store on the
# sshfs mount) logs ONCE instead of once per spawn, per reassign and per poller
# tick. Purely a log-rate guard; it never changes behaviour.
_warned: set[str] = set()


def warn_once(key: str, msg: str, *args: Any) -> None:
    """``log.warning`` the first time *key* is seen in this process."""
    if key in _warned:
        return
    _warned.add(key)
    log.warning(msg, *args, exc_info=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def is_manifest_writer() -> bool:
    """True on the host that owns ``accounts.json``.

    Reuses the existing per-host role switch (``CCSWITCH_DAEMON_ROLE``, default
    ``primary``) rather than inventing a second notion of "am I dobby?".
    """
    return os.environ.get("CCSWITCH_DAEMON_ROLE", "primary").strip().lower() != "worker"


@dataclass(frozen=True)
class AccountHealth:
    """Non-secret health metadata. ``state`` mirrors the ledger's status
    vocabulary (``ok`` / ``needs-reauth`` / ``unknown``) so the two surfaces do
    not invent competing words for the same condition."""

    state: str = "unknown"
    checked_at: datetime | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {"state": self.state, "checkedAt": _iso(self.checked_at),
                "detail": self.detail}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "AccountHealth":
        d = d or {}
        checked = d.get("checkedAt")
        return cls(state=str(d.get("state") or "unknown"),
                   checked_at=datetime.fromisoformat(checked) if checked else None,
                   detail=d.get("detail"))


@dataclass(frozen=True)
class AccountEntry:
    """One account's identity. Globally unique by ``name`` in v1."""

    name: str
    provider: str
    #: None = "the engine registry's default" (see the module docstring).
    engine: str | None = None
    user_auth_mode: str = USER_AUTH_CLAUDE_OAUTH
    runner_auth_mode: str = RUNNER_AUTH_CONFIG_DIR
    enabled: bool = True
    credential_authority: str = AUTHORITY_LOCAL
    health: AccountHealth = field(default_factory=AccountHealth)
    #: Monotonic per-account counter, bumped whenever the credential behind this
    #: entry is replaced (re-auth, device-flow re-login). Non-secret: it says a
    #: rotation happened, never what rotated.
    generation: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "engine": self.engine,
            "userAuthMode": self.user_auth_mode,
            "runnerAuthMode": self.runner_auth_mode,
            "enabled": self.enabled,
            "credentialAuthority": self.credential_authority,
            "health": self.health.to_dict(),
            "generation": self.generation,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "AccountEntry":
        def _dt(key: str) -> datetime | None:
            v = d.get(key)
            return datetime.fromisoformat(v) if v else None

        return cls(
            name=str(d["name"]),
            provider=str(d["provider"]),
            engine=d.get("engine") or None,
            user_auth_mode=str(d.get("userAuthMode") or USER_AUTH_CLAUDE_OAUTH),
            runner_auth_mode=str(d.get("runnerAuthMode") or RUNNER_AUTH_CONFIG_DIR),
            enabled=d.get("enabled", True) is not False,
            credential_authority=str(d.get("credentialAuthority") or AUTHORITY_LOCAL),
            health=AccountHealth.from_dict(d.get("health")),
            generation=int(d.get("generation") or 1),
            created_at=_dt("createdAt"),
            updated_at=_dt("updatedAt"),
        )


@dataclass
class Manifest:
    """The whole file: a schema version, a monotonic revision, and the entries."""

    version: int = MANIFEST_VERSION
    revision: int = 0
    updated_at: datetime | None = None
    accounts: dict[str, AccountEntry] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "revision": self.revision,
            "updatedAt": _iso(self.updated_at),
            "accounts": {n: e.to_dict() for n, e in sorted(self.accounts.items())},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Manifest":
        entries: dict[str, AccountEntry] = {}
        for name, raw in (d.get("accounts") or {}).items():
            if aliases_store_root(name):
                # A nameless/dotted entry cannot address a private per-account
                # path; enumerating it hands the picker a phantom account.
                log.warning("account_registry: dropping manifest entry with unusable name %r", name)
                continue
            merged = {"name": name, **dict(raw)}
            try:
                entries[name] = AccountEntry.from_dict(merged)
            except (KeyError, TypeError, ValueError):
                log.warning("account_registry: dropping malformed manifest entry %r",
                            name, exc_info=True)
        updated = d.get("updatedAt")
        return cls(
            version=int(d.get("version") or MANIFEST_VERSION),
            revision=int(d.get("revision") or 0),
            updated_at=datetime.fromisoformat(updated) if updated else None,
            accounts=entries,
        )


def assert_no_secrets(payload: Mapping[str, Any]) -> None:
    """Raise if any key in the serialised manifest looks like credential material.

    Structural, not a lint: the manifest is replicated across every runner and is
    not a 0600 credential slot, so a field carrying a token would leak the whole
    fleet's credentials in one commit's worth of drift.
    """
    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                key = str(k)
                low = key.lower()
                if key not in _SECRET_KEY_ALLOWLIST and any(m in low for m in _SECRET_KEY_MARKERS):
                    raise AccountRegistryError(
                        f"accounts.json must never hold credential material — "
                        f"refusing key {path + '.' + key!r}")
                walk(v, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(payload, "accounts.json")


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _read_manifest() -> tuple[Manifest, bool]:
    """``(manifest, intact)``.

    ``intact`` is False when the file EXISTS but could not be read or parsed. It
    is the single condition under which a WRITE must not proceed: ``os.replace``
    would destroy identity state we never managed to read. Reads still degrade to
    an empty manifest, because the account store lives on the sshfs mount whose
    transient ``EIO``/``ESTALE`` torn reads are an observed fault (#1005) — a
    spawn or a rotation must degrade, not abort.
    """
    path = paths.accounts_manifest_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Manifest(), True
    except OSError as e:
        warn_once(f"read:{e.errno}",
                  "accounts.json could not be read (%s) — treating as empty for "
                  "this read and refusing to write over it", e)
        return Manifest(), False
    if not raw.strip():
        return Manifest(), True
    try:
        return Manifest.from_dict(json.loads(raw)), True
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        log.error("accounts.json is not valid JSON — treating as empty and NOT "
                  "overwriting it. Repair it, or move it aside and then run "
                  "`ccswitch accounts import` to rebuild it from the store.")
        return Manifest(), False


def load_manifest() -> Manifest:
    """Read the manifest. A missing file is an EMPTY manifest, never an error —
    the importer is what turns an un-migrated store into a populated one. An
    unreadable or corrupt one is also empty *for the reader*; ``locked_manifest``
    is what refuses to overwrite it (so the "NOT overwriting it" promise above is
    actually kept)."""
    return _read_manifest()[0]


@contextlib.contextmanager
def locked_manifest() -> Iterator[Manifest]:
    """Read-modify-write the manifest under an exclusive flock, atomically.

    Mirrors ``ledger.locked_ledger``'s discipline exactly — a dedicated
    ``.lock`` sidecar with a stable inode, and a temp-file + ``os.replace``
    publish so a concurrent reader sees either the old file or the new one and
    never a truncated one. The revision is bumped (and ``updatedAt`` stamped)
    only when the body actually changed the accounts.
    """
    if not is_manifest_writer():
        raise ManifestNotWritable(
            "this host is a runner (CCSWITCH_DAEMON_ROLE=worker); accounts.json "
            "has a single writer (the primary host)")
    root = paths.ensure_root()
    path = paths.accounts_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lf = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        manifest, intact = _read_manifest()
        if not intact:
            # The publish below is os.replace: writing now would silently discard
            # every entry the unreadable file still holds.
            raise AccountRegistryError(
                f"refusing to write {path}: the existing file could not be read "
                f"or parsed, and publishing over it would destroy it. Repair it, "
                f"or move it aside and re-run `ccswitch accounts import` to "
                f"rebuild it from the credential store.")
        before = manifest.to_dict()["accounts"]
        yield manifest
        after = manifest.to_dict()["accounts"]
        if after == before and path.exists():
            return
        manifest.revision += 1
        manifest.updated_at = _utcnow()
        manifest.version = MANIFEST_VERSION
        payload = manifest.to_dict()
        assert_no_secrets(payload)
        fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".accounts-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        lf.close()


# ---------------------------------------------------------------------------
# Filesystem discovery (migration / repair ONLY)
# ---------------------------------------------------------------------------


def discover_filesystem_accounts() -> list[tuple[str, str]]:
    """``(name, provider)`` for every credential artefact on disk.

    THE only remaining filesystem enumeration. Its callers are the importer
    (``ccswitch accounts import``, and the additive reconcile in
    ``ensure_manifest``) — never a normal read. ``all_accounts`` / ``provider_of``
    answer from the manifest, so a name with no credential artefact AND no
    manifest entry raises :class:`UnknownAccountError` instead of resolving to a
    provider nobody declared.

    Each provider's scan is guarded independently. ``Path.glob``/``Path.iterdir``
    raise ``OSError`` where the predecessor's ``Path.exists()`` probe swallowed it,
    and the store is on the sshfs mount that produced torn reads on 2026-09-04
    (#1005) — a transient ``EIO``/``ESTALE`` must cost us one provider's
    enumeration for one call, never a spawn or a rotation. Discovery is purely
    ADDITIVE (see ``ensure_manifest``), so a short scan can only fail to add an
    entry; it can never remove one.
    """
    from . import accounts as _accounts  # local: accounts imports this module
    from . import copilot

    def _scan(provider: str, names) -> list[tuple[str, str]]:
        try:
            return [(n, provider) for n in names()]
        except OSError as e:
            warn_once(f"discover:{provider}:{e.errno}",
                      "account_registry: cannot enumerate %s credential artefacts "
                      "(%s) — skipping that provider for this scan", provider, e)
            return []

    # Copilot FIRST, deliberately. Account names are globally unique in v1, so a
    # name that has BOTH an Anthropic slot and a Copilot token dir has to resolve
    # to one provider — and the predecessor ``provider_of`` resolved it to Copilot
    # (it probed the token dir and returned early). Importing in this order keeps
    # that resolution, so an existing dual-artefact install launches exactly as it
    # did before.
    found = _scan(PROVIDER_COPILOT, copilot.copilot_account_names)
    found += _scan(PROVIDER_ANTHROPIC, _accounts._anthropic_account_names)
    return found


@dataclass(frozen=True)
class ImportResult:
    added: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    revision: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added)


def entry_for(name: str, provider: str, *, now: datetime | None = None) -> AccountEntry:
    """Build a default entry for *provider*. One table drives every auth-mode
    default, so the importer and any add-account path cannot drift apart."""
    if provider not in _PROVIDER_DEFAULTS:
        raise AccountRegistryError(
            f"unknown provider {provider!r} (known: {', '.join(ACCOUNT_PROVIDERS)})")
    user_mode, runner_mode, authority = _PROVIDER_DEFAULTS[provider]
    now = now or _utcnow()
    return AccountEntry(
        name=name, provider=provider, engine=None,
        user_auth_mode=user_mode, runner_auth_mode=runner_mode,
        credential_authority=authority, created_at=now, updated_at=now,
    )


def _import_into(manifest: Manifest, discovered: list[tuple[str, str]],
                 now: datetime) -> ImportResult:
    """Pure half of the importer: fold *discovered* into *manifest*.

    IDEMPOTENT by construction. The only mutation is ``setdefault``-shaped: a
    name already present is left byte-for-byte alone (its enabled flag, health,
    generation and timestamps are operator/runtime state that a re-scan of the
    filesystem knows nothing about and must not clobber). Re-running therefore
    produces an identical dict, so ``locked_manifest``'s change check sees no
    diff and does not bump the revision.
    """
    added, unchanged = [], []
    for name, provider in discovered:
        if aliases_store_root(name):
            continue
        if name in manifest.accounts:
            unchanged.append(name)
            continue
        manifest.accounts[name] = entry_for(name, provider, now=now)
        added.append(name)
    return ImportResult(added=tuple(sorted(added)), unchanged=tuple(sorted(unchanged)),
                        revision=manifest.revision)


def import_filesystem_accounts(*, now: datetime | None = None) -> ImportResult:
    """Register every credential slot / token dir found on disk. Idempotent."""
    now = now or _utcnow()
    with locked_manifest() as manifest:
        result = _import_into(manifest, discover_filesystem_accounts(), now)
        # revision after the (possible) bump is reported by the caller's re-read
    return replace(result, revision=load_manifest().revision)


def ensure_manifest() -> Manifest:
    """The manifest, reconciled with the credential store before it is read.

    Reconcile-on-read, deliberately, and for the same reason ``init_db`` reconciles
    columns at startup: nothing in this fleet runs a migration step, so the only
    reliable moment to pick up a credential slot that appeared by ANY route (the
    CLI's capture-login, the web add flow, an operator copying a slot in) is the
    next read. It is purely ADDITIVE — an entry is never removed by a scan, so a
    runner whose store mount is lagging cannot delete an account, and
    ``remove_account`` stays the only deletion path.

    This does NOT reintroduce the provider guess it replaced: reconcile registers
    a name that has a real credential artefact, with the provider that artefact
    proves. A name with neither an entry nor an artefact still raises
    :class:`UnknownAccountError`.

    Steady state costs one file read plus two directory globs and takes no lock;
    the lock and the write happen only when something is genuinely missing. On a
    RUNNER (writing forbidden) the reconcile is applied in memory and logged.
    """
    manifest, intact = _read_manifest()
    discovered = discover_filesystem_accounts()
    missing = [(n, p) for n, p in discovered
               if n not in manifest.accounts and not aliases_store_root(n)]
    if not missing:
        return manifest
    if not intact:
        # Unreadable/corrupt file: reconcile in memory so reads keep working, but
        # never publish — os.replace would turn a transient EIO into permanent
        # data loss. Same in-memory degrade as the runner branch below.
        log.warning("accounts.json is unreadable — reconciling %d account(s) in "
                    "memory only and NOT writing", len(missing))
        _import_into(manifest, missing, _utcnow())
        return manifest
    if not is_manifest_writer():
        log.warning("accounts.json on this runner is missing %d account(s) the "
                    "credential store has — applying in memory only; the primary "
                    "owns the file", len(missing))
        _import_into(manifest, missing, _utcnow())
        return manifest
    import_filesystem_accounts(now=_utcnow())
    return load_manifest()


# ---------------------------------------------------------------------------
# Queries — the manifest is the source of truth for all of these
# ---------------------------------------------------------------------------


def all_entries() -> list[AccountEntry]:
    return [e for _, e in sorted(ensure_manifest().accounts.items())]


def entry(name: str) -> AccountEntry:
    """One account's entry, or :class:`UnknownAccountError`. Never guesses."""
    e = ensure_manifest().accounts.get(name)
    if e is None:
        raise UnknownAccountError(name)
    return e


def has_account(name: str) -> bool:
    return name in ensure_manifest().accounts


def provider_of(name: str) -> str:
    return entry(name).provider


def engine_of(name: str) -> str | None:
    """The entry's raw engine value; None means "the engine registry's default"
    (resolved by ``server.sessions.engine.parse_agent_engine``)."""
    return entry(name).engine


# ---------------------------------------------------------------------------
# Mutations (primary host only)
# ---------------------------------------------------------------------------


def register_account(name: str, provider: str, *, engine: str | None = None,
                     user_auth_mode: str | None = None,
                     runner_auth_mode: str | None = None,
                     credential_authority: str | None = None) -> AccountEntry:
    """Add (or return the existing) entry for *name*. Names are globally unique."""
    now = _utcnow()
    with locked_manifest() as manifest:
        existing = manifest.accounts.get(name)
        if existing is not None:
            return existing
        e = entry_for(name, provider, now=now)
        if engine is not None:
            e = replace(e, engine=engine)
        if user_auth_mode is not None:
            e = replace(e, user_auth_mode=user_auth_mode)
        if runner_auth_mode is not None:
            e = replace(e, runner_auth_mode=runner_auth_mode)
        if credential_authority is not None:
            e = replace(e, credential_authority=credential_authority)
        manifest.accounts[name] = e
        return e


def forget_account(name: str) -> None:
    """Drop *name* from the manifest (paired with ``accounts.remove_account``)."""
    with locked_manifest() as manifest:
        manifest.accounts.pop(name, None)


def set_health(name: str, state: str, *, detail: str | None = None,
               now: datetime | None = None) -> None:
    """Record a health observation. No-op for an account not in the manifest."""
    now = now or _utcnow()
    with locked_manifest() as manifest:
        e = manifest.accounts.get(name)
        if e is None:
            return
        manifest.accounts[name] = replace(
            e, health=AccountHealth(state=state, checked_at=now, detail=detail),
            updated_at=now)


def set_enabled(name: str, enabled: bool, *, now: datetime | None = None) -> None:
    """Mirror an account's enabled flag into the manifest.

    The *operative* switch is still the ledger (``ledger.set_account_enabled``);
    that is what the selector and every spawn path read, and moving it here would
    be a behaviour change this task has no business making. What the manifest adds
    is a **monotonic revision that advances when the flag changes**, which is what
    the Codex credential authority's rollback check is built on: without it, an
    out-of-order authorization sync could re-enable an account the operator had
    just disabled, and the authority would have no way to tell.

    Kept in lockstep by ``accounts.set_account_enabled``, which writes both. A
    no-op for an account not in the manifest, and a no-op on a runner (only the
    primary may write) — the runner reads the primary's snapshot.
    """
    now = now or _utcnow()
    with locked_manifest() as manifest:
        e = manifest.accounts.get(name)
        if e is None or e.enabled == bool(enabled):
            return
        manifest.accounts[name] = replace(e, enabled=bool(enabled), updated_at=now)


def bump_generation(name: str, *, now: datetime | None = None) -> int:
    """Record that the credential behind *name* was replaced. Returns the new
    generation. Non-secret: the counter says a rotation happened, not what."""
    now = now or _utcnow()
    with locked_manifest() as manifest:
        e = manifest.accounts.get(name)
        if e is None:
            raise UnknownAccountError(name)
        manifest.accounts[name] = replace(e, generation=e.generation + 1,
                                          updated_at=now)
        return e.generation + 1
