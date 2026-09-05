# Deploy — ccswitch web (self-hosted)

The server runs as a **systemd service** on the Tailscale host.

## Components

| Unit | Role |
|------|------|
| `ccswitch.service` | Long-running uvicorn server (`server.app:app`), serves the API + built `web/dist`. |
| `ccswitch-daemon.service` | Session daemon (`python -m server.ipc`) — owns live `claude` PTY sessions in remote mode. |
| `ccswitch-codex-auth.service` | ChatGPT credential authority (`python -m server.codex.auth_main`) — the only holder of OpenAI refresh tokens. Separate OS user, separate mTLS trust domain, no failover. |

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

## HTTPS via nginx + tailscale cert (required once per host)

The edge server binds to `127.0.0.1:8787` (loopback). To expose it over HTTPS on the
Tailscale network — required for browser secure-context APIs such as microphone access —
run this once on the host (nginx must already be installed):

```bash
sudo tailscale cert \
    --cert-file /etc/ccswitch/dobby.tail0dd570.ts.net.crt \
    --key-file  /etc/ccswitch/dobby.tail0dd570.ts.net.key \
    dobby.tail0dd570.ts.net
sudo cp deploy/ccswitch-tailnet.nginx /etc/nginx/sites-available/ccswitch-tailnet
sudo ln -sf /etc/nginx/sites-available/ccswitch-tailnet /etc/nginx/sites-enabled/
sudo nginx -t && sudo nginx -s reload
sudo cp deploy/ccswitch-cert-renew.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ccswitch-cert-renew.timer
```

Access ccswitch at `https://dobby.tail0dd570.ts.net/`. Tailscale provisions the TLS
certificate; `ccswitch-cert-renew.timer` renews it weekly and reloads nginx automatically.

To verify the nginx vhost is active:

```bash
sudo nginx -t
systemctl status ccswitch-cert-renew.timer
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

## AI assistant (on by default)

The assistant drawer (`docs/product/features/ai-assistant.md`) is **enabled by default** as of
the Phase 4 launch — an edge with none of these vars set serves it. Nothing needs installing:
it reuses the normal session stack, so there is no extra venv extra and no model download.

| Var | Default | What it does |
|---|---|---|
| `CCSWITCH_ASSISTANT_ENABLED` | `true` | Kill switch. `false` → `POST /api/assistant/session` returns 404 `ASSISTANT_DISABLED`, `/api/assistant/status` reports `enabled: false`, and the drawer falls back to its placeholder. Note it stops *new* sessions: an already-running assistant keeps its driver until the normal idle sweep closes it (below), so disable then wait out `CCSWITCH_ASSISTANT_IDLE_S` if you need the drivers gone. |
| `CCSWITCH_ASSISTANT_MODEL` | `sonnet` | Model new assistant sessions spawn on. A tier that needs approval is refused with 400 `MODEL_GATED` rather than silently downgraded. |
| `CCSWITCH_ASSISTANT_IDLE_S` | `900` | Idle grace before an assistant's driver is closed. **Also sets the assistant bearer token's max age (`idle_s × 4`, `server/auth/middleware.py`)** — raising it lengthens a credential's lifetime, so treat it as a security knob, not just a timeout. |
| `CCSWITCH_ASSISTANT_MAX_TURNS` | `30` | SDK turn cap per assistant session. |
| `CCSWITCH_ASSISTANT_MAX_PROMPTS_PER_HOUR` | `120` | Per-identity prompt rate limit; over it the WS replies `promptRejected` / `RATE_LIMITED`. |

**The Settings toggle overrides the env var, not the other way round.** `assistant_enabled` in
`server/assistant/policy.py` reads `app_settings.assistant_enabled` first and only falls back to
the env when that column is NULL. So once anyone has flipped Settings → General → AI assistant,
editing `CCSWITCH_ASSISTANT_ENABLED` in `ccswitch.env` has **no effect** — to disable a deployment
for good, turn the switch off in Settings (or `UPDATE app_settings SET assistant_enabled = false
WHERE id = 'singleton';`). Set it back to NULL to hand control to the env var again.
`CCSWITCH_ASSISTANT_MODEL` / `assistant_model` follow the same precedence.

Check what a running edge actually resolved — this is the assistant's analogue of `sttEnabled`,
and unlike STT it is **not** in `GET /api/config`, because the answer depends on DB state:

```bash
curl -s https://dobby.tail0dd570.ts.net/api/assistant/status   # {"enabled":true,"sessionId":…,"model":"sonnet"}
```

Assistant sessions are ordinary sessions with `kind == "assistant"`, hidden from the session
list and scoped to the identity that created them. Their only tools are the in-process
`ccswitch` MCP server, whose calls are additionally fenced by a deny-by-default route
allowlist (`ASSISTANT_ROUTE_ALLOW` in `server/assistant/policy.py`) enforced *before* routing;
the bearer token is loopback-only and never persisted. Metrics to scrape:
`assistant_prompts_total`, `assistant_tool_calls_total{tool,class,outcome}`,
`assistant_ttft_ms`, `assistant_turn_ms`, `assistant_sessions_live`.

See also the `ix_sessions_kind_owner` index note under "Notes" below — the assistant works
without it, but wants it once session volume grows.

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
- **Migrations never run automatically** (see "Notes" under the AI assistant migration,
  `server/db/migrations/versions/0022_assistant_sessions.py`): `_reconcile_columns` ALTER-ADDs the
  new nullable columns on both dialects at startup, but reconcile never creates indexes on an
  existing table. `ix_sessions_kind_owner` (sessions.kind, sessions.owner_identity) must be created
  by hand on the live Postgres DB after this deploy:
  `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sessions_kind_owner ON sessions (kind,
  owner_identity);` — the assistant works without it, just slower once session volume grows.

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

## ChatGPT credential authority (`ccswitch-codex-auth.service`)

`ccswitch-codex-auth.service` runs `python -m server.codex.auth_main` — the **only**
process in the fleet that holds an OpenAI/ChatGPT *refresh* token. Runners
(dobby's daemon, rocky's worker) never hold one: they fetch short-lived access-token
envelopes over mutual TLS and hand them straight to `codex app-server` over stdin.

This is a separate trust domain from the session daemon, and the separation is the
whole point:

* it runs as its own OS user (`ccswitch-auth`), so the user that runs `claude` and
  `codex app-server` cannot read the credential store;
* it has its own key material and its own env file — reusing a
  `CCSWITCH_SESSIOND_*` key is refused at startup, not merely discouraged;
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
sudo install -m644 deploy/ccswitch-codex-auth.sysusers \
     /usr/lib/sysusers.d/ccswitch-codex-auth.conf
sudo systemd-sysusers
sudo install -m644 deploy/ccswitch-codex-auth.service /etc/systemd/system/
sudo systemctl daemon-reload
# Enable only once /etc/ccswitch/codex-auth.env exists (see below):
# sudo systemctl enable --now ccswitch-codex-auth.service
```

### `/etc/ccswitch/codex-auth.env` (root-only, NOT committed)

Deliberately a separate file from `/etc/ccswitch/ccswitch.env`: the authority has no
business inheriting the edge's database URL, GitHub credentials, or session-daemon
client key.

```bash
CCSWITCH_CODEX_AUTHORITY_LISTEN_ENDPOINT=https://0.0.0.0:8442
CCSWITCH_CODEX_AUTHORITY_TLS_CERT=/etc/ccswitch/codex-auth/server.pem
CCSWITCH_CODEX_AUTHORITY_TLS_KEY=/etc/ccswitch/codex-auth/server.key
CCSWITCH_CODEX_AUTHORITY_TLS_CLIENT_CA=/etc/ccswitch/codex-auth/client-ca.pem
# Deny-by-default. Unlike the session daemon's allowlist, an EMPTY value is a
# startup error, not "permissive".
CCSWITCH_CODEX_AUTHORITY_CLIENT_ALLOWLIST=codex-edge,codex-runner:dobby,codex-runner:rocky
```

Client certificate common names carry the role, and the per-route ACL is built on
them: `codex-edge` may administer logins and push the authorization sync;
`codex-runner:<host>` may **only** request an envelope or a refresh. A certificate
presenting both identities is rejected outright.

### Edge and runner configuration

Every host that talks to the authority (the edge, and each runner) needs:

```bash
CCSWITCH_CODEX_AUTHORITY_ENDPOINT=https://dobby:8442
CCSWITCH_CODEX_AUTHORITY_CLIENT_CERT=/etc/ccswitch/codex-auth/<role>.pem
CCSWITCH_CODEX_AUTHORITY_CLIENT_KEY=/etc/ccswitch/codex-auth/<role>.key
CCSWITCH_CODEX_AUTHORITY_CLIENT_CA=/etc/ccswitch/codex-auth/server-ca.pem
```

With `CCSWITCH_CODEX_ENABLED=true` and `CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED=true`
but no authority reachable, `build_codex_manager` returns `None` and Codex is simply
off on that host. It does **not** fall back to managed runner credentials — that
fallback would put a refresh token back on a session host, which is the property
this service exists to remove.

**`CCSWITCH_CODEX_AUTOMATIONS_ENABLED` is a third, separate flag** (default `false`),
set on the host that runs the automation loops. `CCSWITCH_CODEX_ENABLED` governs
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

Point the daemon at it with `CCSWITCH_CODEX_BIN`. A host without Codex is not a
broken host: `probe_availability` reports `binary_not_found` and the daemon keeps
serving Claude sessions.

### Startup doctor

```bash
python -m server.codex.doctor        # JSON report; exit 0 when nothing failed
```

Six named verdicts for the six independent ways a Codex runner can be
misconfigured — `external_auth`, `binary`, `runtime_home`, `authority`,
`mtls_identity`, `manifest` — so "Codex isn't offered" stops being one symptom
with six causes. The daemon runs it once at every boot and logs it as a single
`codex.doctor.report` record; it runs as a supervised background task and can
never delay or fail a boot. It never prints a token-bearing value.

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
   the *edge* has no `CCSWITCH_CODEX_AUTHORITY_*` settings; no runner can help, because
   runners authenticate only with authority-minted envelopes. Fix the edge and stop here.
2. **Fleet settings → the per-host Codex column** (or `engines[codex].unavailableReasons`).
   The reason is the runner's own typed verdict: `disabled` (`CCSWITCH_CODEX_ENABLED`
   unset), `external_auth_disabled` (`CCSWITCH_CODEX_EXTERNAL_AUTH_ENABLED` unset),
   `binary_not_found` (install `codex`, or set `CCSWITCH_CODEX_BIN`),
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
