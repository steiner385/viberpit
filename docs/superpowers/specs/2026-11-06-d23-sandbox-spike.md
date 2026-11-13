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

## 5. Live verification (2026-11-07)

Task 3 of the plan: one real sandboxed session per host, through ViberPit's own spawn path. Each was run outside
the live daemon and edge.

### 5.1 Versions and method

| Host | CLI (`~/.local/bin/claude --version`) | Kernel | bwrap / socat | AppArmor on bwrap's children |
|---|---|---|---|---|
| dobby | **2.1.319** | 7.0.0-42 | 0.11.1 / 1.8.1.1 | `bwrap//&unpriv_bwrap (enforce)` |
| rocky | **2.1.293** | 7.0.0-38 | 0.11.1 / **now installed** | `bwrap//&unpriv_bwrap (enforce)` |

- Python SDK `claude-agent-sdk==0.2.105`. Model: haiku. Mode: `full-auto`.
- **Harness.** A standalone script ran a real `SessionOrchestrator`:
  - the real `real_sdk_factory`, `_session_factory` closure, PreToolUse, PostToolUse and PostToolUseFailure hooks,
    `SandboxGate`, and `get_sandbox_dialog` check (`ensure_driver` on a row with `sandbox=True`);
  - a scratch SQLite DB and a scratch `VIBERPIT_HOME`;
  - a scratch `CLAUDE_CONFIG_DIR`: a copy of one account's `.claude.json` without `mcpServers`, plus the shared
    `settings.json` symlink. Auth was the account's setup token in `CLAUDE_CODE_OAUTH_TOKEN`, so no refresh token
    was copied or rotated;
  - a shallow throwaway clone of `steiner385/viberpit`, with a linked worktree laid out as ViberPit lays one out.
    Both were under `~/.cache/d23probe`, so the home read-deny and its re-allows applied as they do for real
    clones;
  - the daemon's `PATH` and `VIBERPIT_GH_CONFIG_DIR`.
- Turns were driven through `driver.open_stream()` / `send_prompt()`. Each prompt asked the model to run one command
  verbatim.
- **Probe-only variants.** These were harness monkeypatches or `PATH` shims, never product code. They are marked in
  the table:
  - **(U)** an unconfined copy of `/usr/bin/bwrap` first on `PATH`, standing in for a host where finding F4 is
    resolved;
  - **(S)** the CLI's `TMPDIR` pointed outside home, standing in for a fix to finding F5;
  - **(N)** `allowNestedUserNamespaces: true`.
- **Clean-up.** The scratch branch `sandbox-probe/20261107103739` was deleted from an unsandboxed shell (`git
  ls-remote` shows no `sandbox-probe/*` ref; the branches API answers 404). Every scratch dir was removed on both
  hosts.

### 5.2 Results

| # | Check | Expected | Observed | Result |
|---|---|---|---|---|
| 1 | `get_sandbox_dialog` echo, dobby | The block's lists back exactly: `fs_deny_read` / `fs_allow_read` absolute, no `~`, no trailing slash, no extras; `fs_deny_write` = `<common>/hooks`, `<common>/config`; `fs_allow_write` `[]`; `excluded_commands` `[]`; the three GitHub domains; `allow_all_unix_sockets:false`; `mode:"regular"`; `unsandboxed_fallback:false` | Exactly that, so `check_dialog` passed and the state was `active`. The shared settings' `Edit(/home/tony/.claude-accounts/cfg/*/projects/*/memory/**)` allow rule appears under `restrictions.ignored_glob_patterns` (Linux takes no globs), **not** in `fs_allow_write`. `additionalDirectories` does not appear | PASS |
| 2 | Same, rocky | Same | Same on 2.1.293. The ambient file-backed `~/.config/gh` is the gh re-allow | PASS |
| 3 | `dependencies.warnings` | `[]` (seccomp helper present) | `[]` on both hosts; `errors` `[]` | PASS |
| 4 | Sandboxed Bash runs at all | It runs | Two failures. **Before fix 811db328:** `bwrap: Can't create file at <root>/claude-1000/claude-settings-….json: Read-only file system` (F1). **After the fix, with stock bwrap, on both hosts:** `apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted; caller must provide CAP_SYS_ADMIN): Permission denied` (F4). (N) gives the same error | **FAIL** (F4: owner/ops) |
| 5 | Reads (U) | Denied | `ls ~` shows only the stand-ins `.cache .cargo .config .gitconfig .rustup`. `~/.ssh`, `~/.claude-accounts`, `~/.claude`, `~/.config/gh` (dobby) and `~/.cache/viberpit/claude-tmp` do not exist. `/etc/viberpit` is empty. `~/.ssh/id_*`, `~/.aws/credentials` and `~/.claude-accounts/accounts.json` cannot be read | PASS (U) |
| 6 | Re-allows of absent paths (U) | bwrap still starts | `~/.nvm` and `~/.pyenv` are absent on dobby; bwrap started | PASS (U) |
| 7 | Shell snapshot and `PATH` (U) | Snapshot sources, `PATH` intact | `PATH` is the daemon's, from the snapshot | PASS (U) |
| 8 | `uv`, `node`, `cargo` (U) | All run | `node` v22 ran. `~/.cargo/bin/cargo` ran (cargo is not on the daemon's `PATH`). **`uv` not found** (F6) | **FAIL** (F6: owner/ops) |
| 9 | `git fetch` | Works | Stock: F4. (U): `Proxy CONNECT aborted` (F5). (U+S): `* branch master -> FETCH_HEAD`, rc 0 | **FAIL** (F4, F5); PASS (U+S) |
| 10 | `git commit` (U) | Works, writing into the common git dir | `commit_ok a0dc947` | PASS (U) |
| 11 | `git push origin HEAD:sandbox-probe/<ts>` | Works | (U+S): `* [new branch] HEAD -> sandbox-probe/20261107103739`, rc 0; `ls-remote` showed the ref | PASS (U+S) |
| 12 | `gh pr view` / `gh api user` | Authenticate | (U+S): `3366 MERGED`; `steiner385` (dobby, via the dedicated `GH_CONFIG_DIR`) | PASS (U+S) |
| 13 | `git push -u`, `git config`, hooks | Push works; `.git/config` writes fail (spike §1.6) | (U+S): the push went through ("Everything up-to-date") but no upstream was recorded. `git config probe.x 1` failed with `could not lock config file …/.git/config: File exists` (rc 255). Touching `<common>/hooks/probe` failed with `Read-only file system` | PASS (U+S): as predicted |
| 14 | Network allowlist | `api.github.com` allowed, `example.com` denied | (U+S) with `curl`: `api_http=200`; `example.com` got `CONNECT tunnel failed, response 403` plus `<sandbox_violations> deny network-outbound example.com:443 (host is not on the allow list)` | PASS (U+S) |
| 15 | WebFetch `api.github.com` / `example.com` | Allowed / denied | **Not observable.** The CLI does not offer WebFetch to this account at all: the init `tools` list lacks it for haiku and sonnet, with and without the sandbox (F7). The model's `ToolSearch` was denied until fix 811db328 (F3) | NOT RUN (F7) |
| 16 | `Write /tmp/x`, main thread | Denied | `PreToolUse:Write hook error: Write is not allowed in a sandboxed session: '/tmp/d23probe-x.txt' is outside this session's worktree`. The file never appeared on the host | PASS |
| 17 | `Write /tmp/x`, sub-agent | Denied (the hook sees sub-agent calls) | The same denial, inside the sub-agent | PASS |
| 18 | Serialisation: Edit during a sub-agent's `sleep 20` (U) | The retry message | `PreToolUse:Edit hook error: Another command is running in this session; retry this edit when it finishes.` The same Edit was allowed after the sub-agent ended | PASS (U) |
| 19 | Write during `run_in_background` (U) | Refused until the task's end event | The same retry message. The gate held the task in `background` and cleared it at `BackgroundTaskEnded` | PASS (U) |
| 20 | A failed Bash leaves no stale gate entry | Gate empty afterwards | With stock bwrap, sub-agent and background Bash calls failed (F4); the gate was empty after each, and later writes were admitted | PASS |
| 21 | Bash refused while an Edit waits on a card | Refused | The probe ran `full-auto`, which has no cards | NOT RUN |
| 22 | Backgrounded process gone after return (U) | None left | CLI 2.1.319 itself blocks `sleep 300 & …` ("Blocked: sleep 300 followed by …"), so `timeout 300 tail -f /dev/null &` was used: `started_pid=3` (its own PID namespace); `pgrep` on the host found none | PASS (U) |
| 23 | `TMPDIR` is the per-session root (U) | `…/viberpit-sbx/<id>/…` | **Before fix 811db328:** the **shared** `~/.cache/claude-tmp/claude-1000`, writable, listing other sessions' project dirs (F2). **After:** `…/viberpit-sbx/<id>/claude-1000`; `mktemp` works; the shared dir does not exist inside | PASS after fix (U) |
| 24 | Per-session root removed at close | Gone | 0700 while the session ran; gone after `close_session`, in every run | PASS |
| 25 | `~/.cache/viberpit/claude-tmp` denied (U) | Not readable | Does not exist inside | PASS (U) |
| 26 | Shared venv (U) | Known gap | `~/GitHub/viberpit-deploy/.venv/bin/python: No such file or directory` | As expected (known limitation) |
| 27 | Reload releases a gate whose end event was lost | Released | Needs a lost event, which the probe cannot produce | NOT RUN |
| 28 | Rocky: sandboxed `git fetch` | Works through `~/.config/gh` | Stock: F4. (U) cannot run on rocky: an unconfined bwrap fails with `loopback: Failed RTM_NEWADDR`, because `apparmor_restrict_unprivileged_userns=1` | **FAIL** (F4) |

### 5.3 Findings

Fixed in **811db328** `fix(sessions): sandbox temp root and ToolSearch, from the live probe (D-23)`, with tests:

- **F1. The temp-root re-allow broke every Bash.**
  - The CLI's writable temp dir is `$m()` = `realpath(<CLAUDE_CODE_TMPDIR>/claude-<uid>)` (@287555265).
  - The bwrap builder `H8` (@~288403900) restores write paths with `--bind` over a denyRead tmpfs. It then
    `--ro-bind`s each `allowRead` path that is not inside a restored write path.
  - Re-allowing the root, the write path's parent, mounted it read-only on top.
  - The root is no longer re-allowed. The restored write path is readable as it is.
- **F2. The shell's `TMPDIR` was a shared, writable directory.**
  - The child-process tmpdir `hAe()` / `j$e()` (@282251389) is `<CLAUDE_CODE_TMPDIR>/claude-<uid>` only when that
    is at most 44 bytes (`hNr`). Otherwise it is `<os.tmpdir()>/claude-<uid>`.
  - The per-session root is always longer. The shared `settings.json` sets `env.TMPDIR=~/.cache/claude-tmp`.
  - So the sandbox's default write list, and the shell's `TMPDIR`, included the host-wide directory every session's
    CLI uses.
  - The sandboxed spawn's flag layer now sets `env.TMPDIR` to the session's root. The flag layer outranks user
    settings.
- **F3. `ToolSearch` was denied.** 2.1.319 defers tools behind it, and the model called it before WebFetch. It only
  loads schemas, and a tool it loads is still confined when called.

Need an owner decision (not changed):

- **F4. The sandbox cannot run any command on either host (blocker).**
  - Ubuntu's `bwrap-userns-restrict` AppArmor profile puts every process bwrap starts in `unpriv_bwrap`, which has
    `audit deny capability`.
  - The CLI's `apply-seccomp` helper (the unix-socket filter) needs a nested user, PID and mount namespace, which
    needs capabilities. So it fails at `/proc/self/setgroups`.
  - Proof: the same `bwrap --unshare-user --cap-drop ALL … unshare -U -r -p -m --fork --mount-proc true` fails
    under `/usr/bin/bwrap` and succeeds under an unconfined copy.
  - `allowNestedUserNamespaces:true` (N) does not help. `allowAllUnixSockets:true`, which skips the helper, is the
    escape §4.1 rules out.
  - The state is fail-closed but useless:
    - the dialog says `enabled:true`, so the session verifies `active`;
    - every Bash then fails; nothing runs unsandboxed.
  - **The host preflight passes regardless.** Its `bwrap --unshare-net true` probe does not exercise the helper.
  - Options:
    - a local AppArmor rule granting `unpriv_bwrap` the capabilities this needs. That is host-wide: it also affects
      glycin and flatpak bwrap users;
    - a dedicated profile for the CLI's helper;
    - an upstream CLI fix.
  - Whichever is chosen, extend the preflight with a probe that mirrors the helper (the nested `unshare -U -r -p -m`
    inside `bwrap --unshare-user --cap-drop ALL`), so the toggle is disabled until it passes.
- **F5. The proxy bridge sockets are hidden by the home read-deny.**
  - The CLI creates `claude-http-<hex>.sock` / `claude-socks-<hex>.sock` in `os.tmpdir()` (sandbox runtime
    `a1()`).
  - It binds them into bwrap (`--bind h h`, @288417101) **before** the filesystem arguments. So the later
    `--tmpfs /home/tony` covers them whenever `os.tmpdir()` is under home, as it is both before and after F2's fix.
  - Every connection then fails with `Proxy CONNECT aborted`.
  - Re-allowing the directory that holds them would recreate F1, because it is the write path's parent.
  - Options:
    - **(a)** `TMPDIR` in a per-session dir outside home, such as `/tmp/vpsbx-<id>`. This works (it is how row 9
      (U+S) passed), but other sandboxed sessions' dirs there are readable.
    - **(b)** A `CLAUDE_CODE_TMPDIR` of at most 32 bytes, so `hAe()` never falls back, plus a separate per-session
      socket dir that is re-allowed and is not an ancestor of a write path. This is home-length-dependent and needs
      a length guard that fails closed.
    - **(c)** An upstream fix to the bind order.
- **F6. `~/.local` and `~/.npm` are symlinks on dobby** (to `/mnt/ssk-ssd/tony/cache/…`).
  - `H8` refuses to restore a re-allow whose real path leaves the denied region ("resolves outside it").
  - So `~/.local/bin` (uv, and the user's other tools) is missing from the sandboxed shell, though the real path is
    readable.
  - Options: re-allow by real path and put it on the sandboxed `PATH`, or change the host layout.
- **F7. WebFetch is not offered to this account by the CLI at all.** It is absent from the init `tools` for haiku and
  sonnet, sandboxed or not. The WebFetch half of the allowlist is unit-tested only (`test_the_allowlist_matrix_*`).
  The proxy half is verified by row 14.

Notes:

- **rocky push.** rocky's git credential helper is `store` (`~/.git-credentials`, under the home deny, not
  re-allowed). Its clones use the global helper. A sandboxed push on rocky would not authenticate. This was inferred
  from config, not observed, because of F4. The repo is public, so fetch needs no credentials.
- **Host-wide `/tmp` is readable.** That includes `/tmp/claude-1000` (unsandboxed CLIs' `cache-break-state-*`
  files). Reads outside home and the daemon dirs are allowed by design (§1.6); writes there are refused (`/tmp` is
  read-only inside).
- **`.gitmodules` is masked.** The CLI masks a worktree's `.gitmodules` and creates other deny placeholders (`.bashrc`,
  `.mcp.json`, `.claude/commands`, …). Inside, git warns `unable to access …/.gitmodules: Permission denied`. The
  placeholders are not left on the host worktree.
- **Model refusals.** haiku refused the read and escape probes as evasive until each prompt said the run was the
  owner's authorised verification. This is not a product issue.
- **Benchmark row.** "Sandbox settings" should not become Y until F4 and F5 are resolved.

## 6. Enablement design (#3367)

The design that clears F4–F6 and the rocky push is in
`docs/superpowers/plans/2026-11-13-sandbox-enablement-3367.md`. The owner approved option A on 2026-11-13.

- **F4 (host).** A root-owned bwrap copy, `/usr/libexec/viberpit/bwrap` (a root-owned directory chain), with its own AppArmor profile
  `viberpit-sbx-bwrap`, selected by `sandbox.bwrapPath` in `/etc/claude-code/managed-settings.json` and kept
  in step by an apt hook. The files and install script are in `deploy/sandbox/`, and the steps and rollback are
  in `deploy/README.md` § "Claude session sandbox".
- **F5 (code).** The per-session temp root moves to `VIBERPIT_SANDBOX_TMP_PARENT` (default `/var/tmp/vpsbx`)
  as `<parent>/<sha256(id)[:12]>/{t,s}`: `t/` is `CLAUDE_CODE_TMPDIR`, and `s/` is `TMPDIR`, where the proxy
  sockets go. The parent is read-denied and only the session's own `s/` is re-allowed. Path lengths are
  checked against 44 and 107 bytes.
- **F6 (code).** The real path of every home symlink that leaves home is read-denied (this closes a leak:
  `~/.docker` → `/mnt/ssk-ssd/...`). The real paths of toolchain re-allows are re-allowed, and the sandboxed
  `PATH` uses them.
- **Rocky push (code).** `GIT_CONFIG_*` env makes gh git's only github.com credential helper.
- **Preflight (code).** It runs the installed CLI's real `apply-seccomp` helper through the configured bwrap,
  replacing the `unshare` stand-in. A new `bwrap_copy` check compares the copy's sha256 with `/usr/bin/bwrap`.
  The cached verdict re-runs when the CLI binary changes.

The benchmark row stays ❌ until the live re-run (plan §5) is recorded here. It is §7.

## 7. Live re-verification (2026-11-13)

The plan §5 re-run, after the host install (bwrap copy `/usr/libexec/viberpit/bwrap`, AppArmor profile
`viberpit-sbx-bwrap`, `/etc/claude-code/managed-settings.json` with `bwrapPath`, apt hook) on both hosts and with
PR #3383 merged. `VIBERPIT_SANDBOX_ENABLED` stayed off in the live services; it was set in the harness env only.

### 7.1 Versions and method

| Host | CLI | Kernel | bwrap / socat | `apparmor_restrict_unprivileged_userns` | Python (harness venv) |
|---|---|---|---|---|---|
| dobby | **2.1.323** | 7.0.0-42 | 0.11.1 (the copy) / present | 0 | 3.14.4 (`viberpit-deploy/.venv`) |
| rocky | **2.1.293** | 7.0.0-38 | 0.11.1 (the copy) / present | **1** | 3.14.4 (`viberpit/.venv`) |

- `claude-agent-sdk==0.2.105`. Model: haiku. Code: `origin/master` 356f6fe7 (#3383), then this branch's fix (§7.3 F8).
- **Harness.** The §5.1 shape, with **no stand-ins**: a real `SessionOrchestrator` with the real
  `real_sdk_factory`, `ensure_driver` on a scratch row with `sandbox=True`, so the driver's own
  `bwrap_copy_spawn_problem`, `prepare_session_tmp`, `get_sandbox_dialog` check, gate and hooks all ran.
  - Scratch SQLite DB and `VIBERPIT_HOME`; a scratch `CLAUDE_CONFIG_DIR` (0700): one account's `.claude.json`
    without `mcpServers`, plus that host's `settings.json` symlink. Auth: the account's setup token in
    `CLAUDE_CODE_OAUTH_TOKEN` (`compose`, `trust_project` and `oauth_env_for` patched to the scratch dir and
    token).
  - A shallow throwaway clone with a linked worktree, under `~/.cache/d23probe` (so the home deny applies as for
    real clones).
  - The daemon's `PATH` on each host. dobby: `VIBERPIT_GH_CONFIG_DIR=/home/tony/.config/viberpit/gh`; rocky:
    the ambient file-backed `~/.config/gh`, and `VIBERPIT_CLAUDE_BIN` as its daemon sets it.
  - Most shell rows ran from one probe script in the worktree (`bash .d23probe.sh`). It prints names, counts and
    status codes only (`head -c0` for readability, `env | cut -d= -f1` for env).
- **Probe-only variants**, marked in the table:
  - **(C)** row 21: a copy of the shared `settings.json` without the `Edit`/`Write`/`MultiEdit`/`NotebookEdit`
    allow rules. The shared settings allow them, so with the real file no card is ever shown for a file write.
  - **(L)** row 27: `SandboxGate.background_ended` replaced by a no-op for that driver, to simulate the lost end
    event (plan §5.3).
- **Clean-up.** `sandbox-probe/20261113113554` (dobby) and `sandbox-probe/20261113114920` (rocky) were deleted
  from an unsandboxed shell (`gh api -X DELETE …/git/refs/heads/…`). `git ls-remote` shows no
  `sandbox-probe/*` ref, and the branches API answers 404. Every scratch dir (both hosts) and the staged token
  file on rocky were removed, and `/var/tmp/vpsbx` holds only its marker on both hosts.

### 7.2 Results

Rows 1–28 are §5.2's, on dobby unless the row says otherwise; rocky ran the same probe script (rows 1–14, 22–26,
28). N1–N9 are the PR #3383 list and this run's extra checks.

| # | Check | Expected | Observed | Result |
|---|---|---|---|---|
| 1 | `get_sandbox_dialog` echo, dobby | The block's lists back exactly | `check_dialog` ok; `fs_deny_read` **equal** (same order) to `read_policy`, `fs_allow_read` the same set. Deny: home, the eight daemon dirs, the scratch `VIBERPIT_HOME`, `/var/tmp/vpsbx` and the 12 `home_escapes` targets (`/mnt/ssk-ssd/tony/cache/{.claude,.docker,.local,.npm,.wine*}`, `.steam`, `.verdaccio`, two `/mnt/hdd-storage` dirs). Allow: worktree, common `.git`, `shell-snapshots`, the dedicated gh dir, the toolchain list with the real `/mnt/ssk-ssd/tony/cache/.local/{bin,share/uv}` and `…/.npm` beside the lexical ones, and the session's own `/var/tmp/vpsbx/<key>/s`. `fs_allow_write` `[]`; `fs_deny_write` = `<common>/hooks`, `<common>/config`; the three GitHub domains; `allow_all_unix_sockets:false`; `mode:"regular"`; `unsandboxed_fallback:false`; `policy_origins:["file"]`. State `active` | PASS |
| 2 | Same, rocky | Same | Same on 2.1.293; `home_escapes` 0, so no `/mnt` entries; ambient `~/.config/gh` is the gh re-allow | PASS |
| 3 | `dependencies` | `errors` `[]`, `warnings` `[]` | `[]` / `[]` on both hosts (the seccomp helper is present and usable) | PASS |
| 4 | Sandboxed Bash runs at all | It runs | Every Bash ran on both hosts (F4 is gone): `bash_ok uid=1000` | PASS |
| 5 | Reads | Denied | `ls -A ~` shows only the re-allow stubs (dobby `.cache .cargo .config .gitconfig .rustup`; rocky `.cache .config .gitconfig .local .npm`). `~/.ssh`, `~/.claude-accounts`, `~/.claude`, `~/.aws`, `~/.cache/viberpit` absent; `/etc/viberpit`, `/etc/ccswitch`, `/var/lib/viberpit` empty. `~/.ssh/id_*`, `~/.aws/credentials`, `accounts.json`, `~/.git-credentials`, `/etc/viberpit/viberpit.env` unreadable. dobby: ambient `~/.config/gh` absent; only the dedicated `~/.config/viberpit/gh` is visible | PASS |
| 6 | Re-allows of absent paths | bwrap still starts | `~/.nvm`, `~/.pyenv` absent on both hosts; bwrap started | PASS |
| 7 | Shell snapshot and `PATH` | Snapshot sources, `PATH` intact | dobby `PATH` = the daemon's + `/mnt/ssk-ssd/tony/cache/.local/bin:/home/tony/.cargo/bin` (`sandbox_path`, real path); rocky = the daemon's + `~/.local/bin` | PASS |
| 8 | `uv`, `node`, `cargo` | All run | dobby: `uv 0.9.26` (from the real path), node v22.23.4, cargo 1.98.0, gh 2.103.0. rocky: uv, node, gh run; cargo is not installed on rocky (no `~/.cargo`) | PASS (F6 fixed) |
| 9 | `git fetch` | Works | `* branch master -> FETCH_HEAD`, rc 0, both hosts | PASS |
| 10 | `git commit` | Works, into the common git dir | `commit_ok 2c3b8e7` (dobby), `21d4438` and `376feec` (rocky) | PASS |
| 11 | `git push origin HEAD:sandbox-probe/<ts>` | Works | dobby: `* [new branch] HEAD -> sandbox-probe/20261113113554`, rc 0. rocky: see N5 | PASS |
| 12 | `gh api user` / `gh pr view` | Authenticate | `steiner385` / `3383 MERGED` on both hosts | PASS |
| 13 | `git push -u`, `git config`, hooks | Push works; `.git/config` writes fail | Push "Everything up-to-date" and a `--set-upstream-to` hint (no upstream written); `git config probe.x 1` → `could not lock config file …/.git/config: File exists`, rc 255; touching `<common>/hooks/probe` → `Read-only file system` | PASS (as predicted) |
| 14 | Network allowlist | `api.github.com` allowed, `example.com` denied | `api_http=200`; `example.com` → `CONNECT tunnel failed, response 403` and `<sandbox_violations> deny network-outbound example.com:443 (host is not on the allow list)`; both hosts | PASS |
| 15 | WebFetch `api.github.com` / `example.com` | Allowed / denied | **Not offered (F7 stands on 2.1.323).** Unsandboxed `claude -p` init `tools` lacks `WebFetch` for haiku, sonnet and opus (24 tools each; `WebSearch` and `ToolSearch` present), and `ToolSearch` `select:WebFetch` answers "No matching deferred tools found". Per plan §5.1.3 the confine path stays unit-tested (`test_the_allowlist_matrix_*`); egress is proven by row 14 | N/A (F7) |
| 16 | `Write /tmp/x`, main thread | Denied | `PreToolUse:Write hook error: Write is not allowed in a sandboxed session: '/tmp/d23probe-x.txt' is outside this session's worktree`; not on the host | PASS |
| 17 | `Write /tmp/x`, sub-agent | Denied | The same denial inside a foreground sub-agent; not on the host. (In the first conversation haiku refused to delegate a write it had just seen denied; a fresh conversation was used) | PASS |
| 18 | Edit while a sub-agent's Bash runs | The retry message | A background sub-agent's **foreground** Bash (`python3 … time.sleep(45)`; 2.1.323 still blocks `sleep N; echo`) was in `gate.bash` when the main thread's Edit arrived: `PreToolUse:Edit hook error: Another command is running in this session; retry this edit when it finishes.` The same refusal held while a sub-agent's `run_in_background` Bash was in `gate.background` | PASS |
| 19 | Write during `run_in_background` | Refused until the end event | Refused with the same message; the entry stayed in `background` across the turn end and was cleared at `BackgroundTaskEnded`; the next Write was admitted (file created) | PASS |
| 20 | A failed Bash leaves no stale entry | Gate empty afterwards | `ls /nonexistent; exit 3` → `Exit code 3`; gate `{bash:[], writes:[], background:[]}` after it and after every other turn | PASS |
| 21 | Bash refused while an Edit waits on a card | Refused | (C) Interactive mode. The main thread's Write was admitted and its card held 60 s (`gate.writes` = that call). A background sub-agent's Bash during the hold: `PreToolUse:Bash hook error: A file edit is being written in this session; retry this command when it finishes.`, twice (its retry too). Card approved → Write ran; gate empty at turn end. Note: the CLI does not start a message's other tool calls until a pending card in that message resolves, so the race only arises across threads | PASS (C) |
| 22 | Backgrounded process gone after return | None left | `timeout 300 tail -f /dev/null &` → `started_pid=237` (own PID namespace); host `pgrep -af 'tail -f /dev/null'` empty after the turn, both hosts | PASS |
| 23 | `TMPDIR` is the session's own | `/var/tmp/vpsbx/<key>/t/claude-1000` | `TMPDIR` = `CLAUDE_CODE_TMPDIR` = `/var/tmp/vpsbx/448608251ab5/t/claude-1000` (key = `sha256(id)[:12]`); `mktemp` works there | PASS |
| 24 | Temp root removed at close | Gone | 0700 while running; gone after `close_session` in every run (11 sessions, both hosts) | PASS |
| 25 | `~/.cache/viberpit/claude-tmp` denied | Not readable | Absent inside, as is the shared `~/.cache/claude-tmp` | PASS |
| 26 | Shared venv | Known gap | `~/GitHub/viberpit{,-deploy}/.venv/bin/python`: No such file or directory | As expected (known limit) |
| 27 | Reload releases a gate whose end event was lost | Released | (L) Background `timeout 600 tail -f /dev/null`; `TaskStop` → `BackgroundTaskEnded` arrived but was dropped, so `gate.background` kept the id; the host had no `tail` left. Write → `Another command is running…` (fail closed). `reload_session` → `reloaded`, new driver, gate empty and `known`, state `active`; the root was recreated with a matching `.sid`, and its sockets are new (the old CLI's are gone). Write → created | PASS (L) |
| 28 | Rocky: sandboxed `git fetch` | Works through gh | `* branch master -> FETCH_HEAD`, rc 0, under `apparmor_restrict_unprivileged_userns=1` | PASS |
| N1 | Real seccomp helper through the copy | Preflight `seccomp_helper` ok on both hosts | `run_preflight()` on both hosts: all ten checks ok (`bwrap` = `/usr/libexec/viberpit/bwrap`, `bwrap_copy`, `seccomp_helper`, `proxy_socket_dir`, `sandbox_tmp_parent`, `gh`, …). The dialog's empty `warnings` and row 4 show the helper running for real | PASS |
| N2 | Exact read lists in the echo | Equal to `read_policy` | Rows 1–2: `deny_exact` and `allow_exact` true on both hosts | PASS |
| N3 | F5 inside the sandbox | Own key only; sibling invisible; `s/` read-only; `t/` writable; proxy works | `ls /var/tmp/vpsbx` shows only `448608251ab5`. A sibling root (`prepare_session_tmp("d23-sibling-probe")`, with a file in `t/`) → `No such file or directory`. `s/` holds `claude-http-<16>.sock` and `srt-mux-<pid>-0.sock` (2.1.323 names); `touch s/x` → `Read-only file system`; `touch t/x` ok. The proxy works (rows 9, 11, 14) | PASS |
| N4 | F6 | `.docker` hidden; `uv` runs; PATH survives the snapshot | `/mnt/ssk-ssd/tony/cache/.docker` is an empty tmpfs inside (0 entries; `config.json` unreadable), as is `…/.claude`. `uv --version` runs; row 7 PATH | PASS |
| N5 | Push from rocky through gh | Works without `~/.git-credentials` | `* [new branch] HEAD -> sandbox-probe/20261113114920`, and after the F8 fix `21d4438..376feec`; `~/.git-credentials` unreadable inside, so gh (the `GIT_CONFIG_*` helper) authenticated | PASS |
| N6 | A CLI update re-runs the preflight | Fresh run on a new binary | `VIBERPIT_CLAUDE_BIN` → a symlink to `versions/2.1.323`: `preflight()` ran once, then returned the cached object. Repointed to `versions/2.1.319` (an auto-update's shape): the next `preflight()` re-ran (`cli_version` 2.1.319, newer `checked_at`, `seccomp_helper` ok through 2.1.319's helper) | PASS |
| N7 | Unsandboxed `claude -p` with the managed file present | As before | With `/etc/claude-code/managed-settings.json` (`bwrapPath` only): rc 0, `~/.ssh` visible, `accounts.json` readable, `TMPDIR=~/.cache/claude-tmp` (the shared settings), `SANDBOX_RUNTIME` and `HTTPS_PROXY` unset, `/tmp` writable: unsandboxed | PASS |
| N8 | Startup sweep removes leftover roots | Leaked roots go; nothing else | With no live sandboxed session: two `prepare_session_tmp` roots, a planted symlink to `~`, the real leftover sibling root, a key-shaped dir without `.sid` and a non-key dir. `sweep_session_tmp(())` removed 4 (the three `.sid` roots and the symlink, not followed: `~` intact); the two foreign dirs stayed. `serve.py` calls it at daemon start, gated on the kill switch, after the orphan-CLI reap and before `rehydrate()` | PASS |
| N9 | The daemon's env in the sandboxed shell | No daemon secrets | **Before the fix:** the shell's env listed the daemon namespace, including the master key under its legacy-prefix name (the harness had unset the canonical one; `env_compat` had mirrored it). The live daemons carry `VIBERPIT_MASTER_KEY` and `VIBERPIT_DB_URL` (both hosts), `VIBERPIT_GH_PROJECT_TOKEN` (dobby) and more. **After F8:** every `VIBERPIT_*` / legacy-prefix variable is present but **empty** on both hosts, and git, gh and push still work. `CLAUDE_CODE_OAUTH_TOKEN` was never visible (the CLI withholds it) | FAIL → PASS after F8 |

### 7.3 Findings

**F8 (fixed on `fix/sandbox-live-3367`): the daemon's env reached the sandboxed shell.**

- `real_sdk_factory` builds the CLI's env from `os.environ`, and the sandboxed shell inherits the CLI's env.
- So every secret the daemon holds in its env was readable to a sandboxed session (row N9). The secrets are
  `VIBERPIT_MASTER_KEY` (which decrypts every stored account token), `VIBERPIT_DB_URL` with its password, and
  `VIBERPIT_GH_PROJECT_TOKEN`. GitHub egress is allowed, so they could leave the host.
- `read_policy` hides the files those secrets live in, but env is a separate channel.
- A first fix that left the namespace out of `options.env` had **no effect**. claude-agent-sdk 0.2.105
  (`SubprocessCLITransport.connect`) builds the child env as `{**os.environ, …, **options.env}`, so only an
  override removes a value.
- The fix: `sandbox.withheld_env()` maps every key of both spellings of the namespace to `""`.
  `real_sdk_factory(sandboxed=True)` applies it, and the orchestrator passes `sandboxed=True` for a sandboxed spawn.
- The names stay visible with empty values.
- The tests assert on the env the SDK's own transport would spawn the CLI with, and they fail against the first
  attempt.
- **Not changed (owner):** unsandboxed sessions still inherit the full daemon env, master key included. That
  predates D-23 and needs its own issue: does any session need any `VIBERPIT_*` value?

**F9 (owner decision): `git add -A` fails inside the sandbox.**

- The CLI masks a set of dangerous paths in the worktree when they do not exist. On 2.1.323 these are
  `.bashrc`, `.bash_profile`, `.profile`, `.zshrc`, `.zprofile`, `.gitconfig`, `.gitmodules`, `.ripgreprc`,
  `.mcp.json`, `.idea`, `.vscode`, `.claude/commands` and other `.claude/*` entries.
- It masks each one with a `/dev/null` character device bound in place. These do not exist on the host.
- So `git status` lists them as untracked, and `git add -A` (or `git add .`) fails with
  `error: .bash_profile: can only add regular files, symbolic links or git-directories` and
  `fatal: adding files failed` (rc 128).
- `git add <path>` and `git commit -a` work. A model sees the error and adapts, but agent workflows that stage
  everything will fail on the first try.
- Options:
  - **(a)** For sandboxed sessions, add the CLI's mask list to git's excludes through the existing `GIT_CONFIG_*`
    env, for example a `core.excludesFile` in the session's `s/` that also includes the user's global ignore.
    The list is CLI-internal and can change between releases (Principle I).
  - **(b)** Say it in the sandboxed session's prompt ("stage paths explicitly").
  - **(c)** Accept it.
- Not changed here.

**F7 stands** on 2.1.323 (row 15). WebFetch is not even deferred.

**Ordering note (row 21).** In interactive mode, a message's other tool calls wait until a pending card resolves.
So the Bash/Edit race only happens across threads, and the gate handles it.

**Ready to switch on?** Every §5 row except row 15 (N/A, F7) passes on both hosts with no stand-ins, and all
eight items of the PR #3383 list pass. With F8 merged and deployed, the remaining step is plan §6 step 6, flipping
the switch, subject to the owner's call on F9. The benchmark row (plan §6 step 7) can then change.
