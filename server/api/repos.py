"""Repos API — list git repos under a root dir, list branches."""
import os
import subprocess
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query
from server.auth.middleware import current_identity
from server.config import get_settings

router = APIRouter(prefix="/api/repos", tags=["repos"])


@router.get("")
async def list_repos(
    root: str | None = None,
    _identity: str = Depends(current_identity),
) -> list[dict]:
    settings = get_settings()
    root_path = Path(root or settings.repo_root)
    if not root_path.exists():
        raise HTTPException(status_code=404, detail=f"Root not found: {root_path}")
    repos = []
    for child in sorted(root_path.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        # Skip hidden dirs — notably ccswitch's own `.ccswitch/worktrees/...`
        # session worktrees, which are git work trees but not pickable repos.
        if child.name.startswith("."):
            continue
        if not (child / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(child), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        current_branch = result.stdout.strip() if result.returncode == 0 else None
        repos.append({"name": child.name, "path": str(child.resolve()), "currentBranch": current_branch})
    return repos


@router.get("/branches")
async def list_branches(
    path: str = Query(...),
    _identity: str = Depends(current_identity),
) -> dict:
    repo_path = Path(path)
    if not repo_path.exists() or not (repo_path / ".git").exists():
        raise HTTPException(status_code=400, detail=f"Not a git repo: {path}")
    current_result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    current = current_result.stdout.strip() if current_result.returncode == 0 else None
    branches_result = subprocess.run(
        ["git", "-C", str(repo_path), "branch", "--format=%(refname:short)"],
        capture_output=True, text=True,
    )
    branches = [b.strip() for b in branches_result.stdout.splitlines() if b.strip()]
    return {"current": current, "branches": branches}
