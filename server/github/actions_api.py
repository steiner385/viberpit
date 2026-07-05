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


# First-tick backfill bound. Steady-state ticks carry a cursor and return well
# under one page, so pagination stops after page 1; only the very first tick (no
# cursor) can span multiple pages, and this caps that at MAX_BACKFILL_PAGES*100
# runs — deliberately bounded so a repo with years of history never hammers the
# API in a single tick (constitution III / anti-hammer, #451).
MAX_BACKFILL_PAGES = 10


def list_completed_runs(repo: str, since_ts: float | None, *,
                        run=gitrepos._run_gh, max_pages: int = MAX_BACKFILL_PAGES) -> list[dict]:
    from urllib.parse import quote
    date = ""
    if since_ts:
        # Apply a 3-day lookback so runs created before since_ts but completed
        # after it are not permanently dropped by a strict client-side cut.
        lookback_ts = since_ts - 3 * 86400
        day = datetime.fromtimestamp(lookback_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        # URL-encode the qualifier: `>=` are reserved characters in a query string
        # and must be percent-encoded (created=%3E%3DYYYY-MM-DD), not sent raw.
        date = "&created=" + quote(f">={day}", safe="")
    out = []
    for page in range(1, max_pages + 1):
        data = _json(run(["api", f"repos/{repo}/actions/runs"
                                 f"?status=completed&per_page=100&page={page}{date}"]))
        runs = data.get("workflow_runs", [])
        for r in runs:
            created = _iso_ts(r.get("created_at")) or 0.0
            # No strict client-side created <= since_ts filter: idempotence lives in
            # _ingest's existing `seen` set (skips already-stored run ids).
            started, updated = _iso_ts(r.get("run_started_at")), _iso_ts(r.get("updated_at"))
            out.append({
                "id": r["id"], "workflow_path": r.get("path") or "",
                "event": r.get("event") or "", "head_sha": r.get("head_sha") or "",
                "head_branch": r.get("head_branch") or "",
                "conclusion": r.get("conclusion") or "", "created_ts": created,
                "duration_s": max(0.0, (updated - started)) if started and updated else 0.0,
            })
        if len(runs) < 100:  # last (or only) page — steady-state ticks stop here
            break
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


# Process-global cache: repo -> (default-branch HEAD sha, {path: text}). Workflow
# YAML changes only when a commit lands on the default branch, so between 900s
# ticks a repo with an unchanged HEAD skips the whole listing + N content reads
# (#452). Keyed by sha so a genuine workflow change is picked up next tick.
_WORKFLOW_CACHE: dict[str, tuple[str, dict[str, str]]] = {}


def _default_branch_head_sha(repo: str, default_branch: str, *, run) -> str | None:
    """One cheap read of the default branch's HEAD commit sha — the cache key.
    None on any failure, so the caller degrades to an uncached refetch."""
    try:
        data = _json(run(["api", f"repos/{repo}/commits/{default_branch}"]))
    except Exception:
        log.debug("cicd.head_sha_failed repo=%s", repo, exc_info=True)
        return None
    sha = data.get("sha") if isinstance(data, dict) else None
    return sha or None


def workflow_texts(repo: str, default_branch: str, *, run=gitrepos._run_gh,
                   cache: dict[str, tuple[str, dict[str, str]]] | None = None) -> dict[str, str]:
    import base64
    cache = _WORKFLOW_CACHE if cache is None else cache
    head_sha = _default_branch_head_sha(repo, default_branch, run=run)
    if head_sha is not None:
        hit = cache.get(repo)
        if hit is not None and hit[0] == head_sha:
            return dict(hit[1])  # copy: callers must not mutate the cached dict
    try:
        listing = _json(run(["api",
                             f"repos/{repo}/contents/.github/workflows?ref={default_branch}"]))
    except Exception:
        # Transient listing failure — do NOT cache the empty result, so the next
        # tick retries instead of masking the outage until HEAD changes.
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
    if head_sha is not None:  # a successful listing (even empty) caches under this sha
        cache[repo] = (head_sha, dict(out))
    return out
