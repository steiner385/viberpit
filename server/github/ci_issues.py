"""File/close ci-doctor GitHub issues (REST mutations — no GraphQL path in gh).
Best-effort: a failed create returns None; the loop retries next tick because
the finding row keeps issue_number NULL."""
from __future__ import annotations

import json
import logging

from ccswitch import gitrepos

log = logging.getLogger(__name__)

LABEL = "ci-doctor"


def _ensure_label(repo: str, run) -> None:
    # 422 already_exists is the steady state; any failure is non-fatal.
    run(["api", f"repos/{repo}/labels", "-f", f"name={LABEL}",
         "-f", "color=1d76db", "-f", "description=Filed by the ccswitch CI/CD Doctor"])


def create_issue(repo: str, title: str, body: str, *,
                 run=gitrepos._run_gh) -> int | None:
    try:
        _ensure_label(repo, run)
        proc = run(["api", f"repos/{repo}/issues", "-f", f"title={title}",
                    "-f", f"body={body}", "-f", f"labels[]={LABEL}"])
        if proc.returncode != 0:
            log.warning("cicd.issue_create_failed repo=%s: %s", repo, proc.stderr.strip())
            return None
        return int(json.loads(proc.stdout)["number"])
    except Exception:
        log.warning("cicd.issue_create_failed repo=%s", repo, exc_info=True)
        return None


def close_issue(repo: str, number: int, *, run=gitrepos._run_gh) -> bool:
    try:
        proc = run(["api", "-X", "PATCH", f"repos/{repo}/issues/{number}",
                    "-f", "state=closed"])
        return proc.returncode == 0
    except Exception:
        log.warning("cicd.issue_close_failed repo=%s #%s", repo, number, exc_info=True)
        return False
