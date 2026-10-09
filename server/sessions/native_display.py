"""The operator's live desktop as a session target (spec §4.3).

Counterpart of X11Manager (pooled Xvfb displays): this one never starts a display
server — it asks ``desktop_session.resolve_desktop_session`` which desktop is live
right now, hands out the env that points at it, and supervises the Chromium windows
ViberPit itself opens on it so they can be closed with the session.

A legacy Xorg grants SI:localuser, but a Wayland compositor's rootless Xwayland does
not: it demands the per-login, randomly-named cookie (it rotates every login), so
the resolver reads the live cookie from the X server's own argv/environ and proves
it with a real handshake. Resolutions are cached for a short TTL (the callers are
synchronous on an event loop). On a Wayland desktop, managed browsers run
Wayland-native (``--ozone-platform=wayland``) by default, while DISPLAY + XAUTHORITY
are still exported so X-only tools the agent spawns keep working.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Literal

from server.sessions.desktop_session import DesktopSession, DesktopUnavailable, resolve_desktop_session
from server.sessions.x11manager import CHROMIUM_CANDIDATES, _terminate, resolve_chromium

log = logging.getLogger("server.sessions.native_display")

_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")

BrowserPlatform = Literal["auto", "wayland", "x11"]
_PLATFORMS: tuple[BrowserPlatform, ...] = ("auto", "wayland", "x11")
# Launch-failure reasons (the orchestrator surfaces these verbatim).
REASON_NATIVE_UNAVAILABLE = "native_unavailable"
REASON_CAP = "cap"
REASON_LAUNCH_FAILED = "launch_failed"
REASON_DISPLAY_IN_POOL = "display_in_xvfb_pool"
# Inherited vars that would contradict the resolved desktop if they leaked through.
_STALE_ENV = ("WAYLAND_DISPLAY", "WAYLAND_SOCKET", "XDG_SESSION_TYPE", "XAUTHORITY")


def _display_number(display: str) -> int | None:
    m = _DISPLAY_RE.match(display.strip())
    return int(m.group(1)) if m else None


class NativeDisplay:
    def __init__(self, display: str | None = None, *, run_dir: str = "/tmp", chromium_cmd: str,
                 no_sandbox: bool, max_browsers: int, browser_liveness_s: float = 0.0,
                 spawn=asyncio.create_subprocess_exec,
                 resolve: Callable[[], DesktopSession | DesktopUnavailable] | None = None,
                 clock: Callable[[], float] = time.monotonic, ttl_s: float = 5.0,
                 browser_platform: BrowserPlatform = "auto",
                 blocked_range: range | None = None) -> None:
        self._display = (display or "").strip()
        self._run_dir = run_dir
        self._chromium_cmd = chromium_cmd
        self._no_sandbox = no_sandbox
        self._max_browsers = max_browsers
        self._browser_liveness_s = browser_liveness_s
        self._spawn = spawn
        self._resolve = resolve or (lambda: resolve_desktop_session(
            uid=os.getuid(), display=self._display or None))
        self._clock = clock
        self._ttl_s = ttl_s
        self._browser_platform = browser_platform
        self._blocked_range = blocked_range
        self._cached: DesktopSession | DesktopUnavailable | None = None
        self._stamp = 0.0
        self._browsers: dict[str, list] = {}          # session_id → managed Chromium procs
        self._profiles: dict[str, list[str]] = {}     # session_id → throwaway --user-data-dir dirs

    def _current(self) -> DesktopSession | DesktopUnavailable:
        """The desktop right now, re-resolved at most once per TTL. Evaluated live: a
        desktop that logs in after the daemon booted must work, and one that logs out
        must stop being offered."""
        now = self._clock()
        if self._cached is None or now - self._stamp >= self._ttl_s:
            self._cached = self._resolve()
            self._stamp = now
        return self._cached

    def _in_pool(self, session: DesktopSession | DesktopUnavailable) -> bool:
        """True when the target display (the resolved one, else the pinned one) is an
        Xvfb pool display — checked even when nothing resolved, so a pinned pool
        display reports the pool as the reason rather than 'no graphical session'."""
        if self._blocked_range is None:
            return False
        display = session.display if isinstance(session, DesktopSession) else self._display
        number = _display_number(display) if display else None
        return number is not None and number in self._blocked_range

    @property
    def display(self) -> str:
        """For log lines only: the configured display, else the resolved one, else ':0'."""
        if self._display:
            return self._display
        cur = self._current()
        return cur.display if isinstance(cur, DesktopSession) else ":0"

    def available(self) -> bool:
        cur = self._current()
        return isinstance(cur, DesktopSession) and not self._in_pool(cur)

    def unavailable_reason(self) -> str | None:
        cur = self._current()
        if self._in_pool(cur):
            return REASON_DISPLAY_IN_POOL
        return cur.reason if isinstance(cur, DesktopUnavailable) else None

    def _prefer_wayland(self, session: DesktopSession) -> bool:
        if self._browser_platform == "wayland":
            return True
        if self._browser_platform == "x11":
            return False
        return session.kind == "wayland"

    def env(self) -> dict[str, str]:
        cur = self._current()
        if not self.available() or not isinstance(cur, DesktopSession):
            return {}
        return cur.env(prefer_wayland=self._prefer_wayland(cur))

    def metrics(self) -> dict:
        cur = self._current()
        return {"display": self.display, "available": self.available(),
                "kind": cur.kind if isinstance(cur, DesktopSession) else None,
                "reason": self.unavailable_reason(),
                "browsers": sum(len(v) for v in self._browsers.values())}

    async def launch_browser(self, session_id: str, url: str | None) -> str | None:
        """Open a managed browser on the desktop. None = launched; else the failure
        reason (``native_unavailable`` / ``cap`` / ``launch_failed``)."""
        cur = self._current()
        if not self.available() or not isinstance(cur, DesktopSession):
            return REASON_NATIVE_UNAVAILABLE
        procs = self._browsers.setdefault(session_id, [])
        if len(procs) >= self._max_browsers:
            return REASON_CAP
        wayland = self._prefer_wayland(cur)
        argv = [self._chromium_cmd, "--disable-dev-shm-usage", "--no-first-run", "--new-window",
                f"--ozone-platform={'wayland' if wayland else 'x11'}"]
        if self._no_sandbox:
            argv.append("--no-sandbox")
        # Chromium enforces one process per --user-data-dir: a launch on the default
        # profile would hand off to the operator's own running Chrome and exit. A
        # throwaway profile per launch keeps ViberPit's windows ViberPit's.
        profile = tempfile.mkdtemp(prefix=f"viberpit-native-{session_id}-", dir=self._run_dir)
        argv.append(f"--user-data-dir={profile}")
        if url:
            argv.append(url)
        # Inherit the daemon env, but drop anything that could contradict the resolved
        # desktop (a stale inherited cookie or Wayland socket must never win).
        env = {k: v for k, v in os.environ.items() if k not in _STALE_ENV}
        env.update(self.env())
        try:
            proc = await self._spawn(*argv, env=env,
                                     stdout=asyncio.subprocess.DEVNULL,
                                     stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.exception("native display: chromium binary %r missing", self._chromium_cmd)
            shutil.rmtree(profile, ignore_errors=True)
            return REASON_LAUNCH_FAILED
        if self._browser_liveness_s > 0:
            await asyncio.sleep(self._browser_liveness_s)
            if getattr(proc, "returncode", None) is not None:
                log.warning("native display: browser for session %s exited during startup (rc=%s)",
                            session_id, proc.returncode)
                shutil.rmtree(profile, ignore_errors=True)
                return REASON_LAUNCH_FAILED
        procs.append(proc)
        self._profiles.setdefault(session_id, []).append(profile)
        return None

    async def close(self, session_id: str) -> None:
        procs = self._browsers.pop(session_id, [])
        for p in procs:
            await _terminate(p)
        for d in self._profiles.pop(session_id, []):
            shutil.rmtree(d, ignore_errors=True)

    async def stop_all(self) -> None:
        for sid in list(self._browsers) + [s for s in self._profiles if s not in self._browsers]:
            await self.close(sid)


def build_native_display(settings) -> NativeDisplay | None:
    """None when the feature is off or a configured display is not a local ':N'.

    An empty ``native_display`` (the default) auto-discovers the live desktop. A
    display inside the Xvfb pool range still returns an object, but one whose
    ``available()`` is always False, with one warning: X11Manager's orphan sweep
    reaps every Chromium in that range, so honouring it would kill the operator's
    windows on the next daemon boot (the collision spec 002 documents for
    VIBERPIT_REAUTH_DISPLAY).
    """
    if not getattr(settings, "native_display_enabled", True):
        return None
    display = str(getattr(settings, "native_display", "") or "").strip()
    if display and _display_number(display) is None:
        log.warning("native display: VIBERPIT_NATIVE_DISPLAY=%r is not a local ':N' display; disabled", display)
        return None
    platform = str(getattr(settings, "native_browser_platform", "auto") or "auto").strip().lower()
    if platform not in _PLATFORMS:
        log.warning("native display: VIBERPIT_NATIVE_BROWSER_PLATFORM=%r is not one of %s; using 'auto'",
                    platform, "/".join(_PLATFORMS))
        platform = "auto"
    blocked: range | None = None
    if getattr(settings, "enable_xvfb", False):
        start = int(getattr(settings, "display_start", 100))
        count = int(getattr(settings, "display_count", 12))
        blocked = range(start, start + count)
        number = _display_number(display) if display else None
        if number is not None and number in blocked:
            log.warning(
                "native display: %s is inside the Xvfb pool [%d, %d); native routing disabled "
                "on this host — move VIBERPIT_NATIVE_DISPLAY or VIBERPIT_DISPLAY_START",
                display, start, start + count)
    chromium = resolve_chromium()
    if chromium is None:
        log.warning("native display: no Chromium-family binary on PATH %s — 'Open a browser' "
                    "will fail for native sessions", CHROMIUM_CANDIDATES)
    return NativeDisplay(
        display or None,
        chromium_cmd=chromium or "chromium",
        no_sandbox=bool(getattr(settings, "chromium_no_sandbox", False)),
        max_browsers=int(getattr(settings, "max_browsers", 6)),
        browser_liveness_s=float(getattr(settings, "browser_liveness_s", 0.3)),
        browser_platform=platform,  # type: ignore[arg-type]  # validated against _PLATFORMS above
        blocked_range=blocked,
    )
