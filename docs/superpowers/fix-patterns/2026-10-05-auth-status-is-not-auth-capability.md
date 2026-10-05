# Auth status is not auth capability

- **Symptom:** A server reports missing authentication, but its suggested login action always fails.
- **Anti-pattern:** Treating a cached needs-auth flag as proof that the configured transport supports browser OAuth.
- **Fix shape:** Use the existing transport classification to gate both the rendered action and the API before spawning a login helper. Preserve the warning for unsupported transports with host credential setup guidance. Reject ambiguous names instead of authenticating a different server with the same short name.
- **Where to look:** MCP settings, auth start routes, and other consumers of needs-auth caches. The audit found one UI entry point and one API start route; both are covered. The CLI boundary was checked with Claude Code 2.1.289 (`mcp login --help`: HTTP/SSE/connector authentication).

Tracked in #2686.
