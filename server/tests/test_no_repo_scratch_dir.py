"""No-repo sessions run in an isolated scratch dir, not the server cwd.

Regression test for issue #6: create_session with no repo/branch selected must
place the session cwd inside <ccswitch-base>/scratch/<session-id>/, never in
the server process cwd.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ccswitch.worktrees import worktrees_base
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def test_no_repo_session_cwd_is_scratch_dir_not_server_cwd(tmp_path):
    """A session created with no repo/branch must run in a scratch dir, not server cwd."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_path = tmp_path / "ccswitch.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch("server.sessions.orchestrator.select_account", return_value="alpha")

    with env_patch, compose_patch, trust_patch, select_patch:
        _seed_account("alpha")

        async def _run():
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
                usage_fn=_fake_usage,
                repo_root=str(repo_root),
            )
            await orch.start()
            try:
                rec = await orch.create_session("no-repo session")
            finally:
                await orch.stop()
            return rec

        rec = asyncio.run(_run())

    server_cwd = os.getcwd()

    # Must NOT be the server process cwd
    assert rec.cwd != server_cwd, (
        f"no-repo session cwd is the server cwd ({server_cwd!r}); "
        "it must be an isolated scratch directory"
    )

    # Must be inside the ccswitch scratch base (under the repo_root .ccswitch/scratch/)
    scratch_base = worktrees_base(str(repo_root)).parent / "scratch"
    assert rec.cwd.startswith(str(scratch_base)), (
        f"session cwd {rec.cwd!r} is not under the scratch base {scratch_base!r}"
    )

    # The directory must actually exist on disk
    assert os.path.isdir(rec.cwd), (
        f"scratch dir {rec.cwd!r} was not created on disk"
    )
