"""Per-platform session guide (host-platforms spec §4, designer calls 5 and 7).

A session routed for ``windows`` is told how to use the host's VM; a Linux session on
the same host is told nothing, and the guide follows the persisted platform across
every respawn.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace


from server.sessions.platform_guide import MAX_GUIDE_BYTES, load_platform_guide
from server.sessions.tools.instruction import session_instructions

GUIDE = "## Windows VM\nUse `winrun`."


def _cfg(path):
    return SimpleNamespace(host_platform_guide_windows=None if path is None else str(path))


def _guide_file(tmp_path, text=GUIDE):
    p = tmp_path / "windows-guide.md"
    p.write_text(text)
    return p


def _instructions(guide):
    return session_instructions(session_tools=True, signalling_tools=True,
                                self_model_switch=True, platform_guide=guide)


def test_guide_appended_only_for_windows_session(tmp_path):
    cfg = _cfg(_guide_file(tmp_path))
    guide = load_platform_guide("windows", cfg)
    assert guide == GUIDE
    out = _instructions(guide)
    assert out.endswith(GUIDE), "appended last"
    assert out.startswith(_instructions(None))


def test_linux_session_on_same_host_gets_no_guide(tmp_path):
    cfg = _cfg(_guide_file(tmp_path))
    assert load_platform_guide("linux", cfg) is None
    assert load_platform_guide(None, cfg) is None
    assert load_platform_guide("macos", cfg) is None
    assert "winrun" not in _instructions(load_platform_guide("linux", cfg))


def test_missing_guide_file_is_not_an_error(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        assert load_platform_guide("windows", _cfg(tmp_path / "nope.md")) is None
        assert load_platform_guide("windows", _cfg(None)) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_guide_is_capped_at_8_kib(tmp_path):
    cfg = _cfg(_guide_file(tmp_path, "x" * (MAX_GUIDE_BYTES * 2)))
    assert len(load_platform_guide("windows", cfg).encode()) == MAX_GUIDE_BYTES == 8192


def test_the_runbook_template_fits_the_cap_and_states_the_rules():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2]
            / "docs/runbooks/windows-vm-session-guide.md").read_text()
    assert len(text.encode()) <= MAX_GUIDE_BYTES
    for needle in ("ssh win11", "winsync", "winrun", "win11-vm.secret", "win11_ed25519",
                   "windows-latest"):
        assert needle in text


def test_settings_expose_the_guide_path(monkeypatch):
    from server.config import get_settings
    monkeypatch.setenv("VIBERPIT_HOST_PLATFORM_GUIDE_WINDOWS", "/etc/viberpit/windows-guide.md")
    assert get_settings().host_platform_guide_windows == "/etc/viberpit/windows-guide.md"


def test_guide_reapplied_on_respawn(tmp_path, monkeypatch):
    """Every spawn of the same session id re-reads the persisted platform and the file."""
    from server.sessions.orchestrator import SessionOrchestrator
    path = _guide_file(tmp_path)
    monkeypatch.setenv("VIBERPIT_HOST_PLATFORM_GUIDE_WINDOWS", str(path))
    seen: list = []

    async def base(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
                   on_pre_compact=None, platform_guide=None):
        seen.append(platform_guide)
        return object()

    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._sdk_factory = base
    orch._x11 = None
    orch._records = {}
    orch._overlays = {}
    orch._clock = time.time
    platforms = {"win": "windows", "lin": "linux"}

    async def _platform(sid):
        return platforms[sid]

    monkeypatch.setattr(orch, "_session_platform", _platform)
    monkeypatch.setattr(orch, "_rewind_spawn", lambda sid: False, raising=False)
    monkeypatch.setattr(orch, "_display_env", lambda sid: {}, raising=False)
    monkeypatch.setattr(orch, "_oat_env", lambda sid: {}, raising=False)

    async def go():
        for sid in ("win", "win", "lin"):
            await orch._session_factory(sid)("/tmp", "/cfg", None, "interactive", None)

    asyncio.run(go())
    assert seen == [GUIDE, GUIDE, None]
