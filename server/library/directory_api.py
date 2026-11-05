"""The live Anthropic Directory API (public, unauthenticated), used to verify a cached
listing's review state before an install: the cache file lives in the shared config dir, so
any session can forge it. ``fetch_listing`` is the one seam; every failure is ``None`` and
the caller fails closed (never falls back to the cache)."""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

DIRECTORY_URL = "https://api.anthropic.com/api/directory/plugins"
DIRECTORY_TIMEOUT_S = 15.0
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")


async def fetch_listing(listing_id: str, *,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """One Directory listing as the API returns it, or ``None`` on a bad id, any non-200
    status, transport error, timeout or non-object body."""
    if not isinstance(listing_id, str) or not _ID_RE.fullmatch(listing_id):
        return None
    try:
        async with httpx.AsyncClient(timeout=DIRECTORY_TIMEOUT_S, transport=transport) as client:
            resp = await client.get(f"{DIRECTORY_URL}/{listing_id}",
                                    headers={"User-Agent": "viberpit"})
        if resp.status_code != 200:
            log.info("directory_api.status listing=%s status=%s", listing_id, resp.status_code)
            return None
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("directory_api.unreachable listing=%s error=%s", listing_id, type(exc).__name__)
        return None
    return body if isinstance(body, dict) else None
