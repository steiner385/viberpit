"""Storage for standing always-allow rules (spec 2026-10-11 §2).

A rule is one pattern string in an auto-managed, scoped ``PermissionProfile`` (one profile per
global / repo / session scope); ``PermissionRuleStats`` carries its public id, author and hit
counters. Nothing here assesses safety -- callers do. Every function takes an open session and
never commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from server.db.clock import db_utc_now
from server.db.models import PermissionProfile, PermissionRuleStats, Session, _utcnow
from server.permission_rules.patterns import describe
from server.permission_rules.safety import SCOPES

PROFILE_PREFIX = "always-allow:"


def profile_name(scope: str, scope_key: str | None) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    if scope == "global":
        if scope_key is not None:
            raise ValueError("global scope takes no scope_key")
        return f"{PROFILE_PREFIX}global"
    if not scope_key:
        raise ValueError(f"{scope} scope requires a scope_key")
    return f"{PROFILE_PREFIX}{scope}:{scope_key}"


def _iso(ts: datetime) -> str:
    """Stored timestamps are naive UTC; mark them so the wire format is unambiguous."""
    return ts.isoformat() + "Z" if ts.tzinfo is None else ts.isoformat()


@dataclass
class RuleView:
    id: str
    profile_id: str
    pattern: str
    scope: str
    scope_key: str | None
    position: int
    created_by: str
    created_via: str | None
    created_at: datetime
    hit_count: int
    last_hit_at: datetime | None

    def to_wire(self) -> dict:
        try:
            description = describe(self.pattern)
        except Exception:  # a stored pattern the grammar no longer parses still lists
            description = self.pattern
        return {
            "id": self.id,
            "profileId": self.profile_id,
            "pattern": self.pattern,
            "description": description,
            "scope": self.scope,
            "scopeKey": self.scope_key,
            "position": self.position,
            "createdBy": self.created_by,
            "createdVia": self.created_via,
            "createdAt": _iso(self.created_at),
            "hitCount": self.hit_count,
            "lastHitAt": _iso(self.last_hit_at) if self.last_hit_at else None,
        }


def _view(profile: PermissionProfile, stats: PermissionRuleStats, position: int) -> RuleView:
    return RuleView(
        id=stats.id, profile_id=profile.id, pattern=stats.pattern,
        scope=profile.scope or "", scope_key=profile.scope_key, position=position,
        created_by=stats.created_by, created_via=stats.created_via,
        created_at=stats.created_at, hit_count=stats.hit_count or 0,
        last_hit_at=stats.last_hit_at,
    )


async def _locked_profile(db: AsyncSession, name: str) -> PermissionProfile | None:
    # with_for_update serialises concurrent writers on Postgres (SQLite ignores it).
    return (await db.execute(
        select(PermissionProfile).where(PermissionProfile.name == name).with_for_update()
    )).scalar_one_or_none()


async def _profile_for_stats(db: AsyncSession, stats: PermissionRuleStats) -> PermissionProfile | None:
    return (await db.execute(
        select(PermissionProfile).where(PermissionProfile.id == stats.profile_id).with_for_update()
    )).scalar_one_or_none()


def _check_managed(profile: PermissionProfile, scope: str, scope_key: str | None) -> None:
    if profile.scope != scope or profile.scope_key != scope_key:
        raise RuntimeError(f"profile {profile.name!r} is not the managed {scope} auto-profile")


async def _get_or_create_profile(db: AsyncSession, scope: str, scope_key: str | None) -> PermissionProfile:
    name = profile_name(scope, scope_key)
    profile = await _locked_profile(db, name)
    if profile is not None:
        _check_managed(profile, scope, scope_key)
        return profile
    try:
        async with db.begin_nested():
            db.add(PermissionProfile(
                name=name, description=f"Always-allow rules ({scope})", mode=None,
                patterns=[], scope=scope, scope_key=scope_key,
            ))
    except IntegrityError:  # lost the race on the unique name; the winner's row is there now
        pass
    profile = await _locked_profile(db, name)
    if profile is None:
        raise RuntimeError(f"auto-profile {name!r} vanished after create")
    _check_managed(profile, scope, scope_key)
    return profile


async def _refreshed(db: AsyncSession, stats: PermissionRuleStats) -> bool:
    """Re-read ``stats`` once its profile is locked: a concurrent writer may have moved it, or
    revoked it while we waited on the lock (False -- the rule is gone)."""
    try:
        await db.refresh(stats)
    except InvalidRequestError:
        return False
    return True


async def _stats_for(db: AsyncSession, profile_id: str, pattern: str) -> PermissionRuleStats | None:
    return (await db.execute(
        select(PermissionRuleStats).where(
            PermissionRuleStats.profile_id == profile_id, PermissionRuleStats.pattern == pattern)
    )).scalar_one_or_none()


async def add_rule(db: AsyncSession, *, scope: str, scope_key: str | None, pattern: str,
                   created_by: str, created_via: str | None) -> RuleView:
    profile = await _get_or_create_profile(db, scope, scope_key)
    patterns = list(profile.patterns or [])
    stats = await _stats_for(db, profile.id, pattern)
    if pattern in patterns and stats is not None:
        return _view(profile, stats, patterns.index(pattern))
    if pattern not in patterns:
        patterns.append(pattern)
        profile.patterns = patterns
        profile.updated_at = _utcnow()
    if stats is None:
        stats = PermissionRuleStats(profile_id=profile.id, pattern=pattern,
                                    created_by=created_by, created_via=created_via)
        db.add(stats)
    await db.flush()
    return _view(profile, stats, patterns.index(pattern))


async def _views_for(db: AsyncSession, profiles: list[PermissionProfile]) -> list[RuleView]:
    if not profiles:
        return []
    stats_rows = (await db.execute(
        select(PermissionRuleStats).where(PermissionRuleStats.profile_id.in_([p.id for p in profiles]))
    )).scalars().all()
    by_key = {(s.profile_id, s.pattern): s for s in stats_rows}
    out: list[RuleView] = []
    for p in profiles:
        for pos, pat in enumerate(p.patterns or []):
            s = by_key.get((p.id, pat))
            if s is not None:
                out.append(_view(p, s, pos))
    return out


def _sort_key(v: RuleView) -> tuple[int, str, int]:
    return (SCOPES.index(v.scope), v.scope_key or "", v.position)


async def get_rule(db: AsyncSession, rule_id: str) -> RuleView | None:
    stats = await db.get(PermissionRuleStats, rule_id)
    if stats is None:
        return None
    profile = await db.get(PermissionProfile, stats.profile_id)
    if profile is None or stats.pattern not in (profile.patterns or []):
        return None
    return _view(profile, stats, (profile.patterns or []).index(stats.pattern))


async def list_rules(db: AsyncSession, *,
                     scopes: list[tuple[str, str | None]] | None = None,
                     hide_archived_sessions: bool = False) -> list[RuleView]:
    """Every rule, or those in ``scopes``. ``hide_archived_sessions`` drops the rule sets of
    archived sessions (unarchiving brings them back; a permanent delete removes them)."""
    profiles = list((await db.execute(
        select(PermissionProfile).where(PermissionProfile.scope.is_not(None))
    )).scalars().all())
    if scopes is not None:
        wanted = set(scopes)
        profiles = [p for p in profiles if (p.scope, p.scope_key) in wanted]
    if hide_archived_sessions:
        keys = [p.scope_key for p in profiles if p.scope == "session" and p.scope_key]
        archived = set((await db.execute(
            select(Session.id).where(Session.id.in_(keys), Session.status == "archived")
        )).scalars().all()) if keys else set()
        profiles = [p for p in profiles
                    if not (p.scope == "session" and p.scope_key in archived)]
    return sorted(await _views_for(db, profiles), key=_sort_key)


def applicable_scopes(session_id: str, repo: str | None) -> list[tuple[str, str | None]]:
    scopes: list[tuple[str, str | None]] = [("session", session_id)]
    if repo:
        scopes.append(("repo", repo))
    scopes.append(("global", None))
    return scopes


async def applicable_rules(db: AsyncSession, session_id: str, repo: str | None) -> list[RuleView]:
    return await list_rules(db, scopes=applicable_scopes(session_id, repo))


async def launch_patterns(db: AsyncSession, session_id: str) -> list[str]:
    """The patterns of the profile the session was launched with (``Session.tool_allowlist``),
    unassessed; empty when it had none."""
    raw = (await db.execute(
        select(Session.tool_allowlist).where(Session.id == session_id)
    )).scalar_one_or_none()
    return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []


async def delete_session_rules(db: AsyncSession, session_id: str) -> int:
    """Drop a session's own rule set and its stats rows (the session is being permanently
    deleted). Returns how many rules went."""
    profile = await _locked_profile(db, profile_name("session", session_id))
    if profile is None:
        return 0
    n = len(profile.patterns or [])
    await db.execute(
        delete(PermissionRuleStats).where(PermissionRuleStats.profile_id == profile.id))
    await db.delete(profile)
    await db.flush()
    return n


async def update_rule(db: AsyncSession, rule_id: str, *, new_pattern: str) -> RuleView | None:
    stats = await db.get(PermissionRuleStats, rule_id)
    if stats is None:
        return None
    profile = await _profile_for_stats(db, stats)
    if profile is None or not await _refreshed(db, stats):
        return None
    patterns = list(profile.patterns or [])
    if stats.pattern not in patterns:
        return None
    if new_pattern == stats.pattern:
        return _view(profile, stats, patterns.index(stats.pattern))
    if new_pattern in patterns:
        raise ValueError("duplicate")
    pos = patterns.index(stats.pattern)
    patterns[pos] = new_pattern
    profile.patterns = patterns
    profile.updated_at = _utcnow()
    stats.pattern = new_pattern
    stats.hit_count = 0
    stats.last_hit_at = None
    await db.flush()
    return _view(profile, stats, pos)


async def revoke_rule(db: AsyncSession, rule_id: str) -> RuleView | None:
    stats = await db.get(PermissionRuleStats, rule_id)
    if stats is None:
        return None
    profile = await _profile_for_stats(db, stats)
    if profile is not None and not await _refreshed(db, stats):
        return None
    patterns = list(profile.patterns or []) if profile else []
    view = (_view(profile, stats, patterns.index(stats.pattern))
            if profile is not None and stats.pattern in patterns else None)
    await db.execute(delete(PermissionRuleStats).where(PermissionRuleStats.id == rule_id))
    if profile is not None:
        if stats.pattern in patterns:
            patterns.remove(stats.pattern)
        if patterns:
            profile.patterns = patterns
            profile.updated_at = _utcnow()
        else:
            await db.delete(profile)
    await db.flush()
    return view


async def record_hit(db: AsyncSession, rule_id: str) -> None:
    await db.execute(
        update(PermissionRuleStats).where(PermissionRuleStats.id == rule_id)
        .values(hit_count=PermissionRuleStats.hit_count + 1, last_hit_at=db_utc_now())
    )
