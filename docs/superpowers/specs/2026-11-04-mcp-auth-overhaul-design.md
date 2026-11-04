# MCP auth overhaul: one sign-in, every account, every runner

**Status:** design (2026-11-04). Supersedes the distribution and status parts of
`2026-10-13-mcp-oauth-single-refresher-design.md`; keeps its core decision (ViberPit is the only
refresher and the store is the only refresh-token holder).

## Why

Tony opens Settings → MCP servers and sees "Needs auth" almost every time, so he no longer
trusts it. The current design does share one grant across accounts, but what he sees and what
actually works have drifted apart. Evidence captured on 2026-11-04, 07:26 EST:

- **railway** and **aws-oauth** showed *Needs auth*, but fresh probes on every enabled account
  on dobby **and** rocky reported them *connected*. The refresher had renewed Railway every
  50 min all night. The red pill came from one `source=session` row dated 2026-11-01. That row
  belongs to `tony.claude_steinonline.net`, an account that is now **disabled**, so the probe never
  covers it and the row never ages out. The pill shows the worst row ever recorded
  (`mcpStatus.ts`), and rows have no TTL (`server/mcp_health/store.py`).
- **kindash-mcp** really is unsigned: its store bundle is empty. One probe also reports
  "Incompatible auth server: does not support dynamic client registration", which is
  KinDash's own defect.
- **aws-oauth** has two store bundles. Editing the server config changed the CLI's
  `name|hash` key and left an orphan behind. Status matching by `serverName` can be satisfied
  by either bundle.
- The "Account to reauthorize" picker suggests the sign-in is per account. It is not, so the
  picker is misleading.

Structural weaknesses behind these symptoms:

- The refresher runs only inside the dobby edge process, and its health is visible only as
  INFO log lines.
- rocky gets every MCP token through the sshfs mount of dobby's `~/.claude-accounts`. The
  credential-broker plan removes that mount and has no MCP replacement.
- `compose()` on rocky writes the store through a `flock` that is not distributed over sshfs.
- A refresh does not heal a session that already hit an expired token. Only a full login runs
  `after_login`, which reconnects sessions and clears `mcp-needs-auth-cache.json`.
- Sign-in is paste-back only. The browser lands on a dead `localhost` page and the URL has to
  be copied back by hand, which is unusable from a phone.

## Goals (confirmed with Tony)

1. **One configuration.** An MCP server is added once and is available everywhere.
2. **One sign-in per server.** It covers every Claude account and every runner (dobby, rocky,
   any future runner). Which account or host a session lands on never matters.
3. **Sign-ins last.** Tokens refresh in the background. Tony re-authenticates only when the
   provider forces it, for example AWS's ~12 h grant cap.
4. **The page tells the truth.** *Needs auth* means a session would fail right now. Fixing it
   once fixes it everywhere.
5. **Terminal `claude` and `ccrun` count too.** A `claude` run by hand on dobby or rocky under
   a ViberPit account dir gets the same working tokens (Tony's choice, 2026-11-04).
6. **Push + one tap.** When a real re-sign-in is needed, Tony gets a push notification naming
   the server. One tap opens the provider's sign-in page. Finishing it fixes every account and
   runner, with no copy-paste (Tony's choice, 2026-11-04).

## Non-goals

- Claude Desktop and claude.ai connectors. They keep their own sign-ins.
- Fixing KinDash's auth server (no dynamic client registration). That is a KinDash issue; file
  it there. ViberPit only has to report it truthfully as a server-side problem, not as
  "Needs auth".
- Copilot accounts. They have no `.credentials.json` today, and MCP OAuth for them stays out of
  scope.
- Warning ahead of a known hard cap. Tony chose push-on-need, not pre-expiry nagging.

## Anti-goals (a failure even if it "works")

- Any path where a sign-in is tied to one account, or where the user must sign in per host.
- More than one process holding or rotating a refresh token. That is the 2026-10-13 incident
  shape: N CLIs racing one rotating grant.
- A red pill while sessions can call the server, or a green one while they can't.
- Patching or wrapping the `claude` binary (constitution I).
- A notification storm: one push per server per dead grant, not one per account, host or tick.

## Constraints

- Constitution II: there is one credential authority, the edge. Runners and account dirs only
  ever hold the *public view* (no `refreshToken`).
- Constitution III: verify every claim about the CLI against the installed binary, and verify
  every provider behaviour against the real server. Two examples: the CLI's `mcpOAuth` key
  formula, and whether a provider accepts an `https://…ts.net` redirect URI.
- Constitution IV: the new callback route runs behind the existing tailnet auth. The flow uses
  PKCE, a single-use `state` with a short TTL, and the RFC 8707 `resource` exactly as the
  refresher already serialises it. Tokens are never logged.
- The edge is served at `https://dobby.tail0dd570.ts.net/`. Phones reach it over the tailnet.
  Derive the callback from the configured or request origin, never hardcode it.
- The CLI re-reads plaintext `.credentials.json` on every token lookup (observed 2026-10-13).
  A rewritten access token needs no restart. A session already latched into needs-auth does
  need an MCP reconnect.

## Design

### 1. One authority, one writer

The edge owns `store/mcp.shared.json`. **Only edge code writes it.** The writers are the
refresher, the new sign-in callback, the paste-back fallback, and the capture of a sign-in done
in a terminal (§6). Nothing on a runner writes the store, even while the sshfs mount still exists.
Runner-side `compose()` stops calling `capture_cfg_mcp → merge_shared_mcp` against the mounted
store.

**Builder's call:** keep the store as a file on the edge, or move it into the DB, encrypted.
Either is fine as long as there is one writer process.

### 2. Keying: one grant per configured server

Each configured server maps to exactly one live bundle. The CLI looks up a bundle by its own
`serverName|hash` key (verify the formula from the installed binary). The store therefore keeps
a canonical record per *configured server* and **publishes it under whatever key the current
config produces**.

When a server's config changes but the auth server and `resource` are unchanged, the grant
follows the server to its new key. When the auth server or `resource` changes, the old grant is
dropped and the server reads *Not signed in*. Orphaned bundles are deleted, never refreshed. A
one-time migration collapses today's duplicates, for example the two aws-oauth bundles, keeping
the live one.

### 3. Distribution to every account on every host

- **Edge host:** publish the public view into every account dir each refresher tick, as today.
  The set is every Anthropic account, **including disabled ones**, so terminal use under a
  disabled account still works.
- **Runners:** MCP public-view bundles ride the existing `creds_sync` envelope push
  (`FleetOrchestrator.push_credentials`, at least every 30 s). Each runner writes them into its
  *local* account dirs' `.credentials.json` `mcpOAuth`, touching only that key and keeping the
  existing inode/mtime guard. This removes rocky's MCP dependency on the sshfs mount and fills
  the gap in broker spec §4.
- The publish policy stays as it is: newer expiry wins, an empty snapshot can't revoke, and an
  explicit login replaces. One addition: when the authority declares a grant **dead**, the
  publish clears the dir's stale access token too. That way the CLI and the page say *Not signed
  in* rather than *Sign-in expired <old date>*.

### 4. Refresh that heals

Keep the refresher (60 s tick, 10 min lead, CAS write-back, dead-grant detection). Add three
things:

- **Heal on refresh.** After a successful refresh or sign-in, run the same healing as
  `after_login`: clear the server from every `mcp-needs-auth-cache.json`, then
  `reconnect_mcp_everywhere(server, ["needs-auth","failed"])` across the fleet. Today only a
  login does this.
- **A durable health record.** Store per server: last refresh attempt, outcome, consecutive
  transient failures, and grant state. Grant states are `live`, `expiring`, `dead` and `never`.
  The record lives in the DB, not just in memory. The status page reads it (§7).
- **Escalation.** When a grant goes `dead`, send one push (§5). If transient failures last long
  enough that the access token actually expires, that is also user-visible: show it, and push
  once.

### 5. Push + one-tap sign-in

- Add a new notification event class `mcp_auth_needed` in `notify_rules.py` and send it through
  `notify_event(…, url=…)`, so channel rules and quiet hours apply. It fires once per server per
  dead grant, deduped across accounts, hosts and ticks. A notification never fires for a server
  that is merely unchecked.
- The deep link is `/api/mcp/oauth/start?server=<name>`. It runs behind tailnet auth and goes
  straight (302) to the provider's authorize URL, so the tap *is* the sign-in.
- **ViberPit runs its own OAuth client.** On start, the edge:
  1. Discovers protected-resource and auth-server metadata, reusing the refresher's code.
  2. Registers a client through DCR with
     `redirect_uri = https://<edge origin>/api/mcp/oauth/callback`, and caches it per auth
     server.
  3. Builds a PKCE + `state` + `resource` authorize URL.

  The callback exchanges the code, writes the canonical bundle (§2), publishes it (§3), heals it
  (§4), and lands on the MCP page showing the server green. The refresher already refreshes
  with the bundle's own `clientId`, so tokens minted for ViberPit's client need nothing new.
  The CLI only reads `accessToken`, so which client minted it does not matter.
- **Fallback:** if an auth server refuses DCR or the `https` redirect, keep today's paste-back
  PTY flow (`server/mcp_auth.py`) for that server and say why on the page. Probe each currently
  configured provider before building, and record which path each one takes: AWS, Railway,
  Linear, Notion, Sentry, Stripe and KinDash. Linear and Notion use claude.ai's CIMD client id
  today; check that they accept DCR.
- Remove the "Account to reauthorize" picker. A sign-in is never per account.

### 6. Terminal sign-ins

A user may still run `claude mcp login` or `/mcp` in a terminal on any host. That dir briefly
holds a refresh token. Capture it to the authority and strip it from the dir:

- **On the edge host:** the existing capture path, run by the refresher tick.
- **On runners:** the runner daemon sends the bundle to the edge over RPC, an
  "offer MCP bundle" call. The edge merges it under its lock, then strips the refresh token on
  the next publish.

This keeps a single refresh-token holder without blocking terminal use.

### 7. A status page that tells the truth

There is **one status per server**, derived in this order:

1. **Configuration problem:** the server is unreachable, the auth server is incompatible (for
   example no DCR), or the binary is missing. Shows as *Failed*, with the reason.
2. **Grant state** from the authority record:
   - `never` → *Not signed in*
   - `dead` → *Sign-in expired*
   - `live` or `expiring` → candidate for *Connected*
3. **Fresh observations** confirm it or contradict it. Only observations from enabled accounts
   count, and only observations newer than a freshness window (builder's call; 30 min
   suggested). Session-sourced rows count only while that session is live. An observed 401
   while the grant is `live` shows *Failed: token rejected*, a real anomaly worth surfacing.
   Stale rows are ignored for the pill, aged out of the table, and never put an account into
   any picker.

Per-account and per-host rows move into *Details* as diagnostics. "Test" re-probes and
re-derives. *Needs auth* appears only when the authority says the grant is `never`, `dead` or `expired`
(see the designer's call 5 below).

## Delivery order

Each phase is a separate PR, and each ships value on its own:

1. **Truthful status:** §7 plus removing the picker. This alone clears today's false reds. — plan: `docs/superpowers/plans/2026-11-04-mcp-auth-overhaul-phase1-truthful-status.md`
2. **Keying and a single writer:** §2 plus §1, including the duplicate-bundle migration.
3. **Heal and alert:** §4 plus the push half of §5.
4. **One-tap sign-in:** the OAuth client half of §5, with the paste-back fallback.
5. **Distribution off the mount:** §3 runners via `creds_sync`, plus §6 runner capture.

## Calls made by the designer (not confirmed by Tony)

These came out of the builder check. Each is a sensible default, and Tony can override any of
them.

1. **Phase 5 and the mount.** While rocky still mounts dobby's `~/.claude-accounts`, the
   runner does **not** write `mcpOAuth`; the edge's publish already covers that shared tree.
   Runner-local writes start only once the broker (#2629) unmounts rocky. So Phase 5 ships
   behind the broker cut-over. Runner capture (§6) can ship earlier, because it only reads the
   dir and sends over RPC.
2. **Phase 1 grant state.** Phase 1 derives `never`, `dead`, `live` and `expiring` from the
   store bundle itself (refresh token present, `expiresAt`). Phase 3's health record then
   becomes the source. No schema change goes into Phase 1.
3. **Server identity.** A grant follows **(auth-server issuer, `resource`)**, not the server
   name, so a rename keeps the grant. When one logical server yields several CLI keys (plugin
   version skew between hosts, per-account config), the authority publishes the one grant under
   **every** key it sees in any account's composed config. The duplicate-collapse migration
   groups by (issuer, resource) and keeps the bundle with the latest `expiresAt`.
4. **Terminal sign-in replaces the grant only for the same (issuer, `resource`).** That makes it
   the same server, and a sign-in is explicit intent. An offer for a different issuer or
   resource is rejected and logged at WARNING. When an accepted offer replaces a live grant, the
   edge sends one informational push ("railway re-signed in from rocky"), so an accidental
   identity swap is visible.
5. **A refresh that fails until the token expires** is a fifth grant state, `expired`. The page
   shows *Sign-in expired — refresh failing: <reason>*. It sends one push, with the same
   one-tap link, deduped per (server, grant), because signing in again fixes it either way.
   *Needs auth* is the label for `never`, `dead` and `expired`.
6. **Linear, Notion, Sentry and Stripe** have empty, orphaned bundles today and are not in the
   configured server list. Phase 2's migration deletes them. ViberPit's one-tap flow is
   **DCR-only**; there is no CIMD client strategy. Providers that refuse it use paste-back.
   Only currently configured servers are probed for DCR and redirect support.

## Left to the builder

- The exact freshness window, and how long session rows are kept.
- The schema of the health record.
- Whether the store moves to the DB.
- Notification copy.
- How the page renders *Details*.
- How the per-provider DCR/redirect probe results are recorded, as long as they are recorded and
  cited.
