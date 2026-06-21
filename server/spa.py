"""Single-page-app static serving with client-side-routing fallback.

`StaticFiles(html=True)` only serves index.html for the directory root and real
files, so a refresh on a clean deep link like `/session/abc` 404s. `SPAStaticFiles`
catches that 404 and serves `index.html` instead, letting the React router resolve
the path on the client.
"""
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Mount the built SPA at `/` if `dist_dir` exists; otherwise do nothing.

    Must be called AFTER all `/api/*` and `/ws/*` routes are registered so those
    routes match before this catch-all mount.
    """
    if not dist_dir.is_dir():
        return
    app.mount("/", SPAStaticFiles(directory=str(dist_dir), html=True), name="spa")
