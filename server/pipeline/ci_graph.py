"""CI-graph derivation + diff — Python port of plotroom's required-checks.ts
``deriveCiGraph`` and workflow-impact.ts ``diffCiGraphs`` (SPEC §4).

``derive_ci_graph`` parses a GitHub Actions workflow YAML and BFS-walks the
``needs:`` closure starting at the rollup job (default ``ci``), returning the
required-check name prefixes plus per-job ``timeout-minutes``. ``diff_ci_graphs``
renders the human-readable summary lines a reviewer sees when a PR's head blob
of the workflow differs from the repo's current main-derived graph.

Line renderings are VERBATIM ports of plotroom workflow-impact.ts:24-47 — the
glyphs ``+`` (join), ``−`` (U+2212 minus, leave), and the ``unset`` timeout
sentinel must not drift; the frontend and the plotroom parity fixtures pin them.
"""
from __future__ import annotations

from typing import Any

import yaml


def _is_timeout(v: Any) -> bool:
    # plotroom required-checks.ts:76 — finite positive number only.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _prefix_of(job_key: str, job: dict) -> str:
    """Display-name prefix for a job (plotroom required-checks.ts:274-278).

    A job's check name is its ``name:`` when set, else its map key. Reusable
    (``uses:``) jobs prefix-match with a trailing `` /`` because the reusable
    workflow's own jobs render as ``<name> / <inner>``.
    """
    name = job.get("name")
    prefix = name if isinstance(name, str) and name else job_key
    uses = job.get("uses")
    if isinstance(uses, str) and uses:
        return f"{prefix} /"
    return prefix


def derive_ci_graph(yaml_text: str, rollup_job_id: str = "ci") -> dict | None:
    """Derive the BFS ``needs:`` closure of ``rollup_job_id`` from a workflow YAML.

    Returns ``{"prefixes": list[str], "nodes": {prefix: {"timeoutMinutes":
    int|None}}, "workflowName": str|None}``.

    - Unparseable YAML → ``None`` (derivation learned nothing; callers keep their
      last-known-good graph).
    - Valid YAML with a missing ``jobs:`` map, or one that doesn't define
      ``rollup_job_id``, degrades to the rollup-only graph (the rollup check
      itself always exists and nothing else gates the merge).
    - Cycle-safe via a visited set.
    """
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return None

    raw_name = doc.get("name") if isinstance(doc, dict) else None
    workflow_name = raw_name if isinstance(raw_name, str) and raw_name else None

    jobs = doc.get("jobs") if isinstance(doc, dict) else None

    def rollup_only() -> dict:
        return {
            "prefixes": [rollup_job_id],
            "nodes": {rollup_job_id: {"timeoutMinutes": None}},
            "workflowName": workflow_name,
        }

    if not isinstance(jobs, dict):
        return rollup_only()
    if rollup_job_id not in jobs:
        return rollup_only()

    visited: set[str] = {rollup_job_id}
    queue: list[str] = [rollup_job_id]
    prefixes: list[str] = []
    nodes: dict[str, dict] = {}
    while queue:
        job_key = queue.pop(0)
        job = jobs.get(job_key) or {}
        if not isinstance(job, dict):
            job = {}
        prefix = _prefix_of(job_key, job)
        if prefix not in prefixes:
            prefixes.append(prefix)

        raw_needs = job.get("needs")
        if isinstance(raw_needs, str):
            needs_list = [raw_needs]
        elif isinstance(raw_needs, list):
            needs_list = [n for n in raw_needs if isinstance(n, str)]
        else:
            needs_list = []
        needed_keys = [n for n in needs_list if n in jobs]

        timeout = job.get("timeout-minutes")
        timeout_minutes = timeout if _is_timeout(timeout) else None
        # Two job keys can share a display name — first-seen node wins on the
        # timeout value (BFS visits the rollup-closest occurrence first).
        if prefix not in nodes:
            nodes[prefix] = {"timeoutMinutes": timeout_minutes}

        for k in needed_keys:
            if k in visited:
                continue
            visited.add(k)
            queue.append(k)

    return {"prefixes": prefixes, "nodes": nodes, "workflowName": workflow_name}


def _fmt_timeout(m: int | None) -> str:
    return "unset" if m is None else f"{m}m"


def diff_ci_graphs(base: dict, head: dict) -> dict | None:
    """Diff two derived CI graphs (base = current main, head = the PR's blob).

    Reports, in order (plotroom workflow-impact.ts:24-47):
      1. jobs joining the rollup closure (the merge_group gate)
      2. jobs leaving the closure
      3. the required-check set size delta when the closure size changed
      4. ``timeout-minutes`` changes on jobs present in both closures

    Returns ``{"summary": [...]}`` when there are any lines, else ``None`` — the
    caller renders no card.
    """
    summary: list[str] = []
    base_prefixes = list(base.get("prefixes") or [])
    head_prefixes = list(head.get("prefixes") or [])
    base_set = set(base_prefixes)
    head_set = set(head_prefixes)

    for p in head_prefixes:
        if p not in base_set:
            summary.append(f"+ {p} joins the merge_group gate")
    for p in base_prefixes:
        if p not in head_set:
            summary.append(f"− {p} leaves the merge_group gate")

    if len(head_set) != len(base_set):
        delta = len(head_set) - len(base_set)
        verb = "grows" if delta > 0 else "shrinks"
        summary.append(
            f"required-check set {verb} by {abs(delta)}: "
            f"{len(base_set)} → {len(head_set)} checks"
        )

    base_nodes = base.get("nodes") or {}
    head_nodes = head.get("nodes") or {}
    for prefix, head_node in head_nodes.items():
        base_node = base_nodes.get(prefix)
        if base_node is None:
            continue  # joins-the-gate line already covers it
        bt = base_node.get("timeoutMinutes")
        ht = head_node.get("timeoutMinutes")
        if bt != ht:
            summary.append(
                f"{prefix} timeout-minutes {_fmt_timeout(bt)} → {_fmt_timeout(ht)}"
            )

    return {"summary": summary} if summary else None
