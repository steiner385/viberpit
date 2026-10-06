# Human transport is not human authorship

- **Symptom:** Personal reply rankings learn the application's own suggestions because a human clicked them.
- **Anti-pattern:** Inferring authorship from browser authentication alone. Generated replies, starter chips, recovery prompts, and notice prefills all travel through the human's authenticated prompt transport.
- **Fix shape:** Carry explicit suggested provenance from every generated input through callbacks, draft persistence, interruption, WebSocket buffering, rejection, and retry. Clear suggested provenance when the user edits the text. Count only accepted typed inputs with verified human attribution at the server boundary.
- **Where to look:** Composer suggestions and ghost completion, session-card quick replies, YourTurnPanel, assistant starter/follow-up chips, interrupted-question recovery, notice prefills, REST prompt mutations, and WebSocket resend paths. This audit covered those emitters; focused tests verify generated versus edited drafts and retry metadata.

A second boundary matters when clustering these untrusted inputs: `tools=[]` disables native built-ins, while `strict_mcp_config=True` with empty MCP configuration also excludes account-configured tools. Stateless curation uses `--no-session-persistence` to avoid retaining another raw-input transcript outside its bounded corpus. The installed native CLI advertises both flags. Existing title/goal helpers have related configuration and remain outside this feature's change.

The existing-helper isolation audit is tracked in #2706.
