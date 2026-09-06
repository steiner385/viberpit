"""Fire-and-forget push to an external webhook (ntfy / generic). Never raises."""
from __future__ import annotations
import asyncio, json, logging, urllib.request, base64
from typing import TYPE_CHECKING
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from server import config
from server.db import db_session
from server.sessions import push_store
# Same literal server/sessions/types.py defines for SessionRecord.kind — reused
# here for NotificationChannel.kind rather than adding a second constant that
# would carry the identical value.
from server.sessions.types import ASSISTANT_KIND

if TYPE_CHECKING:
    # Typing-only: importing SessionOrchestrator at runtime would cycle back here
    # (orchestrator.py imports this module to call notify_event).
    from server.sessions.orchestrator import SessionOrchestrator

log = logging.getLogger(__name__)


def _post(url: str, payload: dict, opener) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with opener(req, timeout=5):
        pass


async def push(
    title: str,
    body: str,
    *,
    url: str | None = None,
    link: str | None = None,
    opener=None,
) -> bool:
    """POST {title, body, url?} to the webhook. ``url`` overrides the configured
    target endpoint; ``link`` is the in-app deep-link carried in the payload."""
    opener = opener or urllib.request.urlopen
    target = url if url is not None else config.PUSH_WEBHOOK_URL
    if not target:
        return False
    payload = {"title": title, "body": body}
    if link:
        payload["url"] = link
    try:
        await asyncio.to_thread(_post, target, payload, opener)
        return True
    except Exception:
        log.warning("push.failed", exc_info=True)
        return False


def _send_one(subscription_info: dict, data: str, *, sender) -> int:
    """Send one Web Push. Returns the HTTP status; raises WebPushException on failure."""
    resp = sender(
        subscription_info=subscription_info,
        data=data,
        vapid_private_key=config.VAPID_PRIVATE_KEY,
        vapid_claims={"sub": config.VAPID_SUBJECT},
        timeout=5,
    )
    return getattr(resp, "status_code", 0)


async def _webpush_sink(payload: dict, *, sender=webpush) -> None:
    """Send `payload` to every stored subscription. Prune 404/410. Never raises."""
    if not config.VAPID_PRIVATE_KEY:
        return
    data = json.dumps(payload)
    async with db_session() as db:
        subs = await push_store.list_subscriptions(db)
        dead: list[str] = []
        for s in subs:
            sub_info = {"endpoint": s.endpoint,
                        "keys": {"p256dh": s.p256dh, "auth": s.auth}}
            try:
                await asyncio.to_thread(_send_one, sub_info, data, sender=sender)
            except WebPushException as e:
                if getattr(getattr(e, "response", None), "status_code", None) in (404, 410):
                    dead.append(s.endpoint)
                else:
                    log.warning("webpush.send_failed", exc_info=True)
            except Exception:
                log.warning("webpush.send_failed", exc_info=True)
        for endpoint in dead:
            await push_store.delete_subscription(db, endpoint)
        if dead:
            await db.commit()


async def notify(title: str, body: str, *, url: str | None = None) -> None:
    """Fan a notification out to all sinks (webhook + web-push). Fire-and-forget; never raises."""
    try:
        # ``url`` is the in-app deep-link, not the webhook endpoint — pass it as
        # the payload link so the webhook target stays config.PUSH_WEBHOOK_URL.
        await push(title, body, link=url)
    except Exception:
        log.warning("notify.webhook_failed", exc_info=True)
    payload = {"title": title, "body": body}
    if url:
        payload["url"] = url
    try:
        await _webpush_sink(payload)
    except Exception:
        log.warning("notify.webpush_failed", exc_info=True)


def _deliver_channel(url: str, headers: dict | None, payload: dict, opener) -> None:
    """POST a notification payload to a user webhook channel with optional headers."""
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
    with opener(req, timeout=5):
        pass


async def notify_event(
    event_class: str,
    title: str,
    body: str,
    *,
    repo: str | None = None,
    account: str | None = None,
    automation: str | None = None,
    session_id: str | None = None,
    url: str | None = None,
    critical: bool = False,
    fanout_when_unconfigured: bool = True,
    orch: "SessionOrchestrator | None" = None,
) -> None:
    """Route a *classified* event through the user's notification rules (#602).

    ``fanout_when_unconfigured`` controls the no-rules-configured case: True
    preserves the legacy push+webhook fanout (used by needs-input / PR-broke,
    which always alerted); False stays silent unless a rule explicitly matches
    (used by automation error/warn, which never alerted before rules existed —
    so enabling rules must not suddenly spam every automation warning).

    ``orch`` is the calling daemon's SessionOrchestrator, needed only to reach
    ``post_notice`` when the winning rule's channel is an assistant watch (Task
    8.4, spec v2 §4.7 Pack N). Every call site inside orchestrator.py passes
    ``self``. AutomationContext.emit (server/automations/context.py) has no
    orchestrator reference at all and passes None — fine, since that path is
    already narrow (fanout_when_unconfigured=False) and this whole branch fails
    open to the unconditional web push either way; a worker daemon's local
    notify_event call similarly finds no assistant record for an identity whose
    thread lives on the primary and just fails open the same way.

    Loads rules/channels/quiet-hours from the DB, evaluates the pure routing
    engine, and delivers to web-push or a webhook channel accordingly. Fails
    OPEN to the legacy fanout (:func:`notify`) on any routing error, so a bug in
    the rules layer can never silence an alert. Fire-and-forget; never raises."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from server.db.models import AppSettings, NotificationChannel, NotificationRule
    from server.sessions.notify_rules import Event, RuleView, plan

    try:
        async with db_session() as db:
            rows = (await db.execute(select(NotificationRule))).scalars().all()
            rules = [
                RuleView(
                    id=r.id, enabled=r.enabled, event_class=r.event_class,
                    scope_repo=r.scope_repo, scope_account=r.scope_account,
                    scope_automation=r.scope_automation, action=r.action,
                    channel_id=r.channel_id, critical=r.critical,
                    priority=r.priority, created_at=r.created_at,
                    once=bool(r.once), owner_session_id=r.owner_session_id,
                    expires_at=r.expires_at, scope_session_id=r.scope_session_id,
                )
                for r in rows
            ]
            # owner_session_id lives on the RULE, not the channel (a watch's
            # NotificationChannel(kind="assistant") is one per IDENTITY, shared by
            # every watch that identity creates — see server/assistant/api.py's
            # _get_or_create_assistant_channel). The decision only carries back
            # rule_id, so the assistant-delivery branch below looks the owner
            # session up from these already-loaded rows rather than re-querying.
            owner_session_by_rule = {r.id: r.owner_session_id for r in rows}
            settings = (await db.execute(select(AppSettings).where(
                AppSettings.id == "singleton"))).scalar_one_or_none()
            q_start = settings.quiet_hours_start if settings else None
            q_end = settings.quiet_hours_end if settings else None
            channels = {c.id: c for c in (await db.execute(
                select(NotificationChannel))).scalars().all()}
        ev = Event(event_class=event_class, title=title, body=body,
                   repo=repo, account=account, automation=automation, url=url,
                   session_id=session_id)
        decision = plan(ev, rules, q_start, q_end, datetime.now(timezone.utc))
    except Exception:
        log.warning("notify_event.routing_failed", exc_info=True)
        await notify(title, body, url=url)  # fail open to legacy fanout
        return

    if not decision.deliver:
        return
    if decision.action == "default":
        if fanout_when_unconfigured:
            await notify(title, body, url=url)  # no rules configured → legacy fanout
        return
    if decision.action == "push":
        payload = {"title": title, "body": body}
        if url:
            payload["url"] = url
        try:
            await _webpush_sink(payload)
        except Exception:
            log.warning("notify_event.webpush_failed", exc_info=True)
        return
    if decision.action == "channel" and decision.channel_id:
        chan = channels.get(decision.channel_id)
        if chan is None or not chan.enabled:
            return
        if chan.kind == ASSISTANT_KIND:
            owner_session_id = (owner_session_by_rule.get(decision.rule_id)
                               if decision.rule_id is not None else None)
            await _deliver_assistant_notice(
                decision, event_class, title, body, url, orch, owner_session_id)
            return
        payload = {"title": title, "message": body, "body": body}
        if url:
            payload["url"] = url
        try:
            await asyncio.to_thread(
                _deliver_channel, chan.url, chan.headers, payload, urllib.request.urlopen)
        except Exception:
            log.warning("notify_event.channel_failed", exc_info=True)


async def _deliver_assistant_notice(
    decision, event_class: str, title: str, body: str, url: str | None,
    orch: "SessionOrchestrator | None", owner_session_id: str | None,
) -> None:
    """The assistant-channel half of a "channel" decision (Task 8.4, spec v2
    §4.7 Pack N): web push fires regardless (same payload the plain "push"
    action sends — the spec's "Web push is sent regardless"), then the notice
    is delivered into the owner's drawer via ``orch.post_notice``, then a
    once-rule is disabled so it never fires again. Never raises — every step is
    independently best-effort, matching the rest of this module."""
    payload = {"title": title, "body": body}
    if url:
        payload["url"] = url
    try:
        await _webpush_sink(payload)
    except Exception:
        log.warning("notify_event.webpush_failed", exc_info=True)

    if orch is not None and owner_session_id is not None:
        try:
            rec = orch.get_session(owner_session_id)
            identity = getattr(rec, "owner_identity", None) if rec is not None else None
            if identity:
                await orch.post_notice(identity, {
                    "kind": event_class, "title": title, "body": body,
                    "url": url, "watchId": decision.rule_id,
                })
        except Exception:
            log.warning("notify_event.assistant_notice_failed", exc_info=True)

    if decision.once and decision.rule_id:
        try:
            from sqlalchemy import update as sa_update

            from server.db.models import NotificationRule
            async with db_session() as db:
                await db.execute(
                    sa_update(NotificationRule)
                    .where(NotificationRule.id == decision.rule_id)
                    .values(enabled=False)
                )
                await db.commit()
        except Exception:
            log.warning("notify_event.once_disable_failed", exc_info=True)


def generate_vapid_keys() -> tuple[str, str]:
    """Generate a VAPID keypair. Returns (private_key_pem, application_server_key_b64url).

    The PEM private key is what pywebpush signs with; the application server key is the
    unpadded-base64url uncompressed P-256 point the browser passes as applicationServerKey.
    Run once: `python -m server.sessions.push --keygen`, paste the output into the env file.
    """
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    point = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    app_server_key = base64.urlsafe_b64encode(point).rstrip(b"=").decode()
    return pem, app_server_key


if __name__ == "__main__":  # pragma: no cover
    import sys
    from server import hostpaths
    if "--keygen" in sys.argv:
        priv, pub = generate_vapid_keys()
        print(f"# Paste into {hostpaths.ENV_FILE}, then restart the services:")
        print(f"CCSWITCH_VAPID_PRIVATE_KEY={priv!r}")
        print(f"CCSWITCH_VAPID_PUBLIC_KEY={pub}")
