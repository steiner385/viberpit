"""Fire-and-forget push to an external webhook (ntfy / generic). Never raises."""
from __future__ import annotations
import asyncio, json, logging, urllib.request
from server import config

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
