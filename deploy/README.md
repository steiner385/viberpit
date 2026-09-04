# Deploy — ccswitch web (self-hosted)

The server runs as a **systemd service** on the Tailscale host.

## Components

| Unit | Role |
|------|------|
| `ccswitch.service` | Long-running uvicorn server (`server.app:app`), serves the API + built `web/dist`. |
| `ccswitch-daemon.service` | Session daemon (`python -m server.ipc`) — owns live `claude` PTY sessions in remote mode. |

## Deploy (Plan 1: manual)

Deploys are no longer automatic. Run one on demand:

    PYTHONPATH=<repo>/src <repo>/.venv/bin/python -m server.deploy --once

This gates on GitHub CI being green, fast-forwards `origin/master`, syncs deps only
when lockfiles changed, rebuilds `web/dist`, restarts the daemon (if daemon code
changed) then the edge, health-gates on the deployed SHA, and rolls back on failure.

> The notify-on-pending UI and one-click trigger (a long-lived `ccswitch-deploy-controller`
> service) land in Plans 2–3. Until then, `--once` is the supported path and the
> `ccswitch-deploy.timer`/`.service` units have been removed (no more auto-deploy).

## Runtime env (`/etc/ccswitch/ccswitch.env`, root-only, NOT committed)

Holds the secrets/config the server needs — `CCSWITCH_DB_URL`, `CCSWITCH_MASTER_KEY`,
`CCSWITCH_SESSION_SECRET`, `CCSWITCH_TAILNET_ALLOWLIST`, `CCSWITCH_HOST`, `CCSWITCH_PORT`
— plus `PATH`/`HOME` so the service can find `git`/`node`/`claude`. `chmod 600`, `root:root`.

A host may point the **daemon** at a different env file via a local
`ccswitch-daemon.service.d/` drop-in (`EnvironmentFile=` then `EnvironmentFile=/etc/ccswitch/daemon.env`,
as on dobby). Whatever file the daemon actually loads is the one that matters for its `PATH`; check it
with `systemctl cat ccswitch-daemon.service`. The daemon does not strictly need `~/.local/bin` on that
`PATH` any more: `server/sessions/claude_bin.py` falls back to the native installer's well-known
locations (`~/.local/bin/claude`, newest `~/.local/share/claude/versions/*`) when `claude` is not on
`PATH`, and logs a WARNING if none exist. `CCSWITCH_CLAUDE_BIN` still overrides everything.

## Install

```bash
sudo install -m644 deploy/ccswitch.service          /etc/systemd/system/
sudo install -m644 deploy/ccswitch-daemon.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccswitch.service
```

## HTTPS via `tailscale serve` (required once per host)

The edge server binds to `127.0.0.1:8787` (loopback). To expose it over HTTPS on the
Tailscale network — required for browser secure-context APIs such as microphone access —
run this once on the host:

```bash
tailscale serve https / http://127.0.0.1:8787
```

This registers a persistent `tailscale serve` rule that survives reboots. After running it,
access ccswitch at `https://<hostname>.<tailnet-name>.ts.net` instead of the raw IP.
Tailscale provisions and auto-rotates the TLS certificate; no cert management is needed.

To verify the rule is active:

```bash
tailscale serve status
```

## Server-side dictation (optional)

Set `CCSWITCH_ENABLE_STT=true` in `/etc/ccswitch/ccswitch.env` and restart the edge
(`ccswitch.service` only — the daemon is untouched). The venv needs the `stt` extra
(`pip install -e '.[server,stt]'`, ~440 MB; the deploy controller does this automatically
when `pyproject.toml` changes). The `base.en` model (~150 MB) is downloaded into
`~/.cache/huggingface` at the first edge start after enabling, by the lifespan's warm
thread — so the mic is normally ready within ~10 s of startup, and a dictation started
while that download is still running simply waits for `ready`.
`GET /api/config` shows `sttEnabled: true` when it is live.

## Operate

```bash
systemctl status ccswitch.service                 # server health
journalctl -u ccswitch.service -f                 # server logs
journalctl -u ccswitch-daemon.service -f          # daemon logs
# Force a deploy now:
PYTHONPATH=<repo>/src <repo>/.venv/bin/python -m server.deploy --once
```

## Notes

- **Backend dependency changes** (new Python deps) trigger `pip install -e '.[server,stt]'`
  automatically when `uv.lock`/`pyproject.toml` changed since the last deploy.
- **Frontend dependency changes** trigger `npm ci` automatically when
  `web/package-lock.json` changed.
- The deploy runs as `tony` and uses passwordless `sudo` only for `systemctl restart`.
- **Unit-file edits (this file's `.service`/`.service.d` content) do NOT auto-apply.**
  `server.deploy` only runs `systemctl restart` on the already-installed unit in
  `/etc/systemd/system/` (see steps above); it never re-`install`s or
  `daemon-reload`s. After editing a committed unit file, re-run the `Install`
  steps above by hand (`sudo install -m644 ... && sudo systemctl daemon-reload`)
  and then restart the affected service(s), or the running unit stays on its old
  config indefinitely.

## Session daemon (`ccswitch-daemon.service`)

`ccswitch-daemon.service` runs `python -m server.ipc` — the session daemon that owns
live `claude` PTY sessions in remote mode. The daemon is **not enabled until remote
mode is deployed**; the unit file is shipped so `server.deploy` can restart it on
hosts where it is enabled, without affecting hosts where it isn't.

The `RuntimeDirectory=ccswitch` directive creates `/run/ccswitch/` on each start; the
daemon's Unix socket lives at `/run/ccswitch/sessiond.sock`.

### Install

```bash
sudo install -m644 deploy/ccswitch-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
# Enable only when remote-mode edge is deployed:
# sudo systemctl enable --now ccswitch-daemon.service
```

`server.deploy` restarts `ccswitch-daemon.service` before the edge when daemon code
changed, but only when `systemctl is-enabled ccswitch-daemon.service` succeeds — making
the block a complete no-op on hosts where the unit is not yet enabled.

### Stopping / restarting the daemon (`systemctl stop|restart ccswitch-daemon.service`)

The daemon owns every live `claude` session. **Any** stop or restart — the deploy
controller, the health watchdog, or you typing it after an env edit — is the same
SIGTERM, and until 2026-08-30 it ended every in-flight turn (five sessions at once,
each left "Interrupted — re-send to resume").

The daemon now drains itself on SIGTERM (`server/ipc/drain_on_signal.py`): it refuses
new turns, snapshots pending work, tells the UI "update pending", keeps serving while
in-flight turns finish, and only then runs the ordinary shutdown. The wait is bounded by
the same caps as the deploy drain — `CCSWITCH_DRAIN_TURN_CAP_SEC` (180 s) per ordinary
turn, `CCSWITCH_DRAIN_CONDENSE_CAP_SEC` (570 s) while a session is mid-compaction — so an
idle daemon still stops in seconds and a busy one makes `systemctl restart` block for
up to a few minutes. That is the feature. Two unit settings make it work and are pinned
by `test_restart_turn_interruption.py`:

- `KillMode=mixed` — SIGTERM reaches only the daemon; control-group delivery would kill
  the `claude` children in parallel and leave nothing to drain.
- `TimeoutStopSec=660` — the 570 s condense cap plus the ≤90 s shutdown budget. Lower
  it and systemd SIGKILLs the daemon mid-drain.

Escape hatches: a second SIGTERM (`sudo systemctl kill -s TERM ccswitch-daemon.service`)
skips the wait; `systemctl kill -s SIGKILL` is what the health watchdog does to a
daemon whose loop is wedged (it can't run the drain, so waiting 660 s is pointless).

**These are unit-file settings — deploys do not re-install unit files** (see the note
above). After pulling this change, re-run the Install steps and `daemon-reload` on each
daemon host, or the installed unit keeps its old `control-group` / 60 s behaviour and
the drain is defeated before it starts.

## Deploy controller (`ccswitch-deploy-controller.service`)

Long-lived service that polls `origin/master` (notify-only) and runs user-triggered deploys.
Serves a UDS at `/run/ccswitch/deploy.sock`; the edge proxies `/api/deploy/*` to it.

A triggered deploy runs seven phases, in order (`server/deploy/types.py::Phase`):
`preflight` (gate on GitHub CI green) → `save` (snapshot) → `update` (fast-forward, sync deps,
rebuild `web/dist`) → `report` → `runners` (fan out to every fleet host — see
`docs/runbooks/rocky-session-host.md` § "5. Deploy") → `restart` (the edge) → `resume`. The UI's
deploy panel and `GET /api/deploy/status` mirror this phase list.

Poll cadence defaults to **30s** (how quickly the "changes pending" banner appears after a
merge). Tune it with `CCSWITCH_DEPLOY_POLL_INTERVAL=<seconds>` in `ccswitch.env`, then
`sudo systemctl restart ccswitch-deploy-controller.service`. Each tick is one `git fetch` +
one `gh api` CI check, so even small values stay well under the GitHub rate limit.

**Cutover from the old auto-deploy (one-time).** The merge that ships this controller also
deletes `scripts/auto-deploy.sh`, so the old `ccswitch-deploy.timer` keeps firing against a
now-missing script (`203/EXEC`) until you disable it. The new controller is a separate systemd
unit, so it must be installed by hand — a deploy can't install its own successor:

    # 1. retire the old timer (its script is gone after the cutover deploy)
    sudo systemctl disable --now ccswitch-deploy.timer
    # 2. install + start the controller (edit the unit's WorkingDirectory/ExecStart to
    #    THIS host's deploy checkout first if it differs from the committed path)
    sudo install -m644 deploy/ccswitch-deploy-controller.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ccswitch-deploy-controller.service

Until the controller runs, `/api/deploy/status` soft-degrades to "controller unavailable" and
the dashboard's deploy panel stays inert. Replaces the retired `ccswitch-deploy.timer`/`.service`
(no more auto-deploy).

## Health watchdog (`ccswitch-health.timer`)

A 60s oneshot (`scripts/ccswitch-health-check.sh`) that does two things the rest of the
stack cannot:

1. **Control-plane watchdog.** Probes the daemon's Unix socket `/healthz`. A daemon whose
   process is alive but whose uvicorn accept-loop has wedged is invisible to
   `Restart=on-failure` (the process never exits) — this restarts it. Guarded by a 60s
   startup grace (won't fight a booting/deploying daemon) and a 300s cooldown (a genuine
   crash-loop is left to `Restart=on-failure`).
2. **Early warning.** Alerts on disk `>=85%`, any account `status=needs-reauth`, or daemon
   memory near the 12G `MemoryMax`. Alerts go to the journal; set `CCSWITCH_ALERT_WEBHOOK`
   (e.g. an ntfy topic URL) in `/etc/ccswitch/ccswitch.env` to also get push notifications.

Install (rewrite `ExecStart` to this host's deploy checkout first, like the daemon unit):

    sudo install -m644 deploy/ccswitch-health.service /etc/systemd/system/
    sudo install -m644 deploy/ccswitch-health.timer   /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ccswitch-health.timer

The daemon unit also gains `MemoryHigh=10G` (soft reclaim pressure below the 12G hard
`MemoryMax`/`OOMPolicy=stop`) so a restart-storm of relaunched session+MCP fleets degrades
instead of being OOM-killed. On an already-running host apply it live without a restart:

    sudo systemctl set-property --runtime ccswitch-daemon.service MemoryHigh=10G

## Hardware watchdog reachability test (`deploy/watchdog/test-reachability.sh`)

This is a **host-level** hardware-watchdog test, separate from the application health
timer above. It closes the gap that let rocky sit off the network for ~46 min on
2026-08-25 while its `watchdog.service` ran happily the whole time
([#767](https://github.com/steiner385/ccswitch/issues/767)): `test-ssh.sh` (below) only
probes **loopback**, which proves `sshd` is alive *on the box* but says nothing about
whether anything can **reach** the box. A hardware watchdog only fires on a *hung*
system; an alive-but-unreachable host never hangs, so nothing tripped.

`test-reachability.sh` adds the missing **outward** probe — but an outward probe is
exactly what reboot-loops a fleet when the *network* (not the host) is down, so it is
gated by four non-negotiable guards:

1. **Multiple independent targets, all must fail** — one dead peer can't trip it.
2. **Sustained window** (`WDR_FAIL_WINDOW`, default 5 min) of *continuous* total
   isolation before a reboot is even eligible — a blip is ignored.
3. **Reboot cooldown** (`WDR_REBOOT_COOLDOWN`, default 30 min) — a hard rate limit;
   a genuinely-down network becomes "one reboot every 30 min", never a loop.
4. **Boot grace** (`WDR_BOOT_GRACE`, default 10 min) — a no-op right after boot so
   Tailscale/routes/DHCP can settle before the host can re-reboot itself.

With **no targets configured it is a deliberate no-op** — a misconfigured host does
nothing rather than reboot-loop. See `deploy/watchdog/reachability.conf.example` for
the config contract; the guard behaviour is pinned by
`server/tests/test_watchdog_reachability.py`.

Install on the daemon host (this needs the `watchdog` package with
`test-directory = /etc/watchdog.d` in `/etc/watchdog.conf`):

    # 1. config for THIS host (root:root, 0644) — set WDR_TARGETS to a gateway +
    #    a tailnet peer + one target on a different path (>= 3 independent targets)
    sudo install -m644 deploy/watchdog/reachability.conf.example \
        /etc/ccswitch/watchdog-reachability.conf
    sudo $EDITOR /etc/ccswitch/watchdog-reachability.conf   # set WDR_TARGETS

    # 2. drop the test beside the loopback-only test-ssh.sh
    sudo install -m755 deploy/watchdog/test-reachability.sh /etc/watchdog.d/
    sudo systemctl restart watchdog.service

The `watchdog` daemon runs the script every interval with `test`; a non-zero exit
makes it re-run the script with `repair`, and a non-zero `repair` triggers the reset.
State (outage-window start + cooldown anchor) lives in `WDR_STATE_DIR`
(`/var/lib/watchdog-reachability`, must survive reboots for the cooldown to hold).

## Hardware watchdog SSH liveness test (`deploy/watchdog/test-ssh.sh`)

The loopback half of the pair above: it proves `sshd` on this host is not merely
listening but actually **speaking SSH**. It opens a TCP connection to `127.0.0.1:22`,
reads the protocol version-exchange greeting, and requires it to start with `SSH-`.

This replaces an earlier version that lived only on the host and had two defects:

1. Its first branch ran `ssh … localhost exit 0` **as root**, which cannot
   authenticate, so every 10s interval logged
   `Connection closed by authenticating user root 127.0.0.1 … [preauth]` —
   ~8,700 sshd auth failures a day, burying real signal during an incident.
2. Its fallback, `echo > /dev/tcp/127.0.0.1/22`, only proved the port *accepts* a
   connection and then wrote garbage into it (`banner exchange: … invalid format`
   every 10s). It would still have passed with `sshd` wedged mid-handshake.

Reading the greeting fixes both: it attempts **no authentication at all**, so it
produces no auth failures, and a wedged `sshd` that accepts TCP without ever greeting
is now correctly caught.

What remains is one benign line per interval at `LogLevel INFO`
(`Connection closed by 127.0.0.1 port N`, with no `[preauth]` and no user) — that is
unavoidable for *any* TCP connection to `sshd`, and it is an ordinary disconnect
notice rather than an auth failure or a protocol error. Sending a client
identification string first was measured and is **not** quieter: same volume, but the
lines come back tagged `[preauth]`, which reads more like a failure than less.

**A non-zero exit here reboots the physical host**, so the script is deliberately
fail-safe and every knob below serves that:

* It exits **0** on any *inconclusive* result and returns non-zero only on a clear,
  positive determination. In particular, before trusting a failed connect it
  self-tests that this `bash` really supports `/dev/tcp` (a build with
  `--disable-net-redirections` reports `No such file or directory` rather than
  `Connection refused`); if that is unproven it passes rather than reboots.
* Three attempts must **all** fail, absorbing transients; combined with
  `retry-timeout = 60` a reboot needs ~6 consecutive failures over a full minute.
* Every probe is wrapped in `timeout -s KILL`, bounding a run at ~18s — safely under
  `test-timeout = 30` — so it can never hang the daemon.
* There is intentionally **no `set -e`**: an unexpected non-zero from an intermediate
  command must never become the script's exit status.

Install on the daemon host:

    sudo install -m755 deploy/watchdog/test-ssh.sh /etc/watchdog.d/
    sudo systemctl restart watchdog.service

Note that `systemctl restart watchdog.service` can fail on Debian/Ubuntu: the unit's
`ExecStopPost` exits non-zero by design to hand `/dev/watchdog` to
`wd_keepalive.service` via `OnFailure=`, which cancels the queued restart and leaves
`watchdog.service` **inactive**. If that happens, `sudo systemctl start
watchdog.service` completes the handoff back. Always confirm `systemctl is-active
watchdog.service` afterwards, and check the startup log lists the tests you expect:

    sudo journalctl -u watchdog.service -n 30 | grep 'test/repair'

Keep anything else in `/etc/watchdog.d/` **non-executable** — the daemon runs *every*
executable file in that directory, so an executable `test-ssh.sh.bak-<timestamp>` left
beside the script silently keeps running the old test.

## Remote mounts (`ccswitch-mounts.service`, remote session host only)

rocky runs the sessions; **dobby is canonical** for the account store, the shared
`~/.claude` assets, and the archive dir (spec decision 4). rocky reaches all of them
over sshfs across the tailnet. Without the mounts the daemon starts but every session
fails: no credentials, the wrong skill set, and archiving dies with `Permission denied`
on `/mnt/hdd-storage`.

`ccswitch-daemon.service.d/require-mounts.conf` binds the daemon to the mount unit with
`Requires=` + `After=`. That `Requires=` is **load-bearing** — #765 was the daemon
starting into a *dead* sshfs mount and dying with
`OSError: [Errno 5] Input/output error: '/home/tony/.claude-accounts'`. Do not loosen it
to `Wants=`.

The cost of `Requires=` is that a failed mount unit **cancels** the daemon's start job,
and a cancelled start job never engages `Restart=always`. On 2026-08-26 that took the
fleet down: rocky rebooted, `ccswitch-mounts.service` ran while tailscaled was still
settling, the first sshfs attempt for `.claude-accounts` died with
`read: Connection reset by peer`, and the daemon sat dead until
`ccswitch-daemon-ensure.timer` fired ~60s later. Two layers now cover it, in order:

1. **`bin-ccswitch-mounts.sh` retries internally** (`CCSWITCH_MOUNT_ATTEMPTS`, default 5,
   `CCSWITCH_MOUNT_RETRY_DELAY`, default 3s) so a tailnet still settling at boot never
   fails the unit in the first place. Retries are bounded, so a genuinely-down dobby
   still fails loudly. `TimeoutStartSec=300` on the unit must stay above the worst-case
   retry budget, or systemd SIGKILLs the script mid-retry and reopens the race.
2. **`ccswitch-daemon-ensure.timer`** re-starts a daemon left dead by a cancelled job (#765).

Behaviour is pinned by `server/tests/test_mounts_unit.py`, which executes the real script
against stubbed `sshfs`/`mountpoint` (a retry loop that does not retry passes a grep).

Install on the remote session host:

    sudo install -m755 deploy/bin-ccswitch-mounts.sh /home/tony/bin-ccswitch-mounts.sh
    sudo install -m644 deploy/ccswitch-mounts.service /etc/systemd/system/
    sudo mkdir -p /etc/systemd/system/ccswitch-daemon.service.d
    sudo install -m644 deploy/ccswitch-daemon.service.d/require-mounts.conf \
        /etc/systemd/system/ccswitch-daemon.service.d/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ccswitch-mounts.service

### Cache profiles: the account store must be mounted coherently (#1005)

`bin-ccswitch-mounts.sh` picks one of two `-o` profiles per path:

| profile | paths | options |
|---|---|---|
| cached (read-mostly) | `~/.claude`, `/mnt/hdd-storage`, `~/avd-golden` | `dir_cache=yes,dcache_timeout=600,attr_timeout=600,entry_timeout=600` |
| coherent | `~/.claude-accounts` (`CCSWITCH_MOUNT_COHERENT_PATHS`) | `dir_cache=no,dcache_timeout=0,attr_timeout=0,entry_timeout=0` |

The 600s cache (#779, tuned by #826/#851) is a latency win and stays for everything dobby
does not rewrite underneath us. It is **wrong for the account store**. Claude Code writes
`.claude.json` atomically — temp file + `rename(2)` — so both the inode and the size change
on every write, and dobby's live sessions rewrite it continuously. A reader holding a 10-minute
attribute cache therefore gets a **truncated body**, which Claude Code's config loader reports
as corruption:

    Claude configuration file at …/.claude.json is corrupted: JSON Parse error: Unterminated string
    The corrupted file has been backed up to: …/backups/.claude.json.corrupted.<ts>

…and then starts from a **default** config and writes it back — through the mount, onto
dobby's disk. From 2026-09-01 rocky did this ~1,100×/day, repeatedly wiping project trust,
`projects` history, onboarding flags and cached identity for all three accounts while dobby
logged zero such events. The file was never corrupt; the *view* was stale.

**A cache-profile change does not apply itself.** The script leaves a live mount alone on
purpose — unmounting drops every in-flight read. After deploying, remount the account store
when the Sessions page shows nothing mid-turn:

    CCSWITCH_MOUNT_FORCE_REMOUNT=1 CCSWITCH_MOUNT_PATHS=/home/tony/.claude-accounts \
      /home/tony/bin-ccswitch-mounts.sh
    grep .claude-accounts /proc/mounts    # expect attr_timeout=0, no dir_cache=yes

Two further layers make this safe even where a host's mount profile is still wrong:

- **One-shot helper spawns never touch the shared config.** `account_auth.helper_env()`
  points `CLAUDE_CONFIG_DIR` at a host-local mirror under `~/.cache/ccswitch/helper-cfg`
  (`server/sessions/scratch_config.py`): `.claude.json`/`.credentials.json` are copies, the
  shared assets are symlinks, and a torn read reuses the last-known-good copy instead of
  resetting anything. Live sessions still use the real account dir — they must, since their
  history and token refreshes belong there. Kill switch:
  `CCSWITCH_HELPER_SCRATCH_CONFIG=false`.
- **The evidence survives.** `configdir.compose()` used to `rmtree` `backups/` on every call,
  destroying each `.claude.json.corrupted.*` within seconds of its creation — which is why
  three days of resets left nothing to diagnose. It now prunes only entries older than
  `CCSWITCH_CONFIG_BACKUP_RETENTION_S` (default 24h).

**Verifying the fix.** `scripts/ccswitch-health-check.sh` section 6 alerts on either counter
being non-zero over the last hour:

    ALERT[config-torn-read] shared config coherence: N torn read(s) … and M '.claude.json.corrupted.*' reset(s) …

- `N > 0` — **this** host is still reading a stale view; its account-store mount profile is wrong.
- `N == 0, M > 0` — some **other** host is; check each host's `/proc/mounts`.
- Both 0 for a few hours, and `.claude.json`'s size on dobby no longer oscillating
  (`watch -n5 stat -c '%s %i' ~/.claude-accounts/cfg/<account>/.claude.json`) — fixed.

## Hardware watchdog load thresholds (`/etc/watchdog.conf`)

Separate knob from the reachability test above, and a sharp one. `watchdog(8)` will
**hard-reboot the host** when the load average exceeds `max-load-1/5/15`. rocky ships
8 cores but runs six KinDash self-hosted CI runners *plus* the whole claude session
fleet, so load in the tens is normal working load, not a hang.

On 2026-08-26 the stock `max-load-1 = 50` fired during ordinary work (a session doing
Android AVD/qemu work on top of the fleet): load crossed 50 at 09:18:25, and 70s later
the watchdog killed the box — `shutting down the system because of error 253 = 'load
average too high'` — taking every live session with it.

Thresholds are now `150 / 120 / 90`, well clear of realistic peaks while still bounding
a true runaway. Raise them, don't lower them, if the runner count on a host grows:

    load=150,120,90    # confirm with: journalctl -u watchdog | grep 'load='

A hung *kernel* is still caught by the hardware watchdog itself (`iTCO_wdt`, 60s), which
is independent of these software thresholds — high load is not what that protects against.
