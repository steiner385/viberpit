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

## Install

```bash
sudo install -m644 deploy/ccswitch.service          /etc/systemd/system/
sudo install -m644 deploy/ccswitch-daemon.service   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ccswitch.service
```

## Operate

```bash
systemctl status ccswitch.service                 # server health
journalctl -u ccswitch.service -f                 # server logs
journalctl -u ccswitch-daemon.service -f          # daemon logs
# Force a deploy now:
PYTHONPATH=<repo>/src <repo>/.venv/bin/python -m server.deploy --once
```

## Notes

- **Backend dependency changes** (new Python deps) trigger `pip install -e .[server]`
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

## Deploy controller (`ccswitch-deploy-controller.service`)

Long-lived service that polls `origin/master` (notify-only) and runs user-triggered deploys.
Serves a UDS at `/run/ccswitch/deploy.sock`; the edge proxies `/api/deploy/*` to it.

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
