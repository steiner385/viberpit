# D-23 "Sandbox for Claude sessions": verification spike

Date: 2026-11-06. Research only: no product code changed. Constitution III: each claim below was checked against the
installed CLI binary (byte offsets), against ViberPit's code, or by a read-only host command. Claims I inferred from code
without running them are marked **(code-read, not probed)**.

- Decision under test: **D22a** (2026-11-06), `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` §7, line 295.
- Earlier spike: `2026-11-04-d14-session-settings-verification.md` §2. It found three problems: the sandbox fails open
  without socat, it auto-allows Bash, and it blocks the sockets gh and push need.

## 0. Versions and method

| Host | CLI the live sessions run (`readlink /proc/<pid>/exe`) | Notes |
|---|---|---|
| dobby | `~/.local/share/claude/versions/2.1.319` (340,226,576 bytes) | All offsets below are from this file |
| rocky | `~/.local/share/claude/versions/2.1.293` | Same key strings are present: `sandbox.failIfUnavailable is set`, `sandbox_required_unavailable`, `get_sandbox_dialog`, `strictAllowlist`, `Ignored on Linux (seccomp cannot filter by path)` |

- Python SDK: `claude-agent-sdk==0.2.105` (`pyproject.toml:10`).
- Method: `LC_ALL=C /usr/bin/grep -aob '<needle>' "$B"`, then `tail -c +$((off-N+1)) "$B" | head -c M`. Call GNU grep
  as `/usr/bin/grep`: inside a session, `grep` is a shell function that runs the CLI's embedded ugrep, which rejects
  long `-E` patterns.
- CLI probes: `claude --version` printed `2.1.319 (Claude Code)`. `claude --help` lists no sandbox flag; the only route
  is `--settings <file-or-json>`. No stream-json probe and no model call was made. The D-14 spike's no-model probes S, S'
  and R (`get_sandbox_dialog` and live `apply_flag_settings`) are reused as evidence below.

## 1. Findings

| # | Question | Finding | Evidence |
|---|---|---|---|
| 1 | Settings schema | See §1.1. Linux-relevant keys: `enabled`, `failIfUnavailable`, `autoAllowBashIfSandboxed`, `allowUnsandboxedCommands`, `excludedCommands`, `network.{allowedDomains, deniedDomains, strictAllowlist, allowAllUnixSockets, httpProxyPort, socksProxyPort}`, `filesystem.{allowWrite, denyWrite, denyRead, allowRead, disabled}`, `credentials`, `ignoreViolations`, `enableWeakerNestedSandbox`, `allowNestedUserNamespaces`. **`network.allowUnixSockets` and `network.allowLocalBinding` are macOS-only.** | Schema `GLr` @282541783; network `ph` @282520636; filesystem `gh` @282524116 |
| 2 | Missing dependencies | **Fail-closed exists: `sandbox.failIfUnavailable: true`.** At spawn in stream-json mode the CLI writes a `result` frame with `is_error: true`, `subtype: "error_during_execution"` and `startup_failure_reason: "sandbox_required_unavailable"`, then exits 1. Without the key (the default), it prints a stderr warning and runs Bash **unsandboxed**. The Python SDK does **not** add `failIfUnavailable`; the TS SDK does. Turning the sandbox on **mid-session** on a host that lacks deps appears to fail open even with `failIfUnavailable` **(code-read, not probed)**. To detect that the sandbox is active, use the `get_sandbox_dialog` control request. | §1.2 |
| 3 | Delivery | Put a `sandbox` block in the `--settings` JSON at spawn (ViberPit's `spawn_settings(flag_settings)`). `apply_flag_settings` changes it live (probe S). The SDK's `sandbox=` option merges into the same JSON but adds nothing; don't use it. Never write it to `CLAUDE_CONFIG_DIR/settings.json`, which every account shares by symlink. | §1.3 |
| 4 | Permission cards | With `autoAllowBashIfSandboxed: false`, the sandbox auto-allow branch returns `null` and Bash takes the normal permission path. Read-only commands are still CLI-allowed (as they are today without a sandbox); everything else reaches `can_use_tool`. **Whether a card appears is then up to ViberPit's registry.** `full-auto` and `bypassPermissions` approve every Bash call, every `dangerouslyDisableSandbox` retry and every network ask without a card. | §1.4 |
| 5 | Network | `bwrap --unshare-net` plus a socat bridge to an in-process HTTP/SOCKS proxy, which filters **per domain**. A domain not on the list becomes a `can_use_tool` request for the pseudo-tool `SandboxNetworkAccess`, input `{host}`, unless `strictAllowlist: true`, which denies it outright. git over ssh is proxied through `GIT_SSH_COMMAND` with a socat ProxyCommand. **Unix sockets: on Linux it is all or nothing** (seccomp). gh on dobby needs the D-Bus session socket (`/run/user/1000/bus`) to read its keyring token. Rocky's gh token is file-backed. No `GIT_ASKPASS` is set; git uses `credential.helper = !/usr/bin/gh auth git-credential`. | §1.5 |
| 6 | Filesystem | Reads are allowed everywhere by default. Writes are allowed only in the cwd, the Claude temp root (`CLAUDE_CODE_TMPDIR`; sessions set `/home/tony/.cache/viberpit/claude-tmp`) and the child-process tmpdir. `TMPDIR` is pointed there. **The worktree's common git dir (`<clone>/.git`) is granted write automatically**, except `hooks/`, `config`, `config.worktree` and `commondir`, which are write-denied. `/tmp`, `~/.cache`, `~/.npm`, other checkouts and `CLAUDE_CONFIG_DIR` are not writable. | §1.6 |
| 7 | Host preflight | **dobby passes. rocky fails: socat is not installed.** Both hosts have bwrap 0.11.1 and all four `bwrap` probes succeed (rc=0), including the CLI's own `--unshare-user` probe. rocky has `apparmor_restrict_unprivileged_userns=1`, but the Ubuntu `bwrap-userns-restrict` profile allows bwrap. Neither daemon unit restricts namespaces. | §1.7 |

### 1.1 Settings schema (2.1.319, `GLr` @282541783, `ph` @282520636, `gh` @282524116)

| Key | Default | Note (quoting the binary's description where it matters) |
|---|---|---|
| `enabled` | false | "Run Bash commands inside the sandbox." Only the Bash tool and its children are sandboxed. Edit/Write, MCP servers and WebFetch are not. |
| `failIfUnavailable` | false | "Exit with an error at startup if sandbox.enabled is true but the sandbox cannot start (missing dependencies or unsupported platform). When false (default), a warning is shown and commands run unsandboxed." |
| `autoAllowBashIfSandboxed` | **true** | `Nue()` @288690107: `Ar()?.sandbox?.autoAllowBashIfSandboxed??!0` |
| `allowUnsandboxedCommands` | true | When false, "the dangerouslyDisableSandbox parameter is completely ignored and all commands must run sandboxed". A false in `--settings` holds. |
| `excludedCommands` | [] | Bash-rule patterns that run **outside** the sandbox. "A convenience, not a security boundary: excluded commands still go through the permission flow." Calls containing `cd`, `$(…)` or most redirects stay sandboxed whatever the entries say. |
| `network.allowedDomains` | [] | Wildcards (`*.example.com`) are supported. Merged with `WebFetch(domain:…)` allow rules. |
| `network.deniedDomains` | [] | Always blocked. |
| `network.strictAllowlist` | false | "When true, the sandbox runtime deterministically denies hosts not in allowedDomains instead of prompting." Honored from `--settings`. |
| `network.allowUnixSockets` | — | "**macOS only**: Unix socket paths to allow. **Ignored on Linux (seccomp cannot filter by path).**" |
| `network.allowAllUnixSockets` | false | "If true, allow all Unix sockets (disables blocking on both platforms)." |
| `network.allowLocalBinding` | — | "**macOS only**". |
| `network.httpProxyPort` / `socksProxyPort` | — | Use your own proxy instead of the CLI's. |
| `filesystem.allowWrite` / `denyWrite` / `denyRead` / `allowRead` | [] | Merged with Edit/Read permission rules. |
| `filesystem.disabled` | unset (isolation on) | Drops filesystem isolation but keeps network and seccomp. |
| `credentials.{files, envVars}` | — | `deny` or `mask` (sentinel substitution plus proxy injection). Experimental surface. |
| `ignoreViolations` | {} | Map from command pattern to paths whose violations are not reported. |
| `enableWeakerNestedSandbox` | false | Linux: skips the fresh `/proc`. "Reduces security." |
| `allowNestedUserNamespaces` | false | Needed by Chromium/Electron sandboxes and nested bwrap. "**Removes code-execution isolation**." |
| `bwrapPath` / `socatPath` | auto (PATH) | Honored from managed settings only. |

### 1.2 Missing dependencies and detecting an active sandbox

- **Dependency check** `o1()` @288396898. Errors (the sandbox cannot run):
  - `bubblewrap (bwrap) not installed`
  - `socat not installed`
  - a failed `bwrap --unshare-user --dev-bind / / true` probe (`B8`); this probe only runs when euid is 0.
- **Dependency warnings** (the sandbox runs weaker):
  - `seccomp not available - unix socket access not restricted`. **Without seccomp, unix sockets are not blocked at all.**
  - bwrap older than 0.5.0.
- **Spawn path, stream-json** (@322817156):
  - If `getSandboxUnavailableReason()` is set and `isSandboxRequired()` is true, the CLI writes
    `A2e({message:"Sandbox required but unavailable: <reason>. Set sandbox.failIfUnavailable=false to allow unsandboxed
    execution.", reason:"sandbox_required_unavailable", resultIndex:null})`, prints
    `Error: sandbox required but unavailable … refusing to start without a working sandbox` to stderr and exits 1.
  - `A2e` @289521560 and `hU` produce `{type:"result", subtype:"error_during_execution", is_error:true, errors:[msg],
    startup_failure_reason}`.
  - If `isSandboxRequired()` is false, the CLI prints `⚠ Sandbox disabled: … Commands will run WITHOUT sandboxing` and
    carries on. Probe R saw exactly this on rocky.
  - If the deps are present but `ze.initialize()` throws, the CLI prints `❌ Sandbox Error` and exits 1 whatever
    `failIfUnavailable` says.
- **`isSandboxRequired` (`Vd`)** @288703876 is `Kl() && sh() && (sandbox.failIfUnavailable ?? false)`.
- **Python SDK 0.2.105** `subprocess_cli._build_settings_value` sets `settings_obj["sandbox"] = options.sandbox` as given.
  Its `SandboxSettings` TypedDict has no `failIfUnavailable` key, and its docstring example uses `allowUnixSockets` and
  `allowLocalBinding`, both of which are ignored on Linux.
- **The TS SDK differs.** Its copy embedded at @310844606 adds `failIfUnavailable=!0` when `enabled===true`. ViberPit
  uses Python, so it must send the key itself.
- **Losing the sandbox mid-session when it is required.** `Lw()` @288715656 refuses the command: "Sandbox is required
  (sandbox.failIfUnavailable) and stopped being available in this session … do not retry it that way". That is
  fail-closed.
- **Not required, init fails at the first command.** The CLI says "Sandboxing is disabled for the rest of this session"
  (@288717106). That is fail-open.
- **Turned on mid-session on a host without deps (code-read, not probed).** `cb()` needs `startedOnce`. With
  `startedOnce` false, `Hl()` (`isSandboxingEnabled`) returns false because `Uc().errors.length>0`. `isSandboxRequired`
  has only two call sites, and both are startup paths (@320024743, @322817156). So a live `apply_flag_settings
  {sandbox:{enabled:true, failIfUnavailable:true}}` on rocky today would most likely leave Bash running unsandboxed with
  no refusal.
- **Detecting the state: `get_sandbox_dialog`** (control request; schema `Wa`/`G$` @284666901; handler @322962538).
  - Fields: `supported`, `locked`, `enabled` ("sandboxing is running in this session: on in the settings, and the
    sandbox started"), `enabled_in_settings`, `unavailable_reason`, `mode` (`auto-allow|regular|disabled`),
    `unsandboxed_fallback`, `dependencies.{errors,warnings}`, `excluded_commands`, and
    `restrictions.{network_allowed_domains, allow_all_unix_sockets, fs_allow_write, …}`.
  - The handler refuses with "get_sandbox_dialog is not available on this connection" when `Em(e)` is true. On the stdio
    SDK connection it answered (probes S and R).
  - Probe R on rocky returned `enabled:false, enabled_in_settings:true, unavailable_reason:"…socat not installed…"`.
  - I found no sandbox field in the init message and no status line that would serve.

### 1.3 Delivery

- **Spawn.** `server/sessions/claude_sdk_factory.py:52` `spawn_settings(flag_settings)` → `settings=` → `--settings
  <json>` (the flag layer, which sits above user, project and local). Add the `sandbox` block to `flag_settings`.
  - Several sandbox keys are honored only from "user, managed/policy, or CLI (`--settings`)": `strictAllowlist`,
    `allowNestedUserNamespaces`, `credentials.*`, and `filesystem.disabled`.
  - A `false` for `allowUnsandboxedCommands` holds from `--settings`.
  - Project `.claude/settings.json` cannot loosen them, which matters because the repo under edit is untrusted.
- **Live.** `ClaudeAgentDriver.apply_flag_settings` (`claude_driver.py:2409`) already sends the raw control request.
  Probe S: `{sandbox:{enabled:true}}` → dialog `enabled:true`; `{sandbox:{enabled:false}}` → `enabled:false`.
  `get_sandbox_dialog` can go over the same `_send_control_request` seam.
- **Merge semantics are unverified.** It is not known whether a live write merges into the nested `sandbox` object or
  replaces it. Send the full block on every write.
- **Today.** No `sandbox` key exists in the shared account `settings.json`, and there is no `/etc/claude-code` managed
  settings on either host.

### 1.4 Permission cards

- **The sandbox auto-allow is gated.** `B6e()` @~293197900 starts with
  `if(!ze.isSandboxingEnabled()||!ze.isAutoAllowBashIfSandboxedEnabled()||…) return null`.
- **The rule path is gated the same way.** In `Sx()` @295161893 the allow-rule shortcut is suppressed only when
  sandboxing is on and auto-allow is on.
- **With auto-allow off, the Bash flow is unchanged from today.**
  - Explicit allow rules allow.
  - `ai.isReadOnly()` commands are CLI-allowed ("Qie").
  - Everything else returns `passthrough`, "This command requires approval", and so reaches `can_use_tool`.
  - In SDK `acceptEdits`, the CLI's existing acceptEdits shell handling (`Q9t`) applies as it does without a sandbox.
  - In SDK `bypassPermissions`, the CLI never calls `can_use_tool`, but commands are still wrapped.
- **ViberPit modes** (`server/sessions/permissions.py`):
  - `interactive`, `auto`, `allowlist` and `full-auto` run the SDK in `default`. The registry decides.
  - `full-auto` and `bypassPermissions` → `_resolved(True)` for every tool (`permissions.py:298`). That includes Bash,
    Bash with `dangerouslyDisableSandbox:true`, and `SandboxNetworkAccess`.
  - `auto` approves any tool that is not on the denylist, which plausibly includes `SandboxNetworkAccess` (not
    enumerated anywhere in `server/`).
  - Automation sessions resolve to `full-auto` (memory: accept-edits note).
- **So "permission cards still appear" holds only in the interactive-type modes.** In full-auto, containment rests
  entirely on the sandbox settings. That is why `allowUnsandboxedCommands:false` and `strictAllowlist:true` are needed
  below.
- **New card type.** `SandboxNetworkAccess` is a new tool name. It reaches the registry with input `{host}` and the
  description "Allow network connection to <host>?" (@321843572). ViberPit has no handling for it today.

### 1.5 Network

- **Mechanism** (`Lpt` @286500644; Linux wrapper @288416451):
  - `bwrap --unshare-net` cuts the command off from the network.
  - socat bridges a unix socket inside the sandbox to the CLI's HTTP and SOCKS proxies.
  - The env inside gets `HTTP(S)_PROXY`, `ALL_PROXY=socks5h://…`, `GIT_SSH_COMMAND=ssh … -o ProxyCommand='socat -
    PROXY:localhost:%h:%p,proxyport=…'`, and `NO_PROXY` set to localhost, RFC 1918 and link-local.
  - The proxy filters by host.
  - **Loopback services on the host (`:8787`, a local Postgres) cannot be reached from inside**, because the sandbox has
    its own network namespace and `allowLocalBinding` is macOS-only.
- **Unknown host.** With `strictAllowlist:false` the CLI sends `can_use_tool` for `SandboxNetworkAccess`, and an allow
  adds a session host. With `strictAllowlist:true` it denies deterministically and never asks.
- **GitHub over https.** The clone remote is `https://github.com/steiner385/viberpit.git` and
  `credential.helper=!/usr/bin/gh auth git-credential`.
  - git needs `github.com`.
  - gh needs `api.github.com`; `uploads.github.com` for release assets; `codeload.github.com` for archives; and
    `objects.githubusercontent.com` / `raw.githubusercontent.com` (LFS, raw, artifacts).
  - These are GitHub's published host names, not observed through the proxy.
- **git over ssh.** It goes through the socat ProxyCommand to `github.com:22`, which must be allowed. `SSH_AUTH_SOCK` is
  a unix socket and would be blocked. The live session env has no `SSH_AUTH_SOCK`, and ViberPit pushes over https.
- **Unix sockets: this decides the design.**
  - Linux has only `allowAllUnixSockets` (all or nothing).
  - gh on dobby keeps its token **only** in the GNOME keyring (`gh auth status` → `(keyring)`). The daemon-spawned
    session env has no `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR` or `GH_CONFIG_DIR`, so gh falls back to
    `/run/user/1000/bus`.
  - Host probes on dobby, run from a session shell:
    - A: `gh auth token` → rc 0.
    - B: `bwrap --ro-bind / / --dev /dev --unshare-net gh auth token` → **rc 0**. bwrap alone does not block the bus;
      the CLI's seccomp filter does.
    - C: with `DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/bus` → rc 1, "no oauth token found for github.com".
    - D: `gh auth git-credential get`, same env → rc 1. **So sandboxed `git fetch` and `git push` fail on dobby, not only
      gh.**
    - E: `GH_CONFIG_DIR=$VIBERPIT_GH_CONFIG_DIR` (`~/.config/viberpit/gh/hosts.yml`, which holds an `oauth_token`) with
      the bus unreachable → the credential helper returns `protocol/host/username/password`. **A file-backed gh config
      removes the socket dependency.**
  - rocky: `gh auth status` → `(/home/tony/.config/gh/hosts.yml)`. It is already file-backed and needs no socket.
  - **`allowAllUnixSockets:true` is a sandbox escape on both hosts.** It would also open:
    - `/run/docker.sock` (tony is in group `docker` on both hosts; that is root-equivalent);
    - the user D-Bus bus (`systemd-run --user …` runs code outside the sandbox and reads keyring secrets);
    - libvirt on rocky.

### 1.6 Filesystem

- **Default write allow** (@288608093): `Ce = [".", $m()]`, plus `hAe()` when it differs.
  - `$m()` is the Claude temp root, from `Mu()` → `CLAUDE_CODE_TMPDIR`. Sessions set
    `/home/tony/.cache/viberpit/claude-tmp`.
  - `hAe()` is the child-process tmpdir.
  - Inside the sandbox, `TMPDIR` is set to `CLAUDE_CODE_TMPDIR || CLAUDE_TMPDIR || /tmp/claude` (`Lpt`), so `tmp_path`
    and `mktemp` work.
- **Write-denied inside that set:**
  - `.claude/{settings*.json, skills, commands, agents, hooks}` and `.mcp.json` up the tree;
  - many `CLAUDE_CONFIG_DIR` subpaths (`projects`, `plugins`, `hooks`, `settings`, `CLAUDE.md`, …);
  - git `hooks/`, `config`, `config.lock` and `config.worktree`.
- **Worktree common dir.** `fwr()` @288687776 reads `<cwd>/.git` → `gitdir: <clone>/.git/worktrees/<n>`. It checks that
  the parent is `worktrees/` under a `*.git` directory and that the back-pointer matches, then returns `<clone>/.git`.
  `Hd(we, true, ql)` @~288620100 pushes it onto the write allow list and the git safe-directory list, and `So()` re-denies
  `hooks`, `config` and `commondir` in it.
  - ViberPit's layout fits: `.git` → `/home/tony/GitHub/.ccswitch/clones/steiner385/viberpit/.git/worktrees/e25fdd05`,
    whose `gitdir` points back.
  - **Commits, fetches and ref updates should work. `git push -u` / `git branch --set-upstream-to` will fail**, because
    they write `.git/config` (inferred from the deny list, not probed).
- **Not writable:** `/tmp` itself, `~/.cache` (uv, pip, npm, and the pytest slot lock — that guard fails open),
  `~/.npm`, and any other checkout. The worktree's `web/node_modules` is a symlink into
  `/home/tony/GitHub/viberpit-deploy/web/node_modules`, so Vite or Vitest cache writes there would be denied.
- **Reads** are allowed everywhere by default, including `~/.config/gh/hosts.yml` and `~/.ssh`. The token is readable
  inside the sandbox, as it is today.
- **No nested user namespaces** (`allowNestedUserNamespaces:false`). Playwright's Chromium sandbox, nested bwrap and
  docker do not work inside.

### 1.7 Host preflight (script `/tmp/d23-preflight.sh`; logs `/tmp/d23-preflight-{dobby,rocky}.log`)

| Check | dobby (Ubuntu 26.04.1, kernel 7.0.0-42) | rocky (Ubuntu 26.04.1, kernel 7.0.0-38) |
|---|---|---|
| `bwrap` | `/usr/bin/bwrap`, bubblewrap 0.11.1 (pkg 0.11.1-1ubuntu0.3), not setuid | same |
| `socat` | `/usr/bin/socat` 1.8.1.1 | **missing** (candidate 1.8.1.1-1ubuntu0.1) |
| `kernel.unprivileged_userns_clone` | 1 | 1 |
| `user.max_user_namespaces` | 70340 | 115498 |
| `kernel.apparmor_restrict_unprivileged_userns` | 0 | **1** (profile `bwrap-userns-restrict` present) |
| `bwrap --ro-bind / / --dev /dev true` | rc 0 | rc 0 |
| `… --unshare-net true` | rc 0 | rc 0 |
| `bwrap --unshare-user --dev-bind / / true` (CLI's probe) | rc 0 | rc 0 |
| `… --proc /proc --unshare-pid --unshare-net true` | rc 0 | rc 0 |
| Daemon unit namespace hardening | none (`User=tony`) | none |
| seccomp helper | D-14 probe S: `dependencies.warnings:[]` | unknown (probe R was masked by the socat error) |

## 2. Recommended design (meets D22a, with the amendments in §4)

### 2.1 Settings JSON (merged into `flag_settings` at spawn)

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "autoAllowBashIfSandboxed": false,
    "allowUnsandboxedCommands": false,
    "excludedCommands": [],
    "network": {
      "allowedDomains": ["github.com", "*.github.com", "*.githubusercontent.com"],
      "strictAllowlist": true,
      "allowAllUnixSockets": false
    }
  }
}
```

- `failIfUnavailable: true` makes the spawn fail closed (problem 1).
- `autoAllowBashIfSandboxed: false` keeps the cards (problem 2).
- `allowUnsandboxedCommands: false` stops the model from escaping through `dangerouslyDisableSandbox`, which full-auto
  would otherwise approve silently.
- `strictAllowlist: true` means no `SandboxNetworkAccess` asks. The allowlist is not then at the mercy of the registry's
  `auto`/`full-auto` shortcuts.
- The session env (not settings) gets `GH_CONFIG_DIR=<file-backed gh config>` on hosts whose gh token is keyring-only.
  This solves problem 3 without opening sockets. On dobby the path is the existing `VIBERPIT_GH_CONFIG_DIR`. Note that
  `VIBERPIT_GH_CONFIG_DIR` today reaches the session env but gh does not read it; only `GH_CONFIG_DIR` counts.
- `allowAllUnixSockets` stays false. See §4.1.
- Whether the package registries (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`) join the list is an owner
  call (§4.4). Leave them out by default.

### 2.2 Delivery

- **At spawn:** the merged `--settings` (`spawn_settings`). This is the only path with a hard refusal.
- **Toggle ON mid-session: respawn**, using the existing resume path with the new `flag_settings`. Do not use live
  `apply_flag_settings`, because a live enable on a host without deps appears to fail open (§1.2).
  - If live enable is wanted later, it must be followed by the §2.4 check. A failed check must stop the session (hold
    it and respawn), never let it go on.
- **Toggle OFF mid-session:** a live `apply_flag_settings {"sandbox": {"enabled": false}}` is safe (probe S). Respawn
  also works.
- **Storage:** a per-session nullable column, as D-14 §2.4 item 5 laid out (plus a migration and the
  `_REQUIRED_EXTRAS`/reconcile lists). The session default is an `AppSettings` column, default off.
- **Engine gating:** Claude only. Add a capability flag; Codex's sandbox stays derived from the permission mode.

### 2.3 Host preflight (the daemon runs it, cached per host, shown to the edge)

1. `which bwrap` and `which socat`. Both are required. Also check `bwrap --version` ≥ 0.5.0.
2. `bwrap --ro-bind / / --dev /dev --unshare-net true` returns rc 0 within 5 s, run in the daemon's own context so that
   unit hardening counts.
3. Report the userns sysctls for diagnosis only. They are not the gate; the probe in step 2 is.
4. gh credential readiness for sandboxed git: either `gh auth status` reports a file-backed source, or a file-backed
   `GH_CONFIG_DIR` is configured for sessions. If neither, the toggle stays disabled with "gh token is keyring-only;
   sandboxed git cannot authenticate".
5. The CLI version is at least the verified floor (2.1.293).

If any check fails, the Environment card toggle is disabled with the first failure as the reason. Re-run on daemon start
and on demand.

### 2.4 "Sandbox active" check after spawn (and after any live write)

Send `get_sandbox_dialog` and require all of:

- `enabled == true` and `enabled_in_settings == true`;
- `unavailable_reason` absent;
- `dependencies.errors == []`;
- `dependencies.warnings` has no `seccomp not available` entry (otherwise unix sockets are open);
- `mode == "regular"`;
- `unsandboxed_fallback == false`;
- `restrictions.allow_all_unix_sockets == false`.

Store the result on the session row and show it in the Environment card ("Sandbox: active / failed: <reason>").

### 2.5 Failure behaviour

- **At spawn:** the `result` frame carries `startup_failure_reason == "sandbox_required_unavailable"` (and the process
  exits 1), or it says `❌ Sandbox Error`. The session ends in a failed state with the message shown. ViberPit **never**
  retries without the sandbox automatically.
- **After spawn:** if the dialog check fails, stop the turn and hold the session with the reason. Offer the user either
  "Retry" or "Turn off sandbox and continue". The second is an explicit human choice.
- **Mid-session loss:** the CLI itself refuses commands while `failIfUnavailable` is set (`Lw`). Surface the refusal text.

## 3. Open risks

1. **Mid-session enable fails open (code-read, not probed).** Before building any live-enable path, verify with a
   no-model probe (D-14 harness) on a host without socat.
2. **Git config writes are denied.** `git push -u`, `git config`, upstream tracking and `git worktree` admin fail inside
   the sandbox. Sessions need `git push origin HEAD:<branch>`, or `git push` as an `excludedCommands` entry. Excluded
   commands still get a card in interactive modes but **run unsandboxed and are auto-approved in full-auto**.
3. **Loopback is unreachable.** Anything that talks to host services fails inside the sandbox: the edge API at `:8787`,
   the Postgres test suite and dev-server screenshots.
4. **Writes outside the worktree are denied.** `~/.cache` (uv/pip), `~/.npm`, and `web/node_modules` symlinked into the
   deploy checkout cannot be written. gh's own state and update-check writes are untested; set
   `GH_NO_UPDATE_NOTIFIER=1`.
5. **No nested namespaces.** Playwright/Chromium, docker and nested bwrap fail inside.
6. **The registry knows nothing of `SandboxNetworkAccess`.** With `strictAllowlist:true` it is never sent. If the owner
   prefers prompting (§4.4), the registry must card it in every mode except an explicit allow rule, including `auto`
   and `full-auto`.
7. **Full-auto/automation sessions get no cards at all.** The sandbox is their only containment. Excluded commands
   become unsandboxed and silent.
8. **The CLI's sandbox surface is fast-moving.** `credentials`, `tlsTerminate` and the `get_sandbox_dialog` (`@internal`)
   change between releases. Per Principle I, gate the feature behind a kill switch (`VIBERPIT_SANDBOX_ENABLED`) and
   re-verify after each CLI bump. The verified versions are 2.1.293 and 2.1.319.
9. **The domain list is unverified.** The GitHub host list was not observed through the proxy. Verify with a real
   sandboxed `git push` / `gh pr create` in the implementation's first probe.
10. **seccomp status on rocky is unknown** until socat is installed and `get_sandbox_dialog` is read there.
11. **Sandbox merge semantics are unverified.** Whether `apply_flag_settings` merges or replaces the nested `sandbox`
    object is not known, so always send the full block.

## 4. Contradictions with D22a (owner to re-decide)

1. **"gh/git sockets allowed explicitly" cannot be built on Linux.**
   - `network.allowUnixSockets` is macOS-only ("Ignored on Linux (seccomp cannot filter by path)"). The only Linux knob
     is `allowAllUnixSockets`.
   - On both hosts that knob is root-equivalent (docker group) and an escape (D-Bus → `systemd-run --user`).
   - **Proposed replacement:** no sockets. Give gh a file-backed token via `GH_CONFIG_DIR`, which is already the case on
     rocky and which #2118 / `docs/runbooks/gh-credential-without-a-keyring.md` provision on dobby. Then allow only the
     GitHub domains.
   - Alternative: run `gh` and `git push/fetch/pull` as `excludedCommands`, which means unsandboxed and, in full-auto,
     unprompted.
2. **"Permission cards still appear" holds only in interactive-type modes.** Turning off auto-allow restores ViberPit's
   normal Bash gating. It cannot add cards to `full-auto` or `bypassPermissions`, which approve everything; automation
   runs full-auto. The owner should confirm that the sandbox in those modes is containment-only. That confirmation is
   also why `allowUnsandboxedCommands:false` and `strictAllowlist:true` are recommended rather than optional.
3. **"Offered only where preflight passes" means the toggle is disabled on rocky today.** socat is missing there. The
   owner must either `apt install socat` on rocky (needs sudo) or accept a dobby-only feature for now.
4. **The per-session toggle should respawn the session, not switch live.** D22a does not say which. The respawn costs a
   resume (and a prompt-cache miss), but it is the only path that refuses when the sandbox cannot start.
5. **Not in D22a, but needed:** the network allowlist policy. Choose `strictAllowlist:true` with GitHub only
   (recommended), or GitHub plus the package registries, or prompting (which needs the registry work in risk 6).
