# Attention rollups without actionable rows

- **Symptom:** a map says Needs you while the detail shows zero or an empty attention queue, leaving a human without a next action.
- **Anti-pattern:** a parent tier rolls up several kinds of work, while its detail selector includes only some kinds; separate badge arithmetic hides the mismatch. Subtracting child rollups alone also mistakes primary initiative counts for issue counts.
- **Fix shape:** use one scoped selector for map badges, summary counts, and actionable rows. Derive direct issue groups by subtracting child rollups and primary non-program initiative contributions. Preserve blocked and descendant work, and link each group to its actual owner. Explain grouped totals. Keep independently fetched incidents explicit rather than claiming a global all-clear.
- **Where to look:** roadmap `needsYou.ts`, `NeedsYouList`, `TreeRowCard`, portfolio/product/capability details, and `server/pm/tree.py` count construction. Audit confirmed all these frontend consumers now share the selector; issue ordering and reason/action copy are in the existing leaf helpers/components. Inbox assignment without a pending proposal remains a separately documented missing control.

Regression coverage: issue-only attention, descendants, blocked initiatives, no double counting, and desktop/phone navigation. Tracking: #1609.
