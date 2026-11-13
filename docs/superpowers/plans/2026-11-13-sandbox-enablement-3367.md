> **Fix round 1 (2026-11-13, security review of 9581c603).** The copy moved from `/usr/local/libexec` to
> `/usr/libexec/viberpit/bwrap`: dobby's `/usr/local` is `tony:tony 0755`, so tony could rename the tree and plant
> a bwrap of their own at the managed path (and the root apt hook would write into it). `/usr/libexec` is
> `root:root 0755` down from `/` on dobby (checked with `stat`, read-only). The preflight and every sandboxed spawn
> now require a root-owned, non-writable chain up to `/`; `install.sh` refuses otherwise. Also: the startup sweep
> only empties a parent carrying ViberPit's `.viberpit-sandbox-tmp` marker, and only key-named roots with a `.sid`;
> the preflight cache is keyed by the bwrap copy and stock bwrap as well as the CLI; the apt hook cannot fail apt;
> no asset hard-codes the group `tony`.

> **Owner decision (2026-11-13): option A approved.** This covers the host change on both hosts: the private bwrap copy, the narrow AppArmor profile, the managed-settings `bwrapPath`, and the apt hook. It also covers the code PR. Following the recommendations, temp dirs go in `/var/tmp` and F6 is fixed in code. Rollout order is code PR → dobby host → rocky host → deploy → live re-run → flip.

# #3367 — Enabling the D-23 Claude sandbox: plan

Status: research and plan only. Nothing was installed, loaded, enabled or restarted. Probes were unprivileged
`bwrap` runs in `/tmp/3367-probe` and `apparmor_parser -Q` parse-only checks (`-Q` skips the kernel load), on
2026-11-13. Base: `origin/master` at 29372a96 (D-23 = #3368). No open PR touches `sandbox` (GraphQL file search
returned nothing).

Binary offsets are `grep -boa` byte offsets into `~/.local/share/claude/versions/2.1.319` (dobby). Offsets
marked "rocky" are into `2.1.293`.

---

## 0. Summary

| Item | Fix | Where |
|---|---|---|
| F4 AppArmor vs seccomp helper | Add a root-owned **copy of bwrap** at `/usr/libexec/viberpit/bwrap` (mode 0750 root:<ViberPit user's group>) with its **own AppArmor profile**, `viberpit-sbx-bwrap`. Its children are not stacked with `unpriv_bwrap`. Point the CLI at it with `sandbox.bwrapPath` in `/etc/claude-code/managed-settings.json`. Stock `/usr/bin/bwrap` and its profile stay untouched, so Codex is unaffected. Add an apt hook to keep the copy in step with `/usr/bin/bwrap`. | host (both) |
| F5 proxy socket hidden | Move the per-session root to `/var/tmp/vpsbx/<12-hex>/` with sibling dirs `t/` (`CLAUDE_CODE_TMPDIR`) and `s/` (`TMPDIR` = the CLI's `os.tmpdir()`, where the sockets live). Deny `/var/tmp/vpsbx` and re-allow only `<own>/s`. **Verified with a bwrap stand-in.** | code |
| F6 symlinked `~/.local`, `~/.npm` | Re-allowing the symlink targets alone does nothing, because they are not denied. Do three things: deny the real path of every home symlink that leaves home (this closes a **secret leak found here**: `~/.docker` points to `/mnt/ssk-ssd/.../.docker/config.json`); re-allow the real paths of the toolchain entries; and give the sandboxed spawn a PATH that uses those real paths. | code |
| Rocky push credential | Inject the git config `credential.helper=` (reset), then `credential.https://github.com.helper=!/usr/bin/gh auth git-credential`, via `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n` in the sandboxed `session_env()`. This needs no host change. **Verified on rocky:** the store helper is cleared. | code |
| Not yet run live | WebFetch, Bash refused while an Edit waits on a card, and Reload releasing the gate after a lost end event. Probe scripts in §5. | live probe |

Owner decisions are in §7. Nothing found makes this infeasible.

---

## 1. F4 — AppArmor vs the CLI's seccomp helper

### 1.1 What the CLI runs (evidence)

- **The helper is embedded in the CLI binary.** It is not a separate file. It is a native multi-call entry point,
  chosen by `ARGV0`:
  - `swr(){... return{applyPath:\`/proc/self/fd/${Kce}\`,argv0:"apply-seccomp"}}` @288569460, with `Kce=3`
    @288569177.
  - `owr()` opens `/proc/self/exe` on fd 3 with flags `2097152` (`O_PATH`) @288569227. Its error text is
    "failed to open /proc/self/exe for the embedded apply-seccomp helper".
  - The native strings `apply-seccomp: write /proc/self/uid_map`, `.../gid_map` and `apply-seccomp: execvp` are at
    ~15360300.
  - On rocky (2.1.293), `argv0:"apply-seccomp"` is @209765893. So the helper is embedded there too.
- **The helper prefix.** `M8(e,n)` @288400756 returns `` `ARGV0=${n} ${e} ` `` when argv0 is set. `U8()` prepends
  `SRT_ALLOW_NESTED_USERNS=0|1 ` (@~288400841).
- **The argv** (wrapper @288416201 to @288419151):
  - `Et=["--new-session","--die-with-parent", ...fs/net args...]`, then `Et.push("--",sn,"-c")` @288418880, where
    `sn` = which(bash).
  - Without network: the script is `Ht + quote([bash,"-c",cmd])`.
  - With network, `V8()` builds the script. It starts two `socat ... UNIX-CONNECT:<sock>` bridges in the background,
    waits, then runs `Ht + quote([bash,"-c",cmd])`. The user command runs **only** through the helper. The
    helper-less `eval` path is used only when `Ht` is empty.
  - So the process tree is:
    `bwrap → /usr/bin/bash -c '<socat bridges>; SRT_ALLOW_NESTED_USERNS=0 ARGV0=apply-seccomp /proc/self/fd/3 /usr/bin/bash -c "<cmd>"'`.
    The helper is bwrap's **grandchild**. The exec'd file is the CLI binary itself; on dobby its real path is
    `/mnt/ssk-ssd/tony/cache/.local/share/claude/versions/2.1.319` (`readlink -f`).
- **The path changes per version.** The file is `versions/<ver>`. Our fix never names it (see §1.4).

### 1.2 Why AppArmor strips it (evidence)

`/etc/apparmor.d/bwrap-userns-restrict` on dobby (abi 5.0, package `apparmor 5.0.2-0ubuntu1~26.04.1`):

```
profile bwrap /usr/bin/bwrap flags=(attach_disconnected,mediate_deleted) {
  allow capability, ... allow userns, ...
  allow pix /** -> &bwrap//&unpriv_bwrap,
  include if exists <local/bwrap-userns-restrict>
}
profile unpriv_bwrap flags=(attach_disconnected,mediate_deleted) {
  ... allow userns, ...
  allow pix /** -> &unpriv_bwrap,
  audit deny capability,
  include if exists <local/unpriv_bwrap>
}
```

Rocky has an older, locally kept variant: abi 4.0, `allow px /** -> bwrap//&unpriv_bwrap,`, with the newer
packaged copy left as `bwrap-userns-restrict.dpkg-dist`. The effect is the same.

The mechanism:

1. bwrap always sets `no_new_privs`. Under NNP, AppArmor allows only **stacking** transitions, never a transition
   that leaves the current stack (the profile's own comment says so).
2. Every exec under `/usr/bin/bwrap` lands in `bwrap//&unpriv_bwrap`. Observed:
   `/usr/bin/bwrap --ro-bind / / --dev /dev --unshare-user -- cat /proc/self/attr/current` →
   `bwrap//&unpriv_bwrap (enforce)`.
3. The helper (a grandchild) is in the same stack. The kernel gives it full capabilities in the nested user
   namespace it creates, but `audit deny capability` vetoes them. Writing `/proc/self/setgroups` then fails.
4. A stack can only narrow. No local rule at the helper's exec can take `unpriv_bwrap` back off. A local rule on
   bwrap's direct child would have to match `/usr/bin/bash`, which would change every bwrap user on the host.

Reproduced with the **real** helper (not the spike's `unshare` stand-in):

```
$ env -i PATH=/usr/bin:/bin HOME=/tmp/3367-probe/home /usr/bin/bwrap --new-session --die-with-parent \
    --ro-bind / / --dev /dev --proc /proc --unshare-user --unshare-pid --unshare-net -- /usr/bin/bash -c \
    'SRT_ALLOW_NESTED_USERNS=0 ARGV0=apply-seccomp /proc/self/fd/3 /usr/bin/bash -c "echo helper-ok"' 3<"$CLI"
apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted; caller must provide CAP_SYS_ADMIN): Permission denied
rc=1
```

The same command with a non-stock copy of bwrap (`cp /usr/bin/bwrap /tmp/3367-probe/bwrap-copy`) succeeds. The
output also shows what the helper itself does to the sandboxed command:

```
helper-ok
CapEff:	0000000000000000          # the command has no effective capabilities
Seccomp:	2                         # filter installed
PermissionError: [Errno 1] Operation not permitted   # socket(AF_UNIX) blocked
unshare: unshare failed: Operation not permitted     # nested userns blocked
```

The same run with `SRT_ALLOW_NESTED_USERNS=1` prints `nested-userns-allowed`. So it is the helper's seccomp filter
that blocks nested namespaces, not AppArmor. Our block pins `allowNestedUserNamespaces:false`, so the prefix is `=0`.

**The sysctl is not the cause.** dobby already has `kernel.apparmor_restrict_unprivileged_userns = 0`, from
`/etc/sysctl.d/60-ccswitch-userns.conf` (root, dated 2026-06-22, which overrides `/usr/lib/sysctl.d/10-apparmor.conf`).
F4 still reproduces there. The `bwrap` profile attaches by path whatever the sysctl says. Rocky has the sysctl at 1.

### 1.3 Options weighed

| Option | Verdict |
|---|---|
| `kernel.apparmor_restrict_unprivileged_userns=0` | **Does not fix F4.** dobby already runs with 0 and fails (above). It only weakens the host. |
| `aa-disable`/complain on `bwrap-userns-restrict` | Host-wide. Breaks Codex on rocky: `deploy/README.md` "`sandbox: userns_restricted`" requires the profile enforced. Rejected. |
| A `local/unpriv_bwrap` override (`allow capability`, rule priority) | Host-wide. It gives capabilities to the children of **every** bwrap user, including Codex's sandboxed commands, flatpak and glycin. Rejected. |
| A dedicated profile for the helper path (`/**/claude/versions/*`) | Cannot work with stock bwrap. The helper is a grandchild inside `bwrap//&unpriv_bwrap` under NNP (§1.2.4), so no profile can add capabilities back. It would also have to match a disconnected path: the fd was opened outside bwrap's mount namespace, and dobby's binary lives on another filesystem. Rejected. |
| **A dedicated bwrap copy with its own profile, selected by `sandbox.bwrapPath`** | **Recommended.** Only processes started by the copy are affected. Stock `/usr/bin/bwrap` is unchanged, so Codex, flatpak and glycin are untouched. Version-agnostic. |
| An upstream CLI fix | The right long-term fix. Ask for the seccomp filter to go through bwrap's own `--add-seccomp-fd` (an older srt config had `bpfPath`, still in the binary @164907964), so no nested-userns helper is needed. The proxy sockets should also be bound after the filesystem args (F5). No timeline, so it cannot gate enablement. File it at anthropics/claude-code with this evidence. |

`sandbox.bwrapPath` is a real, documented key. Schema @282545730: "Linux/WSL only: Absolute path to the bwrap
(bubblewrap) binary. Overrides auto-detection via PATH. **Only honored from admin-controlled managed settings.**"
It is listed in `us=["bwrapPath","ripgrep","socatPath"]` @282741077. It is read by `gDr()` @284944456 and passed
as `bwrapPath:We?.bwrapPath` to the Linux wrapper @288508815. On rocky the string is @102353655. The managed
directory is `/etc/claude-code` with `managed-settings.json` (strings @~151455136). The CLI's own checks on a
bwrap binary pass for a plain copy: not setuid (`F8`, `mode&2048`), `--version` ≥ 0.5.0 (`P8`/`QR="0.5.0"`), and
`--help` contains `--disable-userns`.

### 1.4 Exact host changes (both hosts)

**`/etc/apparmor.d/viberpit-sbx-bwrap`** (parse-checked, see below):

```
# ViberPit D-23 (#3367): a dedicated bubblewrap for the Claude CLI's sandbox.
#
# /usr/libexec/viberpit/bwrap is a root-owned copy of /usr/bin/bwrap that the CLI
# uses through managed settings (sandbox.bwrapPath). Stock /usr/bin/bwrap keeps Ubuntu's
# bwrap-userns-restrict profile (Codex, flatpak, glycin are unchanged).
#
# Unlike `bwrap`, this profile does not stack unpriv_bwrap on bwrap's children: the CLI's
# embedded apply-seccomp helper (the CLI binary itself, run via /proc/self/fd/3 by the
# shell bwrap starts) must use capabilities inside its own nested user namespace. The
# helper drops every capability and installs a seccomp filter (no AF_UNIX, no nested
# userns) before it runs the sandboxed command.
#
# Exposure: whoever can execute the copy gets bubblewrap without the Ubuntu userns
# restriction. The copy is mode 0750 root:<ViberPit user's group>, so only that user can.
# It lives under /usr/libexec, a root-owned chain: a user-writable ancestor (dobby's
# /usr/local is tony-owned) would let that user swap the binary at this path.

abi <abi/5.0>,

include <tunables/global>

profile viberpit-sbx-bwrap /usr/libexec/viberpit/bwrap flags=(attach_disconnected,mediate_deleted) {
  allow capability,
  allow file rwlkm /{**,},
  allow network,
  allow unix,
  allow ptrace,
  allow signal,
  allow mqueue,
  allow io_uring,
  allow userns,
  allow mount,
  allow umount,
  allow pivot_root,
  allow dbus,

  # Children stay in this profile (no transition, so no_new_privs is satisfied).
  allow ix /**,

  include if exists <local/viberpit-sbx-bwrap>
}
```

Parse check, run on 2026-11-13. Nothing was loaded: `grep -c viberpit /sys/kernel/security/apparmor/profiles` → `0`
on both hosts afterwards.

```
dobby$ apparmor_parser -Q -K -T -I /etc/apparmor.d ./viberpit-sbx-bwrap   → rc=0
dobby$ sudo apparmor_parser -Q -K -T ./viberpit-sbx-bwrap                → rc=0
rocky$ sudo apparmor_parser -Q -K -T /tmp/3367-viberpit-sbx-bwrap        → rc=0
```

**`/etc/claude-code/managed-settings.json`** (neither host has `/etc/claude-code` today):

```json
{ "sandbox": { "bwrapPath": "/usr/libexec/viberpit/bwrap" } }
```

**`/etc/apt/apt.conf.d/80viberpit-sbx-bwrap`** keeps the copy current after a bubblewrap security update. It is a
no-op without the copy, reads the group from the existing copy, and never fails apt (`|| true`); a missed update shows
as the preflight's stale-copy failure:

```
DPkg::Post-Invoke { "if [ -d /usr/libexec/viberpit ] && [ -f /usr/libexec/viberpit/bwrap ] && [ -x /usr/bin/bwrap ] && ! cmp -s /usr/bin/bwrap /usr/libexec/viberpit/bwrap; then g=$(stat -c %G /usr/libexec/viberpit/bwrap) && install -o root -g $g -m 0750 /usr/bin/bwrap /usr/libexec/viberpit/bwrap; fi || true"; };
```

**Install** (dobby first, then rocky; nothing here touches a live session): `deploy/sandbox/install.sh
<viberpit-user>` (default `$SUDO_USER`). It validates the user, and refuses unless every existing directory from
`/usr/libexec/viberpit` up to `/` is root-owned, not a symlink and not group/other-writable. Then it runs:

```
sudo install -d -o root -g root -m 0755 /usr/libexec/viberpit
sudo install -o root -g root -m 0644 viberpit-sbx-bwrap /etc/apparmor.d/viberpit-sbx-bwrap
sudo apparmor_parser -Q -K -T /etc/apparmor.d/viberpit-sbx-bwrap     # parse again in place
sudo apparmor_parser -r -W /etc/apparmor.d/viberpit-sbx-bwrap        # load and cache; persists via apparmor.service
sudo install -o root -g <user's group> -m 0750 /usr/bin/bwrap /usr/libexec/viberpit/bwrap
sudo install -o root -g root -m 0644 80viberpit-sbx-bwrap /etc/apt/apt.conf.d/80viberpit-sbx-bwrap
sudo install -d -o root -g root -m 0755 /etc/claude-code
sudo install -o root -g root -m 0644 managed-settings.json /etc/claude-code/managed-settings.json
```

Load the profile before the copy exists, so that the copy is never run unconfined. On rocky an unconfined copy
fails anyway (`loopback: Failed RTM_NEWADDR`, spike row 28).

**Verify:**

1. `sudo grep viberpit-sbx-bwrap /sys/kernel/security/apparmor/profiles` → `viberpit-sbx-bwrap (enforce)`.
2. `/usr/libexec/viberpit/bwrap --ro-bind / / --dev /dev --unshare-user -- cat /proc/self/attr/current` →
   `viberpit-sbx-bwrap (enforce)`.
3. The real-helper probe from §1.2, with `/usr/libexec/viberpit/bwrap` in place of `bwrap-copy`, must print
   `helper-ok`, `CapEff: 0`, `Seccomp: 2`, a blocked `AF_UNIX` and a blocked nested userns. **Run it on rocky
   too.** That is the case the spike could not cover (sysctl=1).
4. Stock is unchanged: `/usr/bin/bwrap ... cat /proc/self/attr/current` → `bwrap//&unpriv_bwrap (enforce)`. On
   rocky, `codex sandbox -c 'sandbox_mode="workspace-write"' -- true` exits 0 (the README's Codex check).
5. Other users are excluded: `sudo -u nobody /usr/libexec/viberpit/bwrap --version` → `Permission denied`.
6. Run a non-sandboxed `claude -p` smoke test as tony. It must behave as before: the managed file sets only a key
   used when the sandbox is on.
7. After the code PR: the preflight's `seccomp_helper` check passes (§6 step 1, preflight).

**CLI auto-update.** Nothing in the host change names the CLI binary or its version. The profile attaches to the
copy's path, and the helper runs inside it as a child (`ix /**`). A new `versions/<ver>` is covered automatically;
dobby's 2.1.317, 2.1.318 and 2.1.319 sit side by side today. What a CLI update **can** break is the internal helper
protocol (`ARGV0` dispatch, fd 3), or the CLI dropping `bwrapPath`. The preflight probe runs the real helper of the
installed version and fails closed. The preflight must also re-run when the CLI version changes (§6 step 1, preflight).

**Rollback:** `deploy/sandbox/uninstall.sh`. It removes the managed file first (the CLI falls back to
`/usr/bin/bwrap`; the preflight fails; the toggle is disabled), then the copy and the apt hook, then unloads
(`apparmor_parser -R`, allowed to fail) and removes the profile. Every step runs even if an earlier one fails.

**Residual exposure.** Anyone who can run the copy gets bwrap without the userns restriction.
- On dobby that adds nothing, because the restriction is already off host-wide.
- On rocky it is limited to uid tony, who is already in `sudo` and `docker` (`id` on rocky shows groups 27(sudo)
  and 983(docker)).
- Sandboxed commands keep CapEff 0, seccomp, no AF_UNIX and no nested userns, all enforced by the helper.
- The outer CLI-composed shell and socat run in the copy's profile without kernel capabilities (bwrap drops them).
  They run only CLI-generated text.

---

## 2. F5 — proxy sockets hidden under home

### 2.1 Evidence

- The sockets are named `claude-http-<h>.sock` / `claude-socks-<h>.sock`, with `h=h8(8).toString("hex")` (16 hex),
  in `tg()` = `os.tmpdir()`. See `a1()` @~288398673 and the import `import{endianness as m8,tmpdir as tg}from"os"`.
  Rocky 2.1.293 uses the same names: `cw()`, `m6(8)`, `Jp()`.
  - The longest name is `claude-socks-` + 16 + `.sock` = **34 bytes**.
  - `os.tmpdir()` is also where `claude-empty-*` mount sources go (`mkdtempSync(join(tg(),"claude-empty-"))`).
- The default write list is `[".", $m()]` plus `hAe()` if it differs (@~288608093). Rocky has `[".",of()]`
  @209786665. So `os.tmpdir()` is **not** a write path unless `hAe()` falls back to it.
- `hAe()`/`j$e()` returns `<CLAUDE_CODE_TMPDIR>/claude-<uid>` only when that is ≤ `hNr=44` bytes (`hNr=44`
  @282248675; rocky `Cwr=44` @204966957).

### 2.2 Layout

The parent is configurable (`VIBERPIT_SANDBOX_TMP_PARENT`, default `/var/tmp/vpsbx`). It must be outside home.

```
/var/tmp/vpsbx/                 0700 tony   (deny-read for every sandboxed session)
  <key>/                        0700        key = sha256(session_id).hexdigest()[:12]; ".sid" marker file holds the full id
    t/                          0700        CLAUDE_CODE_TMPDIR  -> CLI write root t/claude-<uid> (restored rw by the CLI)
    s/                          0700        TMPDIR (CLI os.tmpdir) -> proxy sockets, claude-empty-*; re-allowed READ-ONLY
```

- **Why `/var/tmp`, not `/tmp`.** rocky's `/tmp` is a 15.5G tmpfs (`findmnt -T /tmp`). `/var/tmp` is ext4 on both
  hosts. `claude_sdk_factory.session_tmpdir_env` moved Bash output off the `/tmp` tmpfs on purpose, because a full
  tmpfs breaks every session at once.
- **Lengths** (computed): `/var/tmp/vpsbx/<12>/t/claude-1000` is **41 bytes**, which is ≤ 44, so `hAe()` never
  falls back and the shell's `TMPDIR` is the write root. The longest socket path,
  `/var/tmp/vpsbx/<12>/s/claude-socks-<16>.sock`, is **64 bytes**, which is ≤ 107 (`sun_path` 108 incl. NUL). With
  `/tmp/vpsbx` the figures are 37 and 60.
- **Why the parent deny is needed.** Every sandboxed shell has the same uid, so 0700 does not stop session B reading
  session A's dir; bwrap's `--ro-bind / /` shows everything not denied. Denying `/var/tmp/vpsbx` hides all
  sessions. The CLI then restores the session's own `t/claude-<uid>` (write) and its own `s` (read).
- **Why siblings.** `s` is not an ancestor of the write root, so the re-allow does not mount read-only over it.
  That is the F1 failure mode.

### 2.3 Stand-in verification (bwrap, same mount order as the CLI)

The order mirrors the CLI: the socket bind comes before the filesystem args (@288417101); then the deny tmpfs; then
the restored write path; then the `allowRead` ro-bind.

```
/usr/bin/bwrap --new-session --die-with-parent --ro-bind / / --dev /dev --proc /proc --unshare-user --unshare-pid --unshare-net \
  --bind $SOCK $SOCK  --tmpfs $P  --bind $A/t/claude-1000 $A/t/claude-1000  --ro-bind $A/s $A/s -- bash -c ...
```

Here `P=/tmp/3367-probe/var/vpsbx`, `A=$P/aaaaaaaaaaaa`, `SOCK=$A/s/claude-socks-0123456789abcdef.sock` (75 bytes),
and a host python server answers `pong`. Output:

```
[ls vpsbx]   only aaaaaaaaaaaa is visible (session bbbbbbbbbbbb is gone)
[ls A/s]     srwxr-xr-x ... claude-socks-0123456789abcdef.sock
[connect]    b'pong'                                   # the socket works through the read-only re-allow
[write t]    write-ok                                  # the write root is still writable (no F1)
[write s]    .../s/x: Read-only file system            # the socket dir is not writable
[read B]     cat: .../bbbbbbbbbbbb/t/claude-1000/out.txt: No such file or directory
```

The control is the same run without the `--ro-bind $A/s` re-allow, which is today's F5 shape: `connect` →
`FileNotFoundError: [Errno 2] No such file or directory`. This reproduces F5 and shows the re-allow fixes it.
Connecting to a socket on a read-only mount is allowed: `EROFS` applies only to regular files, dirs and symlinks.

### 2.4 Code changes

**`server/sessions/sandbox.py`**

- Replace `SESSION_TMP_PARENT`/`session_tmp_path()` (which today sits beside the shared root under `~/.cache`)
  with:
  - `sandbox_tmp_parent()`: from settings `sandbox_tmp_parent` (`VIBERPIT_SANDBOX_TMP_PARENT`, default
    `/var/tmp/vpsbx`). It is an absolute path, `realpath`ed.
  - `session_tmp_key(sid)` = `sha256(sid)[:12]`. It keeps the existing `ValueError` on bad ids.
  - `@dataclass(frozen=True) SessionTmp(root, tmp, sock)`. `session_tmp(sid)` returns paths only.
- `prepare_session_tmp(sid) -> SessionTmp`:
  - Create the parent 0700 and check it with `lstat`: a real dir, `st_uid == getuid()`, `mode & 0o077 == 0`. A
    `/var/tmp` squatter or symlink makes it fail closed (`SANDBOX_START_FAILED`).
  - Do the same for `root`, `t` and `s`.
  - Write or verify `root/.sid` == sid. A key collision fails closed.
  - Length guards: `len(f"{t}/claude-{os.getuid()}".encode()) <= 44` and
    `len(f"{s}/claude-socks-{'0'*16}.sock".encode()) <= 107`. Otherwise fail closed with a reason naming the
    parent. Put the constants (`_CLI_CHILD_TMPDIR_MAX=44`, `_SOCKET_NAME_MAX=34`, `_SUN_PATH_MAX=107`) next to
    their binary offsets.
- `remove_session_tmp(sid)`: rmtree `root`. Keep the existing symlink handling.
- New `sweep_session_tmp(live_ids)`, called at daemon start: remove `<parent>/*` whose `.sid` is not a live
  sandboxed session. This replaces leaked dirs after a crash. The add-only rule does not apply; this is new state.
- `read_policy(config_dir, cwd, sock_dir=None)`:
  - `deny += [sandbox_tmp_parent()]`.
  - If `sock_dir` is given, `allow.append(realpath(sock_dir))`.
  - Update the docstring: the socket dir is a sibling of the write root, never its ancestor (F1).
  - `settings_block`/`spawn_settings_overlay`/`check_dialog` gain the same `sock_dir` argument, so that the echo
    check compares exactly what was sent.
- `spawn_settings_overlay(..., tmp: SessionTmp)`: `env.TMPDIR = tmp.sock`. The flag layer still outranks the
  shared settings' `env.TMPDIR=~/.cache/claude-tmp` (the shared settings `env` keys include `TMPDIR`).
- `_default_socket_dir()` returns `sandbox_tmp_parent()`. The existing `proxy_socket_dir` preflight check then
  passes as it is. Add a `sandbox_tmp_parent` preflight check: it exists, or can be created, with owner and 0700.
  Add a length check too.

**`server/sessions/orchestrator.py`** (around L2660–2695)

- `extra["CLAUDE_CODE_TMPDIR"] = tmp.tmp`.
- `extra["TMPDIR"] = tmp.sock`. Belt and braces: the CLI's own `os.tmpdir()` reads the process env at call time,
  and the flag layer sets the same value.
- `confinement_hook(cwd, tmpdir=tmp.tmp, ...)`. Host-side Read/Grep stays within the write root, not the socket
  dir.
- Pass `sock_dir=tmp.sock` to `spawn_settings_overlay`.

**`server/sessions/claude_driver.py`** (L1261, L1384–1389)

- `self._sandbox_tmp: SessionTmp | None`. `fork_kw["sandbox_tmp"]` carries the dataclass.
- `_remove_sandbox_tmp` is unchanged apart from the type.
- The after-spawn `check_dialog` call passes `sock_dir`.

**`server/config.py`**

- Add `sandbox_tmp_parent: str = Field(default="/var/tmp/vpsbx", alias="VIBERPIT_SANDBOX_TMP_PARENT")`. The new
  setting uses the `VIBERPIT_` prefix (brand ratchet).

**Tests** (`server/tests/test_sandbox.py`)

- Rewrite the `session_tmp_path` tests (~L2505–2540) for the new layout, key, `.sid`, 0700, symlink and squatter
  refusal on the parent, and both length guards (failing case: a long parent).
- Add a `read_policy` test (next to ~L2422): it denies the parent and re-allows exactly `<own>/s`. The write root
  is never in `allowRead`. No `allowRead` entry is an ancestor of `t/claude-<uid>` (an F1 regression guard).
- Spawn test (~L650): the env carries `CLAUDE_CODE_TMPDIR=<t>` and `TMPDIR=<s>`, and the overlay has
  `env.TMPDIR=<s>`.
- Preflight test (~L2668): passes with the default parent and fails with a parent under home.
- `sweep_session_tmp` keeps live dirs and removes the rest.

---

## 3. F6 — symlinked `~/.local` and `~/.npm` on dobby

### 3.1 Evidence

- `ls -la ~` on dobby: `.local -> /mnt/ssk-ssd/tony/cache/.local`, `.npm -> /mnt/ssk-ssd/tony/cache/.npm`. Also
  `.docker -> /mnt/ssk-ssd/tony/cache/.docker`, `.claude.backup -> /mnt/ssk-ssd/tony/cache/.claude`, `.wine*`,
  `.steam`, `.verdaccio`, `jenkins-agent`, `ender3pro-klipper` (the last two point to `/mnt/hdd-storage/...`).
  Rocky has no home symlinks except within ViberPit's mounts.
- **The CLI refuses to restore a re-allow whose real path leaves the denied tmpfs.** At ~288380000+:
  `Not restoring ${R} over denyRead tmpfs ${r}: it resolves outside it, to ${D}`. Inside the sandbox,
  `/home/tony/.local/bin` does not exist.
- **Re-allowing the targets alone does nothing.** The deny is lexical (`home`), and
  `/mnt/ssk-ssd/tony/cache/.local/bin` is already readable inside. The breakage is that the **lexical** path is
  missing.
- **Security gap found.** Because the deny is lexical, every home symlink's target is readable inside today:
  - `/mnt/ssk-ssd/tony/cache/.docker/config.json` (0600, Docker registry auth) and `.token_seed`;
  - `/mnt/ssk-ssd/tony/cache/.claude/` (`history.jsonl`, `file-history`, …).
  - Seen with `ls`; no contents were read.
- **Pre-existing PATH gap.** The daemon's `PATH` on dobby is `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin`
  (from `/proc/<pid>/environ`), and so is the newest shell snapshot's `export PATH=`. `~/.local/bin` (where `uv`
  lives) is on **no** ViberPit session's `PATH`, sandboxed or not. Bare `uv` fails everywhere; `~/.local/bin/uv`
  fails only in the sandbox.

### 3.2 Fix (code-only; resolved at spawn time)

In `read_policy`:

1. `home_escapes = [realpath(p) for p in os.scandir(home) if p.is_symlink() and not _within(realpath(p), home)]`.
   Add each to **deny**. Dangling links are skipped. This closes the leak above.
2. For each `TOOLCHAIN_READS` entry, if `realpath(home/rel) != home/rel` and it resolves outside home, **also**
   re-allow the realpath. It is now inside a denied region (step 1), so the CLI's `allowWithinDeny` restores it.
   Keep the lexical entry too: on a host without the symlink (rocky) it is the one that works.
3. Add a sandbox-only `PATH` in `session_env()` (and the flag-layer `env.PATH`): the daemon's `PATH`, plus the real
   paths of `~/.local/bin` and `~/.cargo/bin` when they exist. Every entry that resolves under a denied root is
   rewritten to its real path.

Also needed: tests for each step with a `tmp_path` home containing a symlink out; a `check_dialog` echo test
(the lists still match exactly); and a preflight diagnostic `home_escapes: N`, informational.

**Known gaps (accepted, fix round 1).** Only top-level home entries are scanned, so a nested link such as
`~/.config/x -> /mnt/...` leaves its target readable. A dangling link's target is not denied, so a target that
appears later stays readable until the next spawn (the policy is resolved at spawn time). A target that is itself
root-owned and not group/other-writable (`~/tool -> /opt/tool`, a root-installed `/usr/local/x`) is not denied:
it is system-installed software any local user can read, and hiding it only breaks toolchains. A user-owned
target (dobby's `/mnt/ssk-ssd/...`, a tony-owned `/usr/local/x`) is still denied.

**Residual.** Tools that find data through `$HOME/.local/share/...`, such as uv-managed Pythons, still look in the
lexical place. Today's venvs use `/usr/bin` Pythons (`pyvenv.cfg home = /usr/bin` for `~/GitHub/viberpit` and
`viberpit-deploy`), so nothing is known to break. If something does, the host-side alternative is to replace the
two symlinks with bind mounts, for example an fstab line
`/mnt/ssk-ssd/tony/cache/.local /home/tony/.local none bind,x-systemd.requires-mounts-for=/mnt/ssk-ssd 0 0`, so
that the real path stays under home. That is an owner call; step 1 would then need mountinfo-based alias detection.

---

## 4. Rocky push credential

### 4.1 Evidence

- rocky: `git config --global --get-all credential.helper` → `store`; `~/.git-credentials` (0600) holds
  `https://steiner385:<redacted>@github.com`.
- rocky: `gh auth status` → `Logged in to github.com account steiner385 (/home/tony/.config/gh/hosts.yml)`. That is
  file-backed and the same account, and `~/.config/gh` is already re-allowed (spike row 2).
- dobby: `git config --get-urlmatch credential.helper https://github.com` → `!/usr/bin/gh auth git-credential`, so
  dobby needs nothing.
- **Probe on rocky.** Inject the helper through git's env config. The output is redacted except for the dummy
  value:

```
$ printf 'protocol=https\nhost=github.com\n\n' | env GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 \
    GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
    GIT_CONFIG_KEY_1=credential.https://github.com.helper \
    'GIT_CONFIG_VALUE_1=!f() { echo username=probe; echo password=DUMMY-FROM-ENV-HELPER; }; f' git credential fill
username=probe
password=DUMMY-FROM-ENV-HELPER          # the store helper never ran (control without env → username=steiner385)
```

### 4.2 Fix (code, both hosts; no host change)

`sandbox.session_env()` adds, for sandboxed sessions only:

```
GIT_CONFIG_COUNT=2
GIT_CONFIG_KEY_0=credential.helper                     GIT_CONFIG_VALUE_0=            (reset the list)
GIT_CONFIG_KEY_1=credential.https://github.com.helper  GIT_CONFIG_VALUE_1=!<shutil.which("gh")> auth git-credential
```

- Add it only when `gh` resolves and the gh config that sandboxed gh will read is file-backed: the `GH_CONFIG_DIR`
  we set, else the ambient one.
- If either fails, omit it. The preflight `gh` check already fails in that case.
- If an inherited `GIT_CONFIG_COUNT` exists, append to it rather than clobbering it.

Tests: the env shape; the omission when gh is keyring-only; the append. Live check: spike row 11 (`git push origin
HEAD:sandbox-probe/<ts>`) **on rocky**.

The host-side alternative is to switch rocky's global helper to gh (`git config --global credential.helper ''`
plus the gh helper) and delete `~/.git-credentials`. That changes unsandboxed behaviour too, so it is not
recommended for this issue.

---

## 5. Probes for checks not yet run live

Use the spike §5.1 harness: a real `SessionOrchestrator`, a scratch DB, a scratch `CLAUDE_CONFIG_DIR`, and a
throwaway clone under home. Use the real host fix and the real code PR. No (U) or (S) stand-ins.

**5.1 WebFetch (F7).**
1. With no model call, read the init `tools` list for each account, with haiku, sonnet and opus, unsandboxed.
   Record which ones list `WebFetch`. The shared settings allow `WebFetch` (the `permissions.allow` key includes
   it), and neither the daemon env nor the session code sets `CLAUDE_CODE_DISABLE_WEB_FETCH` (count 0). So its
   absence is a CLI or account-side gate.
2. If any account and model offers it, run a sandboxed session there in `default` mode:
   - `WebFetch https://api.github.com/zen` → allowed (expect content);
   - `WebFetch https://example.com` → denied by `PreToolUse` ("WebFetch is limited to …");
   - `http://api.github.com` → "https-only".
3. If none offers it, record row 15 as N/A, citing the init lists. The confine path stays unit-tested
   (`test_the_allowlist_matrix_*`), and network egress is proven by row 14.

**5.2 Bash refused while an Edit waits on a card (row 21).** Mode `default`. One prompt: "In one message:
(a) launch a background sub-agent (Task, run_in_background) whose only job is to run Bash `echo from-subagent`;
(b) Edit `README.md` to add one line." Leave the Edit card **unanswered for 60 s**. The sub-agent needs a model
round trip, so its Bash arrives while the Edit is admitted and pending.
- Expect the sub-agent's Bash to get `COMMAND_WAITS_FOR_WRITE`: "A file edit is being written in this session;
  retry …".
- Then approve the card. The sub-agent's retry runs.
- Assert that the gate is empty at turn end.
- If timing puts the Bash first, the inverse refusal (`WRITE_WAITS_FOR_COMMAND`) shows instead. Re-run with a
  sub-agent prompt that starts with one Read.

**5.3 Reload releases a gate whose end event was lost (row 27).**
1. Background Bash: `timeout 600 tail -f /dev/null` with `run_in_background`. The gate holds it in `background`.
2. Harness-only monkeypatch: `SandboxGate.background_ended` becomes a no-op for this driver, simulating the lost
   event. Then `KillShell`/stop the task.
3. Edit `x` → refused (`WRITE_WAITS_FOR_COMMAND`), which is fail-closed as designed.
4. `POST /api/sessions/{id}/reload` (`orch.reload_session`). A new driver means a new, empty gate, `known=True`
   after the verified spawn.
5. Edit `x` → admitted. `pgrep -f 'tail -f /dev/null'` on the host → none (`--die-with-parent`, `--unshare-pid`).
6. Check that `/var/tmp/vpsbx/<key>` was recreated (new `.sid` match) and that the old CLI's sockets are gone.

**5.4 Re-run all of spike §5.2 rows 1–28 on both hosts with no stand-ins.** These also check the fixes:
- The new F5 rows: `ls /var/tmp/vpsbx` inside shows only the own key, and `echo $TMPDIR` is
  `/var/tmp/vpsbx/<key>/t/claude-1000`.
- The F6 rows: `ls /mnt/ssk-ssd/tony/cache/.docker` → no such file, and `uv --version` runs.
- Row 28 and row 11 on rocky.

---

## 6. Rollout order (each step with its rollback)

1. **Code PR** (§2.4, §3.2, §4.2, and the preflight changes below). It ships dark: `VIBERPIT_SANDBOX_ENABLED`
   stays off. Gates: ruff, mypy, `pytest tests/`, `pytest server/tests/` (`-n 4`).
   *Rollback:* revert the PR. Nothing reads the new code while the switch is off.
   - **Preflight changes:**
     - `seccomp_helper` runs the **real** helper through the bwrap the CLI will use: the `sandbox.bwrapPath` of
       `/etc/claude-code/managed-settings.json` (and `managed-settings.d/*.json`), else `which bwrap`. The argv is
       `[bwrap, --new-session, --die-with-parent, --ro-bind, /, /, --dev, /dev, --proc, /proc, --unshare-user,
       --unshare-pid, --unshare-net, --, bash, -c, 'exec 3<"$1"; SRT_ALLOW_NESTED_USERNS=0 ARGV0=apply-seccomp
       /proc/self/fd/3 /bin/true', probe, <realpath(resolve_claude_bin())>]`. It replaces the `unshare` stand-in,
       per the single-source rule.
     - New `bwrap_copy` check: the managed bwrapPath is root-owned and not group- or world-writable, and its
       sha256 equals `/usr/bin/bwrap`'s. Otherwise fail with "the sandbox's bwrap copy is stale; reinstall it".
     - The preflight cache is keyed by CLI version as well as host, so it re-runs after an auto-update.
     - Update the README "Claude session sandbox" section with the host steps.
2. **Host change on dobby** (§1.4). It is independent of the code and safe while the switch is off. Run verify
   steps 1–6. *Rollback:* §1.4 rollback.
3. **Host change on rocky** (§1.4), including verify step 3 (the real helper under sysctl=1) and step 4 (the Codex
   check). *Rollback:* the same.
4. **Deploy the code PR** through the deploy controller, after `tipCi:green`. Confirm that the preflight on both
   hosts shows all checks OK in the Environment card (host readiness). *Rollback:* deploy the previous tip.
5. **Live re-run of spike §5** (§5 above), from a harness outside the live daemon and edge. Record the results in the
   spike doc as §5.4 "Re-run". *Rollback:* none needed (read-only to production).
6. **Flip the switch.** Add `VIBERPIT_SANDBOX_ENABLED=true` to `/etc/viberpit/viberpit.env`. On dobby that file
   is read by both the edge and the daemon; check that `daemon.env` does not override it. On rocky, add it to its
   `/etc/viberpit/viberpit.env`. Restart the edge, then the daemons. Each daemon restart drains its live sessions,
   so do it when the Sessions page shows nothing mid-turn. The default for new sessions stays **off**
   (`AppSettings`); the switch only makes the toggle available. *Rollback:* remove the line and restart the same
   way. Recorded sandboxed sessions are then held, and "Turn off sandbox and continue" still works (README).
7. **Benchmark row.** In `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` L148, change "Sandbox settings"
   from ❌ to Y with the evidence, and regenerate §3 (plan doc L135). Close #3367 with `Fixes #3367` in that PR's
   body. *Rollback:* revert the doc PR.

---

## 7. Owner decisions and risks

1. **F4 trade-off (needs a yes).** A root-owned bwrap copy, usable by uid tony only, runs without Ubuntu's userns
   restriction on its children.
   - dobby: no change, because the restriction is already off host-wide.
   - rocky: tony already holds sudo and docker.
   - The alternatives are waiting for an upstream CLI change, or nothing.
2. **`/etc/claude-code/managed-settings.json` is a host-wide CLI policy file.** It also applies to interactive
   `claude` and to Claude Desktop's Code sessions on dobby (`/etc/apparmor.d/claude-desktop` shows Desktop is
   installed). It only takes effect when one of those turns on the sandbox.
3. **The helper protocol is internal** (Principle I, experimental vendor surface): `ARGV0=apply-seccomp` and fd 3
   could change in any CLI release. Only the preflight probe depends on it, and it fails closed. Gate on the
   probe, not on a version allowlist.
4. **The temp parent:** `/var/tmp/vpsbx` (recommended; disk-backed) or `/tmp/vpsbx` (tmpfs on rocky).
5. **F6 approach:** the code-only fix (recommended) or the host bind mounts.
6. **dobby's `/etc/sysctl.d/60-ccswitch-userns.conf`** disables the userns restriction host-wide. It has been in
   place since 2026-06-22, likely for the managed Chromium sandbox (`config.py` `VIBERPIT_CHROMIUM_NO_SANDBOX`
   comment). It is out of scope and neither needed nor touched by this plan. It is exactly the host weakening the
   issue warns against, and its name trips nothing in the repo (host state).
7. **rocky's `bwrap-userns-restrict` is a stale local version** (abi 4.0) with the packaged update parked as
   `.dpkg-dist`. Not needed here; worth reconciling separately (the Codex README fix depends on it).
8. **Upstream issues to file** (anthropics/claude-code): bind the proxy sockets after the filesystem args (F5), and
   apply seccomp through `bwrap --add-seccomp-fd` instead of a nested-userns helper (F4).

No blocker makes this infeasible. The one fact this plan could not verify without loading a profile is that the
copy really runs as `viberpit-sbx-bwrap` and that the helper succeeds under it on rocky. That is §1.4 verify steps
2–3, the first thing to run after the host change.
