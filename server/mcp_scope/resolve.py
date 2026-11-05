"""Effective MCP enablement for one session (spec 2026-10-09 repo-scope §2) — the single
source of truth for both the CLI's disabledMcpServers list and the UI's hints.

The per-tool layer (D-16b) resolves here too: a Settings default per tool and a session
override per tool, the override winning. The deny rules sent to the CLI are composed from
it by ``flag_settings.compose_permissions``."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


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


SOURCE_DEFAULT = "default"
SOURCE_SESSION = "session"


@dataclass(frozen=True)
class McpToolRules:
    """One session's per-tool rules: ``{server: {tool: enabled}}`` from Settings
    (``defaults``) and from the session (``overrides``). An absent tool is enabled."""
    defaults: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)
    overrides: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)

    def state(self, server: str, tool: str) -> tuple[bool, str]:
        """``(enabled, source)``: the session override when there is one, else the default."""
        ov = self.overrides.get(server, {})
        if tool in ov:
            return ov[tool], SOURCE_SESSION
        return self.defaults.get(server, {}).get(tool, True), SOURCE_DEFAULT

    def with_override(self, server: str, tool: str, enabled: bool) -> "McpToolRules":
        ov = {s: dict(t) for s, t in self.overrides.items()}
        ov.setdefault(server, {})[tool] = enabled
        return McpToolRules(defaults=self.defaults, overrides=ov)


def effective_disabled_tools(rules: McpToolRules, *,
                             disabled_servers: Iterable[str] = ()) -> list[tuple[str, str]]:
    """The sorted ``(server, tool)`` pairs to deny: (default-disabled tools minus the
    session-enabled ones) plus the session-disabled ones. A server that is off at the
    server level contributes none — it is already off, and its tools come back with it."""
    off = set(disabled_servers)
    pairs = {(s, t) for src in (rules.defaults, rules.overrides) for s, ts in src.items()
             if s not in off for t in ts}
    return sorted(p for p in pairs if not rules.state(*p)[0])


def tools_wire(server: str, tools: Iterable[Any] | None,
               rules: McpToolRules | None) -> list[dict] | None:
    """*server*'s ``tools[]`` as served: each ``McpTool.to_wire`` plus ``enabled`` and
    ``source`` (``default`` | ``session``). None — the "unavailable" signal, no switches —
    when the tool list or the rules are unknown."""
    if tools is None or rules is None:
        return None
    out = []
    for t in tools:
        enabled, source = rules.state(server, t.name)
        out.append({**t.to_wire(), "enabled": enabled, "source": source})
    return out


def servers_wire(states: Iterable[Any], rules: McpToolRules | None) -> list[dict]:
    """Sanitized ``McpServerState`` rows as the session MCP endpoint serves them:
    ``to_wire`` plus ``tools`` (``tools_wire``)."""
    return [{**s.to_wire(), "tools": tools_wire(s.name, s.tools, rules)} for s in states]
