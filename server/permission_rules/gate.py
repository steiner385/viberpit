"""Enforcement of standing always-allow rules (spec 2026-10-11 §3).

``StandingRuleGate`` is consulted by ``PermissionRegistry`` only where a card would otherwise be
created. It fails CLOSED: any error means "no rule matched" and the human is asked.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from server.audit import record_audit
from server.config import get_settings
from server.db import db_session
from server.permission_rules import store
from server.permission_rules.patterns import Pattern, PatternError, matches_any, parse
from server.permission_rules.safety import offered_suggestions
from server.sessions.types import SYSTEM_IDENTITY

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    pattern: str
    scope: str
    scope_key: str | None


def first_match(rules: list[store.RuleView], tool: str, input: dict,
                cwd: str | None) -> store.RuleView | None:
    """The rule that covers the request, or None. Shared with the edge's approval path so both
    match identically. Coverage is decided over ALL rules (a compound Bash command's segments
    may be covered by different rules); the hit is attributed to the rule whose pattern
    ``matches_any`` returned. An unparsable stored pattern is skipped with a warning."""
    parsed: list[tuple[Pattern, store.RuleView]] = []
    for r in rules:
        try:
            parsed.append((parse(r.pattern), r))
        except PatternError:
            log.warning("permission_rules: skipping unparsable stored rule %s", r.id)
    winner = matches_any([p for p, _ in parsed], tool, input, cwd)
    if winner is None:
        return None
    return next(r for p, r in parsed if p is winner)


class StandingRuleGate:
    def __init__(self, facts: Callable[[str], tuple[str | None, str | None] | None]):
        self._facts = facts

    @staticmethod
    def _enabled() -> bool:
        return bool(get_settings().permission_rules_enabled)

    async def match(self, session_id: str, tool: str, input: dict) -> RuleHit | None:
        try:
            if not self._enabled():
                return None
            facts = self._facts(session_id)
            if facts is None:
                return None
            repo, cwd = facts
            async with db_session() as db:
                rules = await store.applicable_rules(db, session_id, repo)
                rule = first_match(rules, tool, input, cwd)
                if rule is None:
                    return None
                await store.record_hit(db, rule.id)
                await record_audit(
                    db, "permission.rule_allow", SYSTEM_IDENTITY,
                    {"sessionId": session_id, "ruleId": rule.id, "pattern": rule.pattern,
                     "scope": rule.scope, "tool": tool, "trigger": "request"},
                    via="rule")
                return RuleHit(rule.id, rule.pattern, rule.scope, rule.scope_key)
        except Exception:
            log.warning("permission_rules.gate_failed session=%s tool=%s", session_id, tool,
                        exc_info=True)
            return None

    def suggestions(self, session_id: str, tool: str, input: dict) -> list[dict]:
        try:
            if not self._enabled():
                return []
            facts = self._facts(session_id)
            if facts is None:
                return []
            return offered_suggestions(tool, input, facts[1])
        except Exception:
            log.warning("permission_rules.suggestions_failed session=%s tool=%s", session_id,
                        tool, exc_info=True)
            return []
