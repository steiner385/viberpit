"""Tests for server.sessions.copilot_auth — device-flow prompt scraper + state machine."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from server.sessions.copilot_auth import _parse_device_prompt, CopilotAuthManager


# ---------------------------------------------------------------------------
# _parse_device_prompt — pure unit tests (no async)
# ---------------------------------------------------------------------------

def test_parse_device_prompt_extracts_code_and_url():
    line = "Please visit https://github.com/login/device and enter code ABCD-1234"
    uri, code = _parse_device_prompt(line)
    assert uri == "https://github.com/login/device"
    assert code == "ABCD-1234"


def test_parse_device_prompt_ignores_unrelated_lines():
    assert _parse_device_prompt("starting server on :4142") == (None, None)


def test_parse_device_prompt_uri_only_line():
    # URL present but no code — returns uri; callers accumulate across lines
    uri, code = _parse_device_prompt("Please visit: https://github.com/login/device")
    assert uri == "https://github.com/login/device"
    assert code is None


def test_parse_device_prompt_code_only_line():
    # Code present but no URL — returns code; callers accumulate across lines
    uri, code = _parse_device_prompt("Enter code: ABCD-1234")
    assert uri is None
    assert code == "ABCD-1234"


def test_parse_device_prompt_requires_uppercase_code():
    # lowercase code should not match — uri still returned if present
    uri, code = _parse_device_prompt(
        "Visit https://github.com/login/device and enter abcd-1234"
    )
    assert uri == "https://github.com/login/device"
    assert code is None  # lowercase code is not a valid device code


def test_parse_device_prompt_different_valid_code():
    line = "Go to https://github.com/login/device — code: X1Y2-A3B4"
    uri, code = _parse_device_prompt(line)
    assert uri == "https://github.com/login/device"
    assert code == "X1Y2-A3B4"


# ---------------------------------------------------------------------------
# State-machine tests — injected fake subprocess
# ---------------------------------------------------------------------------

class _FakeStreamReader:
    """Async-iterable line source for fake proc.stdout."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStreamReader(lines)
        self.returncode = None

    def terminate(self) -> None:
        pass


@pytest.mark.asyncio
async def test_state_machine_pending_to_authorized():
    """State progresses pending → awaiting_authorization → authorized.

    The fake stream yields a device-prompt line followed by a second dummy line.
    credentials.json is written between the two lines (simulating the proxy
    writing the file after the user completes device authorization). The reader
    detects the file on the second iteration and transitions to 'authorized'.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        token_dir = Path(tmpdir)
        creds_path = token_dir / "credentials.json"

        device_line = (
            b"Please visit https://github.com/login/device and enter code ABCD-1234\n"
        )

        class _WritingStreamReader:
            """Yields device_line then writes credentials.json then yields a second line."""
            def __init__(self):
                self._step = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self._step == 0:
                    self._step = 1
                    return device_line
                elif self._step == 1:
                    # Simulate credentials appearing while proc is still running
                    creds_path.write_text('{"token": "test"}')
                    self._step = 2
                    return b"proxy: login complete\n"
                else:
                    raise StopAsyncIteration

        class _WritingProc:
            def __init__(self):
                self.stdout = _WritingStreamReader()
            def terminate(self):
                pass

        async def fake_spawn(*args, stdout, stderr):
            return _WritingProc()

        manager = CopilotAuthManager(
            spawn=fake_spawn,
            token_dir_for=lambda account: token_dir,
        )

        with patch("server.sessions.copilot_proxy.resolve_copilot_proxy", return_value="/usr/bin/copilot2api"):
            result = await manager.start("myaccount")

        assert result["state"] == "pending"

        # Wait deterministically for the reader task to finish
        await asyncio.wait_for(manager._states["myaccount"]._task, timeout=2.0)

        final = manager.status("myaccount")
        assert final["state"] == "authorized"
        assert final["userCode"] == "ABCD-1234"
        assert final["verificationUri"] == "https://github.com/login/device"


@pytest.mark.asyncio
async def test_state_machine_multiline_device_prompt():
    """URI and code on separate lines (real copilot2api format) still transitions correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        token_dir = Path(tmpdir)
        creds_path = token_dir / "credentials.json"

        class _MultilineStream:
            """Simulates the real binary output: URI line, code line, then writes creds."""
            def __init__(self):
                self._lines = [
                    b"Please visit: https://github.com/login/device\n",
                    b"Enter code: X1Y2-A3B4\n",
                ]
                self._step = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self._step < len(self._lines):
                    line = self._lines[self._step]
                    self._step += 1
                    return line
                if not creds_path.exists():
                    creds_path.write_text('{"token": "test"}')
                return b"Waiting for authorization...\n" if self._step == len(self._lines) else (_ for _ in ()).throw(StopAsyncIteration)

        class _MLProc:
            def __init__(self):
                self.stdout = _MultilineStream()
            def terminate(self):
                pass

        async def fake_spawn(*args, stdout, stderr):
            return _MLProc()

        manager = CopilotAuthManager(
            spawn=fake_spawn,
            token_dir_for=lambda account: token_dir,
        )

        with patch("server.sessions.copilot_proxy.resolve_copilot_proxy", return_value="/usr/bin/copilot2api"):
            await manager.start("ml_account")

        await asyncio.wait_for(manager._states["ml_account"]._task, timeout=2.0)

        final = manager.status("ml_account")
        assert final["state"] == "authorized"
        assert final["userCode"] == "X1Y2-A3B4"
        assert final["verificationUri"] == "https://github.com/login/device"


@pytest.mark.asyncio
async def test_state_machine_failed_when_no_credentials():
    """State ends as failed when process exits without producing credentials.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        token_dir = Path(tmpdir)

        async def fake_spawn(*args, stdout, stderr):
            return _FakeProc([])  # no output, no credentials

        manager = CopilotAuthManager(
            spawn=fake_spawn,
            token_dir_for=lambda account: token_dir,
        )

        with patch("server.sessions.copilot_proxy.resolve_copilot_proxy", return_value="/usr/bin/copilot2api"):
            await manager.start("myaccount")

        # Wait deterministically for the reader task to finish
        await asyncio.wait_for(manager._states["myaccount"]._task, timeout=2.0)

        final = manager.status("myaccount")
        assert final["state"] == "failed"
        assert "credentials" in (final["error"] or "")


@pytest.mark.asyncio
async def test_cancel_resets_state():
    """cancel() terminates and resets state to pending."""
    with tempfile.TemporaryDirectory() as tmpdir:
        token_dir = Path(tmpdir)

        async def fake_spawn(*args, stdout, stderr):
            # Never-ending stream (won't EOF until cancelled)
            class _InfiniteReader:
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    await asyncio.sleep(100)  # block until cancelled
                    raise StopAsyncIteration
            proc = MagicMock()
            proc.stdout = _InfiniteReader()
            proc.terminate = MagicMock()
            return proc

        manager = CopilotAuthManager(
            spawn=fake_spawn,
            token_dir_for=lambda account: token_dir,
        )

        with patch("server.sessions.copilot_proxy.resolve_copilot_proxy", return_value="/usr/bin/copilot2api"):
            await manager.start("myaccount")

        # Yield to let task start before cancelling
        await asyncio.sleep(0)
        result = await manager.cancel("myaccount")
        assert result["state"] == "pending"


@pytest.mark.asyncio
async def test_start_fails_gracefully_when_binary_missing():
    """start() returns failed state immediately when binary is absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = CopilotAuthManager(
            spawn=asyncio.create_subprocess_exec,
            token_dir_for=lambda account: tmpdir,
        )

        with patch("server.sessions.copilot_proxy.resolve_copilot_proxy", return_value=None):
            result = await manager.start("myaccount")

        assert result["state"] == "failed"
        assert result["userCode"] is None
        assert result["verificationUri"] is None
