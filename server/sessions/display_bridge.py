"""The target host's display bridge: let a source host's sessions draw on this desktop.

Runs on the TARGET daemon (the host with the live desktop). For each accepted source host
the edge lists, it supervises one ``ssh`` to the source carrying a waypipe (Wayland) leg
and an X11 forward, plus the local ``waypipe client`` the Wayland leg connects back to.
The argv is spec §8 "Verified invocation" (docs/superpowers/specs/
2026-11-25-per-host-display-target-design.md) verbatim:

1. **prep** — a short ssh that removes stale remote sockets, writes the target's X cookie
   into a 0600 file on the source (the cookie travels on ssh STDIN, never argv) and prints
   the source's ``$XDG_RUNTIME_DIR``. Exit 3 = no ``/tmp/.X11-unix`` on the source.
2. ``waypipe --socket <T_XRD>/viberpit-bridge/<src>-waypipe.sock client`` on the target.
3. the bridge ssh, its stdin a pipe held open: ``exec waypipe … server -- cat`` on the
   source dies with the connection, so killing ssh leaves no live-but-dead socket.

Desired state, not refcounts: the edge calls :meth:`DisplayBridge.ensure` with the full
source list on every spawn/archive/probe tick. An unlisted source is torn down after
``grace_s``; with no ``ensure`` for ``silence_s`` the set is treated as empty (fail
closed). Teardown runs a cleanup ssh that removes the remote sockets and cookie file;
when the source is unreachable the cleanup is retried, then flagged ``cleanup_pending``
(in memory) and retried on the next ``ensure``.

The desktop comes from :class:`NativeDisplay` (one resolver). Log lines name the source,
leg and state only: never the cookie, never a path's contents.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from server.aio import cancel_and_reap
from server.deploy.steps import SSH_HOST_RE
from server.obs.tasks import supervise
from server.sessions.desktop_session import DesktopSession, _cookie_for
from server.sessions.display_bridge_env import (BRIDGE_DIR, SOURCE_WAYLAND_DISPLAY,
                                                bridge_x11_disabled_reason)
from server.sessions.display_target import _HOST_NAME_RE, accepted_sources
from server.sessions.native_display import _STALE_ENV, NativeDisplay, _display_number
from server.sessions.x11manager import _terminate

log = logging.getLogger("server.sessions.display_bridge")

DISPLAY_BRIDGE_INFO_KEY = "displayBridge"

STATE_UP = "up"
STATE_STARTING = "starting"
STATE_STOPPING = "stopping"
STATE_NOT_ACCEPTED = "not_accepted"
STATE_DESKTOP_UNAVAILABLE = "desktop_unavailable"
STATE_FAILED = "failed"

LEG_WAYLAND = "wayland"
LEG_X11 = "x11"

# Source-side fixed names (spec §8; one target per source host) live with the source's
# liveness check in display_bridge_env, so both ends spell the layout once.
_SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "StreamLocalBindUnlink=yes", "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=3")
# The source's $XDG_RUNTIME_DIR as prep printed it. It lands in a remote shell command and
# in an -R path, so only a plain absolute path is accepted.
_RUNTIME_DIR_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
_CLEANUP_RETRY_DELAYS_S = (5.0, 30.0, 120.0)

Spawn = Callable[..., Awaitable[Any]]


PREP_RC_NO_X11_DIR = 3
# Something the bridge user does not own sits where the bridge must bind: the private dir,
# or (X11 leg) /tmp/.X11-unix/X<N> — world-writable sticky dir, so `rm -f` silently fails on
# another user's squatted socket and session clients would hand the cookie to an impostor.
PREP_RC_SQUATTED = 4
# The source login has no usable $XDG_RUNTIME_DIR (unset, relative, missing or not its own):
# a configuration problem, kept distinct so the log never points at a squat that did not happen.
PREP_RC_NO_RUNTIME_DIR = 5


def _prep_script(x_display: int, *, x11: bool) -> str:
    x_sock = f"/tmp/.X11-unix/X{x_display}"
    lines = [
        "[ -d /tmp/.X11-unix ] || { echo 'viberpit-bridge: /tmp/.X11-unix missing' >&2; "
        f"exit {PREP_RC_NO_X11_DIR}; }}",
        "umask 077",
        'case "$XDG_RUNTIME_DIR" in /*) ;; *) '
        f"echo 'viberpit-bridge: XDG_RUNTIME_DIR unset' >&2; exit {PREP_RC_NO_RUNTIME_DIR};; esac",
        '[ -d "$XDG_RUNTIME_DIR" ] && [ -O "$XDG_RUNTIME_DIR" ] || '
        f"{{ echo 'viberpit-bridge: XDG_RUNTIME_DIR invalid' >&2; exit {PREP_RC_NO_RUNTIME_DIR}; }}",
        f'd="$XDG_RUNTIME_DIR/{BRIDGE_DIR}"',
        'mkdir -p "$d" && chmod 700 "$d"',
        '[ -d "$d" ] && [ -O "$d" ] && [ ! -L "$d" ] || '
        f"{{ echo 'viberpit-bridge: private dir not owned' >&2; exit {PREP_RC_SQUATTED}; }}",
        f'rm -f "$d/waypipe.sock" {x_sock}',
    ]
    if x11:
        # A user-owned or non-sticky /tmp/.X11-unix lets its owner unlink the bound socket.
        lines.append(
            '[ ! -L /tmp/.X11-unix ] && [ -k /tmp/.X11-unix ] && '
            '[ "$(stat -c %u /tmp/.X11-unix)" = 0 ] || '
            "{ echo 'viberpit-bridge: /tmp/.X11-unix not root-owned and sticky' >&2; "
            f"exit {PREP_RC_SQUATTED}; }}")
        lines.append(
            f"if [ -e {x_sock} ] || [ -L {x_sock} ]; then "
            f"echo 'viberpit-bridge: {x_sock} held by another user' >&2; "
            f"exit {PREP_RC_SQUATTED}; fi")
    lines += [
        ': > "$d/xauth" && chmod 600 "$d/xauth"',
        'xauth -q -f "$d/xauth" source -',
        "printf '%s\\n' \"$XDG_RUNTIME_DIR\""]
    return "\n".join(lines)


def _cleanup_script(x_display: int) -> str:
    return (f'rm -f /tmp/.X11-unix/X{x_display} "$XDG_RUNTIME_DIR/{BRIDGE_DIR}/waypipe.sock" '
            f'"$XDG_RUNTIME_DIR/{BRIDGE_DIR}/xauth"')


def _client_socket(t_xrd: str, source: str) -> str:
    return f"{t_xrd}/{BRIDGE_DIR}/{source}-waypipe.sock"


def _desktop_key(desk: DesktopSession) -> tuple:
    """What a running bridge was built from. A change (a new login rotates the cookie and
    its file) means the cookie must be re-pushed and the forwards rebuilt. The cookie is
    compared by digest so the secret is not held beyond the read."""
    number = _display_number(desk.display)
    cookie = _cookie_for(desk.xauthority, str(number)) if number is not None else b""
    return (desk.display, desk.xauthority, desk.wayland_display, desk.xdg_runtime_dir,
            hashlib.sha256(cookie).hexdigest())


async def _terminate_group(proc) -> None:
    """Terminate a process started with ``start_new_session=True`` and its group
    (``waypipe client`` forks one worker per connection)."""
    raw = getattr(proc, "pid", None)
    pid = raw if isinstance(raw, int) else 0
    real = pid > 1
    if real and getattr(proc, "returncode", None) is None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    await _terminate(proc)
    if real:
        try:  # leftovers of the group; a pid is never reused while its group lives
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def _first_exit(procs: Iterable[Any], *, event: asyncio.Event | None = None,
                      timeout: float | None = None) -> str | None:
    """Wait until one of *procs* exits ("exited"), *event* is set ("event") or *timeout*
    passes (None)."""
    waiters: dict[asyncio.Future, str] = {
        asyncio.ensure_future(p.wait()): "exited" for p in procs}
    if event is not None:
        waiters[asyncio.ensure_future(event.wait())] = "event"
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            return None
        kinds = {waiters[d] for d in done}
        return "exited" if "exited" in kinds else "event"
    finally:
        for w in waiters:
            if not w.done():
                await cancel_and_reap(w, phase="display_bridge.wait")


@dataclass
class _Bridge:
    source: str
    state: str = STATE_STARTING
    dest: str | None = None
    since: float | None = None
    legs: tuple[str, ...] = ()
    unlisted_at: float | None = None
    task: asyncio.Task | None = None
    procs: list = field(default_factory=list)       # bridge ssh first, then waypipe client
    client_socket: str | None = None
    desktop_key: tuple | None = None                # what the running attempt was built from
    restart: asyncio.Event = field(default_factory=asyncio.Event)

    def row(self) -> dict:
        state = STATE_STOPPING if self.unlisted_at is not None else self.state
        return {"source": self.source, "state": state, "legs": list(self.legs),
                "since": self.since}


class DisplayBridge:
    def __init__(self, *, native: NativeDisplay, accept_from: frozenset[str],
                 ssh_dest: Callable[[str], str | None], x11_enabled: bool, x_start: int,
                 grace_s: float, silence_s: float, spawn: Spawn = asyncio.create_subprocess_exec,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 settle_s: float = 2.0, prep_timeout_s: float = 20.0,
                 cleanup_timeout_s: float = 20.0, shutdown_cleanup_timeout_s: float = 8.0,
                 backoff_initial_s: float = 1.0,
                 backoff_max_s: float = 60.0, tick_s: float = 5.0) -> None:
        self._native = native
        self._accept = frozenset(s.lower() for s in accept_from)
        self._ssh_dest = ssh_dest
        self._x11_enabled = x11_enabled
        self._x_start = x_start
        self._grace_s = grace_s
        self._silence_s = silence_s
        self._spawn = spawn
        self._clock = clock
        self._wall = wall
        self._sleep = sleep
        self._settle_s = settle_s
        self._prep_timeout_s = prep_timeout_s
        self._cleanup_timeout_s = cleanup_timeout_s
        # stop_all's direct cleanup must fit, with the teardown, inside the daemon's
        # shutdown sweep cap (serve._DAEMON_SHUTDOWN_SWEEP_TIMEOUT_S, 15 s).
        self._shutdown_cleanup_timeout_s = shutdown_cleanup_timeout_s
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._tick_s = tick_s
        self._known: dict[str, _Bridge] = {}
        self._dests: dict[str, str] = {}               # last ssh destination the edge sent
        # Keyed (source, dest): a changed dest leaves a cookie at the old one too.
        self._cleanups: dict[tuple[str, str], asyncio.Task] = {}
        self._cleanup_pending: set[tuple[str, str]] = set()   # cookie maybe left behind
        self._last_ensure: float | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._tick_task: asyncio.Task | None = None

    # ---- public surface -------------------------------------------------------------

    async def ensure(self, sources: list[str], ssh_dests: dict[str, str]) -> list[dict]:
        """Make *sources* the desired set. Idempotent; returns one row per known source."""
        async with self._lock:
            if self._closed:    # the daemon is shutting down: never start an unsupervised bridge
                return []
            now = self._clock()
            self._last_ensure = now
            for src, dest in ssh_dests.items():
                self._dests[src.strip().lower()] = dest
            desired = {s.strip().lower() for s in sources if s.strip()}
            await self._reconcile(desired, now, silent=False)
            for src, dest in sorted(self._cleanup_pending):
                if not self._source_live(src):
                    self._schedule_cleanup(src, dest, retry_delays=())
            return self._rows()

    async def tick(self) -> list[dict]:
        """Re-check the desktop, grace timers and the edge's silence. Run every ``tick_s``."""
        async with self._lock:
            if self._closed:
                return []
            now = self._clock()
            silent = self._last_ensure is None or now - self._last_ensure >= self._silence_s
            desired = set() if silent else {
                s for s, b in self._known.items() if b.unlisted_at is None}
            await self._reconcile(desired, now, silent=silent)
            return self._rows()

    def start(self) -> None:
        if self._tick_task is None:
            self._tick_task = supervise(
                asyncio.create_task(self._tick_loop(), name="display-bridge-tick"),
                name="display-bridge-tick")

    async def stop_all(self) -> None:
        if self._tick_task is not None:
            await cancel_and_reap(self._tick_task, phase="display_bridge.tick")
            self._tick_task = None
        async with self._lock:
            self._closed = True
            ended = [b for b in self._known.values() if b.task is not None or b.procs]
            await asyncio.gather(*(self._teardown(b) for b in ended))
            self._known.clear()
            pending = set(self._cleanup_pending)
            for key, task in self._cleanups.items():
                if not task.done():
                    pending.add(key)    # an interrupted cleanup still owes its attempt
                await cancel_and_reap(task, phase="display_bridge.cleanup")
            self._cleanups.clear()
            pending.update((b.source, b.dest) for b in ended if b.dest)
            order = sorted(pending)
            results = await asyncio.gather(*(
                self._cleanup_once(s, d, timeout_s=self._shutdown_cleanup_timeout_s)
                for s, d in order))
            self._cleanup_pending = {k for k, ok in zip(order, results) if not ok}

    def info(self) -> dict:
        return {"accepts": sorted(self._accept),
                "active": [{"source": b.source, "since": b.since, "legs": list(b.legs)}
                           for b in self._known.values() if b.state == STATE_UP]}

    # ---- desired-state reconcile ----------------------------------------------------

    def _rows(self) -> list[dict]:
        return [self._known[s].row() for s in sorted(self._known)]

    def _legs(self, desk: DesktopSession | None) -> tuple[str, ...]:
        if desk is None:
            return ()
        legs: list[str] = []
        if desk.kind == "wayland" and desk.wayland_display and desk.xdg_runtime_dir:
            legs.append(LEG_WAYLAND)
        if self._x11_enabled and _display_number(desk.display) is not None:
            legs.append(LEG_X11)
        return tuple(legs)

    def _dest_for(self, src: str) -> str | None:
        dest = self._dests.get(src) or self._ssh_dest(src)
        return dest if dest and SSH_HOST_RE.match(dest) else None

    async def _reconcile(self, desired: set[str], now: float, *, silent: bool) -> None:
        if desired:
            await self._native.refresh()
        desk = self._native.current_session() if desired else None
        key = await asyncio.to_thread(_desktop_key, desk) if desk is not None else None
        legs = self._legs(desk)
        for src in sorted(desired):
            b = self._known.setdefault(src, _Bridge(src))
            b.unlisted_at = None
            if src not in self._accept or not _HOST_NAME_RE.match(src):
                self._set_state(b, STATE_NOT_ACCEPTED, "-")
                continue
            if desk is None or not legs:
                if b.task is not None:
                    await self._teardown(b, cleanup=True)
                b.legs = ()
                self._set_state(b, STATE_DESKTOP_UNAVAILABLE, "-")
                continue
            dest = self._dest_for(src)
            if dest is None:
                if b.task is not None:
                    await self._teardown(b, cleanup=True)
                self._set_state(b, STATE_FAILED, "ssh")
                continue
            if b.task is None or b.task.done():
                b.dest = dest
                b.legs = legs
                await self._quiesce_cleanups(src, dest)
                self._set_state(b, STATE_STARTING, "+".join(legs))
                b.task = supervise(asyncio.create_task(
                    self._supervise(b), name=f"display-bridge-{src}"),
                    name=f"display-bridge-{src}")
            elif dest != b.dest or (b.desktop_key is not None and b.desktop_key != key):
                if b.dest and dest != b.dest:
                    # The old login may still hold the cookie file. It may also be the SAME
                    # login under another alias, so it is only cleaned once this source has
                    # no live bridge (_teardown / the next ensure), never against the new one.
                    self._cleanup_pending.add((src, b.dest))
                    await self._quiesce_cleanups(src, dest)
                b.dest = dest
                b.legs = legs
                log.info("display bridge: source=%s leg=%s state=restarting (desktop changed)",
                         src, "+".join(legs))
                b.restart.set()
        for src, b in list(self._known.items()):
            if src in desired:
                continue
            running = b.task is not None or bool(b.procs)
            if silent or not running:
                if running:
                    await self._teardown(b, cleanup=True)
                del self._known[src]
                log.info("display bridge: source=%s leg=- state=removed%s", src,
                         " (edge silent)" if silent and running else "")
            elif b.unlisted_at is None:
                b.unlisted_at = now
                log.info("display bridge: source=%s leg=- state=stopping", src)
            elif now - b.unlisted_at >= self._grace_s:
                await self._teardown(b, cleanup=True)
                del self._known[src]
                log.info("display bridge: source=%s leg=- state=removed", src)

    def _set_state(self, b: _Bridge, state: str, leg: str) -> None:
        if b.state != state:
            log.info("display bridge: source=%s leg=%s state=%s", b.source, leg, state)
        b.state = state
        if state != STATE_UP:
            b.since = None

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            try:
                await self.tick()
            except Exception:
                # One bad tick must not end the loop: grace and silence teardown live here.
                log.exception("display bridge: tick failed")

    # ---- one source's supervised process group ---------------------------------------

    async def _supervise(self, b: _Bridge) -> None:
        delay = self._backoff_initial_s
        while True:
            b.restart.clear()
            ok = await self._start_once(b)
            if ok:
                self._set_state(b, STATE_UP, "+".join(b.legs))
                b.since = self._wall()
                up_at = self._clock()
                why = await _first_exit(b.procs, event=b.restart)
                # Only a bridge that stayed up a full backoff ceiling earns a fresh backoff:
                # one that dies just past settle_s is still crash-looping.
                if self._clock() - up_at >= self._backoff_max_s:
                    delay = self._backoff_initial_s
            else:
                why = "exited"
            await self._kill_procs(b)
            if why == "event":
                self._set_state(b, STATE_STARTING, "+".join(b.legs))
                continue
            self._set_state(b, STATE_FAILED, "+".join(b.legs))
            await self._sleep(delay)
            delay = min(delay * 2, self._backoff_max_s)
            self._set_state(b, STATE_STARTING, "+".join(b.legs))

    async def _start_once(self, b: _Bridge) -> bool:
        desk = self._native.current_session()
        legs = self._legs(desk)
        if desk is None or not legs or b.dest is None:
            return False
        b.legs = legs
        b.desktop_key = await asyncio.to_thread(_desktop_key, desk)
        s_xrd = await self._prep(b, desk)
        if s_xrd is None:
            return False
        try:
            if LEG_WAYLAND in legs:
                assert desk.xdg_runtime_dir is not None   # _legs requires it for the leg
                b.client_socket = sock = _client_socket(desk.xdg_runtime_dir, b.source)
                await asyncio.to_thread(self._fresh_client_socket, sock)
                env = {k: v for k, v in os.environ.items() if k not in _STALE_ENV}
                env.update(desk.env(prefer_wayland=True))
                b.procs.append(await self._spawn(
                    "waypipe", "--socket", sock, "client", env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
            b.procs.insert(0, await self._spawn(
                *self._bridge_argv(b, desk, s_xrd),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
        except OSError:
            log.info("display bridge: source=%s leg=%s state=failed (spawn error)",
                     b.source, "+".join(legs))
            return False
        return await _first_exit(b.procs, timeout=self._settle_s) is None

    @staticmethod
    def _fresh_client_socket(sock: str) -> None:
        d = os.path.dirname(sock)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        try:
            os.unlink(sock)     # waypipe client leaves its socket behind on SIGTERM (§8)
        except FileNotFoundError:
            pass

    def _bridge_argv(self, b: _Bridge, desk: DesktopSession, s_xrd: str) -> list[str]:
        assert b.dest is not None
        argv = ["ssh", *_SSH_OPTS]
        remote_sock = f"{s_xrd}/{BRIDGE_DIR}/waypipe.sock"
        if LEG_WAYLAND in b.legs:
            argv += ["-R", f"{remote_sock}:{b.client_socket}"]
        if LEG_X11 in b.legs:
            argv += ["-R", f"/tmp/.X11-unix/X{self._x_start}:"
                           f"/tmp/.X11-unix/X{_display_number(desk.display)}"]
        if LEG_WAYLAND in b.legs:
            remote = (f"exec waypipe --socket {remote_sock} --display {SOURCE_WAYLAND_DISPLAY} "
                      "--unlink-socket server -- cat")
        else:
            remote = "exec cat"   # holds the X forward; dies with the connection like the server
        return [*argv, b.dest, remote]

    async def _prep(self, b: _Bridge, desk: DesktopSession) -> str | None:
        """Run the prep ssh; return the source's $XDG_RUNTIME_DIR, or None on failure."""
        assert b.dest is not None
        cookie_line = b""
        if LEG_X11 in b.legs:
            number = _display_number(desk.display)
            cookie = await asyncio.to_thread(_cookie_for, desk.xauthority, str(number))
            if cookie:
                cookie_line = f"add :{self._x_start} MIT-MAGIC-COOKIE-1 {cookie.hex()}\n".encode()
        proc = None
        try:
            proc = await self._spawn(
                "ssh", "-o", "BatchMode=yes", b.dest,
                _prep_script(self._x_start, x11=LEG_X11 in b.legs),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            out, _ = await asyncio.wait_for(proc.communicate(cookie_line),
                                            timeout=self._prep_timeout_s)
        except (OSError, asyncio.TimeoutError):
            log.info("display bridge: source=%s leg=prep state=failed", b.source)
            return None
        finally:
            if proc is not None and proc.returncode is None:
                await _terminate_group(proc)
        if proc.returncode != 0:
            why = {PREP_RC_NO_X11_DIR: " (source has no X11 socket dir)",
                   PREP_RC_SQUATTED: " (socket or dir held by another user)",
                   PREP_RC_NO_RUNTIME_DIR: " (source XDG_RUNTIME_DIR unset or invalid)"}.get(
                       proc.returncode, "")
            log.info("display bridge: source=%s leg=prep state=failed rc=%s%s",
                     b.source, proc.returncode, why)
            return None
        self._cleanup_pending.discard((b.source, b.dest))   # prep rewrote the cookie file
        lines = [ln.strip() for ln in (out or b"").decode(errors="replace").splitlines()
                 if ln.strip()]
        s_xrd = lines[-1] if lines else ""
        if not _RUNTIME_DIR_RE.match(s_xrd) or ".." in s_xrd.split("/"):
            log.info("display bridge: source=%s leg=prep state=failed (no runtime dir)", b.source)
            return None
        return s_xrd

    async def _kill_procs(self, b: _Bridge) -> None:
        # ssh first: closing its stdin ends `cat`, so the remote waypipe server exits and
        # unlinks its sockets (§8 finding 2); then the local client and its socket.
        for p in list(b.procs):
            stdin = getattr(p, "stdin", None)
            if stdin is not None:
                try:
                    stdin.close()
                except Exception:   # noqa: BLE001 — a dead pipe must not stop the teardown
                    pass
            await _terminate_group(p)
        b.procs.clear()
        if b.client_socket:
            try:
                os.unlink(b.client_socket)
            except OSError:
                pass
            b.client_socket = None

    def _source_live(self, src: str) -> bool:
        """A bridge for *src* is running or starting. No cleanup ssh may run then: any dest
        could be an alias of the live login, and its `rm -f` would hit the live sockets."""
        b = self._known.get(src)
        return b is not None and b.task is not None and not b.task.done()

    async def _quiesce_cleanups(self, src: str, dest: str) -> None:
        """Before a bridge for (src, dest) (re)starts: stop every in-flight cleanup for the
        source. Other dests stay owed (pending, run after this bridge ends); the new prep
        rewrites everything a cleanup of *dest* would remove."""
        for key in [k for k in self._cleanups if k[0] == src]:
            task = self._cleanups.pop(key)
            if not task.done():
                await cancel_and_reap(task, phase="display_bridge.cleanup")
                if key[1] != dest:
                    self._cleanup_pending.add(key)
        self._cleanup_pending.discard((src, dest))

    async def _teardown(self, b: _Bridge, *, cleanup: bool = False) -> None:
        if b.task is not None:
            await cancel_and_reap(b.task, phase="display_bridge.supervisor")
            b.task = None
        await self._kill_procs(b)
        b.since = None
        b.desktop_key = None
        if b.state == STATE_UP:
            b.state = STATE_STARTING
        if cleanup:
            owed = {d for s, d in self._cleanup_pending if s == b.source}
            if b.dest:
                owed.add(b.dest)
            for dest in sorted(owed):
                self._schedule_cleanup(b.source, dest, retry_delays=_CLEANUP_RETRY_DELAYS_S)

    # ---- remote cleanup (Anti-goal 1: no cookie left behind) --------------------------

    def _schedule_cleanup(self, src: str, dest: str, *, retry_delays: tuple[float, ...]) -> None:
        running = self._cleanups.get((src, dest))
        if running is not None and not running.done():
            return
        self._cleanups[(src, dest)] = supervise(asyncio.create_task(
            self._cleanup_with_retries(src, dest, retry_delays),
            name=f"display-bridge-cleanup-{src}"), name=f"display-bridge-cleanup-{src}")

    async def _cleanup_with_retries(self, src: str, dest: str,
                                    retry_delays: tuple[float, ...]) -> None:
        for delay in (0.0, *retry_delays):
            if delay:
                await self._sleep(delay)
            if self._source_live(src):
                # A bridge came back for this source meanwhile: never race its prep/bind.
                # Still owed unless it is the live dest (whose prep rewrote everything).
                b = self._known[src]
                if dest != b.dest:
                    self._cleanup_pending.add((src, dest))
                return
            if await self._cleanup_once(src, dest):
                self._cleanup_pending.discard((src, dest))
                log.info("display bridge: source=%s leg=cleanup state=done", src)
                return
        self._cleanup_pending.add((src, dest))
        log.info("display bridge: source=%s leg=cleanup state=cleanup_pending", src)

    async def _cleanup_once(self, src: str, dest: str, *, timeout_s: float | None = None) -> bool:
        proc = None
        try:
            proc = await self._spawn(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", dest,
                _cleanup_script(self._x_start),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            rc = await asyncio.wait_for(
                proc.wait(), timeout=self._cleanup_timeout_s if timeout_s is None else timeout_s)
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            if proc is not None and proc.returncode is None:
                await _terminate_group(proc)
        return rc == 0


def build_display_bridge(settings, native: NativeDisplay | None) -> DisplayBridge | None:
    """The daemon's bridge, or None when the kill switch is off or there is no native
    display to bridge onto (the RPC then answers no bridges and ``/info`` says null)."""
    if not getattr(settings, "display_bridge_enabled", True):
        return None
    if native is None:
        log.info("display bridge: native display disabled on this host; bridge off")
        return None
    x_start = int(getattr(settings, "display_bridge_x_start", 150))
    x11_off = bridge_x11_disabled_reason(settings)
    if x11_off is not None:
        log.info("display bridge: X11 leg disabled: %s", x11_off)
    x11 = x11_off is None
    from server.sessions.host_probe import PROBE_INTERVAL_S
    return DisplayBridge(
        native=native, accept_from=accepted_sources(settings),
        # The edge sends each source's ssh destination; the host name is the fallback alias.
        ssh_dest=lambda source: source,
        x11_enabled=x11, x_start=x_start,
        grace_s=float(getattr(settings, "display_bridge_grace_s", 60.0)),
        silence_s=3 * PROBE_INTERVAL_S)
