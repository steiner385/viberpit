from __future__ import annotations
import json
import subprocess
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess]
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def _default_gh_api(path: str) -> str:
    r = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True)
    return r.stdout


def ci_verdict(sha: str, gh_api: Callable[[str], str] | None = None) -> str:
    gh_api = gh_api or _default_gh_api
    try:
        body = json.loads(gh_api(f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs"))
    except Exception:
        return "unavailable"
    runs = body.get("check_runs") or []
    if not runs:
        return "pending"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    if any((r.get("conclusion") or "") not in _OK_CONCLUSIONS for r in runs):
        return "red"
    return "green"
