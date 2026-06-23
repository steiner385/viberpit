"""Config API — exposes server-side config that the frontend needs."""
from fastapi import APIRouter, Depends
from server.auth.middleware import current_identity
from server.config import get_settings

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_config(_identity: str = Depends(current_identity)) -> dict:
    settings = get_settings()
    return {"repoRoot": settings.repo_root, "adoptionEnabled": settings.enable_adoption}
