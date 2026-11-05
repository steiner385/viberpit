---
slug: plugin-marketplace
parent: config-library-snapshots
title: Plugin Marketplace (Settings › Plugins)
state: live
links:
  specs:
    - docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md
    - docs/superpowers/plans/2026-11-05-desktop-parity-d16c-plugin-marketplace.md
  code:
    - server/library/plugin_cli.py
    - server/library/plugin_policy.py
    - server/library/curated.py
    - server/library/scan.py
    - server/library/directory_api.py
    - server/sessions/plugin_reload.py
    - server/api/library.py
    - server/sessions/fleet_orchestrator.py
    - web/src/api/pluginHooks.ts
    - web/src/features/settings/sections/PluginsSection.tsx
    - web/src/features/settings/sections/plugins/MarketplacesPanel.tsx
    - web/src/features/settings/sections/plugins/BrowsePanel.tsx
    - web/src/features/settings/sections/plugins/ConsentDialog.tsx
    - web/src/features/settings/sections/plugins/SessionOutcomes.tsx
---

# Plugin Marketplace

Browse, install, update, enable, disable and uninstall Claude plugins from Settings › Plugins, with the change applied on every host.

**Problem** — Settings › Plugins could only toggle an installed plugin and show its detail. Adding a marketplace or installing a plugin meant running `claude plugin ...` by hand on each host.

**Target users** — the operator who runs sessions across several hosts and wants one plugin set everywhere.

**Desired outcome** — a plugin chosen once in the UI is installed on every registered host, with a per-host result, and nothing runs that the owner did not knowingly approve.

## How it works

- **What can be installed (D1).** Only plugins from a *curated* marketplace, or from the reviewed Directory (`anthropic-plugin-directory`; browse marks only those entries `Reviewed`). Plugins with a `command` source and marketplaces with a `headersHelper` are refused.
- **Curated means confirmed.** The curated set is a ViberPit-owned record of `{marketplace name: confirmed source}`. A marketplace configured outside ViberPit, for example one that was already on a host or one an agent added with `claude plugin marketplace add`, shows as **Unconfirmed** and offers nothing until the owner confirms it in Settings. The confirm dialog shows the source and echoes it back. If the configured source later differs from the confirmed one, it shows **Source changed** and is refused until re-confirmed.
- **Marketplace sources** are a GitHub `owner/repo` or an https git URL. IP literals, `localhost`, private-network names (`*.ts.net`, `*.local`, `*.internal` …), and hosts that do not resolve or resolve to a private, loopback, link-local, CGNAT or reserved address are refused.
- **Plugin sources inside a curated catalog** get the same rules without DNS (`plugin_policy.entry_source_refusal`). Any fetch target in an entry's `source` (a URL or a git `host:path`) must be a public-looking `https://` URL. An entry that names `http://`, ssh, `file:`, an IP literal or a private-network name is hidden from browse, and the edge and every host refuse it with `PLUGIN_ENTRY_SOURCE_REFUSED`.
- **Every host (D2).** Each operation fans out to every registered host. Each host re-runs the policy against its own marketplace state before running the CLI. The result is a per-host outcome (`ok`, `noop`, `failed`, with a failure code). A partial failure is shown with Retry; nothing is rolled back, and a retry is safe.
- **Auto-update (D3).** The CLI's own default is kept: official marketplaces auto-update on startup. The UI shows each plugin's version and "auto-updates".
- **One writer.** `server/library/plugin_cli.py` is the only code that changes plugin state. It runs the installed `claude plugin ...` against the shared config dir, finds its result line by a nonce, and never passes `-y`, `--accept-command` or any other consent-bypass flag. Uninstall keeps the plugin's data.
- **Consent card.** Every install and update asks: "Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust." After a success the UI says "Running sessions pick this up when they restart." — unless "Also reload running sessions now" was ticked, in which case the per-session outcome list shows which sessions reloaded, were held, or were skipped mid-turn.
- **Access.** Every plugin mutation route needs a human sender. They refuse `VIBERPIT_RULES_TRUST_LOCAL_HOST`, because an install reaches every host. Every mutation writes an audit row (`library_marketplace_*`, `library_plugin_*`), with the identity and the hosts.
- **Reload running sessions (opt-in).** The consent card has a checkbox, "Also reload running sessions now" (default off), and Settings › Plugins has a "Reload running sessions" button. Either calls `POST /library/plugins/reload`, which sends the CLI's `reload_plugins` request to every live Claude session on every host and lists a per-session outcome: `applied`, `held`, `busy`, `skipped` or `failed`. By default (`holdOnCacheImpact`) a session that has started talking and whose tools would change keeps its plugins until it restarts, so its prompt cache is kept. A session mid-turn is `busy` and is skipped. The route needs a human sender and is audited (`library_plugins_reloaded`). It installs nothing, so the kill switch does not stop it. Sessions not reloaded pick the change up when they restart.
- **Directory installs are verified live.** Browse shows the **Reviewed** badge from the cache. An install or update of a Directory plugin also fetches that listing from the Anthropic Directory API (`server/library/directory_api.py`, no credentials). It is refused `PLUGIN_UNREVIEWED` (reason `directory_mismatch`) unless the live review state is `done` and the live source matches the cached one. The only live-only keys allowed are `source.marketplace_slug` and `repository.tracked_ref`. If the API cannot be reached the install fails closed with 503 `PLUGIN_DIRECTORY_UNAVAILABLE`; retry. A cache that holds duplicate ids or names is refused.
- **Kill switch.** `VIBERPIT_PLUGIN_INSTALL_ENABLED=false` removes browse, add, confirm, install and update (the routes answer 404 `plugin_install_disabled` and the controls hide). Enable, disable, uninstall and remove still work.

## Security

**What a plugin can run.** A plugin can ship hooks, MCP servers, LSP servers, agents and skills. Once installed and enabled, those load in every session on every host that has it, including full-auto sessions and unattended automations. Hooks and MCP servers are arbitrary code with the session's privileges. Installing a plugin is therefore granting that code the run of your fleet. The consent card says so on every install and update. Treat it as a trust decision about the plugin's author, not as a review of what it does.

**Residual risks to know about.**

- **Same-host tampering.** A session with shell access on a host can modify that host's own marketplace clone, its `known_marketplaces.json`, or the clone's `.git/config` origin between the policy check and the CLI run. That can make an owner-approved install or update on *that host* fetch different code than was approved. Each host checks its own state, so a change made before the check is caught (for a Directory install too, since each host matches its own Directory cache against the listing the edge verified live); a change made after it is not. The fan-out to *other* hosts is protected, because each of them re-checks its own state.
  - **Ruling.** No change the daemon can make closes it. The session and the daemon share one uid, so a read-only clone or `chattr` is reversible by the session, and a managed-settings file (`/etc/claude-code/managed-settings.json`, root) is outside the daemon's reach. `strictKnownMarketplaces` and `blockedMarketplaces` gate `marketplace add` only, never a later edit of the clone or its `.git/config`. The structural fix is a privilege boundary between sessions and the shared config dir (a sandbox or a second uid), which is a separate project. Accepted unless the owner rules otherwise.
- **Directory review cache is forgeable, but install is verified live.** The Directory's review state is cached in `plugin-directory-cache-v2.json` in the shared config dir, and every session can write that dir. An agent on the edge can forge a listing there, and browse then shows it as **Reviewed** because the badge reads the cache. The edge's install and update check compares the cache with the live Directory API, so a forged review or a source pointed at another repo is refused. A host's own re-check still reads its own cache, so a forged cache on that host is the same-host tampering residual above. The live check covers only Directory installs. The cache is also trusted for up to one TTL (about 1 hour) for browse; a failed refresh is retried after a minute.
- **Plugin-entry sources are checked without DNS.** A public-looking name in a catalog entry that resolves to an internal address is not caught. Confirming a marketplace already trusts its author to ship hooks that run anywhere, so a resolving check would not stop that author.
- **DNS rebinding is closed for marketplace add and update, not everywhere.** The addresses the source check resolved are pinned into the clone (`http.curloptResolve` in a per-run `GIT_CONFIG_GLOBAL` that still includes `~/.gitconfig`), and update re-runs the check on the confirmed host before the CLI runs. Not pinned: git sources named by a plugin entry, the CLI's auto-update clones, and a redirect to another host (redirects stay on because GitLab needs them). An `owner/repo` shorthand is cloned from `github.com`, whose name the add policy never resolves or checks (it is trusted as GitHub itself), so there is no checked answer to pin it to; only `https://` URL sources, the ones the SSRF check resolves, are pinned. IPv6 answers in a pin were not probed.
- **Deadlines.** The edge waits for every CLI run a host may make (`plugin_cli.rpc_timeout_s`): two for a Directory install or update, which refreshes the cache first, and one otherwise. A host that still misses it is `host_timeout`, and a retry is safe: an install that already landed answers `noop`.
- **Auto-update.** Official marketplaces auto-update on startup (D3), so their plugin code can change without a click.

**Mitigations.**

- Confirm a marketplace source only after reading it; the confirm shows the exact string and the record is tied to it.
- Keep `VIBERPIT_PLUGIN_INSTALL_ENABLED=false` ready: it stops new installs and updates fleet-wide without a code deploy. It's an edge env var (restart the edge to apply), and worker hosts act on what the edge sends.
- Every add, confirm, install, update, uninstall and toggle has an audit row, so a change can be traced to an identity and a host set.
- Prefer the Reviewed Directory entries; unreviewed marketplace entries are shown without a review badge and their contents may be unknown.

## Not built

- Closing the same-host tampering residual above. It needs a privilege boundary between sessions and the shared config dir; #3314 holds the same-host ruling.
- Pinning plugin-entry git sources and the CLI's auto-update clones.
- A held reply is built from the CLI's code but was never observed live (it needs a session with output tokens); see `docs/superpowers/specs/2026-11-05-d16c-residuals-verification.md`.
