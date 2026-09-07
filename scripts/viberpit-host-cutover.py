#!/usr/bin/env python3
"""Guarded installed-host naming cutover; default is read-only planning.

Prepare a private rollback bundle while live, then re-prepare AFTER draining/stopping
services if configuration changed. Apply/rollback never stop, start, or reload units.
The operator owns daemon-reload, session snapshots, and restoration. Renaming a
bridge uses a tiny missing-path interval, allowed ONLY with all owners stopped.
State trees are moved, never copied/deleted; legacy aliases preserve saved paths.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import socket
import stat
import subprocess
import tempfile

# Share the already-installed compatibility inventory, not another unit registry.
BRIDGE = runpy.run_path(str(Path(__file__).with_name("viberpit-host-aliases.py")))


def snapshot(path: Path) -> dict:
    if not os.path.lexists(path):
        return {"kind": "absent"}
    info = path.lstat()
    result = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
    if path.is_symlink():
        return dict(result, kind="link", target=os.readlink(path))
    if path.is_dir():
        return dict(result, kind="directory", inode=info.st_ino, device=info.st_dev)
    if not path.is_file():
        raise RuntimeError(f"Unsupported filesystem object: {path}")
    data = path.read_bytes()
    return dict(result, kind="file", data=base64.b64encode(data).decode(),
                sha256=hashlib.sha256(data).hexdigest(), mtime_ns=info.st_mtime_ns,
                xattrs={name: base64.b64encode(os.getxattr(path, name)).decode()
                        for name in os.listxattr(path)})


def build_plan(host: str, root: Path = Path("/")) -> dict:
    """Inspect installed files only. No template installation or secret output."""
    if host not in ("dobby", "rocky"):
        raise RuntimeError("Unsupported host")
    root = root.resolve()
    operations: list[dict] = []
    observed: dict[str, dict] = {}
    inventory: dict[str, list[str]] = {}
    units: dict[str, str] = {}

    def path(name):
        return root / name.lstrip("/")

    def observe(p):
        # Resolve the parent, never the final component (it can be a bridge).
        p = p.parent.resolve() / p.name
        observed[str(p)] = snapshot(p)
        return p

    def reverse(old, new):
        old, new = observe(old), observe(new)
        if old.is_symlink():
            if old.resolve() == new.resolve() and new.exists() and not new.is_symlink():
                return
            raise RuntimeError(f"Conflict: noncanonical source {old}")
        if not old.exists():
            if os.path.lexists(new):
                raise RuntimeError(f"Conflict: missing source for {new}")
            return
        if os.path.lexists(new):
            if not new.is_symlink() or new.resolve() != old.resolve():
                raise RuntimeError(f"Conflict: {new}")
            operations.append({"kind": "remove_link", "path": str(new)})
        operations.append({"kind": "move", "path": str(old), "to": str(new)})
        operations.append({"kind": "link", "path": str(old), "target": new.name,
                           "uid": old.lstat().st_uid, "gid": old.lstat().st_gid})

    system = path("/etc/systemd/system")
    for stem in BRIDGE["UNIT_STEMS"]:
        if stem == "ccswitch-mounts":
            continue  # Keep its process/cgroup and ALL installed configuration intact.
        for suffix in ("service", "timer"):
            old = f"{stem}.{suffix}"
            new = old.replace("ccswitch", "viberpit", 1)
            if os.path.lexists(system / old) or os.path.lexists(system / new):
                units[old] = new
    if not units:
        raise RuntimeError("No supported installed units")
    # Retired/locally installed watchdogs must be stopped too, even when they are
    # deliberately outside the rename inventory. Never include the live mounts.
    guards = sorted({p.name for base in ("/etc/systemd/system", "/run/systemd/system")
                     for p in path(base).glob("*")
                     if p.name.startswith(("ccswitch", "viberpit"))
                     and p.suffix in (".service", ".timer")
                     and p.name not in ("ccswitch-mounts.service", "viberpit-mounts.service")})

    moves = [(f"{base}/ccswitch", f"{base}/viberpit") for base in ("/etc", "/var/lib", "/run", "/var/log")]
    checkout = ("/home/tony/GitHub/" + ("cc-account-switcher-deploy" if host == "dobby" else "ccswitch"),
                "/home/tony/GitHub/" + ("viberpit-deploy" if host == "dobby" else "viberpit"))
    moves.append(checkout)
    moves.extend((f"/home/tony/{base}/ccswitch", f"/home/tony/{base}/viberpit")
                 for base in (".cache", ".config", ".local/lib"))
    control = path("/home/tony/.ccswitch")
    if control.exists() and not control.is_symlink():
        if set(p.name for p in control.iterdir()) - {"gh"}:
            raise RuntimeError("Unknown ~/.ccswitch state; classify before moving")
    moves.append(("/home/tony/.ccswitch", "/home/tony/.viberpit"))
    if host == "dobby":
        moves.append(("/opt/ccswitch-codex-auth", "/opt/viberpit-codex-auth"))
    moves.append(("/usr/local/bin/ccswitch-health-check.sh", "/usr/local/bin/viberpit-health-check.sh"))
    if host == "rocky":
        moves.append(("/home/tony/bin-ccswitch-mounts.sh", "/home/tony/bin-viberpit-mounts.sh"))
    # Old environment VALUES are deliberately untouched; these aliases support them.
    replacements = dict(moves)
    replacements[checkout[0] + "/scripts/ccswitch-health-check.sh"] = (
        checkout[1] + "/scripts/viberpit-health-check.sh"
    )
    replacements["/etc/ccswitch/ccswitch.env"] = "/etc/viberpit/viberpit.env"
    replacements.update(units)

    def rewrite(text):
        # Match exact tokens / path prefixes; never ccswitch-auth UID, the private
        # credential directory, agent namespaces, package names, or env prefixes.
        pattern = "|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True))
        text = re.sub(r"(?<![\w/.-])(" + pattern + r")(?=$|[/\s:\"'])",
                      lambda match: replacements[match[1]], text)
        text = re.sub(r"^(RuntimeDirectory|StateDirectory)=ccswitch$",
                      r"\1=viberpit", text, flags=re.MULTILINE)
        log_names = {old.rsplit(".", 1)[0]: new.rsplit(".", 1)[0]
                     for old, new in units.items()}
        text = re.sub(r"^SyslogIdentifier=([^\n]+)$",
                      lambda match: "SyslogIdentifier=" + log_names.get(match[1], match[1]),
                      text, flags=re.MULTILINE)
        # [Install] aliases generated by the additive phase must not self-alias.
        text = re.sub(r"^Alias=(.*)$", lambda m: "Alias=" + " ".join(
            next((old for old, new in units.items() if token == new), token)
            for token in m[1].split()), text, flags=re.MULTILINE)
        return text

    def edit(p, new_text):
        p = observe(p)
        if p.is_symlink() or not p.is_file():
            raise RuntimeError(f"Conflict: expected regular configuration file {p}")
        if p.read_text() != new_text:
            operations.append({"kind": "write", "path": str(p),
                               "data": base64.b64encode(new_text.encode()).decode()})

    # Rewrite bodies first while their original locations still exist. Include
    # persistent and runtime dropins (including systemctl set-property limits).
    for base in ("/etc/systemd/system", "/etc/systemd/system.control",
                 "/run/systemd/system", "/run/systemd/system.control"):
        directory = path(base)
        if not directory.exists():
            continue
        inventory[str(directory)] = sorted(p.name for p in directory.iterdir())
        for old, new in units.items():
            unit_path = directory / old
            if unit_path.exists() and not unit_path.is_symlink():
                edit(unit_path, rewrite(unit_path.read_text()))
            for unit_name in (old, new):
                dropin = directory / (unit_name + ".d")
                if dropin.exists() and not dropin.is_symlink():
                    inventory[str(dropin)] = sorted(p.name for p in dropin.iterdir())
                    for config in dropin.iterdir():
                        if config.is_file() and config.suffix == ".conf":
                            edit(config, rewrite(config.read_text()))
            if (directory / (old + ".d")).exists():
                reverse(directory / (old + ".d"), directory / (new + ".d"))
            if os.path.lexists(unit_path):
                reverse(unit_path, directory / new)
        for relationship in ("wants", "requires", "upholds"):
            for wants in directory.glob(f"*.{relationship}"):
                inventory[str(wants)] = sorted(p.name for p in wants.iterdir())
                for link in wants.iterdir():
                    if link.name not in units:
                        continue
                    if not link.is_symlink() or link.resolve() != (system / link.name).resolve():
                        raise RuntimeError(f"Conflict: dependency {link}")
                    target = observe(link.with_name(units[link.name]))
                    if os.path.lexists(target):
                        raise RuntimeError(f"Conflict: dependency {target}")
                    observe(link)
                    operations.extend([
                        {"kind": "remove_link", "path": str(link)},
                        {"kind": "link", "path": str(target),
                         "target": str(system / units[link.name]),
                         "uid": link.lstat().st_uid, "gid": link.lstat().st_gid},
                    ])
    env = path("/etc/ccswitch/ccswitch.env")
    if os.path.lexists(env):
        reverse(env, env.with_name("viberpit.env"))
    for old, new in moves:
        reverse(path(old), path(new))
    # Repoint only the existing forward CLI symlinks. Keep the logical canonical
    # checkout and .venv in the target; resolving them now would bake the old
    # bridge/owned-venv paths into the launcher again. Only its parent is physical.
    old_cli = path(checkout[0]) / ".venv/bin/ccswitch"
    new_cli = path(checkout[1]) / ".venv/bin/ccswitch"
    for name in ("/usr/local/bin/viberpit", "/home/tony/.local/bin/viberpit"):
        alias = observe(path(name))
        if not alias.is_symlink():
            continue
        if (not old_cli.is_file() or not os.access(old_cli, os.X_OK)
                or alias.resolve() != old_cli.resolve()):
            raise RuntimeError(f"Conflict: CLI alias {alias}")
        observe(old_cli)
        target = os.path.relpath(new_cli, alias.parent)
        if os.readlink(alias) != target:
            owner = alias.lstat()
            operations.extend([
                {"kind": "remove_link", "path": str(alias)},
                {"kind": "link", "path": str(alias), "target": target,
                 "uid": owner.st_uid, "gid": owner.st_gid},
            ])
    tmpfiles = path("/etc/tmpfiles.d/viberpit.conf")
    if tmpfiles.exists():
        old_rule = "L /run/viberpit - - - - /run/ccswitch\n"
        new_rule = "L /run/ccswitch - - - - /run/viberpit\n"
        if tmpfiles.read_text() not in (old_rule, new_rule):
            raise RuntimeError("Conflict: runtime alias tmpfiles rule")
        edit(tmpfiles, new_rule)
    return {"version": 1, "host": host, "root": str(root), "units": units,
            "observed": observed, "inventory": inventory, "operations": operations, "guards": guards}


def save(path: Path, value: dict):
    """Atomic private journal update, durable before the next mutation."""
    descriptor, temp = tempfile.mkstemp(dir=path.parent, prefix=".cutover-")
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sync_directory(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare(plan: dict, bundle: Path):
    bundle.mkdir(mode=0o700, parents=False, exist_ok=False)
    sync_directory(bundle.parent)
    save(bundle / "plan.json", plan)
    save(bundle / "journal.json", {"done": [], "pending": None, "rolled_back": False})


def call_systemctl(*args):
    return subprocess.run(["systemctl", *args], check=True, capture_output=True,
                          text=True, timeout=30).stdout


def stopped(plan, systemctl):
    # A renamed pair temporarily has only one filesystem name between unlink
    # and move. systemd may garbage-collect the missing alias immediately. Query
    # BOTH names: a loaded old process must never hide behind a missing new name.
    groups = [tuple(pair) for pair in plan["units"].items()]
    paired_names = {name for pair in groups for name in pair}
    groups.extend((name,) for name in plan["guards"] if name not in paired_names)
    for names in groups:
        states = {}
        for name in names:
            output = systemctl("show", name, "--property=LoadState,ActiveState,MainPID,ControlPID")
            states[name] = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        for name, values in states.items():
            if (values.get("LoadState") not in ("loaded", "not-found")
                    or values.get("ActiveState") != "inactive"
                    or values.get("MainPID", "0" if name.endswith(".timer") else "missing") != "0"
                    or values.get("ControlPID", "0") != "0"):
                raise RuntimeError(f"All owners/timers must be stopped: {name}")
        # Renamed pairs need one loaded owner; untouched/retired units still
        # need their sole name loaded. Filesystem snapshots validate the initial
        # aliases before writes; this exception also allows rollback in a gap.
        if not any(values.get("LoadState") == "loaded" for values in states.values()):
            raise RuntimeError(f"No loaded owner for stopped guard: {', '.join(names)}")


def load(bundle):
    for p in (bundle, bundle / "plan.json", bundle / "journal.json"):
        if p.is_symlink() or p.stat().st_uid != os.geteuid() or p.stat().st_mode & 0o077:
            raise RuntimeError("Rollback bundle must be private and owned by the executing user")
    return json.loads((bundle / "plan.json").read_text()), json.loads((bundle / "journal.json").read_text())


def perform(operation):
    p = Path(operation["path"])
    kind = operation["kind"]
    if kind == "move":
        destination = Path(operation["to"])
        if os.path.lexists(destination):
            raise RuntimeError(f"Move destination exists: {destination}")
        p.rename(destination)
    elif kind == "remove_link":
        if not p.is_symlink():
            raise RuntimeError(f"Expected symlink: {p}")
        p.unlink()
    elif kind == "link":
        p.symlink_to(operation["target"])
        os.lchown(p, operation["uid"], operation["gid"])
    elif kind == "write":
        original = p.stat()
        attributes = {name: os.getxattr(p, name) for name in os.listxattr(p)}
        descriptor, temp = tempfile.mkstemp(dir=p.parent, prefix=".cutover-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(base64.b64decode(operation["data"]))
                os.fchown(output.fileno(), original.st_uid, original.st_gid)
                os.fchmod(output.fileno(), stat.S_IMODE(original.st_mode))
                for name, value in attributes.items():
                    os.setxattr(output.fileno(), name, value)
                output.flush()
                os.utime(output.fileno(), ns=(original.st_atime_ns, original.st_mtime_ns))
                os.fsync(output.fileno())
            os.replace(temp, p)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    else:
        raise RuntimeError("Unknown operation")
    sync_directory(p.parent)


def apply(bundle: Path, *, systemctl=call_systemctl):
    plan, journal = load(bundle)
    stopped(plan, systemctl)
    if journal["done"] or journal["pending"] or journal["rolled_back"]:
        raise RuntimeError("Bundle already used; use rollback or a fresh plan")
    for p, before in plan["observed"].items():
        if snapshot(Path(p)) != before:
            raise RuntimeError(f"Prepared filesystem changed: {p}")
    for p, names in plan["inventory"].items():
        if sorted(x.name for x in Path(p).iterdir()) != names:
            raise RuntimeError(f"Prepared configuration inventory changed: {p}")
    for operation in plan["operations"]:
        # Close watchdog races at every step; the operator must stop all timers.
        stopped(plan, systemctl)
        record = {"operation": operation, "before": snapshot(Path(operation["path"]))}
        journal["pending"] = record
        save(bundle / "journal.json", journal)
        perform(operation)
        journal["done"].append(record)
        journal["pending"] = None
        save(bundle / "journal.json", journal)


def rollback(bundle: Path, *, systemctl=call_systemctl):
    plan, journal = load(bundle)
    stopped(plan, systemctl)
    if journal["pending"]:
        pending = journal["pending"]
        operation, before = pending["operation"], pending["before"]
        current = snapshot(Path(operation["path"]))
        expected = dict(before)
        kind = operation["kind"]
        if kind in ("move", "remove_link"):
            expected = {"kind": "absent"}
        elif kind == "link":
            expected = dict(kind="link", target=operation["target"], mode=0o777,
                            uid=operation["uid"], gid=operation["gid"])
        elif kind == "write":
            expected.update(data=operation["data"], sha256=hashlib.sha256(
                base64.b64decode(operation["data"])).hexdigest())
        if current == expected:
            if kind == "move" and snapshot(Path(operation["to"])) != before:
                raise RuntimeError("Interrupted move destination changed; manual recovery required")
            if pending.get("undo"):
                journal["done"].pop()
            else:
                journal["done"].append(pending)
        elif current != before or (kind == "move" and os.path.lexists(operation["to"])):
            raise RuntimeError("Interrupted operation changed unexpectedly; manual recovery required")
        journal["pending"] = None
        save(bundle / "journal.json", journal)
    if journal["rolled_back"]:
        return
    while journal["done"]:
        stopped(plan, systemctl)
        record = journal["done"][-1]
        operation, before = record["operation"], record["before"]
        p = Path(operation["path"])
        kind = operation["kind"]
        if kind == "move":
            undo = {"kind": "move", "path": operation["to"], "to": str(p)}
        elif kind == "link":
            if not p.is_symlink() or os.readlink(p) != operation["target"]:
                raise RuntimeError(f"Rollback conflict: {p}")
            undo = {"kind": "remove_link", "path": str(p)}
        elif kind == "remove_link":
            undo = dict(before, kind="link", path=str(p))
        else:
            if snapshot(p).get("data") != operation["data"]:
                raise RuntimeError(f"Rollback file changed: {p}")
            undo = {"kind": "write", "path": str(p), "data": before["data"]}
        journal["pending"] = {"operation": undo, "before": snapshot(Path(undo["path"])),
                              "undo": True}
        save(bundle / "journal.json", journal)
        perform(undo)
        journal["done"].pop()
        journal["pending"] = None
        save(bundle / "journal.json", journal)
    journal["rolled_back"] = True
    save(bundle / "journal.json", journal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("dobby", "rocky"), required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prepare", type=Path, metavar="PRIVATE_BUNDLE")
    group.add_argument("--apply", type=Path, metavar="PRIVATE_BUNDLE")
    group.add_argument("--rollback", type=Path, metavar="PRIVATE_BUNDLE")
    args = parser.parse_args()
    try:
        if socket.gethostname().split(".")[0] != args.host:
            raise RuntimeError("--host does not match local machine")
        bundle = args.prepare or args.apply or args.rollback
        if bundle and os.geteuid() != 0:
            raise RuntimeError("Preparing or changing a host requires root")
        if bundle:
            parent = bundle.parent
            if (not bundle.is_absolute() or parent.resolve() != parent
                    or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022):
                raise RuntimeError("Use an absolute bundle path under a trusted root-owned directory")
        if args.apply or args.rollback:
            plan, _ = load(bundle)
            if plan["host"] != args.host or plan["root"] != "/":
                raise RuntimeError("Bundle belongs to another host/root")
            (apply if args.apply else rollback)(bundle)
            print("Filesystem operation complete. Operator must daemon-reload and verify before starting.")
        else:
            plan = build_plan(args.host)
            if args.prepare:
                prepare(plan, args.prepare)
            # Never display unit bodies, env contents, or secret-bearing backup data.
            print(json.dumps({"host": args.host, "operations": [
                {k: v for k, v in op.items() if k != "data"} for op in plan["operations"]
            ], "bundle": str(bundle) if bundle else None, "stop_before_apply": plan["guards"]}, indent=2))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Aborted: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
