"""Web Push subscription API: VAPID public key + subscribe/unsubscribe."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from server import config
from server.auth.middleware import current_identity
from server.db import get_db
from server.sessions import push_store

router = APIRouter(prefix="/api/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeBody(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class UnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/config")
async def push_config(identity: str = Depends(current_identity)) -> dict:
    return {"vapidPublicKey": config.VAPID_PUBLIC_KEY, "enabled": bool(config.VAPID_PRIVATE_KEY)}


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    await push_store.upsert_subscription(
        db, endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth, identity=identity)
    await db.commit()
    return {"ok": True}


@router.delete("/subscribe")
async def unsubscribe(body: UnsubscribeBody, identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    await push_store.delete_subscription(db, body.endpoint)
    await db.commit()
    return {"ok": True}
