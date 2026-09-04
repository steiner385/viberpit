"""Shared fake SDK factory for orchestrator/WS tests.

Builds a :class:`FakeSDKClient` instead of a real ``ClaudeSDKClient`` so tests
exercise the real orchestrator/driver code path without spawning ``claude`` or
spending quota.  Pass ``messages`` to drive the turn the fake will stream when a
prompt is sent; the default (empty) is fine for tests that only need a live
driver to exist (create/close/worktree/REST).
"""
from __future__ import annotations

from server.tests.fakes.fake_sdk import FakeSDKClient


def fake_sdk_factory(messages=None):
    """Return a ``(cwd, config_dir, can_use_tool, mode, resume) -> FakeSDKClient`` factory."""
    msgs = messages or []

    # Mirror real_sdk_factory's signature — the orchestrator's factory wrapper
    # forwards extra_env=... whenever a session has X11/OAT env, so a fake that
    # omits it raises TypeError on those sessions ("accept and ignore it").
    # ``options_overlay`` arrives the same way for an assistant-kind session.
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
             on_pre_compact=None, options_overlay=None):
        return FakeSDKClient(list(msgs), on_can_use_tool=can_use_tool)

    return make
