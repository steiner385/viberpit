# Shared OAuth stores must merge account snapshots monotonically

- **Symptom:** Browser login succeeds and one account connects, but another account's background sync restores empty or older tokens to the shared store.
- **Anti-pattern:** Atomic replacement protects JSON integrity, not read-modify-write consistency or credential freshness. Whole-map writes and shallow updates treat stale account snapshots as authoritative.
- **Fix shape:** Serialize read/merge/write, retain unrelated server keys, and choose whole credential bundles by token presence and expiry. Discovery-only entries never erase authenticated bundles. Explicit login can replace only the authenticated server's entries, including plugin-qualified names. Do not mix a token with another observation's client/issuer/refresh metadata.
- **Where to look:** Periodic account sync, account capture, and MCP login persistence. The audit found all three paths; each now uses `creds.merge_shared_mcp`. Local process locks do not provide cross-host SSHFS exclusion; credential-authority policy still owns host-level write authority.

Tracked in #2695. The live incident was confirmed by a successful AWS OAuth exchange, valid credentials in one account, and the shared entry reverting to discovery-only metadata. Propagating the valid bundle restored the affected account's native connection.
