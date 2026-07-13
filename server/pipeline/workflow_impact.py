"""Workflow-change impact orchestration (SPEC §4 / plotroom poller.ts:1060-1100).

For a PR whose head touches ``.github/workflows/``, this resolves the repo's
rollup workflow file, fetches it at the repo default branch (base) and at the
PR head sha, derives a CI graph from each (:mod:`server.pipeline.ci_graph`), and
diffs them into a human-readable impact card. Any failure returns ``None`` — an
honest null the frontend renders as "CI change, inspect manually".

Rollup-job-id decision
-----------------------
ccswitch's migrated ciGraph meta (``history.py:ci_graph``) stores only
``{prefixes, workflowName}`` — NOT the rollup job id or the per-job node/timeout
detail that plotroom keeps in-memory. deriveCiGraph needs a rollup job id to
root the BFS. Plotroom defaults ``rollupJobId = 'ci'`` (required-checks.ts:258)
and the ccswitch repos this targets gate merges on a ``ci`` rollup job (see
required.py's module docstring). We therefore default the rollup job id to
``'ci'``; both base and head graphs are derived with the same id, so the diff is
symmetric even if a given repo used a differently-named rollup (the base would
degrade to a rollup-only ``['ci']`` graph and the diff still surfaces gate
membership changes). The workflow FILE is resolved by matching each listed
workflow's parsed ``name:`` to the stored ``workflowName`` — this is how we know
WHICH file to fetch without a stored path.

Compute-site decision
----------------------
Impact is computed in the pipeline SSE frame build (``server/api/pipeline.py``
``_build_state``): it already has async access and a per-cycle cadence. The
edge opens the read-only ``pipeline_history.db`` at the operational default
path for the ciGraph meta (same pattern as ``timelines_for`` reading the app
DB directly — no orchestrator RPC needed) and uses the ambient gh helper for
blobs. The resolver's ``prune`` is called there with the set of live
(repo, head_sha) keys so stale entries for closed/merged PRs evict each
cycle — mirroring plotroom poller.ts:906-911.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Callable

from server.pipeline.ci_graph import derive_ci_graph, diff_ci_graphs

_WF_DIR = ".github/workflows/"


class GhBlobs:
    """gh-backed blob source: list ``.github/workflows/`` + fetch a file at a ref.

    ``ref=None`` means the repo's default branch (the contents API defaults to it
    when ``?ref=`` is omitted); a sha or branch name is passed through otherwise.
    All calls are synchronous ``gh`` subprocess invocations — the resolver's
    async entrypoint offloads them via ``asyncio.to_thread``.
    """

    def __init__(self, run=None):
        from src.ccswitch import gitrepos

        self._run = run or gitrepos._run_gh

    def _api(self, path: str):
        r = self._run(["api", path])
        if r.returncode != 0:
            raise RuntimeError(f"gh api {path} failed: {r.stderr.strip()[:200]}")
        return json.loads(r.stdout or "null")

    def list_workflow_files(self, repo: str, ref: str | None = None) -> list[str]:
        suffix = f"?ref={ref}" if ref else ""
        listing = self._api(f"repos/{repo}/contents/.github/workflows{suffix}")
        items = listing if isinstance(listing, list) else []
        return [
            it["path"]
            for it in items
            if isinstance(it, dict)
            and it.get("type") == "file"
            and isinstance(it.get("path"), str)
            and it["path"].endswith((".yml", ".yaml"))
        ]

    def fetch_file(self, repo: str, path: str, ref: str | None = None) -> str | None:
        suffix = f"?ref={ref}" if ref else ""
        content = self._api(f"repos/{repo}/contents/{path}{suffix}")
        if not isinstance(content, dict):
            return None
        raw = content.get("content")
        if not isinstance(raw, str):
            return None
        return base64.b64decode(raw).decode(errors="replace")


class WorkflowImpactResolver:
    """Resolve + cache workflow-impact diffs, keyed ``(repo, head_sha)``.

    ``blobs`` supplies ``list_workflow_files(repo, ref)`` and ``fetch_file(repo,
    path, ref)``. ``ci_graph_for(repo)`` returns the stored ciGraph meta dict
    (``{prefixes, workflowName}``) or ``None``.
    """

    def __init__(
        self,
        blobs,
        ci_graph_for: Callable[[str], dict | None],
        rollup_job_id: str = "ci",
    ):
        self._blobs = blobs
        self._ci_graph_for = ci_graph_for
        self._rollup_job_id = rollup_job_id
        # (repo, head_sha) → impact dict | None (None = derived, no change).
        self._cache: dict[tuple[str, str], dict | None] = {}
        # repo → resolved workflow file path (base branch), cached per repo.
        self._path_cache: dict[str, str | None] = {}

    # -- workflow-file resolution --------------------------------------------

    def _resolve_workflow_path(self, repo: str, workflow_name: str) -> str | None:
        """Path of the workflow whose parsed ``name:`` equals ``workflow_name``.

        Cached per repo (the base-branch listing rarely changes). ``None`` and
        NOT cached on a transient listing/parse failure so the next cycle retries.
        """
        if repo in self._path_cache:
            return self._path_cache[repo]
        paths = self._blobs.list_workflow_files(repo, None)
        for path in paths:
            text = self._blobs.fetch_file(repo, path, None)
            if not isinstance(text, str):
                continue
            g = derive_ci_graph(text, self._rollup_job_id)
            if g and g.get("workflowName") == workflow_name:
                self._path_cache[repo] = path
                return path
        return None  # not resolvable this cycle — don't poison the cache

    # -- core resolution ------------------------------------------------------

    def _compute(self, repo: str, head_sha: str) -> dict | None:
        meta = self._ci_graph_for(repo)
        if not meta:
            return None
        workflow_name = meta.get("workflowName")
        if not workflow_name:
            return None
        path = self._resolve_workflow_path(repo, workflow_name)
        if not path:
            return None
        base_text = self._blobs.fetch_file(repo, path, None)
        head_text = self._blobs.fetch_file(repo, path, head_sha)
        if not isinstance(base_text, str) or not isinstance(head_text, str):
            return None
        base_graph = derive_ci_graph(base_text, self._rollup_job_id)
        head_graph = derive_ci_graph(head_text, self._rollup_job_id)
        if base_graph is None or head_graph is None:
            return None
        return diff_ci_graphs(base_graph, head_graph)

    def impact_for(self, repo: str, head_sha: str) -> dict | None:
        """Cached impact for ``(repo, head_sha)``; any failure → ``None``.

        The cache stores the deterministic outcome for a sha (including a genuine
        no-change ``None``), so a repeated call never re-fetches. A transport or
        derivation failure is NOT cached — the next cycle retries.
        """
        if not repo or not head_sha:
            return None
        key = (repo, head_sha)
        if key in self._cache:
            return self._cache[key]
        try:
            result = self._compute(repo, head_sha)
        except Exception:
            return None  # transient — leave uncached so the next cycle retries
        self._cache[key] = result
        return result

    async def impact_for_async(self, repo: str, head_sha: str) -> dict | None:
        """Async wrapper — offloads the blocking gh subprocess work to a thread."""
        if not repo or not head_sha:
            return None
        if (repo, head_sha) in self._cache:
            return self._cache[(repo, head_sha)]
        return await asyncio.to_thread(self.impact_for, repo, head_sha)

    def prune(self, live_keys: set[tuple[str, str]]) -> None:
        """Evict cache entries whose (repo, head_sha) is no longer live.

        Called once per SSE cycle with the set of open-PR head keys, mirroring
        plotroom poller.ts:906-911.
        """
        for key in list(self._cache):
            if key not in live_keys:
                del self._cache[key]
