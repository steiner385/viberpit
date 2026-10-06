"""Authenticated personal quick replies across sessions."""
from fastapi import APIRouter, Depends

from server.auth.middleware import current_identity
from server.quick_replies import get_quick_replies

router = APIRouter(prefix='/api/quick-replies', tags=['quick-replies'])


@router.get('')
async def quick_replies(identity: str = Depends(current_identity)) -> dict[str, list[str]]:
    return {'replies': await get_quick_replies(identity)}
