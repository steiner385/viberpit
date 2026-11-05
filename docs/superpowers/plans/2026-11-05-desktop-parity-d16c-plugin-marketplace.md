# Desktop-parity D-16c: plugin marketplace — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Settings › Plugins can:
- add and remove curated marketplaces;
- browse their plugins and the *reviewed* Anthropic Directory;
- install, update and uninstall plugins on every host, showing a consent card first.

Enable and disable go through the same CLI path, so the CLI is the only writer.

**Facts (binding):** `docs/superpowers/specs/2026-11-04-d16-verification.md` §1 (CLI 2.1.318).

**Owner decisions (2026-11-04, binding):**
- **D1 (a): curated marketplaces only.**
  - Install is offered only from marketplaces the owner added in Settings, and from the Anthropic Directory filtered to `review.state == "done"`.
  - Every marketplace add shows its source and needs an explicit confirm.
  - `command`-source plugins and `headersHelper` plugins are refused: ViberPit never passes `-y` or `--accept-command`.
- **D2 (a): every host.** Fan each install, update, uninstall, enable/disable and marketplace change out to every host's daemon, and report per-host outcomes.
- **D3 (a): keep the CLI's auto-update default.** Show the version and "auto-updates" on each row.

**Decision:** D15 ("plugin marketplace browse/install").
**Spec row:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md`, around line 365.

**Tech Stack:** Python, FastAPI, pytest; React + TS, vitest, Playwright.

## Global Constraints

- **One CLI wrapper.**
  - Add `server/library/plugin_cli.py`. It runs `claude plugin … --json` with `CLAUDE_CONFIG_DIR=shared_base()`, so the recorded `installPath` is `~/.claude/plugins/cache/…` and survives account removal.
  - It parses **only the first stdout line** and keys idempotency on `failureCode`, not the exit code. A no-op exits 1 with `already_in_goal_state`, `not_installed` or `not_configured`; treat those as success-noop.
  - Use the installed binary that sessions use: find how ViberPit resolves the `claude` path and reuse it.
  - Use an argv list, never a shell, with a timeout and a pinned environment. Never pass `-y`, `--accept-command`, `--sparse` or `--claudeai`.
  - Scope is always `user`.
- **Policy (D1), enforced server-side in one function**, e.g. `plugin_policy.check_install(plugin_id, marketplace, catalog)`:
  - **Marketplace add.** The source must be a GitHub `owner/repo` or an `https://` git URL. Local paths, `http://`, `file:`, and anything with shell metacharacters are refused, and so are names that impersonate official marketplaces (the CLI also guards this).
    - The route takes `{source, confirm: true}`.
    - The UI must have shown the source verbatim before the confirm.
  - **Install.** It is allowed only when the plugin's marketplace is one the owner added through ViberPit, or the plugin is `anthropic-plugin-directory` with `review.state == "done"` (from the directory cache).
    - `claude-plugins-official` counts as owner-added: it is already configured on this host. Reuse the configured set from `known_marketplaces.json`.
    - Ruling: every marketplace already configured on the host when this ships counts as curated. The owner configured them by hand.
  - **Command plugins.** A plugin whose CLI result carries `shownCommand`, or a `command`/`headersHelper` source, is refused with 409 `PLUGIN_COMMAND_SOURCE_REFUSED`. ViberPit never accepts it.
  - **Install-time marketplaces.** `--marketplace <source>` is never used: install only from already-added marketplaces.
- **Fan-out (D2).**
  - Add a daemon RPC `plugin_op(op, args)` that runs the wrapper on that host.
  - The edge calls every host's daemon: the primary plus each worker (find how the fleet enumerates hosts).
  - The response is `{op, hosts: [{host, outcome: "ok"|"noop"|"failed", failureCode?, message?, version?}]}`.
  - A partial failure is reported per host, not rolled back. Retrying is idempotent, because the wrapper treats a no-op as success.
  - An old daemon without the RPC reports `{outcome: "failed", failureCode: "daemon_outdated"}` for that host.
  - In in-process mode (no remote daemons) the edge runs it locally.
- **Reads.**
  - `GET /library/plugins` (existing) gains per-plugin `version`, `autoUpdate` (D3, from `known_marketplaces.json` `autoUpdate` plus the CLI's official-set rule from spike §1.5; derive it from the CLI result where possible rather than re-implementing the official set) and `source`.
  - New `GET /library/plugins/browse` returns the available plugins:
    - from each owner-added marketplace's `marketplace.json`;
    - plus reviewed Directory plugins only, from the directory cache or `plugin list --available --json` run on the edge host. That output is 7 MB, so cache it (TTL ≥ 1 h) and never send unreviewed entries to the client.

    Each entry is `{pluginId, name, description, marketplace, version, reviewed: bool, components?: {hooks, mcpServers, agents, skills}, installed: bool}`, with descriptions capped at 500 chars. Pagination or search is server-side: `?q=&marketplace=&offset=&limit=` (limit ≤ 100).
- **Consent card.** Before install or update, the UI shows:
  - what the plugin can run (hooks, MCP servers, LSP, agents, skills) from the catalog or the plugin manifest where known, otherwise "Contents unknown — inspect the marketplace before installing";
  - the marketplace source;
  - this verbatim copy: `Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust.`

  The install route requires `confirm: true`.
- **Mutations.**
  - `POST /library/marketplaces {source, confirm}`, `DELETE /library/marketplaces/{name}` and `POST /library/marketplaces/{name}/update`.
  - `POST /library/plugins/{id}/install {confirm}`, `POST /library/plugins/{id}/update {confirm}` and `DELETE /library/plugins/{id}` (uninstall, default `--keep-data`; ruling: never prune data from the UI).
  - The existing `PATCH /library/plugins/{id} {enabled}` is re-routed through the wrapper and fan-out. Delete `mutate.set_plugin_enabled`'s direct `settings.json` write, so there is a single writer (Principle VI). Keep the pre-mutation snapshot and audit.
  - Every mutation requires a person (`require_human` / `human_sender`). An assistant or token caller gets 403.
  - Each mutation writes an audit row: `library_marketplace_added|removed|updated`, `library_plugin_installed|updated|uninstalled|toggled`, recording `identity`, `source` / `pluginId` and the per-host outcomes.
  - Expected refusals log at INFO.
- **Delete the dead `GET /library/marketplace`** (`library.py:~587`, #596) and its tests.
- **Kill switch:** `VIBERPIT_PLUGIN_INSTALL_ENABLED`, default `true` now that D1 is settled. When it is false, every install, update and add route returns 404 `plugin_install_disabled`, and browse returns `[]`. Enable, disable, uninstall and marketplace remove stay available. Use the `VIBERPIT_` prefix.
- **Running sessions:** an install takes effect for new sessions. The UI says: `Running sessions pick this up when they restart.` Ruling: a live `reload_plugins` push is a follow-up (it costs prompt cache).
- **Engines:** Claude only. The Codex library is unsupported (`CODEX_LIBRARY_UNSUPPORTED_DETAIL`).
- **Gates:**
  - Server: ruff, mypy, focused pytest, the parity tests, and the full `server/tests -n 4` in a clean env before the PR.
  - Web: typecheck, lint, full vitest, build, budget (the section is lazy), and e2e.
  - A no-model CLI contract test, `test_plugin_cli_contract.py` (`slow`; skip without the binary; throwaway `CLAUDE_CONFIG_DIR`; a local `directory` marketplace fixture, used **only in the test** since the route refuses local paths). It asserts:
    - the wrapper's first-line parse for add, install, disable, the no-op disable, update, uninstall and remove;
    - that the `failureCode` mapping holds;
    - that the `settings.json` symlink survives.

## Review Focus

1. Installing an unreviewed Directory plugin, or a plugin from a marketplace never added through Settings, is refused server-side, even with a crafted request (Task 1).
2. A command-source or `headersHelper` plugin is refused, and `-y` / `--accept-command` never appear in any argv (Task 1).
3. Fan-out with one host down gives per-host outcomes; the other hosts still apply; a retry is idempotent (Task 1).
4. Enable/disable has exactly one writer: no direct `settings.json` write remains (Task 1).
5. The consent card's copy is verbatim; install is impossible without `confirm`; unreviewed Directory entries never reach the client (Task 2).

---

### Task 1: Server — CLI wrapper, policy, fan-out RPC, routes

**Files:**
- `server/library/plugin_cli.py`, plus a policy module;
- `server/library/scan.py` (version, autoUpdate, browse);
- `server/api/library.py` (routes, deleting the dead route);
- `mutate.py` (remove the direct write);
- the daemon RPC `plugin_op`, plus remote/fleet/facade;
- `session_actions`/human gate; settings for the kill switch.
- Tests: `server/tests/test_plugin_marketplace.py` and the contract test.

- [ ] **Step 1: failing tests:** every bullet above plus Review Focus 1–4.
- [ ] **Step 2–4:** focused tests, parity, ruff, mypy.
- [ ] **Step 5: Commit** `feat(plugins): curated marketplaces, reviewed browse, install/update/uninstall on every host (D-16c)`

### Task 2: Web — Settings › Plugins marketplace

**Files:**
- `settings/sections/PluginsSection.tsx`, split into lazy subcomponents if it grows:
  - a Marketplaces panel (add with source preview and confirm, remove, update);
  - a Browse panel (search, marketplace filter, reviewed badge, components summary);
  - the Consent dialog (verbatim copy);
  - installed rows showing version, "auto-updates", update and uninstall;
  - a per-host outcome toast/list.
- hooks plus mocks; `types.ts`.
- Tests: unit tests, plus e2e `web/e2e/plugins-marketplace.spec.ts` (route-mocked, 1440 and 390).

- [ ] **Step 1: failing tests:** Review Focus 5:
  - the add flow shows the source before confirming;
  - browse renders, and a search hits the server;
  - install opens the consent dialog with the exact copy, posts `confirm: true`, and shows per-host results including a failed host;
  - update and uninstall work;
  - the kill switch hides install and add;
  - Codex is hidden.
- [ ] **Step 2–4:** full web gates and e2e.
- [ ] **Step 5: Commit** `feat(web): plugin marketplaces, browse and install with consent in Settings › Plugins (D-16c)`

### Task 3: Benchmark and docs

- Benchmark: `Plugins + marketplace browse/install` → Y. Regenerate §3 and report the target (D-16b: 94%).
- Spec §8/§10/§11:
  - D1–D3 as built;
  - the single-writer wrapper;
  - fan-out semantics;
  - consent copy;
  - kill switch;
  - the dead route deleted;
  - the live `reload_plugins` follow-up.

  Mark D-16c and D-16 complete.
- Product docs: the library/plugins doc, including a **security note** on what plugins can run.
- Ceilings: measured + 0.3, only if moved.
- [ ] Commit `docs: D-16c benchmark rerun and plugin marketplace docs`.
