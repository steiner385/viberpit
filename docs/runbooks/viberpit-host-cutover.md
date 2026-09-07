# Dobby and rocky naming cutover

This is an additive bridge for hosts with live ViberPit sessions. Canonical unit,
process, directory, environment and package names remain legacy during this phase.
The script never starts, stops, restarts, enables or disables services. A systemd
manager reload loads aliases and Description-only dropins without restarting units.
It does not install application code or change the mount/authentication topology.

## Before applying

Work on rocky first, then dobby. Capture current session activity and host health,
including daemon RPC, edge health, unit PIDs, invocation IDs, cgroups, enabled state,
KillMode, stop timeout, environment-file sources, socket ownership and permissions.
Do not print credentials or environment-file contents. Check that no concurrent
operator is changing these units. Run from the reviewed checkout:

```bash
python3 scripts/viberpit-host-aliases.py --host rocky | tee /tmp/viberpit-rocky-aliases-dry-run.log
sudo python3 scripts/viberpit-host-aliases.py --host rocky --apply | tee /tmp/viberpit-rocky-aliases-apply.log
```

Use `--host dobby` on dobby after rocky is verified. The hostname must match; apply
requires root. Dry-run is the default and writes nothing. Use `set -o pipefail` when
capturing exit status through `tee`. Any existing object with different contents or
symlink target aborts the entire preflight before writes. Missing required source
paths also abort. Investigate conflicts; do not remove objects just to make it pass.

The bounded bridge creates:

- ViberPit aliases for supported installed `/etc/systemd/system/ccswitch*` units,
  plus Description-only dropins. Retired `ccswitch-deploy.*` units are excluded.
  An installed disabled daemon-ensure timer receives an alias without boot enablement.
- `/etc/viberpit`, `/var/lib/viberpit`, `/run/viberpit`, all pointing at their existing
  `ccswitch` counterpart; `/etc/ccswitch/viberpit.env` points at `ccswitch.env`.
  `/etc/tmpfiles.d/viberpit.conf` contains exactly
  `L /run/viberpit - - - - /run/ccswitch`. The non-overwriting `L` rule preserves the
  alias over reboot; the target may be created later by the existing runtime owner.
- Dobby's `~/GitHub/viberpit-deploy` alias to `cc-account-switcher-deploy`, or rocky's
  `~/GitHub/viberpit` alias to `ccswitch`.
- `~/.viberpit` and `/var/log/viberpit` only where their old sources exist.
- `~/.cache/viberpit`, `~/.config/viberpit`, `~/.local/lib/viberpit` only where the
  corresponding existing ccswitch source exists. No historical worktree or native
  agent-managed cache namespace is renamed.
- `/usr/local/bin/viberpit` and `~/.local/bin/viberpit` when their parent directories
  and the host checkout's executable `.venv/bin/ccswitch` exist. Old launchers are
  untouched. Rocky's `~/bin-viberpit-mounts.sh` aliases the old script if present.

The root-created symlinks share the original target's permissions and data; they
are not copies. No `/opt` credential-authority alias, account rename, auth UID change,
credential move, database rename, remount, or environment variable rewrite is done.

## Verify before proceeding to the other host

The script records before/after snapshots and requires each original unit to retain
its Id, MainPID, InvocationID, ControlGroup, active/substate, boot enablement, KillMode,
stop timeout and environment-file source. Each alias must resolve to the same unit
identity and process. systemd may report `indirect` instead of `disabled` or `static` after
adding a top-level alias; these precise transitions are allowed only with unchanged
persistent/runtime `.wants`, `.requires` and `.upholds` symlinks. Other unit-file
state changes fail verification. The manifest records these dependency links.
Timers can naturally fire during this interval: a changed
snapshot is an investigation signal, never a reason to restart a daemon.

Check the apply exit status and final `verified` record. Verify alias and original
paths with `stat -L` (device and inode must match), confirm unchanged socket and
credential-path permissions, and run the new CLI's `--help`. Recheck edge health and
daemon RPC on both hosts, and compare live session identities/activity with baseline.
A dry run or fixture test alone is not live verification. Re-running apply is
idempotent and rechecks aliases without a reload if no creations remain.

On dobby only, an existing `/mnt/hdd-storage/ccswitch-archives` receives a
`viberpit-archives` alias in that directory, recorded in the same manifest. Rocky
only observes this shared alias: the script never writes through its SSHFS mount.
Symlink ownership follows the containing directory's owner; target permissions
remain unchanged.

Origin URL updates are separate: verify each main/deploy checkout is the same
repository, then set its origin to `https://github.com/steiner385/viberpit.git`.
Do not rewrite fleet cwd values or historical worktree remote namespaces.

## Operating through the new names

Use `systemctl status viberpit-daemon.service` on either host, or
`systemctl status viberpit.service` for dobby's edge. The returned canonical Id and
cgroup still use the old name during this bridge.

Journal entries belong to that canonical Id. A live check on dobby confirmed that
`journalctl -u viberpit-daemon.service` alone returns no existing daemon entries.
Resolve the alias before following logs:

```bash
journalctl -u "$(systemctl show --value --property=Id viberpit-daemon.service)" -f
```

## Exact rollback without stopping sessions

Apply prints a private JSONL manifest at
`/var/lib/viberpit-host-aliases-<unique-id>.jsonl` before mutation. Preserve it even
when apply fails. Its `created` records enumerate only objects created by that run;
pre-existing matching objects are excluded. `intent` records cover interruptions
between creation and journaling; inspect any intent without a completion record
against the filesystem and operator activity before deciding ownership.

For the chosen manifest, process **only its created objects in reverse order**:

1. For a symlink, compare `readlink <path>` with that record's exact `target`, then
   `sudo unlink <path>`. Do not follow the symlink or delete its target.
2. For a file, compare its contents with the manifest's `content`, then
   `sudo rm -- <path>`. These are only the Description dropins and tmpfiles rule.
3. For a directory, use `sudo rmdir -- <path>`; leave it if nonempty.
4. Run `sudo systemctl daemon-reload` once, and repeat original-unit identity and
   health checks. There is no stop, restart, disable or enable step.

If an object differs, stop rollback and investigate instead of deleting another
operator's change. Never recursively remove directories. Retain the manifest and
verification logs as evidence. A crash after creation but before its completion
record requires manual review of that intent, not blind replay. The manifest itself
may be removed only after rollback verification and preserving the audit record.

## Pending canonical flip — not completed by this bridge

Legacy Id/cgroup names, ExecStart and WorkingDirectory paths, installed unit filenames,
CCSWITCH environment keys, virtualenv interpreter paths, Python package name, DB
identity, mounts and historical session cwd references intentionally remain.

Before making new names canonical, deploy compatible Phase 3 unit/path resolution
and canonical unit definitions. Inventory every installed dropin and
`systemd.control` limit; preserve all effective settings. Coordinate admission
control and a quiet window on each runner so no live turn is terminated. Preserve
one daemon owner per host, mount service identity and credential authority. Prepare
an explicit reverse migration before switching unit ownership. Never arm an
unattended restart to finish cosmetic naming. Change environment prefixes only
after every live shell and entrypoint supports both names; package renaming is a
separate migration. Existing process command lines cannot be renamed in place.

The remaining canonical cutover is tracked by
[#1370](https://github.com/steiner385/viberpit/issues/1370) (runtime resolution),
[#1371](https://github.com/steiner385/viberpit/issues/1371) (unit definitions), and
[#1372](https://github.com/steiner385/viberpit/issues/1372) (host migration). This
bridge does not close those tasks. Before that window, explicitly inventory and
remove/reverse these bridge aliases using their manifests: never copy new units or
move directories through existing symlinks. Preserve all installed dropins,
`systemd.control` resource limits, boot dependencies, and the non-remounted mount
service. Reissue the credential authority's identity and rename its private state
only in the separately planned credential rotation. Keep historical session cwd,
Git worktree metadata, native agent caches, and account stores stable.

## Live receipt — 2026-09-07, America/New_York

Applied rocky first, then dobby, against deployed application SHA `3dd25c7e`.
Both authenticated daemon health endpoints and dobby's edge remained healthy.
The bridge used no service start, stop, restart, enable, disable, or remount.

| Long-running service | Dobby PID (unchanged) | Rocky PID (unchanged) |
| --- | --- | --- |
| Session daemon | 1912988 | 2752401 |
| Web edge | 1923058 | — |
| Deploy controller | 1493124 | — |
| Credential authority | 3168 | — |

The invocation IDs and cgroups of these services also remained unchanged. Both
daemons retained `KillMode=mixed` and an 11-minute stop timeout; their resource
limits remained unchanged. Original state/config paths retained their device/inode,
owner and mode, including the root-only env files. Mount topology was unchanged;
rocky's shared archive alias resolves to the same directory as the original.
The watchdog's periodic oneshot naturally receives new invocation IDs on its normal
schedule; its timer and boot dependency links were preserved.

The `viberpit` command works through both `/usr/local/bin` and `~/.local/bin` on
both hosts. CLI help still spells the legacy console-script name until the package
rename. Dobby's relocated `~/.local/bin` required repairing one newly created alias;
the final installer handles relocated parent/source paths. No old launcher or
virtualenv was modified. Re-running the final installer on both hosts found zero
changes, and the runtime alias rule was checked using the installed tmpfiles tool.

Rollback manifests (host-local, root-readable):

- Rocky: `/var/lib/viberpit-host-aliases-fe8a0c45f4414dd5b1a1b533dce0ff60.jsonl`.
- Dobby: `/var/lib/viberpit-host-aliases-40c5ec5b4d31446bb164415a89aaa86f.jsonl`.
- Dobby CLI repair: `/var/lib/viberpit-host-aliases-cli-repair-a8cfba3234984ac3a0ff9dd1a9daf030.jsonl`.
  For full rollback, verify the repaired alias against this record's `after` target
  and remove it directly; skip that same path in the original manifest. Do not
  restore the original broken target.

Before/after host snapshots and origin-URL rollback records live under
`~/.local/state/viberpit-cutover/` on each host. Dobby's main and deploy checkouts,
and rocky's runtime checkout, now use `https://github.com/steiner385/viberpit.git`.
This also updates the main checkout's linked worktrees through shared Git config;
no managed clone or historical worktree directory was moved.
