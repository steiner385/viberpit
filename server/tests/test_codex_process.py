"""Binary resolution, contract probing, CODEX_HOME safety, and child supervision."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from server.codex import capabilities as caps
from server.codex import process as proc
from server.codex import schema

LOCAL_MOUNTS = (("/", "ext4"), ("/tmp", "tmpfs"))


# ---------------------------------------------------------------------------
# resolve_codex_bin
# ---------------------------------------------------------------------------


def test_resolve_prefers_an_explicit_executable_path(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert proc.resolve_codex_bin(str(binary)) == str(binary)


def test_resolve_rejects_an_explicit_path_that_is_not_executable(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("")
    binary.chmod(0o644)
    assert proc.resolve_codex_bin(str(binary)) is None


def test_resolve_falls_back_to_path_lookup(monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert proc.resolve_codex_bin("codex") == "/usr/bin/codex"


def test_resolve_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)
    assert proc.resolve_codex_bin("codex") is None


# ---------------------------------------------------------------------------
# network-filesystem refusal
# ---------------------------------------------------------------------------


def test_filesystem_type_uses_the_longest_matching_mount_point():
    mounts = (("/", "ext4"), ("/mnt", "ext4"), ("/mnt/share", "fuse.sshfs"))
    assert proc.filesystem_type_for("/mnt/share/codex", mounts=mounts) == "fuse.sshfs"
    assert proc.filesystem_type_for("/mnt/other", mounts=mounts) == "ext4"


def test_filesystem_type_does_not_match_a_partial_path_component():
    mounts = (("/", "ext4"), ("/mnt/share", "nfs4"))
    # "/mnt/shared" must not match the "/mnt/share" mount point.
    assert proc.filesystem_type_for("/mnt/shared/x", mounts=mounts) == "ext4"


@pytest.mark.parametrize(
    "fstype", ["nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "sshfs", "fuseblk", "9p", "ceph"]
)
def test_network_filesystems_are_refused(fstype):
    mounts = (("/", "ext4"), ("/net", fstype))
    with pytest.raises(proc.CodexHomeRefused) as excinfo:
        proc.assert_local_filesystem("/net/codex", mounts=mounts)
    assert fstype in str(excinfo.value)


@pytest.mark.parametrize("fstype", ["ext4", "xfs", "btrfs", "tmpfs", "overlay", "zfs"])
def test_local_filesystems_are_accepted(fstype):
    assert proc.assert_local_filesystem("/data/codex", mounts=(("/", fstype),)) == fstype


def test_undeterminable_filesystem_is_refused():
    """No /proc/mounts means we cannot prove locality — fail closed, not open."""
    with pytest.raises(proc.CodexHomeRefused):
        proc.assert_local_filesystem("/data/codex", mounts=())


def test_filesystem_type_follows_a_symlinked_path_component(tmp_path):
    """A lexical (abspath) match reads the LINK's mount, not the target's (#1005)."""
    share = tmp_path / "share"
    share.mkdir()
    link = tmp_path / "state"
    link.symlink_to(share)
    mounts = (("/", "ext4"), (str(share), "fuse.sshfs"))
    assert proc.filesystem_type_for(link / "ccswitch" / "codex", mounts=mounts) == "fuse.sshfs"


def test_filesystem_type_resolves_a_path_that_does_not_exist_yet(tmp_path):
    """The refusal must run BEFORE the home is created, i.e. on a missing tail."""
    share = tmp_path / "share"
    share.mkdir()
    (tmp_path / "state").symlink_to(share)
    mounts = (("/", "ext4"), (str(share), "nfs4"))
    candidate = tmp_path / "state" / "ccswitch" / "codex" / "openai-main"
    assert not candidate.exists()
    assert proc.filesystem_type_for(candidate, mounts=mounts) == "nfs4"
    with pytest.raises(proc.CodexHomeRefused):
        proc.assert_local_filesystem(candidate, mounts=mounts)


def test_parse_proc_mounts_extracts_mount_point_and_type():
    text = (
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "tony@rocky:/home /mnt/share fuse.sshfs rw,nosuid 0 0\n"
        "garbage-line\n"
    )
    assert proc.parse_proc_mounts(text) == (("/", "ext4"), ("/mnt/share", "fuse.sshfs"))


def test_parse_proc_mounts_unescapes_octal_paths():
    assert proc.parse_proc_mounts("d /mnt/a\\040b ext4 rw 0 0\n") == (("/mnt/a b", "ext4"),)


# ---------------------------------------------------------------------------
# ensure_codex_home
# ---------------------------------------------------------------------------


def test_ensure_codex_home_creates_a_private_per_account_directory(tmp_path):
    home = proc.ensure_codex_home(tmp_path / "codex", "openai-main", mounts=LOCAL_MOUNTS)
    assert home == tmp_path / "codex" / "openai-main"
    assert home.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "codex").stat().st_mode) == 0o700


def test_ensure_codex_home_tightens_a_loose_existing_directory(tmp_path):
    home = tmp_path / "codex" / "acct"
    home.mkdir(parents=True)
    home.chmod(0o755)
    proc.ensure_codex_home(tmp_path / "codex", "acct", mounts=LOCAL_MOUNTS)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_ensure_codex_home_refuses_a_symlink(tmp_path):
    """Never symlink the runner home at the authority home (plan, Task 2.2)."""
    base = tmp_path / "codex"
    base.mkdir()
    (tmp_path / "authority").mkdir()
    (base / "acct").symlink_to(tmp_path / "authority")
    with pytest.raises(proc.CodexHomeRefused) as excinfo:
        proc.ensure_codex_home(base, "acct", mounts=LOCAL_MOUNTS)
    assert "symlink" in str(excinfo.value)


def test_ensure_codex_home_refuses_a_symlinked_ancestor(tmp_path):
    """An ancestor link redirects the home just as effectively as a link on it."""
    real = tmp_path / "authority"
    real.mkdir()
    (tmp_path / "state").symlink_to(real)
    base = tmp_path / "state" / "codex"
    base.mkdir()
    with pytest.raises(proc.CodexHomeRefused) as excinfo:
        proc.ensure_codex_home(base, "acct", mounts=LOCAL_MOUNTS)
    assert "symlink" in str(excinfo.value)
    assert not (real / "codex" / "acct").exists()  # refused BEFORE creating anything


def test_ensure_codex_home_refuses_a_network_mount_reached_through_a_symlink(tmp_path):
    """The deployed shape: ~/.local/state linked onto this fleet's sshfs share."""
    share = tmp_path / "share"
    share.mkdir()
    (tmp_path / "state").symlink_to(share)
    mounts = (("/", "ext4"), (str(share), "fuse.sshfs"))
    with pytest.raises(proc.CodexHomeRefused) as excinfo:
        proc.ensure_codex_home(tmp_path / "state" / "codex", "acct", mounts=mounts)
    assert "fuse.sshfs" in str(excinfo.value)
    assert not (share / "codex").exists()


def test_ensure_codex_home_refuses_a_network_filesystem(tmp_path):
    mounts = ((str(tmp_path), "nfs4"), ("/", "ext4"))
    with pytest.raises(proc.CodexHomeRefused):
        proc.ensure_codex_home(tmp_path / "codex", "acct", mounts=mounts)
    assert not (tmp_path / "codex").exists()  # refused BEFORE anything was created


def test_ensure_codex_home_rejects_a_traversing_account_name(tmp_path):
    with pytest.raises(proc.CodexHomeRefused):
        proc.ensure_codex_home(tmp_path / "codex", "../escape", mounts=LOCAL_MOUNTS)


# ---------------------------------------------------------------------------
# version probe + availability
# ---------------------------------------------------------------------------


def _runner(rc: int = 0, out: str = schema.PINNED_CLI_VERSION, err: str = ""):
    async def run(binary: str, *args: str) -> tuple[int, str, str]:
        return rc, out, err

    return run


def test_probe_cli_version_returns_the_trimmed_output():
    got = asyncio.run(proc.probe_cli_version("/usr/bin/codex", runner=_runner(out=" x 1.0 \n")))
    assert got == "x 1.0"


def test_probe_cli_version_raises_on_a_failed_exec():
    with pytest.raises(proc.CodexVersionProbeFailed):
        asyncio.run(proc.probe_cli_version("/usr/bin/codex", runner=_runner(rc=1, err="boom")))


def test_probe_cli_version_raises_on_empty_output():
    with pytest.raises(proc.CodexVersionProbeFailed):
        asyncio.run(proc.probe_cli_version("/usr/bin/codex", runner=_runner(out="  ")))


def _probe(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("external_auth_enabled", True)
    kw.setdefault("binary", "codex")
    kw.setdefault("resolver", lambda name: "/usr/bin/codex")
    kw.setdefault("runner", _runner())
    return asyncio.run(proc.probe_availability(**kw))


def test_probe_reports_disabled_when_the_flag_is_off():
    got = _probe(enabled=False)
    assert not got.available
    assert got.reason is caps.CodexUnavailableReason.DISABLED
    assert got.capabilities == caps.AgentCapabilities()


def test_probe_reports_external_auth_disabled():
    got = _probe(external_auth_enabled=False)
    assert got.reason is caps.CodexUnavailableReason.EXTERNAL_AUTH_DISABLED


def test_probe_reports_a_missing_binary():
    got = _probe(resolver=lambda name: None)
    assert got.reason is caps.CodexUnavailableReason.BINARY_NOT_FOUND


def test_probe_reports_a_failed_version_probe():
    got = _probe(runner=_runner(rc=127, err="not found"))
    assert got.reason is caps.CodexUnavailableReason.VERSION_PROBE_FAILED


def test_probe_reports_unsupported_version_with_the_typed_drift_report():
    got = _probe(runner=_runner(out="codex-cli 0.999.0"))
    assert got.reason is caps.CodexUnavailableReason.UNSUPPORTED_VERSION
    assert schema.ContractDriftReason.CLI_VERSION.value in got.detail
    assert got.cli_version == "codex-cli 0.999.0"
    assert got.capabilities == caps.AgentCapabilities()


def test_probe_accepts_the_pinned_version():
    got = _probe()
    assert got.available
    assert got.reason is None
    assert got.cli_version == schema.PINNED_CLI_VERSION
    assert got.binary_path == "/usr/bin/codex"
    assert got.capabilities == caps.CODEX_CAPABILITIES


def test_an_operator_may_widen_the_supported_version_set():
    got = _probe(runner=_runner(out="codex-cli 0.154.0"), supported_versions=("codex-cli 0.154.0",))
    assert got.available
    assert got.accepted_drift is not None
    assert got.accepted_drift.reasons == (schema.ContractDriftReason.CLI_VERSION,)


def test_widening_never_removes_the_pinned_version():
    got = _probe(supported_versions=("codex-cli 0.154.0",))
    assert got.available and got.accepted_drift is None


def test_probe_never_raises_when_the_resolver_explodes():
    """Codex unavailability must never propagate into a Claude session path."""

    def boom(name):
        raise OSError("stale file handle")

    got = _probe(resolver=boom)
    assert got.reason is caps.CodexUnavailableReason.BINARY_NOT_FOUND
    assert not got.available


# ---------------------------------------------------------------------------
# child environment
# ---------------------------------------------------------------------------


def test_child_env_is_a_minimal_allowlist_plus_codex_home():
    env = proc.build_child_env(
        Path("/var/lib/codex/acct"),
        parent_env={
            "PATH": "/usr/bin",
            "HOME": "/home/tony",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "CCSWITCH_MASTER_KEY": "hunter2",
            "OPENAI_API_KEY": "sk-proj-abcdefghijklmnop",
        },
    )
    assert env["CODEX_HOME"] == "/var/lib/codex/acct"
    assert env["PATH"] == "/usr/bin"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CCSWITCH_MASTER_KEY" not in env


def test_child_env_refuses_a_secret_shaped_allowlisted_value():
    """Credentials go over JSON-RPC stdin only — never into the child's env."""
    with pytest.raises(proc.CodexProcessError):
        proc.build_child_env(
            Path("/var/lib/codex/acct"),
            parent_env={"PATH": "/usr/bin", "HTTPS_PROXY": "http://sk-proj-abcdefghijklmn@p"},
        )


# ---------------------------------------------------------------------------
# CodexProcess supervision
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])

    async def readline(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeChild:
    def __init__(self, stderr_chunks: list[bytes] | None = None) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.stderr = _FakeStream(stderr_chunks)
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self._exited.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0


def _fake_spawn(child: _FakeChild, record: list):
    async def spawn(*argv, **kwargs):
        record.append((argv, kwargs))
        return child

    return spawn


def test_process_spawns_the_app_server_subcommand(tmp_path):
    async def go():
        child, calls = _FakeChild(), []
        p = proc.CodexProcess(
            "/usr/bin/codex", tmp_path, spawn=_fake_spawn(child, calls),
            parent_env={"PATH": "/usr/bin"},
        )
        await p.start()
        argv, kwargs = calls[0]
        assert argv == ("/usr/bin/codex", "app-server")
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        assert p.pid == 4242
        assert p.transport is not None
        await p.stop()
        assert child.terminated

    asyncio.run(go())


def test_process_stderr_tail_is_scrubbed(tmp_path):
    async def go():
        child = _FakeChild([b"panic: token sk-proj-abcdefghijklmnop rejected\n"])
        p = proc.CodexProcess(
            "/usr/bin/codex", tmp_path, spawn=_fake_spawn(child, []),
            parent_env={"PATH": "/usr/bin"},
        )
        await p.start()
        for _ in range(20):
            await asyncio.sleep(0)
        tail = p.stderr_tail()
        assert "sk-proj-abcdefghijklmnop" not in tail
        assert "[REDACTED]" in tail
        await p.stop()

    asyncio.run(go())


def test_process_kills_a_child_that_ignores_sigterm(tmp_path):
    async def go():
        child = _FakeChild()

        def ignore_term():
            child.terminated = True  # no exit

        child.terminate = ignore_term  # type: ignore[method-assign]
        p = proc.CodexProcess(
            "/usr/bin/codex", tmp_path, spawn=_fake_spawn(child, []),
            parent_env={"PATH": "/usr/bin"},
        )
        await p.start()
        await p.stop(timeout=0.05)
        assert child.killed

    asyncio.run(go())


def test_process_start_failure_is_typed(tmp_path):
    async def go():
        async def spawn(*argv, **kwargs):
            raise FileNotFoundError("/usr/bin/codex")

        p = proc.CodexProcess(
            "/usr/bin/codex", tmp_path, spawn=spawn, parent_env={"PATH": "/usr/bin"})
        with pytest.raises(proc.CodexProcessError):
            await p.start()

    asyncio.run(go())


def test_process_wait_reports_the_exit_code(tmp_path):
    async def go():
        child = _FakeChild()
        p = proc.CodexProcess(
            "/usr/bin/codex", tmp_path, spawn=_fake_spawn(child, []),
            parent_env={"PATH": "/usr/bin"})
        await p.start()
        waiter = asyncio.ensure_future(p.wait())
        await asyncio.sleep(0)
        child.returncode = 3
        child._exited.set()
        assert await asyncio.wait_for(waiter, 1.0) == 3

    asyncio.run(go())


def test_version_probe_kills_a_child_that_outlives_its_timeout():
    """wait_for only cancels communicate(); the wedged child must not be orphaned."""

    class _WedgedChild:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False
            self._exited = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()  # never returns
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self._exited.set()

        async def wait(self) -> int:
            await self._exited.wait()
            return self.returncode or 0

    async def go():
        child = _WedgedChild()

        async def spawn(*argv, **kwargs):
            return child

        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(
                proc._default_runner("/usr/bin/codex", "--version", spawn=spawn), 0.02
            )
        assert child.killed
        assert child.returncode == -9  # killed AND reaped

    asyncio.run(go())


def test_process_stop_re_raises_an_outer_cancellation():
    """Swallowing CancelledError here would eat the daemon drain's stop signal."""

    async def go():
        child = _FakeChild()
        p = proc.CodexProcess(
            "/usr/bin/codex", "/tmp", spawn=_fake_spawn(child, []),
            parent_env={"PATH": "/usr/bin"},
        )
        await p.start()

        async def stubborn() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)  # slow to die: we get cancelled meanwhile
                raise

        assert p._stderr_task is not None
        p._stderr_task.cancel()
        p._stderr_task = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        stopping = asyncio.ensure_future(p.stop(timeout=0.01))
        await asyncio.sleep(0.01)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping

    asyncio.run(go())


def test_default_mounts_reader_reads_the_real_proc_mounts():
    """The refusal check must consult the REAL mount table, not a guess."""
    if not os.path.exists("/proc/mounts"):  # pragma: no cover - linux deploy target
        pytest.skip("no /proc/mounts")
    mounts = proc.read_mounts()
    assert any(point == "/" for point, _ in mounts)
    assert proc.filesystem_type_for(os.getcwd()) is not None
