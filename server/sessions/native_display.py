"""The host's real X display as a session target (spec §4.3).

Counterpart of X11Manager (pooled Xvfb displays): this one never starts an X
server — it only checks that the operator's desktop is there, hands out the env
that points at it, and supervises the Chromium windows ccswitch itself opens on
it so they can be closed with the session. No XAUTHORITY is set: the daemon
runs as the desktop's user and the X server grants SI:localuser access (verified
on dobby; the reauth fallback has relied on the same posture since #331).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile

from server.sessions.x11manager import CHROMIUM_CANDIDATES, _terminate, resolve_chromium

log = logging.getLogger("server.sessions.native_display")

_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")


def _display_number(display: str) -> int | None:
    m = _DISPLAY_RE.match(display.strip())
    return int(m.group(1)) if m else None


class NativeDisplay:
    def __init__(self, display: str = ":0", *, run_dir: str = "/tmp", chromium_cmd: str,
                 no_sandbox: bool, max_browsers: int, browser_liveness_s: float = 0.0,
                 spawn=asyncio.create_subprocess_exec, socket_dir: str = "/tmp/.X11-unix",
                 blocked_range: range | None = None) -> None:
        self._display = display.strip()
        self._number = _display_number(self._display)
        self._run_dir = run_dir
        self._chromium_cmd = chromium_cmd
        self._no_sandbox = no_sandbox
        self._max_browsers = max_browsers
        self._browser_liveness_s = browser_liveness_s
        self._spawn = spawn
        self._socket_dir = socket_dir
        self._blocked_range = blocked_range
        self._browsers: dict[str, list] = {}          # session_id → managed Chromium procs
        self._profiles: dict[str, list[str]] = {}     # session_id → throwaway --user-data-dir dirs

    @property
    def display(self) -> str:
        return self._display

    def available(self) -> bool:
        """True when the X server socket exists right now and the display is outside
        the Xvfb pool range. Evaluated live: a desktop that logs in after the daemon
        booted must work, and one that logs out must stop being offered."""
        if self._number is None:
            return False
        if self._blocked_range is not None and self._number in self._blocked_range:
            return False
        return os.path.exists(os.path.join(self._socket_dir, f"X{self._number}"))

    def env(self) -> dict[str, str]:
        return {"DISPLAY": self._display}

    def metrics(self) -> dict:
        return {"display": self._display, "available": self.available(),
                "browsers": sum(len(v) for v in self._browsers.values())}

    async def launch_browser(self, session_id: str, url: str | None) -> bool:
        if not self.available():
            return False
        procs = self._browsers.setdefault(session_id, [])
        if len(procs) >= self._max_browsers:
            return False
        argv = [self._chromium_cmd, "--disable-dev-shm-usage", "--no-first-run", "--new-window"]
        if self._no_sandbox:
            argv.append("--no-sandbox")
        # Chromium enforces one process per --user-data-dir: a launch on the default
        # profile would hand off to the operator's own running Chrome and exit. A
        # throwaway profile per launch keeps ccswitch's windows ccswitch's.
        profile = tempfile.mkdtemp(prefix=f"ccswitch-native-{session_id}-", dir=self._run_dir)
        argv.append(f"--user-data-dir={profile}")
        if url:
            argv.append(url)
        # Inherit the daemon env and point DISPLAY at the desktop; deliberately no
        # XAUTHORITY (SI:localuser access — see the module docstring).
        env = dict(os.environ, **self.env())
        try:
            proc = await self._spawn(*argv, env=env,
                                     stdout=asyncio.subprocess.DEVNULL,
                                     stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.exception("native display: chromium binary %r missing", self._chromium_cmd)
            shutil.rmtree(profile, ignore_errors=True)
            return False
        if self._browser_liveness_s > 0:
            await asyncio.sleep(self._browser_liveness_s)
            if getattr(proc, "returncode", None) is not None:
                log.warning("native display: browser for session %s exited during startup (rc=%s)",
                            session_id, proc.returncode)
                shutil.rmtree(profile, ignore_errors=True)
                return False
        procs.append(proc)
        self._profiles.setdefault(session_id, []).append(profile)
        return True

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
    """None when the feature is off or the display string is not a local ':N'.

    A display inside the Xvfb pool range still returns an object, but one whose
    ``available()`` is always False, with one warning: X11Manager's orphan sweep
    reaps every Chromium in that range, so honouring it would kill the operator's
    windows on the next daemon boot (the collision spec 002 documents for
    CCSWITCH_REAUTH_DISPLAY).
    """
    if not getattr(settings, "native_display_enabled", True):
        return None
    display = str(getattr(settings, "native_display", ":0") or "").strip()
    number = _display_number(display)
    if number is None:
        log.warning("native display: CCSWITCH_NATIVE_DISPLAY=%r is not a local ':N' display; disabled", display)
        return None
    blocked: range | None = None
    if getattr(settings, "enable_xvfb", False):
        start = int(getattr(settings, "display_start", 100))
        count = int(getattr(settings, "display_count", 12))
        blocked = range(start, start + count)
        if number in blocked:
            log.warning(
                "native display: %s is inside the Xvfb pool [%d, %d); native routing disabled "
                "on this host — move CCSWITCH_NATIVE_DISPLAY or CCSWITCH_DISPLAY_START",
                display, start, start + count)
    chromium = resolve_chromium()
    if chromium is None:
        log.warning("native display: no Chromium-family binary on PATH %s — 'Open a browser' "
                    "will fail for native sessions", CHROMIUM_CANDIDATES)
    return NativeDisplay(
        display,
        chromium_cmd=chromium or "chromium",
        no_sandbox=bool(getattr(settings, "chromium_no_sandbox", False)),
        max_browsers=int(getattr(settings, "max_browsers", 6)),
        browser_liveness_s=float(getattr(settings, "browser_liveness_s", 0.3)),
        blocked_range=blocked,
    )
