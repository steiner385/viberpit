#!/usr/bin/env python3
"""Add ViberPit host aliases without replacing or restarting running services.

The injected root/systemctl arguments are a test seam, deliberately not CLI options.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Callable
import uuid

# Bounded to the units shipped for these two hosts; retired deploy units excluded.
UNIT_STEMS = (
    "ccswitch", "ccswitch-daemon", "ccswitch-daemon-remote",
    "ccswitch-deploy-controller", "ccswitch-codex-auth", "ccswitch-mounts",
    "ccswitch-cert-renew", "ccswitch-daemon-ensure", "ccswitch-health",
)
PROPERTIES = (
    "Id", "MainPID", "InvocationID", "ControlGroup", "ActiveState", "SubState",
    "UnitFileState", "KillMode", "TimeoutStopUSec", "EnvironmentFiles", "Description",
)
IDENTITY = ("Id", "MainPID", "InvocationID", "ControlGroup", "ActiveState", "SubState")


def call_systemctl(*args: str) -> str:
    return subprocess.run(
        ["systemctl", *args], check=True, text=True, capture_output=True, timeout=60,
    ).stdout


def run(
    host: str, *, apply: bool = False, root: Path = Path("/"),
    hostname: str | None = None, uid: int | None = None,
    systemctl: Callable[..., str] = call_systemctl,
) -> dict:
    if host not in ("dobby", "rocky") or (hostname or socket.gethostname()).split(".")[0] != host:
        raise RuntimeError("--host must match the local host (dobby or rocky)")
    if apply and (os.geteuid() if uid is None else uid) != 0:
        raise RuntimeError("--apply requires root")
    root = root.resolve()
    objects: list[dict[str, str]] = []
    units: list[str] = []
    before: dict[str, dict[str, str]] = {}

    def snapshot(unit: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in systemctl("show", unit, "--property=" + ",".join(PROPERTIES)).splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key] = result[key] + "\n" + value if key in result else value
        if not result.get("Id") or (unit.endswith(".service") and "MainPID" not in result):
            raise RuntimeError(f"Incomplete systemd snapshot: {unit}")
        return result

    def path(name: str) -> Path:
        return root / name.lstrip("/")

    def link(destination: str, target: str, *, required: bool = True) -> None:
        source = path(target)
        if not source.exists():
            if required:
                raise RuntimeError(f"Missing required source: {target}")
            return
        parent = path(destination).parent
        relative = os.path.relpath(source, parent)
        # Retain existing working alias spellings. A relocated parent changes the
        # meaning of '..', so fall back to physical paths when lexical traversal
        # would point somewhere else (e.g. ~/.local/bin in a cache mount).
        if (parent / relative).resolve() != source.resolve():
            relative = os.path.relpath(source.resolve(), parent.resolve())
        objects.append({"kind": "symlink", "path": destination, "target": relative})

    for base in ("/etc", "/var/lib", "/run"):
        link(f"{base}/viberpit", f"{base}/ccswitch")
    link("/etc/ccswitch/viberpit.env", "/etc/ccswitch/ccswitch.env")
    link("/var/log/viberpit", "/var/log/ccswitch", required=False)
    link("/home/tony/.viberpit", "/home/tony/.ccswitch", required=False)
    checkout = "/home/tony/GitHub/" + (
        "cc-account-switcher-deploy" if host == "dobby" else "ccswitch"
    )
    link("/home/tony/GitHub/" + ("viberpit-deploy" if host == "dobby" else "viberpit"), checkout)
    for base in (".cache", ".config", ".local/lib"):
        link(f"/home/tony/{base}/viberpit", f"/home/tony/{base}/ccswitch", required=False)
    if host == "dobby":
        link("/mnt/hdd-storage/viberpit-archives", "/mnt/hdd-storage/ccswitch-archives", required=False)
    if host == "rocky":
        link("/home/tony/bin-viberpit-mounts.sh", "/home/tony/bin-ccswitch-mounts.sh", required=False)
    cli = path(checkout + "/.venv/bin/ccswitch")
    if cli.is_file() and os.access(cli, os.X_OK):
        for base in ("/usr/local/bin", "/home/tony/.local/bin"):
            if path(base).is_dir():
                link(base + "/viberpit", checkout + "/.venv/bin/ccswitch")
    for stem in UNIT_STEMS:
        for suffix in ("service", "timer"):
            unit = f"{stem}.{suffix}"
            source = f"/etc/systemd/system/{unit}"
            if not path(source).exists():
                continue
            units.append(unit)
            before[unit] = snapshot(unit)
            description = before[unit].get("Description", unit)
            description = description.replace("cc-account-switcher", "ViberPit").replace("ccswitch", "ViberPit")
            if "\n" in description or "\r" in description:
                raise RuntimeError(f"Unsafe Description: {unit}")
            link(source.replace("/ccswitch", "/viberpit"), source)
            directory = source + ".d"
            objects.append({"kind": "directory", "path": directory})
            objects.append({"kind": "file", "path": directory + "/90-viberpit-description.conf",
                            "content": f"[Unit]\nDescription={description}\n"})
    if not units:
        raise RuntimeError("No supported installed units found")
    objects.append({"kind": "file", "path": "/etc/tmpfiles.d/viberpit.conf",
                    "content": "L /run/viberpit - - - - /run/ccswitch\n"})

    # Check every object and parent before creating even the rollback manifest.
    directories = {item["path"] for item in objects if item["kind"] == "directory"}
    pending: list[dict[str, str]] = []
    for item in objects:
        dest = path(item["path"])
        if dest.is_symlink():
            matches = item["kind"] == "symlink" and os.readlink(dest) == item["target"]
        elif dest.exists():
            matches = (item["kind"] == "directory" and dest.is_dir()) or (
                item["kind"] == "file" and dest.is_file() and dest.read_text() == item["content"]
            )
        else:
            matches = False
            if not dest.parent.is_dir() and str(Path(item["path"]).parent) not in directories:
                raise RuntimeError(f"Missing parent: {dest.parent}")
            pending.append(item)
            continue
        if not matches:
            raise RuntimeError(f"Conflict: {item['path']}")

    def boot_links() -> dict[str, str]:
        # Top-level aliases are not boot dependencies. Preserve actual dependency
        # links in both persistent and runtime unit search paths.
        return {
            str(entry.relative_to(root)): os.readlink(entry)
            for base in ("/etc/systemd/system", "/run/systemd/system",
                         "/usr/lib/systemd/system", "/lib/systemd/system")
            for relation in ("wants", "requires", "upholds")
            for entry in path(base).glob(f"*.{relation}/*")
            if entry.is_symlink()
        }

    before_boot_links = boot_links()
    result = {"host": host, "planned": pending, "created": [], "before": before}
    if not apply:
        return result
    if not pending:
        for unit in units:
            alias = snapshot(unit.replace("ccswitch", "viberpit", 1))
            if any(before[unit].get(key) != alias.get(key) for key in IDENTITY):
                raise RuntimeError(f"Alias identity changed for {unit}")
        return result
    manifest = path(f"/var/lib/viberpit-host-aliases-{uuid.uuid4().hex}.jsonl")
    result["manifest"] = str(manifest)
    print(f"Rollback manifest: {manifest}", flush=True)
    # Exclusive creation, private permissions, fsync after each record. Intent records
    # also cover interruption between filesystem mutation and its completion record.
    with manifest.open("x", encoding="utf-8") as journal:
        manifest.chmod(0o600)

        def record(value: dict) -> None:
            journal.write(json.dumps(value) + "\n")
            journal.flush()
            os.fsync(journal.fileno())

        record({"host": host, "before": before, "boot_links": before_boot_links})
        for item in pending:
            dest = path(item["path"])
            record({"intent": item})
            if item["kind"] == "symlink":
                dest.symlink_to(item["target"])
                owner = dest.parent.stat()
                os.lchown(dest, owner.st_uid, owner.st_gid)
            elif item["kind"] == "directory":
                dest.mkdir(mode=0o755)
            else:
                with dest.open("x", encoding="utf-8") as output:
                    output.write(item["content"])
                    output.flush()
                    os.fsync(output.fileno())
                dest.chmod(0o644)
            result["created"].append(item)
            record({"created": item})
        systemctl("daemon-reload")
        after = {unit: snapshot(unit) for unit in units}
        aliases = {unit: snapshot(unit.replace("ccswitch", "viberpit", 1)) for unit in units}
        after_boot_links = boot_links()
        record({"after": after, "aliases": aliases, "boot_links": after_boot_links})
        if before_boot_links != after_boot_links:
            raise RuntimeError(f"Systemd boot dependency links changed; inspect {manifest}")
        for unit in units:
            state_pair = (before[unit].get("UnitFileState"), after[unit].get("UnitFileState"))
            # systemd classifies disabled/static units with a top-level alias as indirect.
            # This does not enable it; the dependency-link check above proves no
            # persistent or runtime boot dependency was added or removed.
            state_matches = state_pair[0] == state_pair[1] or state_pair in (
                ("disabled", "indirect"), ("static", "indirect"),
            )
            if not state_matches or any(
                before[unit].get(key) != after[unit].get(key)
                for key in PROPERTIES if key not in ("Description", "UnitFileState")
            ):
                raise RuntimeError(f"Systemd state changed for {unit}; inspect {manifest}")
            if any(after[unit].get(key) != aliases[unit].get(key) for key in IDENTITY):
                raise RuntimeError(f"Alias identity changed for {unit}; inspect {manifest}")
        record({"verified": True})
        result.update(after=after, aliases=aliases)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("dobby", "rocky"), required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.host, apply=args.apply), indent=2))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Aborted: {exc}\n")


if __name__ == "__main__":
    main()
