# D-16c residuals: verification record

Date: 2026-11-05. Constitution III: every claim below was checked against the installed binary, by a no-model probe,
or against the live Anthropic Directory API. Claims that were not checked are listed under **Gaps**.
Design: `2026-11-05-d16c-residuals-design.md`. Parent record: `2026-11-04-d16-verification.md` (§0 explains the offsets).
Issue: #3314.

## 0. Versions and how to read the offsets

- **Binary (dobby).** `~/.local/bin/claude` resolves to `versions/2.1.318` (`claude --version`: `2.1.318 (Claude Code)`),
  sha256 prefix `708c77b204db3008`, the same file as the D-16 record. Every byte offset (`@N`) below is into this file.
  The bundle is minified, so each offset was read in context.
- **Binary (rocky).** Live worker sessions run `versions/2.1.293`. Not probed.
- **git.** 2.53.0 on dobby and rocky.
- **Python SDK.** `claude-agent-sdk` 0.2.105 has no `reload_plugins` method. `_query._send_control_request` is the seam.
- **Probes.** The git probes ran on dobby under a throwaway `HOME` and `CLAUDE_CONFIG_DIR` with
  `ANTHROPIC_BASE_URL=http://127.0.0.1:9`. The Directory API was called with curl and no credentials.

## 1. `reload_plugins` (binary reads)

| What | Where | Finding |
|---|---|---|
| Control-request schema | `@283016339` | `{subtype: "reload_plugins", hold_on_cache_impact?: boolean, add_plugins?: [...]}` |
| Handler | `@320846464`..`@320848300` | see below |
| `$te` | `@304823890` | decides whether the reload would invalidate the cache |
| `B_` | `@280189494` | `costLedger.totalOutputTokens()` |
| SDK `reloadPlugins(e)` | `@308686330` | sends `hold_on_cache_impact: true` only when `holdOnCacheImpact` is truthy (TypeScript SDK; Python has none) |

- **Schema text for `hold_on_cache_impact`:** "When true, the reload is not applied if applying it would change the
  session's tool list while the conversation's prompt cache depends on that list (the same check the interactive
  /reload-plugins makes before it asks for --force): the response then carries held: true and cache_impact, and the
  session keeps its current plugins. Default false: apply unconditionally."
- **`add_plugins`** is refused on Remote Control and cloud connections and when managed settings set
  `disableSideloadFlags`. ViberPit never sends it.
- **Handler.** On hold it calls `$te(...)`. If `wouldInvalidateCache`, it replies `{commands, agents, plugins,
  mcpServers, error_count, held: true, cache_impact: {mcp_servers_added, mcp_servers_removed, lsp_tool_change}}` and
  keeps the current plugins. Otherwise it reloads (`hj(...)`, `applyPluginMcpDiff`, `loadAllPlugins`) and replies
  `{commands, agents, plugins, mcpServers, error_count, held: false}`. The `held` key is present only when
  `hold_on_cache_impact` was sent.
- **`$te`.** `mcpServersAdded` and `mcpServersRemoved` are the diff between the plugin-sourced MCP clients now connected
  and the plugin MCP config on disk. `lspToolChange` is adds, may-add, removes, may-remove or null.
  `wouldInvalidateCache = (mcpChanged || lspToolChange !== null) && !toolSearchEnabled && B_() > 0`.
- **Consequences.**
  - Commands, skills, hooks and agents never hold.
  - A session with no output tokens yet (no completed model turn) is never held.
  - With tool search on, a session is never held.

## 2. Anthropic Directory API (curl, ~19:50 UTC, no credentials)

| Request | Result |
|---|---|
| `GET https://api.anthropic.com/api/directory/plugins` | 200, `application/json`, 420,908 bytes: `{data: [100 listings], facets, next_page: "page_…", omitted: []}` |
| `?limit=2` | honoured |
| `?q=superpowers&limit=3` | 200 (text search) |
| `?slug=…` | 200, but not a filter (0 rows) |
| `?id=`, `?ids=`, `?surface=` | 400 `unknown parameter` |
| `GET …/plugins/plugin_01EHy1YLV4sSFaCaZ5zowWX2` | 200, one listing, 5,671 bytes |

- **One listing's keys:** `added_time, checks, compatibility, components, description, id, installation, links, media,
  metrics, name, publisher, slug, source, summary, surfaces, tags, taxonomy, updated_time, verified_tier`.
  `checks.review.state` was `done` and `checks.review.by.type` was `partner`.
- **Public by design (binary).** `GCr = "/api/directory/plugins"` `@284110796`. `Jg` `@284153823` exempts a request with
  `publicPluginDirectoryCatalog: true`, `auth: "none"`, host `api`, method GET on that path (no `q`) from the
  essential-traffic-only and data-residency refusals. The catalog fetch is public and unauthenticated by design.

### 2.1 Cache against live

- **Sample (16 listings by id: 12 reviewed, 4 unreviewed).** `source.repository` (`url`, optional `path`,
  `reviewed_commit`) was byte-identical in all 16. The live `source` adds only `marketplace_slug:
  "anthropic-plugin-directory"`. Review state agreed in all 16.
- **Names cannot be bound to the live listing.** The cache's entry name (`entryNames[id]`, for example `superpowers`) is
  derived by the CLI. The live `slug` is `obra/superpowers`, and is `None` for some listings
  (`ant.dir.gh.wonderwhy-er.desktopcommandermcp`).
- **dobby's cache:** 3,605 listings, 3,605 unique ids, 3,605 unique names, `fetchedAt 2026-11-05T19:43:54Z`.
- **Full comparison (~20:50 UTC, 47 pages at `limit=100`).**
  - The live API has 4,684 listings. dobby's cache has 3,605 and every one is present live (the CLI filters the
    catalog). Review state agreed for all 3,605.
  - Live `source` keys: `type` (3,605), `repository` (3,605), `marketplace_slug` (3,362).
  - After dropping `marketplace_slug`, 3,512 `source` objects are byte-equal to the cache. The other 93 differ only by a
    live-only `repository.tracked_ref` (a branch name, for example `release`, `dist-claude-slim`) that the CLI's cache
    drops. No cached listing carries a key the live one lacks.
- **The install gate's rule follows.** Every cached `source` and `repository` key must match live (`url` by
  normalization). The only live-only keys tolerated are `source.marketplace_slug` and `repository.tracked_ref`
  (`plugin_policy._LIVE_ONLY_KEYS`).

## 3. git clone pinning (dobby)

- **The CLI runs the system `git`.** `claude plugin marketplace add https://github.com/octocat/Hello-World --json`, with
  a logging `git` wrapper first on `PATH`. The first call was `git -c core.askPass= -c protocol.ext.allow=never … -c
  core.hooksPath=/dev/null config --includes --null --show-scope --get-regexp ^(core\.sshcommand|ssh\.variant)$`, then
  the clone into `plugins/marketplaces/temp_git_<ts>_<rand>..clone`.
- **The CLI replaces `GIT_CONFIG_COUNT`.** The env it passed carried its own `GIT_CONFIG_COUNT=40` with
  `GIT_CONFIG_KEY_0..39` (hooks disabled, `gc.auto=0`, …), `GIT_SSH_COMMAND=false` and `GIT_CEILING_DIRECTORIES=/tmp`.
  The caller's `GIT_CONFIG_COUNT=1` / `http.curloptResolve` entry was gone. Without a pin the result was `failureCode
  manifest_missing`: the clone succeeded and Hello-World is not a marketplace.
- **`GIT_CONFIG_GLOBAL` survives.** With `GIT_CONFIG_GLOBAL=<file>` holding `[http]` `curloptResolve =
  github.com:443:127.0.0.1`, the result was `failureCode error_unknown`: "Failed to clone marketplace repository: … fatal:
  unable to access 'https://github.com/octocat/Hello-World.git/': SSL certificate verification failed: certificate has
  expired". The clone connected to 127.0.0.1:443 (a local listener) and never reached GitHub. So `GIT_CONFIG_GLOBAL`
  survives the CLI's env scrub and `http.curloptResolve` pins the clone.
- **The real global config stays readable.** With `[include] path = ~/.gitconfig` first in the file,
  `GIT_CONFIG_GLOBAL=<file> git config --show-origin --get user.name` printed `file:/home/tony/.gitconfig  Tony Stein`.
- **Redirects must stay on.** `git -c http.followRedirects=false ls-remote https://github.com/octocat/Hello-World` works.
  Against `https://gitlab.com/gitlab-org/gitlab-test` it fails with `error: 301`. A redirect to an unpinned host is
  therefore a residual.
- **A pin with the real answer works.** `github.com:443:140.82.114.4` made `ls-remote` succeed.

## Gaps (not verified)

- A `held: true` reply was never observed. It needs `totalOutputTokens() > 0`, a completed model turn, which the
  no-model harness never runs. The held shape above is from the handler's code.
- IPv6 answers in a pin (libcurl's `--resolve` wants `[v6]`). Only IPv4 hosts were probed.
- rocky 2.1.293: no probe. `reload_plugins` appears 20 times in its strings (D-16 record §0).
