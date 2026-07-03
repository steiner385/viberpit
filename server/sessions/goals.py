"""Single source of truth for 'what is this session's goal'. Consumers (autopilot,
dashboards) MUST read goal through here — never a parallel lookup (constitution VI).

Tiers, highest-first:
  explicit       — the built-in /goal condition (record.active_goal)
  initial_prompt — fallback: the auto-derived session name (a one-line intent summary)
The companion inferred-session-goals feature inserts an 'inferred' tier between them."""
from __future__ import annotations


def effective_goal(record) -> tuple[str, str]:
    active = (getattr(record, "active_goal", None) or "").strip()
    if active:
        return active, "explicit"
    inferred = (getattr(record, "inferred_goal", None) or "").strip()
    if inferred:
        return inferred, "inferred"
    return (getattr(record, "name", None) or "").strip(), "initial_prompt"
