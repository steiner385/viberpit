"""The host bridge never replaces existing objects or changes process identity."""
import importlib.util
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "host_aliases", Path(__file__).parents[1] / "scripts/viberpit-host-aliases.py"
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


@pytest.fixture
def host(tmp_path):
    for directory in ("etc/ccswitch", "var/lib/ccswitch", "run/ccswitch",
                      "etc/systemd/system", "etc/tmpfiles.d", "usr/local/bin",
                      "home/tony/GitHub/cc-account-switcher-deploy/.venv/bin"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "etc/ccswitch/ccswitch.env").touch()
    (tmp_path / "etc/systemd/system/ccswitch-daemon.service").write_text("[Service]\n")
    (tmp_path / "etc/systemd/system/ccswitch-daemon-ensure.timer").touch()
    target = tmp_path / "home/tony/GitHub/cc-account-switcher-deploy/.venv/bin/ccswitch"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    return tmp_path


def systemctl(*args):
    if args == ("daemon-reload",):
        return ""
    assert args[0] == "show"
    unit = args[1].replace("viberpit", "ccswitch")
    process = "" if unit.endswith(".timer") else f"MainPID=123\nInvocationID=abc\nControlGroup=/system.slice/{unit}\n"
    return f"Id={unit}\n{process}ActiveState=active\nUnitFileState=disabled\nDescription=ccswitch session daemon\n"


def test_dry_run_does_not_write(host):
    before = set(host.rglob("*"))
    result = bridge.run("dobby", root=host, hostname="dobby", uid=0, systemctl=systemctl)
    assert result["planned"]
    assert set(host.rglob("*")) == before


def test_conflict_preflight_creates_nothing(host):
    conflict = host / "usr/local/bin/viberpit"
    conflict.write_text("unrelated")
    before = set(host.rglob("*"))
    with pytest.raises(RuntimeError, match="Conflict"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)
    assert set(host.rglob("*")) == before
    assert conflict.read_text() == "unrelated"


def test_apply_idempotent_and_manifest_only_created_objects(host):
    result = bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)
    assert (host / "etc/viberpit").resolve() == host / "etc/ccswitch"
    assert (host / "etc/viberpit/viberpit.env").resolve() == host / "etc/ccswitch/ccswitch.env"
    assert (host / "etc/systemd/system/viberpit-daemon-ensure.timer").is_symlink()
    assert (host / "etc/tmpfiles.d/viberpit.conf").read_text() == "L /run/viberpit - - - - /run/ccswitch\n"
    assert result["created"] and Path(result["manifest"]).exists()
    again = bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)
    assert again["created"] == []


def test_identity_change_fails_and_keeps_manifest(host):
    reloaded = False

    def changed(*args):
        nonlocal reloaded
        if args == ("daemon-reload",):
            reloaded = True
        return systemctl(*args).replace("MainPID=123", f"MainPID={456 if reloaded else 123}")

    with pytest.raises(RuntimeError, match="changed"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=changed)
    assert list((host / "var/lib").glob("viberpit-host-aliases-*.jsonl"))


@pytest.mark.parametrize("hostname,uid", [("rocky", 0), ("dobby", 1000)])
def test_host_and_root_guards(host, hostname, uid):
    with pytest.raises(RuntimeError):
        bridge.run("dobby", apply=True, root=host, hostname=hostname, uid=uid, systemctl=systemctl)
    assert not (host / "etc/viberpit").exists()


def test_broken_symlink_is_conflict(host):
    os.symlink("missing", host / "etc/viberpit")
    with pytest.raises(RuntimeError, match="Conflict"):
        bridge.run("dobby", root=host, hostname="dobby", systemctl=systemctl)


def test_alias_must_resolve_same_unit_even_on_rerun(host):
    bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)

    def wrong_alias(*args):
        output = systemctl(*args)
        return output.replace("Id=ccswitch", "Id=other") if args[1].startswith("viberpit") else output

    with pytest.raises(RuntimeError, match="Alias identity"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=wrong_alias)


def test_existing_description_is_rebranded_without_losing_role(host):
    bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)
    dropin = host / "etc/systemd/system/ccswitch-daemon.service.d/90-viberpit-description.conf"
    assert dropin.read_text() == "[Unit]\nDescription=ViberPit session daemon\n"


def test_missing_required_source_aborts_all_writes(host):
    (host / "run/ccswitch").rmdir()
    with pytest.raises(RuntimeError, match="Missing required source"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)
    assert not (host / "etc/viberpit").exists()


def test_excludes_retired_units_and_optional_missing_paths(host):
    (host / "etc/systemd/system/ccswitch-deploy.service").touch()
    result = bridge.run("dobby", root=host, hostname="dobby", systemctl=systemctl)
    paths = [item["path"] for item in result["planned"]]
    assert "/etc/systemd/system/viberpit-deploy.service" not in paths
    assert "/home/tony/.viberpit" not in paths
    assert "/var/log/viberpit" not in paths


@pytest.mark.parametrize("service_state", ["static", "disabled", "enabled"])
def test_real_systemctl_alias_preserves_boot_enablement(host, service_state):
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        pytest.skip("systemctl is required for the offline unit-file regression")
    timer = host / "etc/systemd/system/ccswitch-daemon-ensure.timer"
    timer.write_text("[Unit]\nDescription=ccswitch daemon ensure\n[Timer]\nOnBootSec=1min\n[Install]\nWantedBy=timers.target\n")
    service = host / "etc/systemd/system/ccswitch-daemon.service"
    install = "" if service_state == "static" else "[Install]\nWantedBy=multi-user.target\n"
    service.write_text("[Service]\nExecStart=/bin/true\n" + install)
    if service_state == "enabled":
        wants = service.parent / "multi-user.target.wants"
        wants.mkdir()
        (wants / service.name).symlink_to("../" + service.name)
    before_wants = set(service.parent.glob("*.wants/*"))

    def offline_state(unit):
        probe = subprocess.run(
            ["systemctl", "--root", str(host), "is-enabled", unit],
            capture_output=True, text=True, check=False,
        )
        assert probe.returncode in (0, 1), probe.stderr
        return probe.stdout.strip()

    def offline(*args):
        if args == ("daemon-reload",):
            return ""  # no live manager involved; is-enabled reads the fixture
        state = offline_state(args[1])
        return systemctl(*args).replace("UnitFileState=disabled", f"UnitFileState={state}")

    assert offline_state(timer.name) == "disabled"
    assert offline_state(service.name) == service_state
    bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=offline)
    assert offline_state(timer.name) == "indirect"
    assert offline_state(service.name) == ("enabled" if service_state == "enabled" else "indirect")
    assert set(service.parent.glob("*.wants/*")) == before_wants
    bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=offline)


def test_new_boot_dependency_fails_even_with_allowed_indirect_transition(host):
    def changes_dependency(*args):
        if args == ("daemon-reload",):
            wants = host / "etc/systemd/system/timers.target.wants"
            wants.mkdir()
            (wants / "ccswitch-daemon-ensure.timer").symlink_to("../ccswitch-daemon-ensure.timer")
        return systemctl(*args).replace("UnitFileState=disabled", "UnitFileState=indirect")

    with pytest.raises(RuntimeError, match="boot dependency links changed"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=changes_dependency)


def test_enabled_transition_remains_forbidden(host):
    reloaded = False

    def changes_state(*args):
        nonlocal reloaded
        if args == ("daemon-reload",):
            reloaded = True
        return systemctl(*args).replace("UnitFileState=disabled", "UnitFileState=enabled" if reloaded else "UnitFileState=disabled")

    with pytest.raises(RuntimeError, match="Systemd state changed"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=changes_state)


def test_cli_link_with_relocated_parent_resolves_same_executable(host):
    physical = host / "mnt/cache/tony/bin"
    physical.mkdir(parents=True)
    logical = host / "home/tony/.local/bin"
    logical.parent.mkdir(parents=True)
    logical.symlink_to(physical)
    checkout = host / "home/tony/GitHub/cc-account-switcher-deploy"
    (checkout / ".venv").rename(checkout / ".venv-owned")
    (checkout / ".venv").symlink_to(".venv-owned")
    bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)
    target = checkout / ".venv/bin/ccswitch"
    assert (logical / "viberpit").resolve() == target.resolve()
    assert os.access(logical / "viberpit", os.X_OK)
    bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=systemctl)


def test_snapshot_preserves_repeated_environment_files(host):
    def multiple_envs(*args):
        return systemctl(*args) + "EnvironmentFiles=/etc/ccswitch/ccswitch.env (ignore_errors=no)\nEnvironmentFiles=/etc/ccswitch/host.env (ignore_errors=no)\n"

    result = bridge.run("dobby", root=host, hostname="dobby", systemctl=multiple_envs)
    assert result["before"]["ccswitch-daemon.service"]["EnvironmentFiles"] == (
        "/etc/ccswitch/ccswitch.env (ignore_errors=no)\n/etc/ccswitch/host.env (ignore_errors=no)"
    )


def test_change_to_first_environment_file_is_not_hidden_by_last(host):
    reloaded = False

    def changing_envs(*args):
        nonlocal reloaded
        if args == ("daemon-reload",):
            reloaded = True
            return ""
        first = "changed.env" if reloaded else "ccswitch.env"
        return systemctl(*args) + f"EnvironmentFiles=/etc/ccswitch/{first}\nEnvironmentFiles=/etc/ccswitch/host.env\n"

    with pytest.raises(RuntimeError, match="Systemd state changed"):
        bridge.run("dobby", apply=True, root=host, hostname="dobby", uid=0, systemctl=changing_envs)
