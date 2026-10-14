# Deploy — ViberPit web (self-hosted)

Canonical templates use `viberpit*` names. The legacy `ccswitch-daemon.service`-style template paths are
compatibility symlinks to the same files, and `[Install] Alias=` retains old
systemctl callers. Install the **canonical** filenames so enablement creates
aliases pointing to them. The remote daemon template is installed as
`viberpit-daemon.service`, never alongside the local daemon template.

For an existing running host, follow [the host cutover runbook](../docs/runbooks/viberpit-host-cutover.md)
before installing these templates. Preserve effective host drop-ins, resource
limits, secrets, socket bindings and enabled-unit state. Do not copy generic
units over a live host's settings. In particular, keep the current mounts
service running: changing its name must not execute `ExecStop` or remount the
account store. Keep `Requires=`/`After=` on its surviving service identity until
a later cold boot can adopt the canonical mount unit.

Configuration uses `/etc/viberpit/viberpit.env`; `VIBERPIT_*` environment keys
remain supported. The credential authority keeps the `ccswitch-auth` Unix user
and private `/var/lib/ccswitch-codex-auth` state until credential rotation.
Database identities and existing session/worktree namespaces also stay intact.


The server runs as a **systemd service** on the Tailscale host.

## Components

| Unit | Role |
|------|------|
| `viberpit.service` | Long-running uvicorn server (`server.app:app`), serves the API + built `web/dist`. |
| `viberpit-daemon.service` | Session daemon (`python -m server.ipc`) — owns live `claude` PTY sessions in remote mode. |
| `viberpit-codex-auth.service` | ChatGPT credential authority (`python -m server.codex.auth_main`) — the only holder of OpenAI refresh tokens. Separate OS user, separate mTLS trust domain, no failover. |

## Deploy (Plan 1: manual)

Deploys are no longer automatic. Run one on demand:

    PYTHONPATH=<repo>/src <repo>/.venv/bin/python -m server.deploy --once

This gates on GitHub CI being green, fast-forwards `origin/master`, syncs deps only
when lockfiles changed, rebuilds `web/dist`, restarts the daemon (if daemon code
changed) then the edge, health-gates on the deployed SHA, and rolls back on failure.

> The notify-on-pending UI and one-click trigger (a long-lived `viberpit-deploy-controller`
> service) land in Plans 2–3. Until then, `--once` is the supported path and the
> `ccswitch-deploy.timer`/`.service` units have been removed (no more auto-deploy).

## Runtime env (`/etc/viberpit/viberpit.env`, root-only, NOT committed)

Holds the secrets/config the server needs — `VIBERPIT_DB_URL`, `VIBERPIT_MASTER_KEY`,
`VIBERPIT_SESSION_SECRET`, `VIBERPIT_TAILNET_ALLOWLIST`, `VIBERPIT_HOST`, `VIBERPIT_PORT`
— plus `PATH`/`HOME` so the service can find `git`/`node`/`claude`. `chmod 600`, `root:root`.

A host may point the **daemon** at a different env file via a local
`viberpit-daemon.service.d/` drop-in (`EnvironmentFile=` then `EnvironmentFile=/etc/viberpit/daemon.env`,
as on dobby). Whatever file the daemon actually loads is the one that matters for its `PATH`; check it
with `systemctl cat viberpit-daemon.service`. The daemon does not strictly need `~/.local/bin` on that
`PATH` any more: `server/sessions/claude_bin.py` falls back to the native installer's well-known
locations (`~/.local/bin/claude`, newest `~/.local/share/claude/versions/*`) when `claude` is not on
`PATH`, and logs a WARNING if none exist. `VIBERPIT_CLAUDE_BIN` still overrides everything.

`VIBERPIT_GH_CONFIG_DIR` belongs here too on any host whose `gh` token lives in a desktop keyring —
**and in the daemon's own env file if it loads a different one** (on dobby it does, so the var must be
set in both). Unset, the services have no file-backed gh credential, so a keyring that autologin never
unlocks leaves **every** gh-backed automation unauthenticated from boot until a human unlocks it at the
console — `github.host_gh_unauthenticated` in the journal. See
[gh credential without a keyring](../docs/runbooks/gh-credential-without-a-keyring.md) (#2052).

## Install

```bash
sudo install -m644 deploy/viberpit.service          /etc/systemd/system/
sudo install -m644 deploy/viberpit-daemon.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now viberpit.service
```

## Edge unit: `--no-proxy-headers` (#2980)

`viberpit.service` runs uvicorn with `--no-proxy-headers`. The app applies `X-Forwarded-For`
itself (`server/auth/identity.py` `TransportPeerMiddleware`) after recording the real socket
peer, so always-allow rule writes and profile-pattern writes can tell tailscaled's serve hop
(root) and nginx (www-data) from a process on the host (threat note:
`docs/security/2026-10-11-local-identity-assertion.md`). `request.client` is rewritten exactly as
before, so the loopback-only token gates are unchanged.

Unit files do not auto-apply. On a host whose installed unit predates this, those writes through
`tailscale serve` fail closed with `RULES_HUMAN_ONLY`, and the edge log carries a WARNING naming
the flag. Rules are written from another tailnet device: a browser on the edge host itself is
indistinguishable from an agent session there. Reinstall the unit, and the nginx vhost (it now
overwrites `X-Forwarded-For`, #3010):

```bash
sudo install -m644 deploy/viberpit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart viberpit.service
sudo cp deploy/viberpit-tailnet.nginx /etc/nginx/sites-available/viberpit-tailnet
sudo nginx -t && sudo nginx -s reload
```

A drop-in that resets `ExecStart=` (dobby's `viberpit.service.d/loopback-bind.conf` from the
2026-08-30 serve migration) overrides the reinstalled unit, so add `--no-proxy-headers` there
too. Confirm the running edge has it; the WARNING above keeps firing until it does:

```bash
systemctl cat viberpit.service | grep '^ExecStart='   # the LAST one wins
pgrep -af 'uvicorn server.app' | grep -c -- --no-proxy-headers   # want 1
```

### Writing rules from the host's own browser (opt-in)

By default, a browser on the edge host gets `RULES_HUMAN_ONLY` when it writes always-allow rules or
permission-profile patterns, because it looks the same as an agent session there. If you mainly use
ViberPit from the host and accept that agent sessions on it could then write rules too, opt in:

```bash
echo 'VIBERPIT_RULES_TRUST_LOCAL_HOST=true' | sudo tee -a /etc/viberpit/viberpit.env
sudo systemctl restart viberpit.service   # the edge only; sessions on the daemon keep running
```

Confirm it on Settings › About, or with `journalctl -u viberpit.service | grep RULES_TRUST_LOCAL_HOST`.
Keep `/etc/viberpit/viberpit.env` `root:root 0600`. If a session can write that file, it can turn
this on. Rules written under the opt-in show `createdVia: local-trusted`, and their audit rows (and
those for profile-pattern writes) carry `via: local-trusted`.

The edge reads this flag only from its service environment, that is `/etc/viberpit/viberpit.env`
as systemd applies it at start. A `VIBERPIT_RULES_TRUST_LOCAL_HOST` line in the deploy checkout's
`.env` is deliberately ignored: that file is writable by the operator's user, so any agent session
could otherwise turn the flag on for itself.

## HTTPS via nginx + tailscale cert (required once per host)

The edge server binds to `127.0.0.1:8787` (loopback). To expose it over HTTPS on the
Tailscale network — required for browser secure-context APIs such as microphone access —
run this once on the host (nginx must already be installed):

```bash
sudo tailscale cert \
    --cert-file /etc/viberpit/dobby.tail0dd570.ts.net.crt \
    --key-file  /etc/viberpit/dobby.tail0dd570.ts.net.key \
    dobby.tail0dd570.ts.net
sudo cp deploy/viberpit-tailnet.nginx /etc/nginx/sites-available/viberpit-tailnet
sudo ln -sf /etc/nginx/sites-available/viberpit-tailnet /etc/nginx/sites-enabled/
sudo nginx -t && sudo nginx -s reload
sudo cp deploy/viberpit-cert-renew.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now viberpit-cert-renew.timer
```

Access ViberPit at `https://dobby.tail0dd570.ts.net/`. Tailscale provisions the TLS
certificate; `viberpit-cert-renew.timer` renews it weekly and reloads nginx automatically.

To verify the nginx vhost is active:

```bash
sudo nginx -t
systemctl status viberpit-cert-renew.timer
```

## Server-side dictation (optional)

Set `VIBERPIT_ENABLE_STT=true` in `/etc/viberpit/viberpit.env` and restart the edge
(`viberpit.service` only — the daemon is untouched). The venv needs the `stt` extra
(`pip install -e '.[server,stt]'`, ~440 MB; the deploy controller does this automatically
when `pyproject.toml` changes). The `base.en` model (~150 MB) is downloaded into
`~/.cache/huggingface` at the first edge start after enabling, by the lifespan's warm
thread — so the mic is normally ready within ~10 s of startup, and a dictation started
while that download is still running simply waits for `ready`.
`GET /api/config` shows `sttEnabled: true` when it is live.

## AI assistant (on by default)

The assistant drawer (`docs/product/features/ai-assistant.md`) is **enabled by default** as of
the Phase 4 launch — an edge with none of these vars set serves it. Nothing needs installing:
it reuses the normal session stack, so there is no extra venv extra and no model download.

| Var | Default | What it does |
|---|---|---|
| `VIBERPIT_ASSISTANT_ENABLED` | `true` | Kill switch. `false` → `POST /api/assistant/session` returns 404 `ASSISTANT_DISABLED`, `/api/assistant/status` reports `enabled: false`, and the drawer falls back to its placeholder. Note it stops *new* sessions: an already-running assistant keeps its driver until the normal idle sweep closes it (below), so disable then wait out `VIBERPIT_ASSISTANT_IDLE_S` if you need the drivers gone. |
| `VIBERPIT_ASSISTANT_MODEL` | `sonnet` | Model new assistant sessions spawn on. A tier that needs approval is refused with 400 `MODEL_GATED` rather than silently downgraded. |
| `VIBERPIT_ASSISTANT_IDLE_S` | `900` | Idle grace before an assistant's driver is closed. **Also sets the assistant bearer token's max age (`idle_s × 4`, `server/auth/middleware.py`)** — raising it lengthens a credential's lifetime, so treat it as a security knob, not just a timeout. |
| `VIBERPIT_ASSISTANT_MAX_TURNS` | `30` | SDK turn cap per assistant session. |
| `VIBERPIT_ASSISTANT_MAX_PROMPTS_PER_HOUR` | `120` | Per-identity prompt rate limit; over it the WS replies `promptRejected` / `RATE_LIMITED`. |
| `VIBERPIT_ASSISTANT_ENGINE` | `claude_code` | Which agent engine the drawer runs on. `codex` mounts the Codex overlay (`server/assistant/options_codex.py`): the ViberPit tools become client-declared `dynamicTools` that the edge itself executes, the thread runs `read-only` + `untrusted` in a repo-free `cwd`, and every built-in approval is declined. Default Claude because the boundary is unit-tested but has no end-to-end acceptance run yet (#1321/#1290). Needs `VIBERPIT_CODEX_ENABLED=true` and an OpenAI account on the host, or `POST /api/assistant/session` answers 409 `ASSISTANT_ENGINE_UNAVAILABLE` rather than falling back to Claude. Env only — deliberately not a Settings toggle, because the engine picks the security overlay. **A Codex drawer does not resume**: `thread/resume` cannot re-declare the tool surface, so each reopen after the idle reap starts a fresh thread and the model loses prior context (the ViberPit transcript is unaffected). |
| `VIBERPIT_ASSISTANT_OPERATOR_TOOLS` | `false` | Opt-in operator tier: deploy start, host upgrade, account refresh/enable and re-auth kick-off. Off by default because of the blast radius. `AppSettings.assistant_operator_tools` (Settings → General → AI assistant → Operator tools) overrides it with the same precedence as the kill switch below. Takes effect for a thread on its **next** spawn — the tools are registered at session start, so an assistant already running keeps the surface it started with until the idle reap. |
| `VIBERPIT_ASSISTANT_EXTERNAL_TOKENS` | `false` | Opt-in `ccas2:` external tokens for the CLI/MCP bridges (Pack X, "Use from other agents" in the feature doc). Off by default: `false` means `POST /api/assistant/tokens` (mint), `GET`/`DELETE /api/assistant/tokens[/{id}]` (list/revoke) all 404, so no external client can reach the edge over the tailnet without a browser session. |
| `VIBERPIT_ASSISTANT_TOKEN_MAX_DAYS` | `90` | Hard cap on a minted `ccas2:` token's lifetime regardless of the `ttlDays` the caller requests. |
| `VIBERPIT_ASSISTANT_WATCH_MAX_PER_IDENTITY` | `20` | Per-identity cap on live proactive-notice watches (`watch()`, Pack N) so a runaway loop can't spam the notification table. |

**The Settings toggle overrides the env var, not the other way round.** `assistant_enabled` in
`server/assistant/policy.py` reads `app_settings.assistant_enabled` first and only falls back to
the env when that column is NULL. So once anyone has flipped Settings → General → AI assistant,
editing `VIBERPIT_ASSISTANT_ENABLED` in `viberpit.env` has **no effect** — to disable a deployment
for good, turn the switch off in Settings (or `UPDATE app_settings SET assistant_enabled = false
WHERE id = 'singleton';`). Set it back to NULL to hand control to the env var again.
`VIBERPIT_ASSISTANT_MODEL` / `assistant_model` and `VIBERPIT_ASSISTANT_OPERATOR_TOOLS` /
`assistant_operator_tools` follow the same precedence.

Check what a running edge actually resolved — this is the assistant's analogue of `sttEnabled`,
and unlike STT it is **not** in `GET /api/config`, because the answer depends on DB state:

```bash
curl -s https://dobby.tail0dd570.ts.net/api/assistant/status   # {"enabled":true,"sessionId":…,"model":"sonnet","engine":"claude_code"}
```

Assistant sessions are ordinary sessions with `kind == "assistant"`, hidden from the session
list and scoped to the identity that created them. Their only tools are the in-process
`viberpit` MCP server, whose calls are additionally fenced by a deny-by-default route
allowlist (`ASSISTANT_ROUTE_ALLOW` in `server/assistant/policy.py`) enforced *before* routing;
the bearer token is loopback-only and never persisted. Metrics to scrape:
`assistant_prompts_total`, `assistant_tool_calls_total{tool,class,outcome}`,
`assistant_ttft_ms`, `assistant_turn_ms`, `assistant_sessions_live`.

See also the `ix_sessions_kind_owner` index note under "Notes" below — the assistant works
without it, but wants it once session volume grows.

**`assistant_tokens` table** (external `ccas2:` tokens, migration `0028_assistant_tokens`) stores
only a `token_hash`, never the raw token — a database dump or backup cannot be used to mint a
working credential. `expires_at` and `revoked_at` are both checked on every request in addition to
`VIBERPIT_ASSISTANT_EXTERNAL_TOKENS`, so flipping that var off leaves already-issued rows inert
rather than needing a bulk revoke.

## Operate

```bash
systemctl status viberpit.service                 # server health
journalctl -u viberpit.service -f                 # server logs
journalctl -u viberpit-daemon.service -f          # daemon logs
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
- **Migrations never run automatically** (see "Notes" under the AI assistant migration,
  `server/db/migrations/versions/0022_assistant_sessions.py`): `_reconcile_columns` ALTER-ADDs the
  new nullable columns on both dialects at startup, but reconcile never creates indexes on an
  existing table. `ix_sessions_kind_owner` (sessions.kind, sessions.owner_identity) must be created
  by hand on the live Postgres DB after this deploy:
  `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sessions_kind_owner ON sessions (kind,
  owner_identity);` — the assistant works without it, just slower once session volume grows.

## Where the logs are (`/var/log/viberpit`, per host, per process) — #1010

Each process writes JSON lines to **journald** (`journalctl -u viberpit`,
`journalctl -u viberpit-daemon`) *and* to a rotating file. The file is chosen by
`server/obs/logdir.py` and is unique per **host** and per **process**:

```
/var/log/viberpit/viberpit-edge.jsonl      # viberpit.service
/var/log/viberpit/viberpit-daemon.jsonl    # viberpit-daemon.service
```

Before #1010 there was no per-host part. The default asked
`os.access("/var/log", os.W_OK)` — writability of the *parent*, which is root-owned
— so under `User=tony` it was always false and every process fell through to
`~/.claude-accounts/logs`. On a session runner that directory is the **sshfs mount
of dobby's account store**, so rocky's daemon appended to dobby's file and both
hosts rotated it. When #1009 needed rocky's daemon logs, nothing it had written was
recoverable; the timeline had to be rebuilt from the replay ring.

Three things keep that from coming back, and all three need to hold:

1. `LogsDirectory=viberpit` in each unit — systemd creates `/var/log/viberpit`
   owned by `User=` on **that host**, so locality is a property of the unit, the
   same way `StateDirectory=` fixed `CODEX_HOME` for #1005.
2. The resolver refuses any directory that is not provably on a local filesystem,
   via the same mount-table reader (`server/fsmounts.py`) that refuses a
   `CODEX_HOME` on sshfs. A log directory pointed at a share (the `*_LOG_DIR`
   override under Runtime env, above) degrades to
   **stdout-only with a warning** — the journal still has every line.
3. The filename carries the process, so the edge and the daemon never share one
   `RotatingFileHandler` target (rollover renames the live file out from under the
   other writer).

**Applying this on a host that predates it** — unit files do not auto-apply (see
Notes above):

```bash
sudo install -m644 deploy/viberpit.service deploy/viberpit-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
# /var/log/viberpit may already exist root-owned from an earlier install:
sudo chown tony:tony /var/log/viberpit
sudo systemctl restart viberpit.service          # daemon: see the stop/restart warning below
```

Until that is done the resolver falls back to `/var/lib/viberpit/logs`
(`StateDirectory=`, already per-host), so an un-migrated host still logs locally —
just not under `/var/log`. Confirm with:

```bash
ls -l /var/log/viberpit/          # expect viberpit-<process>.jsonl owned by tony
journalctl -u viberpit-daemon -p warning --since '-1h' | grep -c 'not provably host-local'
```

The old log file under `~/.claude-accounts/logs/` is not migrated or deleted by this
change — it is a mixed-host, mixed-process file and is only useful as history.

## Session daemon (`viberpit-daemon.service`)

`viberpit-daemon.service` runs `python -m server.ipc` — the session daemon that owns
live `claude` PTY sessions in remote mode. The daemon is **not enabled until remote
mode is deployed**; the unit file is shipped so `server.deploy` can restart it on
hosts where it is enabled, without affecting hosts where it isn't.

The `RuntimeDirectory=viberpit` directive creates `/run/viberpit/` on each start; the
daemon's Unix socket lives at `/run/viberpit/sessiond.sock`.

### Install

```bash
sudo install -m644 deploy/viberpit-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
# Enable only when remote-mode edge is deployed:
# sudo systemctl enable --now viberpit-daemon.service
```

`server.deploy` restarts `viberpit-daemon.service` before the edge when daemon code
changed, but only when `systemctl is-enabled viberpit-daemon.service` succeeds — making
the block a complete no-op on hosts where the unit is not yet enabled.

### Stopping / restarting the daemon (`systemctl stop|restart viberpit-daemon.service`)

The daemon owns every live `claude` session. **Any** stop or restart — the deploy
controller, the health watchdog, or you typing it after an env edit — is the same
SIGTERM, and until 2026-08-30 it ended every in-flight turn (five sessions at once,
each left "Interrupted — re-send to resume").

The daemon now drains itself on SIGTERM (`server/ipc/drain_on_signal.py`): it refuses
new turns, snapshots pending work, tells the UI "update pending", keeps serving while
in-flight turns finish, and only then runs the ordinary shutdown. The wait is bounded by
the same caps as the deploy drain — `VIBERPIT_DRAIN_TURN_CAP_SEC` (180 s) per ordinary
turn, `VIBERPIT_DRAIN_CONDENSE_CAP_SEC` (570 s) while a session is mid-compaction — so an
idle daemon still stops in seconds and a busy one makes `systemctl restart` block for
up to a few minutes. That is the feature. Two unit settings make it work and are pinned
by `test_restart_turn_interruption.py`:

- `KillMode=mixed` — SIGTERM reaches only the daemon; control-group delivery would kill
  the `claude` children in parallel and leave nothing to drain.
- `TimeoutStopSec=660` — the 570 s condense cap plus the ≤90 s shutdown budget. Lower
  it and systemd SIGKILLs the daemon mid-drain.

Escape hatches: a second SIGTERM (`sudo systemctl kill -s TERM viberpit-daemon.service`)
skips the wait; `systemctl kill -s SIGKILL` is what the health watchdog does to a
daemon whose loop is wedged (it can't run the drain, so waiting 660 s is pointless).

**These are unit-file settings — deploys do not re-install unit files** (see the note
above). After pulling this change, re-run the Install steps and `daemon-reload` on each
daemon host, or the installed unit keeps its old `control-group` / 60 s behaviour and
the drain is defeated before it starts.

### One owner per session: the resume lease and the orphan reap (#1379)

`KillMode=mixed` is what makes the drain possible, and it has a cost: the old unit's
`claude` children may outlive it. Every deploy logs it —

```
systemd: viberpit-daemon.service: Killing process 4041937 (claude) with signal SIGKILL.
systemd: viberpit-daemon.service: Unit process 4041937 (claude) remains running after unit stopped.
```

On 2026-09-06 a fleet-wide restart resumed **one** session on **both** hosts. Each daemon
rehydrated the shared row, each concluded it owned it (the host stamp flips when a drain
is cut short — `drain cap — restarting anyway`), and each spawned its own
`claude --resume <conversation-id>` into its own copy of the worktree. The twins
force-pushed each other's branches, armed auto-merge on each other's PRs, and split the
agent's memory across three account config dirs. Two mechanisms now prevent it:

- **A per-session lease** (`server/sessions/session_lease.py`) — one `daemon_leases` row
  per session, keyed `session:<id>`, holder = host name. Every driver build claims it
  first; a daemon that finds a *different* host holding it with a fresh heartbeat logs
  `session-lease: refusing to …` and skips. The same host always re-acquires instantly,
  so a systemd restart resumes its own sessions with no delay, and a genuinely dead host's
  sessions are picked up elsewhere once its heartbeat lapses past the TTL. `stop()` and
  every driver teardown hand the lease back, so a planned restart hands over immediately.
- **A boot-time orphan reap** (`server/sessions/orphan_cli.py`) — at daemon start, before
  anything can rehydrate, an SDK-protocol `claude` is SIGTERM'd (then SIGKILL'd) only when
  **nothing alive owns it** (walking `ppid` finds no live non-CLI ancestor, only pid 1 or
  a parent that already exited) **and** its `CLAUDE_CONFIG_DIR` maps to a managed account.
  A human's interactive `claude`, an internal pty-host/spare, any CLI on an unmanaged
  config dir, and the CLIs of any *other live* ViberPit process on the box (a co-located
  edge running its own orchestrator) are never touched. Each reap is logged at WARNING
  with the pid, account, cwd and the conversation id it was resuming.

Knobs (both default to the safe behaviour; set them only to debug an incident):

| Var | Default | Effect |
| --- | --- | --- |
| `VIBERPIT_SESSION_LEASE_TTL_S` | `120` | How stale a holder's heartbeat may get before another host may claim its sessions. Renewal is TTL/5. |
| `VIBERPIT_SESSION_LEASE_ENFORCE` | on | `false` still takes and logs the lease but stops a LOSS from blocking a build — an operator's forced takeover before the TTL. |
| `VIBERPIT_REAP_ORPHAN_CLIS` | on | `false` leaves surviving CLIs alone (e.g. to inspect one). |

Triage — live CLIs per conversation id, on each host:

```bash
for p in $(pgrep -f '[/]claude '); do echo "$(hostname) pid=$p ppid=$(ps -o ppid= -p $p) \
  $(tr '\0' ' ' </proc/$p/cmdline | grep -oE -- '--resume [a-f0-9-]{36}') cwd=$(readlink /proc/$p/cwd)"; done
journalctl -u viberpit-daemon.service --since today | grep -E 'session-lease|orphan-cli'
```

## ChatGPT credential authority (`viberpit-codex-auth.service`)

`viberpit-codex-auth.service` runs `python -m server.codex.auth_main` — the **only**
process in the fleet that holds an OpenAI/ChatGPT *refresh* token. Runners
(dobby's daemon, rocky's worker) never hold one: they fetch short-lived access-token
envelopes over mutual TLS and hand them straight to `codex app-server` over stdin.

This is a separate trust domain from the session daemon, and the separation is the
whole point:

* it runs as its own OS user (`ccswitch-auth`), so the user that runs `claude` and
  `codex app-server` cannot read the credential store;
* it has its own key material and its own env file — reusing a
  `VIBERPIT_SESSIOND_*` key is refused at startup, not merely discouraged;
* it is **not** `PartOf=` the daemon. A daemon restart (which touches every live
  session) must not restart the process holding every ChatGPT credential.

There is **no failover in v1.** A second authority sharing the state directory
would be a second writer of the per-account generation counter, so the second start
takes an exclusive lock or exits 3.

> **Host procedures live in `docs/runbooks/codex-credential-authority.md`**: minting
> this service's own PKI (it must not share the session-daemon CA), onboarding a
> runner, reauth, backup/restore, Codex CLI upgrades, rollback, and the kill switch.
> This section is the architecture and the env reference.

### Install

```bash
sudo install -m644 deploy/viberpit-codex-auth.sysusers \
     /usr/lib/sysusers.d/viberpit-codex-auth.conf
sudo systemd-sysusers
sudo install -m644 deploy/viberpit-codex-auth.service /etc/systemd/system/
sudo systemctl daemon-reload
# Enable only once /etc/viberpit/codex-auth.env exists (see below):
# sudo systemctl enable --now viberpit-codex-auth.service
```

### `/etc/viberpit/codex-auth.env` (root-only, NOT committed)

Deliberately a separate file from `/etc/viberpit/viberpit.env`: the authority has no
business inheriting the edge's database URL, GitHub credentials, or session-daemon
client key.

```bash
VIBERPIT_CODEX_AUTHORITY_LISTEN_ENDPOINT=https://0.0.0.0:8442
VIBERPIT_CODEX_AUTHORITY_TLS_CERT=/etc/viberpit/codex-auth/server.pem
VIBERPIT_CODEX_AUTHORITY_TLS_KEY=/etc/viberpit/codex-auth/server.key
VIBERPIT_CODEX_AUTHORITY_TLS_CLIENT_CA=/etc/viberpit/codex-auth/client-ca.pem
# Deny-by-default. Unlike the session daemon's allowlist, an EMPTY value is a
# startup error, not "permissive".
VIBERPIT_CODEX_AUTHORITY_CLIENT_ALLOWLIST=codex-edge,codex-runner:dobby,codex-runner:rocky
```

Client certificate common names carry the role, and the per-route ACL is built on
them: `codex-edge` may administer logins and push the authorization sync;
`codex-runner:<host>` may **only** request an envelope or a refresh. A certificate
presenting both identities is rejected outright.

### Edge and runner configuration

Every host that talks to the authority (the edge, and each runner) needs:

```bash
VIBERPIT_CODEX_AUTHORITY_ENDPOINT=https://dobby:8442
VIBERPIT_CODEX_AUTHORITY_CLIENT_CERT=/etc/viberpit/codex-auth/<role>.pem
VIBERPIT_CODEX_AUTHORITY_CLIENT_KEY=/etc/viberpit/codex-auth/<role>.key
VIBERPIT_CODEX_AUTHORITY_CLIENT_CA=/etc/viberpit/codex-auth/server-ca.pem
```

With `VIBERPIT_CODEX_ENABLED=true` and `VIBERPIT_CODEX_EXTERNAL_AUTH_ENABLED=true`
but no authority reachable, `build_codex_manager` returns `None` and Codex is simply
off on that host. It does **not** fall back to managed runner credentials — that
fallback would put a refresh token back on a session host, which is the property
this service exists to remove.

**`VIBERPIT_CODEX_AUTOMATIONS_ENABLED` is a third, separate flag** (default `false`),
set on the host that runs the automation loops. `VIBERPIT_CODEX_ENABLED` governs
sessions a human dispatched and is watching; this one governs sessions an automation
dispatches into `full-auto` with git/gh/pytest and nobody watching, so the two are
not the same decision. Turning it off forces every automation origin back to Claude
immediately, with no per-origin config to unwind — the fastest kill switch if a Codex
automation misbehaves. Which origins may use it at all is a code-declared ceiling
(`server/automations/engine_policy.py`), not an env var; as of Phase 7 Task 7.1 every
origin is `claude_only`, so this flag currently changes nothing on its own.

### Verifying the isolation

The claim is that the session user cannot reach the credential store. Check it:

```bash
sudo -u tony test -r /var/lib/ccswitch-codex-auth/accounts && echo BROKEN || echo ok
sudo ls -ld /var/lib/ccswitch-codex-auth        # expect drwx------ ccswitch-auth
```

The service also asserts this itself on every login (`CredentialStore.assert_private`)
and refuses to publish a credential from a group- or world-readable home.

### Revoking an account

Order matters, and the API enforces it: **revoke at the authority, fan invalidation
out to every runner, then write the manifest.** A partial fan-out returns HTTP 502
with `degraded: true` rather than reporting the account disabled — some host may
still be running an app-server with a live envelope. Retry until it is clean.

Restarting the authority is cheap: it holds no sessions, cancels in-flight device
logins, and the persisted generation counter means a restart cannot re-mint a number
an in-flight runner is still fencing against.

### Installing the Codex CLI on a runner

```bash
bash scripts/fetch-codex.sh          # download, verify, install
bash scripts/fetch-codex.sh --record # pin a new release's digest (trusted host, once)
```

Three independent checks, so no single one can be waived into running the wrong
bytes: the artefact's SHA-256 must match `scripts/codex-cli.sha256`;
`codex --version` must equal `server/codex/schema.py:PINNED_CLI_VERSION`, which the
script *reads* from that module rather than restating; and the daemon separately
re-generates and hashes the app-server JSON schema before offering Codex at all.
Until a digest is recorded the script fails closed rather than installing
unverified bytes — see the header of `scripts/codex-cli.sha256` for why a
placeholder digest would be worse than none.

Point the daemon at it with `VIBERPIT_CODEX_BIN`. A host without Codex is not a
broken host: `probe_availability` reports `binary_not_found` and the daemon keeps
serving Claude sessions.

### Startup doctor

```bash
python -m server.codex.doctor        # JSON report; exit 0 when nothing failed
```

Seven named verdicts for the seven independent ways a Codex runner can be
misconfigured — `external_auth`, `binary`, `runtime_home`, `authority`,
`mtls_identity`, `manifest`, `sandbox` — so "Codex isn't offered" stops being one
symptom with six causes. The daemon runs it once at every boot and logs it as a single
`codex.doctor.report` record; it runs as a supervised background task and can
never delay or fail a boot. It never prints a token-bearing value.

**`sandbox: userns_restricted`** — Codex is offered and sessions start, but every
command in an asking mode fails before it runs (`bwrap: loopback: Failed
RTM_NEWADDR: Operation not permitted`, #2919). The host restricts unprivileged user
namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`), and Codex falls back
to its vendored `bwrap` when none is on PATH — only `/usr/bin/bwrap` is covered by
the AppArmor exemption. Fix the host, not the session:

```bash
sudo apt install bubblewrap                                # provides /usr/bin/bwrap
sudo aa-enforce /etc/apparmor.d/bwrap-userns-restrict      # ships disabled
codex sandbox -c 'sandbox_mode="workspace-write"' -- true  # exit 0 = fixed
```

`full-auto`/`bypassPermissions` run unsandboxed and are unaffected either way.

Metrics are in `server/obs/metrics.py` (`codex_*`) — the runbook's § 8 lists what
each one is worth alerting on. No label carries a native thread id, prompt,
command, or token.

### "Codex isn't offered" — where to look

`python -m server.codex.doctor` on the runner answers most of this directly. When
you need the fleet-level view instead: Codex is offered only when the edge has an
authority **and** some enabled, healthy runner advertises a usable Codex boundary. Each daemon reports its own verdict on
`GET /info` under `engines`, the fleet probe stores it on the host row, and both the
Fleet settings table and `GET /api/config` render it. Work down in this order:

1. **`/api/config` → `engines[codex].blockedReason`.** `authority_unconfigured` means
   the *edge* has no `VIBERPIT_CODEX_AUTHORITY_*` settings; no runner can help, because
   runners authenticate only with authority-minted envelopes. Fix the edge and stop here.
2. **Fleet settings → the per-host Codex column** (or `engines[codex].unavailableReasons`).
   The reason is the runner's own typed verdict: `disabled` (`VIBERPIT_CODEX_ENABLED`
   unset), `external_auth_disabled` (`VIBERPIT_CODEX_EXTERNAL_AUTH_ENABLED` unset),
   `binary_not_found` (install `codex`, or set `VIBERPIT_CODEX_BIN`),
   `version_probe_failed`, `unsupported_version`, or `runtime_home_refused`.
3. **`engine_not_advertised`** means that daemon predates `/info.engines` — it is a
   legacy runner mid-upgrade, read as Claude-only. Deploy it.
4. **`available: true` but `dispatchable: false`** with `contract_hash_mismatch` is the
   mixed-version case: the *runner* verified a Codex app-server contract this *edge* is
   not pinned to. One of the two is ahead; the edge's pin is
   `server/codex/schema.py:PINNED_CONTRACT_SHA256`.

None of this affects host health. A runner with no Codex binary stays `healthy`, keeps
taking Claude sessions, and remains a deploy target — Codex unavailability is dropped
from Codex dispatch only.

## Deploy controller (`viberpit-deploy-controller.service`)

Long-lived service that polls `origin/master` (notify-only) and runs user-triggered deploys.
Serves a UDS at `/run/viberpit/deploy.sock`; the edge proxies `/api/deploy/*` to it.

A triggered deploy runs seven phases, in order (`server/deploy/types.py::Phase`):
`preflight` (gate on GitHub CI green) → `save` (snapshot) → `update` (fast-forward, sync deps,
rebuild `web/dist`) → `report` → `runners` (fan out to every fleet host — see
`docs/runbooks/rocky-session-host.md` § "5. Deploy") → `restart` (the edge) → `resume`. The UI's
deploy panel and `GET /api/deploy/status` mirror this phase list.

Poll cadence defaults to **30s** (how quickly the "changes pending" banner appears after a
merge). Tune it with `VIBERPIT_DEPLOY_POLL_INTERVAL=<seconds>` in `viberpit.env`, then
`sudo systemctl restart viberpit-deploy-controller.service`. Each tick is one `git fetch` +
one `gh api` CI check, so even small values stay well under the GitHub rate limit.

**Daemon-restart cooldown.** A deploy whose diff touches daemon code drains and restarts every
live session, so the controller refuses (in `preflight`, before anything moves) a second such
deploy within `VIBERPIT_DEPLOY_DAEMON_RESTART_COOLDOWN_SEC` (default **600**; `0` disables) of the
last one, naming the time a retry will be accepted. Edge-only deploys are never held. The last
restart time is `daemonRestartedAt` in the persisted status, so it survives a controller restart.
To override for an urgent fix, start the deploy with `{"force": true}` in the
`POST /api/deploy/start` body (the UI does not send it).

**Cutover from the old auto-deploy (one-time).** The merge that ships this controller also
deletes `scripts/auto-deploy.sh`, so the old `ccswitch-deploy.timer` keeps firing against a
now-missing script (`203/EXEC`) until you disable it. The new controller is a separate systemd
unit, so it must be installed by hand — a deploy can't install its own successor:

    # 1. retire the old timer (its script is gone after the cutover deploy)
    sudo systemctl disable --now ccswitch-deploy.timer
    # 2. install + start the controller (edit the unit's WorkingDirectory/ExecStart to
    #    THIS host's deploy checkout first if it differs from the committed path)
    sudo install -m644 deploy/viberpit-deploy-controller.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now viberpit-deploy-controller.service

Until the controller runs, `/api/deploy/status` soft-degrades to "controller unavailable" and
the dashboard's deploy panel stays inert. Replaces the retired `ccswitch-deploy.timer`/`.service`
(no more auto-deploy).

## Health watchdog (`viberpit-health.timer`)

A 60s oneshot (`scripts/viberpit-health-check.sh`) that does two things the rest of the
stack cannot:

1. **Control-plane watchdog.** Probes the daemon's Unix socket `/healthz`. A daemon whose
   process is alive but whose uvicorn accept-loop has wedged is invisible to
   `Restart=on-failure` (the process never exits) — this restarts it. Guarded by a 60s
   startup grace (won't fight a booting/deploying daemon) and a 300s cooldown (a genuine
   crash-loop is left to `Restart=on-failure`).
2. **Early warning.** Alerts on disk `>=85%`, any account `status=needs-reauth`, or daemon
   memory near the unit's `MemoryMax` (92% of the value `systemctl show` reports, so a host
   drop-in that raises it is honoured; `VIBERPIT_MEM_WARN_BYTES` overrides). Alerts go to the journal; set `VIBERPIT_ALERT_WEBHOOK`
   (e.g. an ntfy topic URL) in `/etc/viberpit/viberpit.env` to also get push notifications.

Install (rewrite `ExecStart` to this host's deploy checkout first, like the daemon unit):

    sudo install -m644 deploy/viberpit-health.service /etc/systemd/system/
    sudo install -m644 deploy/viberpit-health.timer   /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now viberpit-health.timer

The daemon units size their memory limits as a **percentage of host RAM**, so one unit
fits any host: `viberpit-daemon.service` (desktop host) `MemoryHigh=55%` / `MemoryMax=65%`,
`viberpit-daemon-remote.service` (headless runner) `75%` / `88%`. `MemoryHigh` is soft
reclaim pressure below the hard `MemoryMax`, so a restart-storm of relaunched
session+MCP fleets degrades instead of being OOM-killed. Don't pin absolute values in a
host drop-in: an absolute 15G on dobby's 19 GB left the desktop too little, and session
test runs OOM-killed Chrome (2026-10-09). A `systemctl set-property` override (stored in
`/etc/systemd/system.control/`) outranks the unit, so re-apply the unit's values on a
host that has one — live, no restart:

    sudo systemctl set-property viberpit-daemon.service MemoryHigh=55% MemoryMax=65%

Both daemon units set `OOMPolicy=continue` (#2673). systemd's default, `stop`, stops the
whole unit whenever the kernel OOM-kills *any* process in its cgroup — including on a
host-wide OOM — so one session's runaway build took every live session down with it
(2026-10-04). With `continue` only the OOM victim dies; if that is the daemon process
itself, `Restart=always` brings it back. Apply it with a drop-in on every daemon host
(dobby's `viberpit-daemon.service`, and each remote runner's installed daemon unit) — a
`daemon-reload` is enough, no restart:

    sudo install -D -m644 /dev/stdin /etc/systemd/system/viberpit-daemon.service.d/oom-policy.conf <<<$'[Service]\nOOMPolicy=continue'
    sudo systemctl daemon-reload
    systemctl show -p OOMPolicy viberpit-daemon.service   # expect continue

**Per-session memory attribution** (#2739). The kernel's OOM line names only a bare pid
inside the daemon cgroup. Every 30 s the daemon sums resident memory per direct child
(each live `claude` CLI and everything beneath it) and, past 4 GiB, logs a WARNING
`session.memory_high` with the owning `session`, `rootPid`, `rssMb` and the `top` three
processes — so a runaway `node` is traceable before the OOM kill, not after. Throttled to
one record per root per 10 min unless it grows 1.5×. Knobs: `VIBERPIT_SESSION_MEMORY_WARN_MB`,
`VIBERPIT_SESSION_MEMORY_INTERVAL_S`; `VIBERPIT_SESSION_MEMORY_WATCH=off` disables it.
The same pass raises every process in each session subtree to `oom_score_adj`
`VIBERPIT_SESSION_OOM_SCORE_ADJ` (default 500, `0` disables), so a host-wide OOM kills a
session's runaway `pytest`/`dotnet`/`node` before Chrome (300) or desktop apps (200).
A fast path marks the daemon's direct children (fresh CLI roots, via
`/proc/<pid>/task/*/children`) every `VIBERPIT_SESSION_OOM_MARK_INTERVAL_S` (default 2 s),
so everything a session spawns inherits the score at fork.

**Unit drift is reported, not fixed, by deploys** (#2702). After each runner deploys, it
compares the safety directives in `DAEMON_UNIT_GUARDS` (`server/deploy/steps.py`:
`KillMode`, `OOMPolicy`) between the committed unit (`viberpit-daemon.service` for the
local daemon, `viberpit-daemon-remote.service` for remote runners) and the installed one
(`systemctl show`, over ssh for remote hosts). A mismatch becomes a deploy warning naming
the host, unit and directive. The deploy still succeeds, because the fix needs sudo on that
host and retrying the deploy cannot supply it.

## Assistant nightly eval (`viberpit-assistant-eval.timer`)

A nightly oneshot (03:30 local, up to 15 min jitter, `Persistent=true`) that runs
`scripts/eval_assistant.py` — the AI assistant's full case corpus
(`docs/superpowers/evals/assistant/cases.jsonl`, ~126 cases, each tagged with a capability
pack) as real one-shot assistant turns against an in-memory fixture. It needs no edge or
daemon, only an enabled Anthropic account on the host (`pick_helper_config_dir()`), so
**it spends real account quota every night**: one sonnet turn plus one haiku judge call
per case. That is why it is opt-in and not wired into CI — no CI runner has the accounts.

Install (rewrite `WorkingDirectory` / `ExecStart` to this host's deploy checkout first,
like the daemon unit):

    sudo install -m644 deploy/viberpit-assistant-eval.service /etc/systemd/system/
    sudo install -m644 deploy/viberpit-assistant-eval.timer   /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now viberpit-assistant-eval.timer

Kick a run by hand with `sudo systemctl start viberpit-assistant-eval.service` and follow
it with `journalctl -u viberpit-assistant-eval -f`.

Results land in `/home/tony/.claude-accounts/evals/assistant/results-<date>-<suffix>.md`
— outside the deploy worktree, so a run never dirties it. The newest file **is the
record**: its header line carries the overall pass rate and a `| pack | cases | passed |
pass rate |` table breaks it down per pack. There is no metrics push path for a oneshot,
so nothing is exported to `/metrics`. The unit exits non-zero (shows `failed` in
`systemctl status`) when the pass rate is under the script's 90% threshold or a case
could not run. Operator-tier cases (pack O) only run with `--operator`, which the unit
does not pass.

## Hardware watchdog reachability test (`deploy/watchdog/test-reachability.sh`)

This is a **host-level** hardware-watchdog test, separate from the application health
timer above. It closes the gap that let rocky sit off the network for ~46 min on
2026-08-25 while its `watchdog.service` ran happily the whole time
([#767](https://github.com/steiner385/viberpit/issues/767)): `test-ssh.sh` (below) only
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
        /etc/viberpit/watchdog-reachability.conf
    sudo $EDITOR /etc/viberpit/watchdog-reachability.conf   # set WDR_TARGETS

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

## Remote mounts (`viberpit-mounts.service`, remote session host only)

rocky runs the sessions; **dobby is canonical** for the account store, the shared
`~/.claude` assets, and the archive dir (spec decision 4). rocky reaches all of them
over sshfs across the tailnet. Without the mounts the daemon starts but every session
fails: no credentials, the wrong skill set, and archiving dies with `Permission denied`
on `/mnt/hdd-storage`.

`viberpit-daemon.service.d/require-mounts.conf` binds the daemon to the mount unit with
`Requires=` + `After=`. That `Requires=` is **load-bearing** — #765 was the daemon
starting into a *dead* sshfs mount and dying with
`OSError: [Errno 5] Input/output error: '/home/tony/.claude-accounts'`. Do not loosen it
to `Wants=`.

The cost of `Requires=` is that a failed mount unit **cancels** the daemon's start job,
and a cancelled start job never engages `Restart=always`. On 2026-08-26 that took the
fleet down: rocky rebooted, `viberpit-mounts.service` ran while tailscaled was still
settling, the first sshfs attempt for `.claude-accounts` died with
`read: Connection reset by peer`, and the daemon sat dead until
`viberpit-daemon-ensure.timer` fired ~60s later. Two layers now cover it, in order:

1. **`bin-viberpit-mounts.sh` retries internally** (`VIBERPIT_MOUNT_ATTEMPTS`, default 5,
   `VIBERPIT_MOUNT_RETRY_DELAY`, default 3s) so a tailnet still settling at boot never
   fails the unit in the first place. Retries are bounded, so a genuinely-down dobby
   still fails loudly. `TimeoutStartSec=300` on the unit must stay above the worst-case
   retry budget, or systemd SIGKILLs the script mid-retry and reopens the race.
2. **`viberpit-daemon-ensure.timer`** re-starts a daemon left dead by a cancelled job (#765).

Behaviour is pinned by `server/tests/test_mounts_unit.py`, which executes the real script
against stubbed `sshfs`/`mountpoint` (a retry loop that does not retry passes a grep).

Install on the remote session host:

    sudo install -m755 deploy/bin-viberpit-mounts.sh /home/tony/bin-viberpit-mounts.sh
    sudo install -m644 deploy/viberpit-mounts.service /etc/systemd/system/
    sudo mkdir -p /etc/systemd/system/viberpit-daemon.service.d
    sudo install -m644 deploy/viberpit-daemon.service.d/require-mounts.conf \
        /etc/systemd/system/viberpit-daemon.service.d/
    sudo systemctl daemon-reload
    sudo systemctl enable --now viberpit-mounts.service

### Cache profiles: the account store must be mounted coherently (#1005)

`bin-viberpit-mounts.sh` picks one of two `-o` profiles per path:

| profile | paths | options |
|---|---|---|
| cached (read-mostly) | `~/.claude`, `/mnt/hdd-storage`, `~/avd-golden` | `dir_cache=yes,dcache_timeout=600,attr_timeout=600,entry_timeout=600` |
| coherent | `~/.claude-accounts` (`VIBERPIT_MOUNT_COHERENT_PATHS`) | `dir_cache=no,dcache_timeout=0,attr_timeout=0,entry_timeout=0` |

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

    VIBERPIT_MOUNT_FORCE_REMOUNT=1 VIBERPIT_MOUNT_PATHS=/home/tony/.claude-accounts \
      /home/tony/bin-viberpit-mounts.sh
    grep .claude-accounts /proc/mounts    # expect attr_timeout=0, no dir_cache=yes

Two further layers make this safe even where a host's mount profile is still wrong:

- **One-shot helper spawns never touch the shared config.** `account_auth.helper_env()`
  points `CLAUDE_CONFIG_DIR` at a host-local mirror under `~/.cache/ccswitch/helper-cfg`
  (`server/sessions/scratch_config.py`): `.claude.json`/`.credentials.json` are copies, the
  shared assets are symlinks, and a torn read reuses the last-known-good copy instead of
  resetting anything. Live sessions still use the real account dir — they must, since their
  history and token refreshes belong there. Kill switch:
  `VIBERPIT_HELPER_SCRATCH_CONFIG=false`.
- **The evidence survives.** `configdir.compose()` used to `rmtree` `backups/` on every call,
  destroying each `.claude.json.corrupted.*` within seconds of its creation — which is why
  three days of resets left nothing to diagnose. It now prunes only entries older than
  `VIBERPIT_CONFIG_BACKUP_RETENTION_S` (default 24h).

**Verifying the fix.** `scripts/viberpit-health-check.sh` section 6 alerts on either counter
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
