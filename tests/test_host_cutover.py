"""Offline host fixtures exercise the actual migration filesystem operations."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "host_cutover", Path(__file__).parents[1] / "scripts/viberpit-host-cutover.py"
)
assert SPEC and SPEC.loader
cutover = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cutover)


def put(root, name, text=""):
    path = root / name.lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "host"
    unit = put(root, "/etc/systemd/system/ccswitch-daemon.service",
               "[Service]\nExecStart=/home/tony/GitHub/ccswitch/.venv/bin/python -m server.ipc\n"
               "EnvironmentFile=/etc/ccswitch/ccswitch.env\nRuntimeDirectory=ccswitch\n"
               "KillMode=process\nStateDirectory=ccswitch\nSyslogIdentifier=ccswitch-daemon\n")
    unit.with_name("viberpit-daemon.service").symlink_to(unit.name)
    put(root, "/etc/systemd/system/ccswitch-mounts.service", "[Service]\nExecStart=/mount\n")
    put(root, "/etc/systemd/system.control/ccswitch-daemon.service.d/50-MemoryMax.conf",
        "[Service]\nMemoryMax=123456\n")
    put(root, "/etc/systemd/system/ccswitch-daemon.service.d/security.conf",
        "[Service]\nProtectHome=yes\nUser=ccswitch-auth\n"
        "ReadOnlyPaths=/var/lib/ccswitch-codex-auth\n")
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir()
    (wants / unit.name).symlink_to("../" + unit.name)
    put(root, "/etc/ccswitch/ccswitch.env", "SECRET=do-not-display\n")
    (root / "etc/ccswitch/viberpit.env").symlink_to("ccswitch.env")
    for base in ("etc", "var/lib", "run", "home/tony/GitHub"):
        source = root / base / "ccswitch"
        source.mkdir(parents=True, exist_ok=True)
        source.with_name("viberpit").symlink_to("ccswitch")
    put(root, "/etc/tmpfiles.d/viberpit.conf", "L /run/viberpit - - - - /run/ccswitch\n")
    return root


def stopped(*args):
    return "LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\n"


def test_round_trip_preserves_installed_units_and_private_state(host, tmp_path):
    plan = cutover.build_plan("rocky", host)
    bundle = tmp_path / "backup"
    cutover.prepare(plan, bundle)
    cutover.apply(bundle, systemctl=stopped)
    units = host / "etc/systemd/system"
    body = (units / "viberpit-daemon.service").read_text()
    assert "RuntimeDirectory=viberpit" in body
    assert "KillMode=process" in body
    assert "SyslogIdentifier=viberpit-daemon" in body
    assert "/etc/viberpit/viberpit.env" in body
    assert (units / "ccswitch-daemon.service").is_symlink()
    assert (units / "ccswitch-mounts.service").is_file()
    assert not (units / "ccswitch-mounts.service").is_symlink()
    assert (host / "etc/systemd/system.control/viberpit-daemon.service.d/50-MemoryMax.conf").read_text().endswith("123456\n")
    security = (units / "viberpit-daemon.service.d/security.conf").read_text()
    assert "User=ccswitch-auth" in security
    assert "/var/lib/ccswitch-codex-auth" in security
    assert (units / "multi-user.target.wants/viberpit-daemon.service").resolve() == units / "viberpit-daemon.service"
    assert not cutover.build_plan("rocky", host)["operations"]
    cutover.rollback(bundle, systemctl=stopped)
    assert not (units / "ccswitch-daemon.service").is_symlink()
    assert (units / "viberpit-daemon.service").is_symlink()
    assert (host / "etc/viberpit").is_symlink()
    assert (host / "etc/ccswitch/ccswitch.env").read_text() == "SECRET=do-not-display\n"


def test_conflict_preflight_has_no_mutations(host):
    target = host / "etc/viberpit"
    target.unlink()
    target.mkdir()
    with pytest.raises(RuntimeError, match="Conflict"):
        cutover.build_plan("rocky", host)
    assert not (host / "etc/ccswitch").is_symlink()


def test_stopped_guard_and_config_drift(host, tmp_path):
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    with pytest.raises(RuntimeError, match="stopped"):
        cutover.apply(bundle, systemctl=lambda *args: stopped().replace("MainPID=0", "MainPID=22"))
    put(host, "/etc/systemd/system/ccswitch-daemon.service.d/new.conf", "[Service]\nTasksMax=20\n")
    with pytest.raises(RuntimeError, match="changed"):
        cutover.apply(bundle, systemctl=stopped)


def test_relocated_parent(host, tmp_path):
    relocated = tmp_path / "cache"
    relocated.mkdir()
    parent = host / "home/tony/.cache"
    parent.symlink_to(relocated)
    (relocated / "ccswitch").mkdir()
    (relocated / "viberpit").symlink_to("ccswitch")
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    cutover.apply(bundle, systemctl=stopped)
    assert not (relocated / "viberpit").is_symlink()
    assert (relocated / "ccswitch").resolve() == relocated / "viberpit"


@pytest.mark.parametrize("fail_after", [False, True])
def test_interrupted_operation_rolls_back(host, tmp_path, monkeypatch, fail_after):
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    original = cutover.perform
    count = 0

    def interrupted(operation):
        nonlocal count
        count += 1
        if count == 8:
            if fail_after:
                original(operation)
            raise OSError("simulated interruption")
        original(operation)

    monkeypatch.setattr(cutover, "perform", interrupted)
    with pytest.raises(OSError, match="interruption"):
        cutover.apply(bundle, systemctl=stopped)
    monkeypatch.setattr(cutover, "perform", original)
    cutover.rollback(bundle, systemctl=stopped)
    assert not (host / "etc/systemd/system/ccswitch-daemon.service").is_symlink()
    assert (host / "etc/systemd/system/viberpit-daemon.service").is_symlink()


def test_retired_watchdogs_guarded_and_missing_mainpid_rejected(host, tmp_path):
    put(host, "/etc/systemd/system/ccswitch-deploy.timer", "[Timer]\nOnBootSec=60\n")
    plan = cutover.build_plan("rocky", host)
    assert "ccswitch-deploy.timer" in plan["guards"]
    bundle = tmp_path / "backup"
    cutover.prepare(plan, bundle)
    with pytest.raises(RuntimeError, match="stopped"):
        cutover.apply(bundle, systemctl=lambda *args: stopped().replace("MainPID=0\n", ""))
    assert not (host / "etc/ccswitch").is_symlink()


def test_configuration_metadata_and_no_secret_summary(host, tmp_path):
    import os

    unit = host / "etc/systemd/system/ccswitch-daemon.service"
    unit.chmod(0o640)
    os.setxattr(unit, "user.cutover-test", b"retained")
    before = unit.stat()
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    assert bundle.stat().st_mode & 0o077 == 0
    assert (bundle / "plan.json").stat().st_mode & 0o077 == 0
    cutover.apply(bundle, systemctl=stopped)
    assert unit.stat().st_mode == before.st_mode
    assert unit.stat().st_mtime_ns == before.st_mtime_ns
    assert os.getxattr(unit, "user.cutover-test") == b"retained"
    cutover.rollback(bundle, systemctl=stopped)
    assert unit.stat().st_mtime_ns == before.st_mtime_ns


def test_reverse_plan_conflicts_do_not_overwrite_edits(host, tmp_path):
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    cutover.apply(bundle, systemctl=stopped)
    rule = host / "etc/tmpfiles.d/viberpit.conf"
    rule.write_text("operator edit\n")
    with pytest.raises(RuntimeError, match="Rollback file changed"):
        cutover.rollback(bundle, systemctl=stopped)
    assert rule.read_text() == "operator edit\n"


def test_helper_names_change_without_touching_live_mount_unit(host, tmp_path):
    health = put(host, "/usr/local/bin/ccswitch-health-check.sh", "#!/bin/sh\nsystemctl is-active ccswitch-daemon.service\n")
    helper = put(host, "/home/tony/bin-ccswitch-mounts.sh", "#!/bin/sh\n# exact installed mount topology\n")
    helper.with_name("bin-viberpit-mounts.sh").symlink_to(helper.name)
    unit = host / "etc/systemd/system/ccswitch-daemon.service"
    with unit.open("a") as output:
        output.write("ExecStartPost=/usr/local/bin/ccswitch-health-check.sh\n")
    before = cutover.snapshot(host / "etc/systemd/system/ccswitch-mounts.service")
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    cutover.apply(bundle, systemctl=stopped)
    assert health.is_symlink()
    assert helper.is_symlink()
    assert "ExecStartPost=/usr/local/bin/viberpit-health-check.sh" in unit.read_text()
    assert cutover.snapshot(host / "etc/systemd/system/ccswitch-mounts.service") == before
    assert helper.read_text() == "#!/bin/sh\n# exact installed mount topology\n"


def test_systemd_alias_garbage_collection_during_apply_and_rollback(host, tmp_path):
    queried = set()

    def manager(*args):
        name = args[1]
        queried.add(name)
        present = (host / "etc/systemd/system" / name).exists()
        return stopped().replace("LoadState=loaded", "LoadState=loaded" if present else "LoadState=not-found")

    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    cutover.apply(bundle, systemctl=manager)
    cutover.rollback(bundle, systemctl=manager)
    assert {"ccswitch-daemon.service", "viberpit-daemon.service"} <= queried


def test_guard_missing_pair_or_missing_retired_unit_is_not_safe(host):
    plan = cutover.build_plan("rocky", host)
    with pytest.raises(RuntimeError, match="loaded"):
        cutover.stopped(plan, lambda *args: stopped().replace("loaded", "not-found"))
    put(host, "/etc/systemd/system/ccswitch-deploy.timer", "[Timer]\nOnBootSec=60\n")
    plan = cutover.build_plan("rocky", host)
    with pytest.raises(RuntimeError, match="loaded"):
        cutover.stopped(plan, lambda *args: stopped().replace(
            "loaded", "not-found" if args[1] == "ccswitch-deploy.timer" else "loaded"))


@pytest.mark.parametrize("unsafe", ["LoadState=masked", "ActiveState=active", "MainPID=123"])
def test_guard_does_not_hide_live_or_invalid_old_identity(host, unsafe):
    plan = cutover.build_plan("rocky", host)
    key = unsafe.split("=")[0]
    original = {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"}[key]
    with pytest.raises(RuntimeError, match="stopped"):
        cutover.stopped(plan, lambda *args: stopped().replace(
            f"{key}={original}", unsafe) if args[1] == "ccswitch-daemon.service" else stopped())


def test_prepare_syncs_parent_before_persisting_journal(host, tmp_path, monkeypatch):
    events = []
    original_sync = cutover.sync_directory
    original_save = cutover.save

    def sync(path):
        events.append(("sync", path))
        original_sync(path)

    def save(path, value):
        events.append(("save", path))
        original_save(path, value)

    monkeypatch.setattr(cutover, "sync_directory", sync)
    monkeypatch.setattr(cutover, "save", save)
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    assert events[0] == ("sync", tmp_path)
    assert events[1] == ("save", bundle / "plan.json")


def test_only_known_migrated_syslog_identifiers_are_rewritten(host, tmp_path):
    config = put(host, "/etc/systemd/system/ccswitch-daemon.service.d/logging.conf",
                 "[Service]\nSyslogIdentifier=ccswitch-daemon-custom\n"
                 "Environment=PACKAGE=ccswitch\nUser=ccswitch-auth\n")
    mount = host / "etc/systemd/system/ccswitch-mounts.service"
    mount.write_text("[Service]\nSyslogIdentifier=ccswitch-mounts\n")
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    cutover.apply(bundle, systemctl=stopped)
    assert "SyslogIdentifier=ccswitch-daemon-custom" in config.read_text()
    assert "Environment=PACKAGE=ccswitch" in config.read_text()
    assert "User=ccswitch-auth" in config.read_text()
    assert "SyslogIdentifier=ccswitch-mounts" in mount.read_text()


def test_forward_cli_aliases_repoint_logical_checkout_with_relocated_parent(host, tmp_path):
    import os

    old_cli = put(host, "/home/tony/GitHub/ccswitch/.venv-owned/bin/ccswitch", "#!/bin/sh\nexit 0\n")
    old_cli.chmod(0o755)
    checkout = host / "home/tony/GitHub/ccswitch"
    (checkout / ".venv").symlink_to(".venv-owned")
    relocated = tmp_path / "relocated-bin"
    relocated.mkdir()
    logical_parent = host / "home/tony/.local/bin"
    logical_parent.parent.mkdir(exist_ok=True)
    logical_parent.symlink_to(relocated)
    aliases = []
    for parent in (host / "usr/local/bin", logical_parent):
        parent.mkdir(parents=True, exist_ok=True)
        alias = parent / "viberpit"
        alias.symlink_to(os.path.relpath(old_cli, parent.resolve()))
        aliases.append(alias)
    old_targets = [os.readlink(alias) for alias in aliases]
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan("rocky", host), bundle)
    cutover.apply(bundle, systemctl=stopped)
    for alias in aliases:
        assert "GitHub/viberpit/.venv/bin/ccswitch" in os.readlink(alias)
        assert alias.is_file()
        assert os.access(alias, os.X_OK)
    assert not cutover.build_plan("rocky", host)["operations"]
    cutover.rollback(bundle, systemctl=stopped)
    assert [os.readlink(alias) for alias in aliases] == old_targets


def test_foreign_cli_symlink_conflict_preflight(host):
    foreign = put(host, "/usr/local/bin/unrelated", "#!/bin/sh\n")
    foreign.with_name("viberpit").symlink_to(foreign.name)
    with pytest.raises(RuntimeError, match="CLI"):
        cutover.build_plan("rocky", host)
    assert not (host / "etc/ccswitch").is_symlink()


@pytest.mark.parametrize("machine,old_checkout,new_checkout", [
    ("dobby", "cc-account-switcher-deploy", "viberpit-deploy"),
    ("rocky", "ccswitch", "viberpit"),
])
def test_checkout_health_execstart_uses_canonical_script(host, tmp_path, machine, old_checkout, new_checkout):
    unit = put(host, "/etc/systemd/system/ccswitch-health.service",
               f"[Service]\nExecStart=/home/tony/GitHub/{old_checkout}/scripts/ccswitch-health-check.sh\n"
               "ExecStartPost=/unrelated/scripts/ccswitch-health-check.sh\n")
    bundle = tmp_path / "backup"
    cutover.prepare(cutover.build_plan(machine, host), bundle)
    cutover.apply(bundle, systemctl=stopped)
    assert f"ExecStart=/home/tony/GitHub/{new_checkout}/scripts/viberpit-health-check.sh" in unit.read_text()
    assert "ExecStartPost=/unrelated/scripts/ccswitch-health-check.sh" in unit.read_text()
