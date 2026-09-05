"""Resolving, probing, and supervising one `codex app-server` child process.

This is the process half of the Codex runner: everything that touches the binary,
the filesystem, and the OS. :mod:`server.codex.manager` owns the per-account
policy (locking, login, ref-counting, restart) on top of it, exactly as
`server/sessions/copilot_proxy.py` splits "resolve + spawn + health-gate" from the
manager that ref-counts it. That module is this repo's established supervised
subprocess idiom and this one deliberately mirrors its shape and naming
(``resolve_*`` → ``spawn`` → typed start error → ``stop`` → orphan reaping).

Three rules are enforced here rather than left to callers:

* **Availability is a value, never an exception.** :func:`probe_availability`
  catches everything and returns a typed
  :class:`~server.codex.capabilities.CodexAvailability`. A missing binary, a
  version bump, or an unreadable ``$PATH`` disables Codex and nothing else; it can
  never propagate into a daemon that is also serving Claude sessions.
* **`CODEX_HOME` is local, private, and real.** Codex keeps SQLite, WAL, thread
  and history state under it. The plan forbids that on a network filesystem — the
  2026-09-04 sshfs torn-read incident (#1005) is what a shared mount does to a
  file format that assumes local locking. :func:`assert_local_filesystem` refuses
  a network fstype *and* refuses when the mount table cannot prove locality, and
  it asks that question of the *resolved* path, because a single symlinked
  ancestor (``~/.local/state`` pointed at this fleet's sshfs share is the deployed
  shape) would otherwise walk straight past a lexical check.
  :func:`ensure_codex_home` additionally refuses a symlink anywhere in the home's
  path, so the runner home can never be aimed at the authority's managed home.
* **No credential ever reaches the child's environment or argv.** The child gets a
  minimal allowlist plus ``CODEX_HOME``, and :func:`build_child_env` refuses to
  build an environment that carries anything credential-shaped (checked with
  :mod:`server.codex.redaction`, the one scrubber). Tokens go over JSON-RPC stdin,
  which is what :mod:`server.codex.protocol` does.

Like the rest of ``server/codex/``, nothing here reads ``CCSWITCH_*``: settings are
passed in by the caller.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

from server.codex import redaction, schema
from server.codex.capabilities import CodexAvailability, CodexUnavailableReason
from server.codex.protocol import AppServerTransport, StreamTransport
from server.obs.context import log_event

logger = logging.getLogger(__name__)

CODEX_EXE: Final = "codex"
#: The app-server is a subcommand, not a separate binary.
APP_SERVER_ARGV: Final[tuple[str, ...]] = ("app-server",)

DEFAULT_VERSION_PROBE_TIMEOUT_S: Final = 10.0
DEFAULT_STOP_TIMEOUT_S: Final = 5.0
#: Bytes of child stderr kept for crash diagnostics. Small on purpose: this is a
#: post-mortem hint, not a log sink, and it is held per live runner.
STDERR_TAIL_BYTES: Final = 4096

#: Filesystem types that must never hold Codex runtime state. Any ``fuse*`` type is
#: refused wholesale: sshfs (the mount that caused #1005) reports ``fuse.sshfs``,
#: and every other FUSE backend shares the same "locking and mtime are a polite
#: fiction" property that SQLite/WAL cannot tolerate.
NETWORK_FSTYPES: Final[frozenset[str]] = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "smb3",
        "sshfs",
        "afs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "davfs",
        "beegfs",
        "gpfs",
        "ocfs2",
        "gfs2",
    }
)

#: Environment variables forwarded to the child. Everything else is dropped, so a
#: daemon environment holding `CCSWITCH_MASTER_KEY`, an Anthropic OAuth token, or
#: an OpenAI key cannot leak into a process we did not write.
CHILD_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


class CodexProcessError(RuntimeError):
    """The app-server child could not be built, spawned, or configured."""


class CodexVersionProbeFailed(CodexProcessError):
    """`codex --version` did not produce a usable version string."""


class CodexHomeRefused(CodexProcessError):
    """A candidate `CODEX_HOME` failed the locality/privacy preconditions."""


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


def resolve_codex_bin(configured: str = CODEX_EXE) -> str | None:
    """Resolve the configured binary, or ``None`` when it is unusable.

    An explicit path (anything containing a separator) is honoured only when it is
    an executable file; a bare name goes through ``PATH``. Returning ``None``
    rather than raising keeps "not installed" a *disabled feature*, mirroring
    ``copilot_proxy.resolve_copilot_proxy``.
    """
    candidate = (configured or CODEX_EXE).strip() or CODEX_EXE
    if os.sep in candidate:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        return None
    return shutil.which(candidate)


# ---------------------------------------------------------------------------
# Network-filesystem refusal
# ---------------------------------------------------------------------------

Mounts = Sequence[tuple[str, str]]


def _unescape_mount_field(field: str) -> str:
    """Decode the octal escapes ``/proc/mounts`` uses for spaces and tabs."""
    out: list[str] = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 3 < len(field) and field[index + 1: index + 4].isdigit():
            try:
                out.append(chr(int(field[index + 1: index + 4], 8)))
                index += 4
                continue
            except ValueError:  # pragma: no cover - isdigit already guarantees this
                pass
        out.append(char)
        index += 1
    return "".join(out)


def parse_proc_mounts(text: str) -> tuple[tuple[str, str], ...]:
    """Parse ``/proc/mounts`` text into ``(mount_point, fstype)`` pairs."""
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mounts.append((_unescape_mount_field(fields[1]), fields[2]))
    return tuple(mounts)


def read_mounts(path: str = "/proc/mounts") -> tuple[tuple[str, str], ...]:
    """Read the live mount table. Returns ``()`` when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return parse_proc_mounts(handle.read())
    except OSError:
        return ()


def resolve_real_path(path: str | Path) -> str | None:
    """Absolute, symlink-free form of *path*; ``None`` when it cannot be resolved.

    ``os.path.abspath`` is purely *lexical*, so it answers "which mount holds this
    path" for the link rather than for the directory Codex would really write to:
    with ``/tmp/x/state -> /mnt/shared/state``, an abspath match reports the local
    root filesystem for a home that lands on sshfs. ``realpath`` is non-strict, so
    it resolves the components that exist and appends the not-yet-created tail
    verbatim — which is what lets the refusal run *before* the home is created.
    """
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except OSError:
        return None


def first_symlinked_component(path: str | Path) -> Path | None:
    """The outermost component of *path* that is a symlink, or ``None``.

    Whole-path, not just the leaf: an ancestor link redirects the home exactly as
    effectively as a link on the home itself.
    """
    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    current = Path(absolute.anchor or os.sep)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return current
        except OSError:  # pragma: no cover - an unreadable component is not provably real
            return current
    return None


def filesystem_type_for(path: str | Path, *, mounts: Mounts | None = None) -> str | None:
    """The fstype of the mount that holds *path*, by longest mount-point match.

    The path need not exist yet — this is a mount-table question, so it is
    answerable before the directory is created (which is what lets the caller
    refuse *before* writing anything). Symlinks are resolved first, so the answer
    describes the filesystem the bytes would land on.
    """
    table = read_mounts() if mounts is None else tuple(mounts)
    if not table:
        return None
    target = resolve_real_path(path)
    if target is None:
        return None
    best: tuple[int, str] | None = None
    for point, fstype in table:
        normalized = os.path.abspath(point)
        if target == normalized or target.startswith(
            normalized if normalized.endswith(os.sep) else normalized + os.sep
        ):
            if best is None or len(normalized) > best[0]:
                best = (len(normalized), fstype)
    return best[1] if best is not None else None


def is_network_fstype(fstype: str) -> bool:
    lowered = fstype.lower()
    return (
        lowered in NETWORK_FSTYPES
        or lowered.startswith("fuse")
        or lowered.startswith("nfs")
    )


def assert_local_filesystem(path: str | Path, *, mounts: Mounts | None = None) -> str:
    """Return the fstype of *path*, or refuse if it is not provably local.

    Fails closed twice over: a known network type is refused, and so is an
    *undeterminable* one. "We could not read the mount table" is not evidence of
    locality, and the cost of being wrong is a corrupted thread database on a
    shared mount rather than a Codex session that does not start.
    """
    fstype = filesystem_type_for(path, mounts=mounts)
    if fstype is None:
        raise CodexHomeRefused(
            f"refusing CODEX_HOME at {path}: the mount table does not say which "
            "filesystem holds it, so it cannot be proven local"
        )
    if is_network_fstype(fstype):
        raise CodexHomeRefused(
            f"refusing CODEX_HOME at {path}: it is on a network filesystem "
            f"({fstype}); Codex keeps SQLite/WAL, history and thread state there"
        )
    return fstype


def ensure_codex_home(
    base: str | Path, account: str, *, mounts: Mounts | None = None
) -> Path:
    """Create (or tighten) the local, private `CODEX_HOME` for one account.

    Order matters: locality is asserted *before* anything is created, so a refusal
    leaves no directory behind on the mount we just rejected.
    """
    base_path = Path(os.path.expanduser(str(base)))
    safe = account.strip()
    if not safe or safe in (".", "..") or os.sep in safe or (os.altsep and os.altsep in safe):
        raise CodexHomeRefused(f"refusing CODEX_HOME for an unusable account name: {safe!r}")
    assert_local_filesystem(base_path, mounts=mounts)

    home = base_path / safe
    linked = first_symlinked_component(home)
    if linked is not None:
        # A symlink anywhere in the home's path is how Codex state would silently
        # land in the credential authority's managed home (or on a shared mount).
        # The locality assert above already follows links, so this is the identity
        # half of the same rule: the directory we create must be the one we checked.
        raise CodexHomeRefused(
            f"refusing CODEX_HOME at {home}: {linked} is a symlink; the runner home "
            "and every component of its path must be real local directories, never "
            "links to the authority home or onto a shared mount"
        )
    base_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not home.is_dir():
        raise CodexHomeRefused(f"refusing CODEX_HOME at {home}: it is not a directory")
    # mkdir's mode is masked by umask and ignored for an existing directory, so set
    # the mode explicitly on both levels.
    for target in (base_path, home):
        if stat.S_IMODE(target.stat().st_mode) != 0o700:
            target.chmod(0o700)
    return home


# ---------------------------------------------------------------------------
# Version / contract probe
# ---------------------------------------------------------------------------

CommandRunner = Callable[..., Awaitable[tuple[int, str, str]]]
BinaryResolver = Callable[[str], "str | None"]


async def _kill_probe_child(child: Any) -> None:
    """SIGKILL and reap a probe child that outlived its timeout.

    ``asyncio.wait_for`` only cancels the ``communicate()`` await; the child itself
    keeps running, so a wedged ``codex --version`` would be orphaned for the life of
    the daemon. Best-effort throughout: this runs while an exception is already
    unwinding and must not replace it.
    """
    if getattr(child, "returncode", None) is not None:
        return
    try:
        child.kill()
    except (ProcessLookupError, OSError):
        return
    except Exception:  # noqa: BLE001 -- reaping must not mask the original failure
        logger.debug("codex: version probe child could not be killed", exc_info=True)
        return
    try:
        await child.wait()
    except asyncio.CancelledError:
        # Already unwinding a cancellation: the child has its SIGKILL and the loop's
        # child watcher reaps it. The caller re-raises the original CancelledError,
        # so the cancellation is delayed here, never swallowed.
        pass
    except Exception:  # noqa: BLE001 -- best effort
        logger.debug("codex: version probe child could not be reaped", exc_info=True)


async def _default_runner(
    binary: str,
    *args: str,
    spawn: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
) -> tuple[int, str, str]:
    child = await spawn(
        binary,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await child.communicate()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 -- kill, then re-raise
        await _kill_probe_child(child)
        raise
    return (
        child.returncode if child.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def probe_cli_version(
    binary: str,
    *,
    runner: CommandRunner | None = None,
    timeout_s: float = DEFAULT_VERSION_PROBE_TIMEOUT_S,
) -> str:
    """Execute ``<binary> --version`` and return the trimmed version line."""
    run = runner or _default_runner
    try:
        code, out, err = await asyncio.wait_for(run(binary, "--version"), timeout_s)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- every failure is one typed outcome
        raise CodexVersionProbeFailed(
            f"codex --version could not be executed: {redaction.scrub_exception(exc)}"
        ) from None
    version = out.strip()
    if code != 0 or not version:
        raise CodexVersionProbeFailed(
            f"codex --version exited {code}: "
            f"{redaction.scrub_text(err.strip() or version)[:200] or 'no output'}"
        )
    return version


async def probe_availability(
    *,
    enabled: bool,
    external_auth_enabled: bool,
    binary: str = CODEX_EXE,
    supported_versions: Iterable[str] = (),
    resolver: BinaryResolver | None = None,
    runner: CommandRunner | None = None,
    timeout_s: float = DEFAULT_VERSION_PROBE_TIMEOUT_S,
) -> CodexAvailability:
    """Decide whether this runner may offer Codex. Never raises.

    The version comparison is *not* re-implemented here: it goes through
    :func:`server.codex.schema.check_contract`, whose typed
    :class:`~server.codex.schema.ContractDrift` is the single report for "the
    boundary moved". ``supported_versions`` widens the pin — an operator may admit
    a version they have verified themselves — but it can never *remove* the pinned
    version, which is the one this codebase was written against.
    """
    if not enabled:
        return CodexAvailability.unavailable(
            CodexUnavailableReason.DISABLED, "CCSWITCH_CODEX_ENABLED is false"
        )
    if not external_auth_enabled:
        # Runners authenticate app-server only with host-supplied ChatGPT tokens
        # (plan: no credentials on runners). Without the experimental API there is
        # no way to log one in, so offering Codex would offer a session that cannot
        # authenticate.
        return CodexAvailability.unavailable(
            CodexUnavailableReason.EXTERNAL_AUTH_DISABLED,
            "CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED is false",
        )
    resolve = resolver or resolve_codex_bin
    try:
        path = resolve(binary)
    except Exception as exc:  # noqa: BLE001 -- a broken PATH disables Codex only
        return CodexAvailability.unavailable(
            CodexUnavailableReason.BINARY_NOT_FOUND,
            f"resolving {binary!r} failed: {redaction.scrub_exception(exc)}",
        )
    if not path:
        return CodexAvailability.unavailable(
            CodexUnavailableReason.BINARY_NOT_FOUND, f"{binary!r} is not installed"
        )
    try:
        version = await probe_cli_version(path, runner=runner, timeout_s=timeout_s)
    except CodexVersionProbeFailed as exc:
        return CodexAvailability.unavailable(
            CodexUnavailableReason.VERSION_PROBE_FAILED, str(exc), binary_path=path
        )

    drift = schema.check_contract(
        cli_version=version, contract_sha256=schema.PINNED_CONTRACT_SHA256
    )
    if drift is None:
        return CodexAvailability.ready(binary_path=path, cli_version=version)
    allowed = {v.strip() for v in supported_versions if v and v.strip()}
    allowed.add(schema.PINNED_CLI_VERSION)
    if version not in allowed:
        return CodexAvailability.unavailable(
            CodexUnavailableReason.UNSUPPORTED_VERSION,
            str(drift),
            binary_path=path,
            cli_version=version,
        )
    log_event(
        logger,
        logging.WARNING,
        "codex.process.version_drift_accepted",
        observed=version,
        expected=schema.PINNED_CLI_VERSION,
        reasons=list(drift.reason_codes()),
    )
    return CodexAvailability.ready(
        binary_path=path, cli_version=version, accepted_drift=drift
    )


# ---------------------------------------------------------------------------
# The child process
# ---------------------------------------------------------------------------


def build_child_env(
    codex_home: str | Path, *, parent_env: dict[str, str] | None = None
) -> dict[str, str]:
    """Build the app-server child's environment: an allowlist plus `CODEX_HOME`.

    Refuses to produce an environment carrying anything credential-shaped, even
    from an allowlisted variable (a proxy URL with inline credentials is the real
    case). Credentials reach Codex over JSON-RPC stdin and nowhere else — not argv,
    not the environment, not a file on the runner.
    """
    source = os.environ if parent_env is None else parent_env
    env = {name: source[name] for name in CHILD_ENV_ALLOWLIST if source.get(name)}
    for name, value in env.items():
        if redaction.contains_secret_shaped_value(value):
            raise CodexProcessError(
                f"refusing to spawn codex app-server: environment variable {name} "
                "carries a credential-shaped value"
            )
    env["CODEX_HOME"] = str(codex_home)
    return env


def is_outer_cancellation() -> bool:
    """True when the *current* task is itself being cancelled.

    ``await task`` on a task we just cancelled ourselves raises ``CancelledError``
    in the awaiter: an expected teardown outcome, safe to swallow. An **outer**
    cancellation — the daemon drain cancelling us — must never be swallowed, or the
    drain silently loses its stop signal. ``Task.cancelling()`` is what tells the
    two apart, and both this module and :mod:`server.codex.manager` ask it here so
    the rule has one implementation.
    """
    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0


class CodexProcessHandle(Protocol):
    """What :mod:`server.codex.manager` needs from a running app-server child."""

    @property
    def pid(self) -> int | None: ...
    @property
    def transport(self) -> AppServerTransport: ...
    async def start(self) -> None: ...
    async def wait(self) -> int: ...
    async def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_S) -> None: ...
    def stderr_tail(self) -> str: ...


class CodexProcess:
    """One supervised `codex app-server` child, framed as a JSONL transport.

    The class owns the OS object only. It does not initialize, log in, or restart —
    :mod:`server.codex.manager` does, because those are per-account policy and this
    is per-process mechanism.
    """

    def __init__(
        self,
        binary: str,
        codex_home: str | Path,
        *,
        spawn: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
        parent_env: dict[str, str] | None = None,
    ) -> None:
        self._binary = binary
        self._home = Path(codex_home)
        self._spawn = spawn
        self._parent_env = parent_env
        self._child: Any | None = None
        self._transport: AppServerTransport | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: bytearray = bytearray()

    def __repr__(self) -> str:
        return f"<CodexProcess pid={self.pid} home={self._home}>"

    @property
    def pid(self) -> int | None:
        return getattr(self._child, "pid", None) if self._child is not None else None

    @property
    def returncode(self) -> int | None:
        return getattr(self._child, "returncode", None) if self._child is not None else None

    @property
    def alive(self) -> bool:
        return self._child is not None and self.returncode is None

    @property
    def transport(self) -> AppServerTransport:
        if self._transport is None:
            raise CodexProcessError("codex app-server has not been started")
        return self._transport

    async def start(self) -> None:
        if self._child is not None:
            return
        env = build_child_env(self._home, parent_env=self._parent_env)
        try:
            child = await self._spawn(
                self._binary,
                *APP_SERVER_ARGV,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self._home),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one typed start failure
            raise CodexProcessError(
                f"codex app-server failed to spawn: {redaction.scrub_exception(exc)}"
            ) from None
        if child.stdin is None or child.stdout is None:  # pragma: no cover - PIPE is set
            raise CodexProcessError("codex app-server was spawned without stdio pipes")
        self._child = child
        self._transport = StreamTransport(child.stdout, child.stdin)
        if child.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(child.stderr))
        log_event(
            logger, logging.INFO, "codex.process.started", pid=child.pid, home=str(self._home)
        )

    async def _drain_stderr(self, stream: Any) -> None:
        """Keep a small, scrubbed tail of child stderr for crash diagnostics."""
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                self._stderr.extend(line)
                if len(self._stderr) > STDERR_TAIL_BYTES:
                    del self._stderr[: len(self._stderr) - STDERR_TAIL_BYTES]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- diagnostics must never kill the runner
            return

    def stderr_tail(self) -> str:
        """The retained stderr tail, scrubbed. Safe to log or put in an event."""
        return redaction.scrub_text(self._stderr.decode("utf-8", "replace"))

    async def wait(self) -> int:
        if self._child is None:
            raise CodexProcessError("codex app-server has not been started")
        return await self._child.wait()

    async def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        """SIGTERM, then SIGKILL after *timeout* — the copilot proxy's discipline."""
        child, self._child = self._child, None
        transport, self._transport = self._transport, None
        task, self._stderr_task = self._stderr_task, None
        if child is not None:
            try:
                if getattr(child, "returncode", None) is None:
                    child.terminate()
                    try:
                        await asyncio.wait_for(child.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        child.kill()
                        await child.wait()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001 -- teardown is best effort
                logger.exception("codex app-server terminate failed")
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # Our own cancel() landing is expected; an outer one is the daemon
                    # drain and must keep propagating.
                    if is_outer_cancellation():
                        raise
                except Exception:  # noqa: BLE001 -- a dead diagnostics task is not fatal
                    logger.debug("codex: stderr drain teardown failed", exc_info=True)
        finally:
            if transport is not None:
                try:
                    await transport.aclose()
                except Exception:  # noqa: BLE001 -- the child is already gone
                    pass
