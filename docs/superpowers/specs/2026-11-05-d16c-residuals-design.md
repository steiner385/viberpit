# D-16c residuals: live `reload_plugins` push, Directory review verified live, DNS pinning — design

Date: 2026-11-05. Issue #3314 (split out of #3312). Parent feature: `docs/product/features/plugin-marketplace.md`.
Probe record for every binary/API claim below: `2026-11-05-d16c-residuals-verification.md` (constitution III).

## Intent

D-16c ships plugin install/update/enable/disable fanned out to every host, but (1) a change reaches only
new and restarted sessions, and (2) `plugin-marketplace.md` § Security lists four residual risks the plan
left for an owner ruling or a real-binary probe. This design closes what can be closed from user space,
records why the rest cannot, and leaves one ruling for the owner.

## Goals

1. **Live push.** An owner who installs, updates or toggles a plugin can push it into running sessions
   on every host, per session, without a respawn, and see per-session outcomes.
2. **Directory review verified live.** An install from the Anthropic Directory is gated on the Directory
   API's review state for that listing, not on the forgeable cache file in the shared config dir.
3. **DNS pinning.** The address a marketplace source resolved to at the SSRF check is the address the
   CLI's `git clone`/`fetch` connects to, on every host, for `marketplace add` and `marketplace update`.
4. **A recorded ruling** on the one residual that no user-space change closes (same-host tampering).

## Non-goals / anti-goals

- No `-y`, `--accept-command`, managed-settings writes, or any root action. The daemon stays unprivileged.
- No change to what may be installed (D1), to fan-out semantics (D2) or auto-update (D3).
- The push is never automatic. It costs prompt cache, so it is **opt-in per action** (my call: default off,
  the owner can reverse it; see "Left to the owner").
- No reload mid-turn: a session whose turn is running or awaiting a card is skipped, never interrupted.
- Browse's "Reviewed" badge is still read from the cache (3,605 listings; verifying each per browse is not
  worth an API round trip). Only the install/update gate is verified live.

## Facts the design rests on (all probed, 2.1.318)

- `reload_plugins` (`@283016339`) takes `hold_on_cache_impact` and `add_plugins`. Reply:
  `{commands, agents, plugins[], mcpServers, error_count, held?, cache_impact?}`.
- The hold (`$te`, `@304823890`): `wouldInvalidateCache = (mcpServersAdded|mcpServersRemoved|lspToolChange)
  && !toolSearch && totalOutputTokens() > 0`. So (a) only MCP-server and LSP tool-list changes count,
  never commands/skills/hooks; (b) a session that has not produced output tokens is never held. A held
  reply keeps the old plugins and carries `cache_impact: {mcp_servers_added, mcp_servers_removed,
  lsp_tool_change}`.
- The Python SDK (0.2.105) has no `reload_plugins` method; it goes through `_query._send_control_request`
  like `apply_flag_settings` and rewind.
- The Directory: `GET https://api.anthropic.com/api/directory/plugins/{id}` is **public, unauthenticated**
  (the CLI exempts it from essential-traffic-only as `publicPluginDirectoryCatalog`, `auth: "none"`) and
  returns one listing with `checks.review.state` and `source.repository.url`. The list form pages 100 at a
  time (`next_page`), no id/slug filter.
- The CLI clones with the system `git` on `PATH`, replacing `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*` with its
  own 40 entries, but it does not touch `GIT_CONFIG_GLOBAL`. A global config with `http.curloptResolve`
  pins the clone (probed: pinned to 127.0.0.1 the clone failed on the local listener; unpinned it reached
  GitHub). git 2.53 on both hosts. `http.followRedirects=false` breaks GitLab (301), so redirects stay.

## Design

### 1. Live push (`reload_plugins`)

- **Driver** `ClaudeAgentDriver.reload_plugins(*, hold_on_cache_impact: bool) -> dict`. Refuses
  `RELOAD_UNAVAILABLE` (no live client, no control channel), `RELOAD_BUSY` (`rewind_busy()`: a turn is
  starting, running, or waiting on a card), `RELOAD_REFUSED` (a control `error` reply), `RELOAD_FAILED`
  (no reply). Sends the raw request with a 60 s timeout (plugins may start MCP servers). Pure helpers in
  `server/sessions/plugin_reload.py`: codes, `ReloadError`, `request(hold)`, `outcome(reply)` that reduces the
  reply to `{outcome: applied|held, plugins: [names], errorCount, cacheImpact?}`.
- **Orchestrator (this host)** `reload_plugins_live(hold_on_cache_impact: bool) -> list[dict]`: every live
  driver (`_drivers ∩ _records`) that has `reload_plugins` (Claude only; Codex has no plugins), concurrently,
  each isolated. Row: `{sessionId, name, host, outcome: applied|held|busy|skipped|failed, failureCode?,
  message?, cacheImpact?, plugins?}`. Detached/ended records are not in `_drivers`, so they never appear.
- **RPC** `reload_plugins_live` in `_SIMPLE`. **Fleet** `reload_plugins_everywhere(hold)` mirrors
  `plugin_op_everywhere`: every registered host, an unreachable/old/timed-out host is one row
  `{host, outcome: failed, failureCode: host_unreachable|daemon_outdated|host_timeout}`; never
  `_MERGE_ALL_HOSTS` (that drops a dead worker).
- **Route** `POST /library/plugins/reload {holdOnCacheImpact?: bool = true}` → human gate
  (`trust_local_host=False`) → fan-out → audit `library_plugins_reloaded {hold, sessions}` → `{op:
  "reload_plugins", ok, sessions: [...]}`. `ok` = at least one row and no `failed` row. Not behind the
  install kill switch (it installs nothing; like enable/disable it applies state already on disk).
- **Web.** The consent card gets a checkbox "Also reload running sessions now" (default off). When ticked,
  a successful install/update (any host `ok`) is followed by the reload call, and a per-session outcome list
  renders under the host outcomes (`SessionOutcomes`). The Plugins section header gets a "Reload running
  sessions" button for toggles and for a later push. Copy: the hold is explained once ("A session that has
  started talking and whose tools would change keeps its plugins until it restarts, so its prompt cache
  is kept. Sessions mid-turn are skipped."). The restart copy stays for sessions not reloaded.

### 2. Directory review verified live

- `server/library/directory_api.py`: `fetch_listing(listing_id) -> dict | None` (httpx, 15 s, no auth,
  `User-Agent: viberpit`), and a module seam tests replace. Only the install/update gate calls it.
- `plugin_policy.check_directory_listing(cache_listing, api_listing)` (pure): refuse `PLUGIN_UNREVIEWED`
  unless the API listing's `id` equals the cache listing's, its review state is `done` **and** its
  `source.repository.url` normalizes equal to the cache listing's (a forged cache can point a reviewed id
  at another repo; the CLI installs from the cache's source). `PLUGIN_DIRECTORY_UNAVAILABLE` (503) when
  the API cannot be reached or answers anything but a listing: fail closed, the owner retries.
- `_check_install` on the edge: cache check (unchanged) then the live check for `@anthropic-plugin-directory`.
  Hosts keep their cache re-check (a forged host cache is the same-host residual).

### 3. DNS pinning for marketplace clones

- `plugin_policy.check_marketplace_source` gains a sibling `resolved_pin(source) -> str | None`: for an
  `https://` source, `"<host>:443:<addr>,<addr>"` from the same resolve the check does (the check already
  refuses internal/unresolvable); `None` for `owner/repo` (github.com is not checked today and is not
  pinned) and for anything the check refuses.
- `plugin_cli._env` takes an optional `git_pin`; when set it writes a per-run temp global config
  (`[include] path = ~/.gitconfig` so credential helpers survive, then `[http] curloptResolve = <pin>`) and
  sets `GIT_CONFIG_GLOBAL`. The file is removed after the run.
- `marketplace_add` pins the vetted source. `marketplace_update` pins the host's configured source
  (`expected_source` → `https:` normalized form → host), which also re-applies the SSRF check on update
  (today none runs). Unit tests cover the file contents and env; a `slow` contract test behind
  `VIBERPIT_TEST_NETWORK=1` drives the real CLI with a pin to 127.0.0.1 and asserts the clone never reached
  GitHub.

### 4. Same-host tampering (ruling, not code)

No change the daemon can make closes it: the session and the daemon share one uid, so a read-only clone,
`chattr`, or a managed-settings file (`/etc/claude-code/managed-settings.json`, root) is either reversible
by the session or outside the daemon's reach, and `strictKnownMarketplaces`/`blockedMarketplaces` gate
`marketplace add` only, never a later edit of the clone or its `.git/config`. The structural fix is a
privilege boundary between sessions and the shared config dir (a sandbox or a second uid), which is a
separate project. Documented as accepted-unless-ruled-otherwise; the issue stays open for the ruling.

## Left to the owner

- Whether the live push stays opt-in per action (built, recommended) or becomes the default after an
  install (one-line flip of the checkbox default).
- The same-host tampering ruling above.
