"""Effective MCP enablement for one session (spec 2026-10-09 repo-scope §2) — the single
source of truth for both the CLI's disabledMcpServers list and the UI's hints."""
from __future__ import annotations


def effective_disabled(servers: list[str], *, server_defaults: dict[str, bool],
                       repo_rules: dict[str, bool], session_overrides: dict[str, bool]) -> list[str]:
    off: set[str] = set()
    for key in set(servers):
        for source in (session_overrides, repo_rules, server_defaults):
            if key in source:
                if not source[key]:
                    off.add(key)
                break
    return sorted(off)
