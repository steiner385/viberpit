"""Fire-and-forget push to an external webhook (ntfy / generic). Never raises."""
from __future__ import annotations
import asyncio, json, logging, urllib.request, base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from server import config
from server.db import db_session
from server.sessions import push_store

log = logging.getLogger(__name__)


def _post(url: str, payload: dict, opener) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with opener(req, timeout=5):
        pass


async def push(title: str, body: str, *, url: str | None = None, opener=urllib.request.urlopen) -> bool:
    target = url if url is not None else config.PUSH_WEBHOOK_URL
    if not target:
        return False
    try:
        await asyncio.to_thread(_post, target, {"title": title, "body": body}, opener)
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
        await push(title, body, url=url)
    except Exception:
        log.warning("notify.webhook_failed", exc_info=True)
    payload = {"title": title, "body": body}
    if url:
        payload["url"] = url
    try:
        await _webpush_sink(payload)
    except Exception:
        log.warning("notify.webpush_failed", exc_info=True)


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
    if "--keygen" in sys.argv:
        priv, pub = generate_vapid_keys()
        print("# Paste into /etc/ccswitch/ccswitch.env, then restart the services:")
        print(f"CCSWITCH_VAPID_PRIVATE_KEY={priv!r}")
        print(f"CCSWITCH_VAPID_PUBLIC_KEY={pub}")
