"""GitHub Actions reads for the CI/CD Doctor. Sync (the loop wraps them in
asyncio.to_thread). Actions data has no GraphQL API — REST via gh is the
documented exception to the GraphQL-for-reads norm. Every function accepts an
injected runner so tests never touch the network."""
from __future__ import annotations

import io
import json
import logging
import subprocess
import zipfile
from datetime import datetime, timezone
from typing import Any

from ccswitch import gitrepos

log = logging.getLogger(__name__)


def _run_gh_bytes(args: list[str]) -> bytes:
    """gh with binary stdout (artifact zips). Same env discipline as _run_gh."""
    proc = subprocess.run(["gh", *args], capture_output=True, env=gitrepos._gh_env())
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.decode(errors="replace").strip() or "gh failed")
    return proc.stdout


def _iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _json(proc) -> dict:  # type: ignore[return-value]
    if proc.returncode != 0:
        raise gitrepos.GhError(proc.stderr.strip() or "gh api failed")
    return json.loads(proc.stdout)


def list_completed_runs(repo: str, since_ts: float | None, *,
                        run=gitrepos._run_gh) -> list[dict]:
    date = ""
    if since_ts:
        date = "&created=>=" + datetime.fromtimestamp(
            since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    data = _json(run(["api", f"repos/{repo}/actions/runs"
                             f"?status=completed&per_page=100{date}"]))
    out = []
    for r in data.get("workflow_runs", []):
        created = _iso_ts(r.get("created_at")) or 0.0
        if since_ts and created <= since_ts:
            continue
        started, updated = _iso_ts(r.get("run_started_at")), _iso_ts(r.get("updated_at"))
        out.append({
            "id": r["id"], "workflow_path": r.get("path") or "",
            "event": r.get("event") or "", "head_sha": r.get("head_sha") or "",
            "head_branch": r.get("head_branch") or "",
            "conclusion": r.get("conclusion") or "", "created_ts": created,
            "duration_s": max(0.0, (updated - started)) if started and updated else 0.0,
        })
    out.sort(key=lambda r: r["created_ts"])
    return out


def list_jobs(repo: str, run_id: int, *, run=gitrepos._run_gh) -> list[dict]:
    data = _json(run(["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]))
    out = []
    for j in data.get("jobs", []):
        s, c = _iso_ts(j.get("started_at")), _iso_ts(j.get("completed_at"))
        out.append({"id": j["id"], "name": j.get("name") or "",
                    "conclusion": j.get("conclusion") or "",
                    "duration_s": max(0.0, (c - s)) if s and c else 0.0})
    return out


def junit_artifacts(repo: str, run_id: int, *, run=gitrepos._run_gh,
                    run_bytes=_run_gh_bytes) -> list[tuple[str, bytes]]:
    """(artifact name, xml bytes) for every junit-* artifact of the run.
    Contract: Plan A names all JUnit artifacts with the junit- prefix."""
    data = _json(run(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts"]))
    out: list[tuple[str, bytes]] = []
    for a in data.get("artifacts", []):
        if not str(a.get("name", "")).startswith("junit") or a.get("expired"):
            continue
        try:
            blob = run_bytes(["api", f"repos/{repo}/actions/artifacts/{a['id']}/zip"])
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for member in z.namelist():
                    if member.endswith(".xml"):
                        out.append((a["name"], z.read(member)))
        except Exception:
            log.warning("cicd.artifact_fetch_failed repo=%s run=%s artifact=%s",
                        repo, run_id, a.get("name"), exc_info=True)
    return out


def repo_ci_meta(repo: str, *, run=gitrepos._run_gh) -> tuple[str, set[str]]:
    """(default branch, required status-check contexts). Branch-protection reads
    need admin scope — degrade to an empty set (verdicts then skip demotion)."""
    branch = "master"
    try:
        meta = _json(run(["api", f"repos/{repo}"]))
        branch = meta.get("default_branch") or "master"
    except Exception:
        log.warning("cicd.repo_meta_failed repo=%s", repo, exc_info=True)
    required: set[str] = set()
    try:
        data = _json(run(["api",
                          f"repos/{repo}/branches/{branch}/protection/required_status_checks"]))
        required = set(data.get("contexts") or [])
    except Exception:
        log.debug("cicd.protection_unreadable repo=%s", repo, exc_info=True)
    return branch, required


def workflow_texts(repo: str, default_branch: str, *, run=gitrepos._run_gh) -> dict[str, str]:
    import base64
    try:
        listing = _json(run(["api",
                             f"repos/{repo}/contents/.github/workflows?ref={default_branch}"]))
    except Exception:
        return {}
    out: dict[str, str] = {}
    items: list[Any] = listing if isinstance(listing, list) else []
    for item in items:
        path = item.get("path", "")
        if item.get("type") != "file" or not path.endswith((".yml", ".yaml")):
            continue
        try:
            content = _json(run(["api", f"repos/{repo}/contents/{path}?ref={default_branch}"]))
            out[path] = base64.b64decode(content.get("content") or "").decode(errors="replace")
        except Exception:
            log.debug("cicd.workflow_read_failed repo=%s path=%s", repo, path, exc_info=True)
    return out
