"""Workflow-YAML waste detection (spec Part 4b item 4). Pure text analysis."""
from __future__ import annotations

from typing import Any

import yaml

from .types import Finding


def _triggers(doc: dict) -> dict:
    on = doc.get("on", doc.get(True))  # YAML 1.1: bare `on:` parses as boolean True
    if isinstance(on, list):
        return {k: None for k in on}
    return on if isinstance(on, dict) else {}


def workflow_waste_findings(repo: str, path: str, text: str) -> list[Finding]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    findings: list[Finding] = []
    on = _triggers(doc)
    push: Any = on.get("push") if "push" in on else None
    unfiltered_push = "push" in on and not (isinstance(push, dict) and push.get("branches"))
    if unfiltered_push and "pull_request" in on:
        findings.append(Finding(
            kind="waste_double_run", repo=repo, key=path,
            title=f"{path}: unfiltered `push` + `pull_request` runs every PR twice",
            detail={"path": path, "fix": "restrict push to the default branch"}))
    if "concurrency" not in doc:
        findings.append(Finding(
            kind="waste_no_concurrency", repo=repo, key=path,
            title=f"{path}: no concurrency group — superseded runs are never cancelled",
            detail={"path": path, "fix": "add a concurrency group with cancel-in-progress"}))
    jobs = doc.get("jobs") or {}
    for job in (jobs.values() if isinstance(jobs, dict) else []):
        steps = job.get("steps") or [] if isinstance(job, dict) else []
        uses_pip = any("pip install" in str(s.get("run", "")) for s in steps if isinstance(s, dict))
        uncached_setup = any(
            str(s.get("uses", "")).startswith("actions/setup-python")
            and not (s.get("with") or {}).get("cache")
            for s in steps if isinstance(s, dict))
        if uses_pip and uncached_setup:
            findings.append(Finding(
                kind="waste_no_pip_cache", repo=repo, key=path,
                title=f"{path}: setup-python without pip cache",
                detail={"path": path, "fix": "set `cache: pip` on actions/setup-python"}))
            break  # one finding per workflow file
    return findings
