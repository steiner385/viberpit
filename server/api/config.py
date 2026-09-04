"""Config API — exposes server-side config that the frontend needs."""
from fastapi import APIRouter, Depends
from server.auth.middleware import current_identity
from server.config import get_settings
from server.stt.engine import is_available as stt_available

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_config(_identity: str = Depends(current_identity)) -> dict:
    settings = get_settings()
    return {
        "repoRoot": settings.repo_root,
        "adoptionEnabled": settings.enable_adoption,
        # True only when the flag is on AND the `stt` extra is importable, so the UI
        # never offers a mic engine the edge cannot actually run.
        "sttEnabled": bool(settings.enable_stt and stt_available()),
    }
