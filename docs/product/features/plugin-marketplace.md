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
    - server/api/library.py
    - server/sessions/fleet_orchestrator.py
    - web/src/api/pluginHooks.ts
    - web/src/features/settings/sections/PluginsSection.tsx
    - web/src/features/settings/sections/plugins/MarketplacesPanel.tsx
    - web/src/features/settings/sections/plugins/BrowsePanel.tsx
    - web/src/features/settings/sections/plugins/ConsentDialog.tsx
---

# Plugin Marketplace

Browse, install, update, enable, disable and uninstall Claude plugins from Settings › Plugins, with the change applied on every host.

**Problem** — Settings › Plugins could only toggle an installed plugin and show its detail. Adding a marketplace or installing a plugin meant running `claude plugin ...` by hand on each host.

**Target users** — the operator who runs sessions across several hosts and wants one plugin set everywhere.

**Desired outcome** — a plugin chosen once in the UI is installed on every registered host, with a per-host result, and nothing runs that the owner did not knowingly approve.

## How it works

- **What can be installed (D1).** Only plugins from a *curated* marketplace, or from the reviewed Directory (`anthropic-plugin-directory`; browse marks only those entries `Reviewed`). Plugins with a `command` source and marketplaces with a `headersHelper` are refused.
- **Curated means confirmed.** The curated set is a ViberPit-owned record of `{marketplace name: confirmed source}`. A marketplace configured outside ViberPit, for example one that was already on a host or one an agent added with `claude plugin marketplace add`, shows as **Unconfirmed** and offers nothing until the owner confirms it in Settings. The confirm dialog shows the source and echoes it back. If the configured source later differs from the confirmed one, it shows **Source changed** and is refused until re-confirmed.
- **Marketplace sources** are a GitHub `owner/repo` or an https git URL. IP literals, `localhost`, and hosts that do not resolve or resolve to a private, loopback, link-local, CGNAT or reserved address are refused.
- **Every host (D2).** Each operation fans out to every registered host. Each host re-runs the policy against its own marketplace state before running the CLI. The result is a per-host outcome (`ok`, `noop`, `failed`, with a failure code). A partial failure is shown with Retry; nothing is rolled back, and a retry is safe.
- **Auto-update (D3).** The CLI's own default is kept: official marketplaces auto-update on startup. The UI shows each plugin's version and "auto-updates".
- **One writer.** `server/library/plugin_cli.py` is the only code that changes plugin state. It runs the installed `claude plugin ...` against the shared config dir, finds its result line by a nonce, and never passes `-y`, `--accept-command` or any other consent-bypass flag. Uninstall keeps the plugin's data.
- **Consent card.** Every install and update asks: "Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust." After a success the UI says "Running sessions pick this up when they restart."
- **Access.** Every plugin mutation route needs a human sender. They refuse `VIBERPIT_RULES_TRUST_LOCAL_HOST`, because an install reaches every host. Every mutation writes an audit row (`library_marketplace_*`, `library_plugin_*`), with the identity and the hosts.
- **Kill switch.** `VIBERPIT_PLUGIN_INSTALL_ENABLED=false` removes browse, add, confirm, install and update (the routes answer 404 `plugin_install_disabled` and the controls hide). Enable, disable, uninstall and remove still work.

## Security

**What a plugin can run.** A plugin can ship hooks, MCP servers, LSP servers, agents and skills. Once installed and enabled, those load in every session on every host that has it, including full-auto sessions and unattended automations. Hooks and MCP servers are arbitrary code with the session's privileges. Installing a plugin is therefore granting that code the run of your fleet. The consent card says so on every install and update. Treat it as a trust decision about the plugin's author, not as a review of what it does.

**Residual risks to know about.**

- **Same-host tampering.** A session with shell access on a host can modify that host's own marketplace clone, its `known_marketplaces.json`, or the clone's `.git/config` origin between the policy check and the CLI run. That can make an owner-approved install or update on *that host* fetch different code than was approved. Each host checks its own state, so a change made before the check is caught; a change made after it is not. The fan-out to *other* hosts is protected, because each of them re-checks its own state.
- **Directory review cache is forgeable.** The Directory's review state is read from a cache file (`plugin-directory-cache-v2.json`) in the shared config dir, and every session can write that dir. An agent on the edge can set `checks.review.state: "done"` on an unreviewed listing, or add a forged listing outright. Browse then shows it as **Reviewed**, the edge and host checks pass, and the CLI installs from that cache. The "Reviewed" badge is only as trustworthy as every session that can write the shared config dir. Separately, the cache is trusted for up to one TTL (about 1 hour), including after a failed refresh, so a plugin that loses its review inside that window can still be installed.
- **No SSRF check inside a confirmed catalog.** The source rules above apply to the marketplace itself. The plugin entries inside a confirmed marketplace's catalog can name any source, including internal hosts, and are not checked. Confirming a marketplace already trusts its author to ship hooks that run anywhere, so this check would not stop that author.
- **A Directory install can outlive its deadline on a worker.** A worker refreshes the Directory cache before it installs, so the run can take longer than the edge's RPC deadline. The edge then reports that host as `host_timeout`, but the install may still finish there. A retry is safe: an install that already landed answers `noop`.
- **DNS rebinding.** The marketplace source check resolves the host name once. A name that resolves to a public address at the check and to an internal one when the CLI fetches is not caught.
- **Auto-update.** Official marketplaces auto-update on startup (D3), so their plugin code can change without a click.

**Mitigations.**

- Confirm a marketplace source only after reading it; the confirm shows the exact string and the record is tied to it.
- Keep `VIBERPIT_PLUGIN_INSTALL_ENABLED=false` ready: it stops new installs and updates fleet-wide without a code deploy. It's an edge env var (restart the edge to apply), and worker hosts act on what the edge sends.
- Every add, confirm, install, update, uninstall and toggle has an audit row, so a change can be traced to an identity and a host set.
- Prefer the Reviewed Directory entries; unreviewed marketplace entries are shown without a review badge and their contents may be unknown.

## Not built

A live `reload_plugins` push to running sessions. An install affects new and restarted sessions only, so as not to miss the prompt cache.
