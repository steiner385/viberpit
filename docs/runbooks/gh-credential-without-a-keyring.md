# Giving a host a `gh` credential that survives a reboot

**Symptom.** Starting shortly after a reboot, the journal fills with

```
github.host_gh_unauthenticated: this host's ambient gh credential is missing, so every
gh call is unauthenticated (401 on writes, IP rate-limit 403 on reads). …
```

and, before the collapse landed, one WARNING per target instead — `github.issue_comment_failed
repo=… #1713: gh: Requires authentication (HTTP 401)`, `github.issue_states_failed …: gh: API rate
limit exceeded for <public IP> … (HTTP 403)`. The named issues/PRs are fine; nothing is wrong with
any of them. Every gh-backed automation on the host is degraded at once: `pr_poller`, `pr_status`,
`merge_queue`, `deploy_monitor`, `backlog_runner` issue writes, error triage, the CI/CD Doctor.

**Two neighbours you will see beside it, and this runbook is not for either.** All three come from
the same collapse point (`server/github/host_failures.py`) on the `server.github.host` logger, so
they read alike in the journal — but they are separate conditions with separate fixes, which is
exactly why they are separate event names (#1922–#1927, #1941):

| event | means | what to do |
|---|---|---|
| `github.host_gh_unauthenticated` | no usable ambient credential | this runbook |
| `github.host_gh_unreachable` | the request never reached GitHub | check the host's network, then <https://www.githubstatus.com>; it clears itself |
| `github.host_gh_rate_limited` | the token worked and is over budget | wait for the window to reset; if it recurs, look at poll intervals |

Each reports at most once per six hours while the condition holds (`LogThrottle`); the per-target
detail is still there at DEBUG.

## Why it happens

On dobby, `gh`'s token lives **only in the GNOME login keyring**, and gdm's autologin cannot unlock
it. Every boot:

```
gdm-autologin: gkr-pam: no password is available for user
gkr-pam: couldn't unlock the login keyring.
```

`~/.config/gh/hosts.yml` carries no `oauth_token` (just `users: {steiner385: {}}`), and
`gh auth status` says `✓ Logged in to github.com account steiner385 (keyring)`. Meanwhile
`gitrepos._run_gh` strips `GITHUB_TOKEN`/`GH_TOKEN` on purpose (a stray bot login in the shared gh
config would otherwise hijack the identity), and the daemon unit sets no `VIBERPIT_GH_CONFIG_DIR` —
so there is **no file-backed fallback credential**. From boot until a human types the passphrase in
the desktop unlock dialog, every ambient gh call runs unauthenticated. `steiner385/viberpit` is
private, so that means 401 on writes and the 60-requests/hour **unauthenticated IP** bucket on
reads.

It has previously self-healed in 2–4 minutes only because someone happened to be at the machine. On
an unattended reboot the window is open-ended.

### Confirming it, in ten seconds

```bash
journalctl -b 0 | grep -E 'gkr-pam|completed password prompt'   # is the keyring still locked?
gh auth status                                                  # "(keyring)" = no file-backed token
grep -c oauth_token ~/.config/gh/hosts.yml                      # 0 = nothing on disk
```

Compare the `gkr-pam` timestamp against the first failure. `journalctl --since/--until` parse as
**local** time even when you ask for `--utc` output.

Reproduce without changing any state — make the secret service unreachable and gh silently drops to
unauthenticated:

```bash
env DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent/bus gh api user
# gh: Requires authentication (HTTP 401)   ← byte-for-byte the logged string
```

## What the code does about it (already landed)

The `gh_credential` module classifies that stderr as one condition, reports it **once** (with a 6-hour
heartbeat while it holds), and holds ambient gh traffic to **one probe per 60 s** until a call
succeeds. So the locked window costs ~60 probe requests/hour instead of every loop's full tick rate,
and normal service resumes within a minute of the keyring being unlocked, with no operator action.

That is damage control, not a fix: while it holds, gh-backed automation is still not working. Kill
switch `VIBERPIT_GH_UNAUTH_BACKOFF=false` disables the back-off only — detection and the log record
stay live.

## The fix — pick one (owner action; both need a decision)

### Option 1 — file-backed gh config for the daemon (preferred)

The code path already exists: `gitrepos._gh_env()` honours `VIBERPIT_GH_CONFIG_DIR`, and the
multi-host fleet design already assumes a per-host gh config dir (it still spells the path with the
pre-rebrand directory name; either path works — the env var decides).

```bash
# 1. Auth into a dedicated config dir with storage on disk rather than in the keyring.
export GH_CONFIG_DIR=~/.config/viberpit/gh
mkdir -p "$GH_CONFIG_DIR" && chmod 700 "$GH_CONFIG_DIR"
gh config set -h github.com secure_storage false
gh auth login --hostname github.com --git-protocol https --web   # interactive; owner only

# 2. Confirm the token actually landed in the file, not the keyring.
grep -q oauth_token "$GH_CONFIG_DIR/hosts.yml" && chmod 600 "$GH_CONFIG_DIR/hosts.yml"
GH_CONFIG_DIR=~/.config/viberpit/gh gh auth status     # must NOT say "(keyring)"

# 3. Point the services at it. BOTH env files — see the warning below.
systemctl cat viberpit-daemon.service | grep EnvironmentFile   # which file does the DAEMON load?
systemctl cat viberpit.service        | grep EnvironmentFile   # and the EDGE?
```

> **The daemon and the edge do not necessarily read the same env file, and the daemon is the one
> running the failing loops.** On dobby a drop-in (`viberpit-daemon.service.d/env-file.conf`) *resets*
> `EnvironmentFile=` and then sets `/etc/viberpit/daemon.env`, so the daemon never reads the edge's
> `/etc/viberpit/viberpit.env` at all. Add the line to **each** file the two units actually load, or
> the storm continues from the daemon with the edge looking fixed.

```bash
sudo sh -c 'echo VIBERPIT_GH_CONFIG_DIR=/home/tony/.config/viberpit/gh >> /etc/viberpit/daemon.env'
sudo sh -c 'echo VIBERPIT_GH_CONFIG_DIR=/home/tony/.config/viberpit/gh >> /etc/viberpit/viberpit.env'
```

Restarting `viberpit.service` (the edge) is cheap. **`viberpit-daemon.service` owns every live
`claude` process** — read `deploy/README.md` § "Stopping / restarting the daemon" first and do it
when the Sessions page shows nothing mid-turn. Confirm afterwards with
`journalctl -u viberpit-daemon -f | grep host_gh_credential_restored`, or simply that the
`host_gh_unauthenticated` record stops recurring after the next reboot.

**The trade-off (constitution Principle IV).** This moves the token from a keyring — encrypted at
rest, unlocked per login session — to a `0600` file readable by anything running as `tony`, which
includes every managed session. The scope is a GitHub OAuth token for the user's repos. It is the
option the code was designed for, and the multi-host fleet needs it anyway (a headless runner has no
desktop keyring at all), but it is a genuine posture change and it is the owner's call to make.

### Option 2 — make autologin unlock the keyring

A blank keyring passphrase, or a PAM/`gnome-keyring` change so gdm's autologin unlocks it. Needs
root, and it weakens at-rest protection for **every** secret in that keyring — not just gh's — which
is a wider blast radius than option 1 for the same class of exposure.

### Not an option

Setting `GITHUB_TOKEN`/`GH_TOKEN` in the unit environment. `_run_gh` strips both by design, and
undoing that reintroduces the identity-hijack bug it exists to prevent (#913).

## Related

- `docs/runbooks/onboarding-a-session-runner.md` — a new host needs this done as part of setup.
- The transient sibling: a *momentary* keyring stall under memory pressure shows up as deploy
  preflight's `could not read Username for 'https://github.com'` (`server/deploy/git.py`). Same
  single-credential-source weakness, different duration; that one self-heals on a retry.
