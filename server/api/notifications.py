"""Notifications API (#602): channels, routing rules, quiet hours, and a
routing-preview "test" endpoint.

Server-side webhook delivery (avoiding the prod insecure-context constraint)
happens at the notify() call site; this router is CRUD over the DB models plus a
dry-run of the pure engine in :mod:`server.sessions.notify_rules`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from server.auth.middleware import current_identity
from server.db import get_db
from server.db.models import AppSettings, NotificationChannel, NotificationRule
from server.sessions.notify_rules import (
    EVENT_CLASSES,
    Event,
    RuleView,
    plan,
    select_rule,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_VALID_ACTIONS = {"push", "channel", "ignore"}


# --------------------------------------------------------------------------- #
# Serialization                                                               #
# --------------------------------------------------------------------------- #
def _channel_dict(c: NotificationChannel) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "kind": c.kind,
        "url": c.url,
        "headers": c.headers,
        "enabled": c.enabled,
        "createdAt": c.created_at.isoformat() if c.created_at else None,
    }


def _rule_dict(r: NotificationRule) -> dict:
    return {
        "id": r.id,
        "enabled": r.enabled,
        "eventClass": r.event_class,
        "scopeRepo": r.scope_repo,
        "scopeAccount": r.scope_account,
        "scopeAutomation": r.scope_automation,
        "action": r.action,
        "channelId": r.channel_id,
        "critical": r.critical,
        "priority": r.priority,
        "createdAt": r.created_at.isoformat() if r.created_at else None,
    }


def _rule_view(r: NotificationRule) -> RuleView:
    return RuleView(
        id=r.id,
        enabled=r.enabled,
        event_class=r.event_class,
        scope_repo=r.scope_repo,
        scope_account=r.scope_account,
        scope_automation=r.scope_automation,
        action=r.action,
        channel_id=r.channel_id,
        critical=r.critical,
        priority=r.priority,
        created_at=r.created_at,
        once=bool(r.once),
        owner_session_id=r.owner_session_id,
        expires_at=r.expires_at,
    )


async def _get_or_create_settings(db) -> AppSettings:
    row = (
        await db.execute(select(AppSettings).where(AppSettings.id == "singleton"))
    ).scalar_one_or_none()
    if row is None:
        row = AppSettings(id="singleton")
        db.add(row)
        await db.flush()
    return row


def _valid_hhmm(value: str | None) -> bool:
    if value is None:
        return True
    parts = value.split(":")
    if len(parts) != 2:
        return False
    hh, mm = parts
    if not (hh.isdigit() and mm.isdigit()):
        return False
    h, m = int(hh), int(mm)
    return 0 <= h <= 23 and 0 <= m <= 59


def _validate_webhook_url(url: str) -> None:
    """Validate a user-supplied webhook target (SSRF defense-in-depth).

    This surface is operator-only (behind tailnet-identity auth, constitution IV):
    the actor configuring a channel is the trusted host operator, so loopback and
    LAN/tailnet targets (e.g. a local ntfy on 127.0.0.1 or a 100.64/10 tailnet
    host) are legitimate and allowed. What is *never* a legitimate notification
    target is the cloud instance-metadata service / link-local range — the classic
    SSRF pivot to steal instance credentials — so we reject those outright. Raises
    HTTPException(422) on a bad scheme or a blocked host."""
    from ipaddress import ip_address
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(422, "url must be an http:// or https:// URL")
    if not parsed.hostname:
        raise HTTPException(422, "url must include a host")
    host = parsed.hostname
    # Block the cloud metadata endpoint by name and any link-local IP literal.
    if host in ("metadata.google.internal",):
        raise HTTPException(422, "url host is not an allowed webhook target")
    try:
        ip = ip_address(host)
    except ValueError:
        return  # a DNS name — trusted-operator surface, allow (see docstring)
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise HTTPException(422, "url host is not an allowed webhook target")


# --------------------------------------------------------------------------- #
# Channels                                                                    #
# --------------------------------------------------------------------------- #
class ChannelBody(BaseModel):
    name: str
    url: str
    kind: str = "webhook"
    headers: dict | None = None
    enabled: bool = True


class ChannelPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    kind: str | None = None
    headers: dict | None = None
    enabled: bool | None = None


@router.get("/channels")
async def list_channels(
    _identity: str = Depends(current_identity), db=Depends(get_db)
) -> list[dict]:
    rows = (
        await db.execute(select(NotificationChannel).order_by(NotificationChannel.created_at))
    ).scalars().all()
    return [_channel_dict(c) for c in rows]


@router.post("/channels")
async def create_channel(
    body: ChannelBody, _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    _validate_webhook_url(body.url)
    ch = NotificationChannel(
        name=body.name,
        url=body.url,
        kind=body.kind or "webhook",
        headers=body.headers,
        enabled=body.enabled,
    )
    db.add(ch)
    await db.flush()
    await db.commit()
    return _channel_dict(ch)


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    body: ChannelPatch,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    ch = (
        await db.execute(
            select(NotificationChannel).where(NotificationChannel.id == channel_id)
        )
    ).scalar_one_or_none()
    if ch is None:
        raise HTTPException(404, "channel not found")
    if body.url is not None:
        _validate_webhook_url(body.url)
        ch.url = body.url
    if body.name is not None:
        ch.name = body.name
    if body.kind is not None:
        ch.kind = body.kind
    if body.headers is not None:
        ch.headers = body.headers
    if body.enabled is not None:
        ch.enabled = body.enabled
    await db.flush()
    await db.commit()
    return _channel_dict(ch)


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: str, _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    ch = (
        await db.execute(
            select(NotificationChannel).where(NotificationChannel.id == channel_id)
        )
    ).scalar_one_or_none()
    if ch is None:
        raise HTTPException(404, "channel not found")
    # Null out any rule that referenced this channel so it never dangles.
    referencing = (
        await db.execute(
            select(NotificationRule).where(NotificationRule.channel_id == channel_id)
        )
    ).scalars().all()
    for rule in referencing:
        rule.channel_id = None
    await db.delete(ch)
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Rules                                                                       #
# --------------------------------------------------------------------------- #
class RuleBody(BaseModel):
    eventClass: str = "*"
    scopeRepo: str | None = None
    scopeAccount: str | None = None
    scopeAutomation: str | None = None
    action: str = "push"
    channelId: str | None = None
    critical: bool = False
    priority: int = 100
    enabled: bool = True


class RulePatch(BaseModel):
    eventClass: str | None = None
    scopeRepo: str | None = None
    scopeAccount: str | None = None
    scopeAutomation: str | None = None
    action: str | None = None
    channelId: str | None = None
    critical: bool | None = None
    priority: int | None = None
    enabled: bool | None = None


async def _validate_rule(
    db, *, event_class: str, action: str, channel_id: str | None
) -> None:
    if event_class != "*" and event_class not in EVENT_CLASSES:
        raise HTTPException(422, f"invalid eventClass {event_class!r}")
    if action not in _VALID_ACTIONS:
        raise HTTPException(422, f"invalid action {action!r}")
    if action == "channel":
        if channel_id is None:
            raise HTTPException(422, "action 'channel' requires channelId")
        exists = (
            await db.execute(
                select(NotificationChannel.id).where(NotificationChannel.id == channel_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(422, f"channel {channel_id!r} does not exist")


@router.get("/rules")
async def list_rules(
    _identity: str = Depends(current_identity), db=Depends(get_db)
) -> list[dict]:
    rows = (
        await db.execute(
            select(NotificationRule).order_by(
                NotificationRule.priority, NotificationRule.created_at
            )
        )
    ).scalars().all()
    return [_rule_dict(r) for r in rows]


@router.post("/rules")
async def create_rule(
    body: RuleBody, _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    await _validate_rule(
        db, event_class=body.eventClass, action=body.action, channel_id=body.channelId
    )
    rule = NotificationRule(
        enabled=body.enabled,
        event_class=body.eventClass,
        scope_repo=body.scopeRepo,
        scope_account=body.scopeAccount,
        scope_automation=body.scopeAutomation,
        action=body.action,
        channel_id=body.channelId,
        critical=body.critical,
        priority=body.priority,
    )
    db.add(rule)
    await db.flush()
    await db.commit()
    return _rule_dict(rule)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: RulePatch,
    _identity: str = Depends(current_identity),
    db=Depends(get_db),
) -> dict:
    rule = (
        await db.execute(select(NotificationRule).where(NotificationRule.id == rule_id))
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(404, "rule not found")
    # Resolve the post-update values so validation sees the effective state.
    new_event_class = body.eventClass if body.eventClass is not None else rule.event_class
    new_action = body.action if body.action is not None else rule.action
    new_channel_id = body.channelId if body.channelId is not None else rule.channel_id
    await _validate_rule(
        db, event_class=new_event_class, action=new_action, channel_id=new_channel_id
    )
    rule.event_class = new_event_class
    rule.action = new_action
    rule.channel_id = new_channel_id
    if body.scopeRepo is not None:
        rule.scope_repo = body.scopeRepo
    if body.scopeAccount is not None:
        rule.scope_account = body.scopeAccount
    if body.scopeAutomation is not None:
        rule.scope_automation = body.scopeAutomation
    if body.critical is not None:
        rule.critical = body.critical
    if body.priority is not None:
        rule.priority = body.priority
    if body.enabled is not None:
        rule.enabled = body.enabled
    await db.flush()
    await db.commit()
    return _rule_dict(rule)


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str, _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    rule = (
        await db.execute(select(NotificationRule).where(NotificationRule.id == rule_id))
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(404, "rule not found")
    await db.delete(rule)
    await db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Quiet hours                                                                 #
# --------------------------------------------------------------------------- #
class QuietHoursBody(BaseModel):
    start: str | None = None
    end: str | None = None


@router.get("/quiet-hours")
async def get_quiet_hours(
    _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    row = await _get_or_create_settings(db)
    await db.commit()
    return {"start": row.quiet_hours_start, "end": row.quiet_hours_end}


@router.put("/quiet-hours")
async def put_quiet_hours(
    body: QuietHoursBody, _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    if not _valid_hhmm(body.start) or not _valid_hhmm(body.end):
        raise HTTPException(422, "start/end must be 'HH:MM' or null")
    row = await _get_or_create_settings(db)
    row.quiet_hours_start = body.start
    row.quiet_hours_end = body.end
    await db.flush()
    await db.commit()
    return {"start": row.quiet_hours_start, "end": row.quiet_hours_end}


# --------------------------------------------------------------------------- #
# Test / preview                                                              #
# --------------------------------------------------------------------------- #
class TestBody(BaseModel):
    eventClass: str = "needs_input"
    repo: str | None = None
    account: str | None = None
    automation: str | None = None


@router.post("/test")
async def test_routing(
    body: TestBody, _identity: str = Depends(current_identity), db=Depends(get_db)
) -> dict:
    """Dry-run the routing engine for an event. Does NOT deliver anything."""
    from datetime import datetime, timezone

    ev = Event(
        event_class=body.eventClass,
        title="Test notification",
        body="Routing preview",
        repo=body.repo,
        account=body.account,
        automation=body.automation,
    )
    rule_rows = (
        await db.execute(select(NotificationRule))
    ).scalars().all()
    rules = [_rule_view(r) for r in rule_rows]
    settings = await _get_or_create_settings(db)
    await db.commit()
    decision = plan(
        ev, rules, settings.quiet_hours_start, settings.quiet_hours_end, datetime.now(timezone.utc)
    )
    matched = select_rule(rules, ev)
    return {
        "action": decision.action,
        "channelId": decision.channel_id,
        "deliver": decision.deliver,
        "suppressedByQuiet": decision.suppressed_by_quiet,
        "matchedRuleId": matched.id if matched else None,
    }
