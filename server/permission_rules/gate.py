"""Enforcement of standing always-allow rules (spec 2026-10-11 §3).

``StandingRuleGate`` is consulted by ``PermissionRegistry`` only where a card would otherwise be
created. It fails CLOSED: any error means "no rule matched" and the human is asked.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from server.audit import record_audit
from server.config import get_settings
from server.db import db_session
from server.permission_rules import store
from server.permission_rules.patterns import Pattern, PatternError, covering, parse
from server.permission_rules.safety import assess, offered_suggestions
from server.sessions.types import SYSTEM_IDENTITY

log = logging.getLogger(__name__)


#: Scope label of a pattern from the session's launch profile (``Session.tool_allowlist``).
#: It has no stats row: hits are audited but not counted.
LAUNCH_SCOPE = "launch"
# Launch patterns have no creation time of their own (RuleView requires one).
_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    pattern: str
    scope: str
    scope_key: str | None
    #: Every rule credited, ``rule_id``'s first (a compound command may need several).
    rule_ids: tuple[str, ...] = field(default=())


def covering_rules(rules: list[store.RuleView], tool: str, input: dict,
                   cwd: str | None) -> list[store.RuleView]:
    """The rules that together cover the request, in first-use order; empty when it is not
    covered. Coverage is decided over ALL rules (a compound Bash command's segments may be
    covered by different rules) and every rule that covered a segment is credited. An
    unparsable stored pattern is skipped with a warning."""
    parsed: list[tuple[Pattern, store.RuleView]] = []
    for r in rules:
        try:
            parsed.append((parse(r.pattern), r))
        except PatternError:
            log.warning("permission_rules: skipping unparsable stored rule %s", r.id or r.pattern)
    return [next(r for p, r in parsed if p is hit)
            for hit in covering([p for p, _ in parsed], tool, input, cwd)]


def launch_rules(session_id: str, patterns: list[str]) -> list[store.RuleView]:
    """The launch profile's patterns that may be enforced, as rules.

    Named-profile patterns were never validated when saved before #2952, so each one goes
    through the same safety check as a session-scope rule; a refused pattern is skipped.
    """
    out: list[store.RuleView] = []
    for pos, pattern in enumerate(patterns):
        if not isinstance(pattern, str):
            continue
        verdict = assess(pattern, "session")
        if verdict.level == "refuse":
            log.info("permission_rules: launch pattern refused session=%s pattern=%r reasons=%s",
                     session_id, pattern, "; ".join(verdict.reasons))
            continue
        out.append(store.RuleView(
            id="", profile_id="", pattern=pattern, scope=LAUNCH_SCOPE, scope_key=session_id,
            position=pos, created_by="launch-profile", created_via=None,
            created_at=_EPOCH, hit_count=0, last_hit_at=None))
    return out


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
                # Most specific first: this session's own rules, then its launch profile,
                # then the repo and global rule sets.
                n_session = sum(1 for r in rules if r.scope == "session")
                launch = launch_rules(session_id, await store.launch_patterns(db, session_id))
                rules = [*rules[:n_session], *launch, *rules[n_session:]]
                credited = covering_rules(rules, tool, input, cwd)
                if not credited:
                    return None
                for r in credited:
                    if r.scope != LAUNCH_SCOPE:
                        await store.record_hit(db, r.id)
                rule = credited[0]
                detail: dict[str, object] = {
                    "sessionId": session_id, "ruleId": rule.id or None, "pattern": rule.pattern,
                    "scope": rule.scope, "tool": tool, "trigger": "request"}
                if len(credited) > 1:
                    detail["ruleIds"] = [r.id or None for r in credited]
                    detail["patterns"] = [r.pattern for r in credited]
                await record_audit(db, "permission.rule_allow", SYSTEM_IDENTITY, detail,
                                   via="rule")
                return RuleHit(rule.id, rule.pattern, rule.scope, rule.scope_key,
                               tuple(r.id for r in credited))
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
