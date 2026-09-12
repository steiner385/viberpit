"""GitHub write seam for map-changes PRs (roadmap drill-down spec §9).

Every call is a ``gh`` subprocess through ``gitrepos._run_gh`` with an
injectable runner, so tests record argv and answer canned JSON. Commits go
through GraphQL ``createCommitOnBranch`` — atomic, server-side, and its
``expectedHeadOid`` turns a moved branch head into an explicit conflict — so
the shared local clone (read SHA-pinned by the sync loop, used as cwd by
sessions) is never mutated. Branch, PR and content reads are REST.

Auth (R8b-3): the GitHub App installation token for the repo's owner first;
when GitHub answers ``Resource not accessible by integration`` (the App lacks
``contents:write`` / ``pull_requests:write``) the call is retried with the
classic PAT ``CCSWITCH_GH_PROJECT_TOKEN`` and the instance sticks with it;
with neither, ambient ``gh`` credentials."""
from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ccswitch import gitrepos

log = logging.getLogger(__name__)

_INTEGRATION_DENIED = "Resource not accessible by integration"
# GitHub's wording, verified live 2026-09-12 against createCommitOnBranch: a
# stale-but-real expectedHeadOid answers type STALE_DATA / "Expected branch to
# point to "<sha>" but it did not.  Pull and try again."; a nonexistent one
# answers type NOT_FOUND / "No commit exists with specified expectedHeadOid".
# Both mean "re-read the head": the caller retries once, then reports 409.
_STALE_TYPES = ("STALE_DATA",)
_STALE_HEAD = ("Expected branch to point to", "expectedHeadOid")

_COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}
"""


class GhWriteError(RuntimeError):
    def __init__(self, step: str, detail: str, *, conflict: bool = False) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail
        self.conflict = conflict


@dataclass(frozen=True)
class PrState:
    number: int
    url: str
    state: str
    merged: bool
    merged_at: str | None
    head_sha: str
    mergeable_state: str | None

    @property
    def behind(self) -> bool:
        return self.mergeable_state == "behind"

    @property
    def conflicting(self) -> bool:
        return self.mergeable_state == "dirty"


def _pr_state(d: Mapping[str, object]) -> PrState:
    head_obj = d.get("head")
    head: Mapping[str, object] = head_obj if isinstance(head_obj, Mapping) else {}
    return PrState(
        number=int(str(d.get("number") or 0)), url=str(d.get("html_url") or ""),
        state=str(d.get("state") or ""), merged=bool(d.get("merged")),
        merged_at=(str(d["merged_at"]) if d.get("merged_at") else None),
        head_sha=str(head.get("sha") or ""),
        mergeable_state=(str(d["mergeable_state"]) if d.get("mergeable_state") else None),
    )


class GhWrite:
    def __init__(self, repo: str, *, run: Callable = gitrepos._run_gh,
                 app_token: Callable[[str], str | None] = gitrepos.app_token_for_repo,
                 env: Mapping[str, str] = os.environ) -> None:
        self.repo = repo
        self._run = run
        self._app = app_token(repo)
        self._pat = env.get("CCSWITCH_GH_PROJECT_TOKEN") or None
        self._use_pat = False

    # -- plumbing -------------------------------------------------------------
    def _token(self) -> str | None:
        if self._use_pat:
            return self._pat
        return self._app or self._pat

    def _gh(self, args: list[str], step: str, *, input: str | None = None,
            allow_404: bool = False) -> str:
        token = self._token()
        proc = self._run(args, token, input=input, timeout=60)
        if proc.returncode != 0 and _INTEGRATION_DENIED in (proc.stderr or "") + (proc.stdout or "") \
                and token == self._app and self._pat:
            log.info("map_changes.gh: App token refused for %s; falling back to the project PAT", self.repo)
            self._use_pat = True
            proc = self._run(args, self._pat, input=input, timeout=60)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if allow_404 and "HTTP 404" in err:
                return ""
            raise GhWriteError(step, err[:300])
        return proc.stdout or ""

    @staticmethod
    def _json(text: str) -> dict:
        return json.loads(text) if text.strip() else {}

    # -- refs -----------------------------------------------------------------
    def default_branch_head(self, default_branch: str) -> str:
        d = self._json(self._gh(["api", f"repos/{self.repo}/git/ref/heads/{default_branch}"], "head"))
        sha = str((d.get("object") or {}).get("sha") or "")
        if not sha:
            raise GhWriteError("head", f"no sha for {default_branch}")
        return sha

    def ensure_branch(self, branch: str, from_sha: str) -> str:
        existing = self._json(self._gh(["api", f"repos/{self.repo}/git/ref/heads/{branch}"], "branch", allow_404=True))
        sha = str((existing.get("object") or {}).get("sha") or "")
        if sha:
            return sha
        created = self._json(self._gh(["api", "-X", "POST", f"repos/{self.repo}/git/refs",
                                       "-f", f"ref=refs/heads/{branch}", "-f", f"sha={from_sha}"], "branch"))
        return str((created.get("object") or {}).get("sha") or from_sha)

    def delete_branch(self, branch: str) -> None:
        self._gh(["api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{branch}"], "delete_branch", allow_404=True)

    # -- content --------------------------------------------------------------
    def list_dir(self, path: str, ref: str) -> list[str]:
        """Paths of the Markdown files directly under *path* at *ref*.

        Empty when the directory does not exist on the ref (a branch opened
        before any doc landed). The contents API answers a directory with an
        array of stubs — no ``content`` — so each doc still needs
        :meth:`read_file`."""
        out = self._gh(["api", f"repos/{self.repo}/contents/{path}?ref={ref}"], "list", allow_404=True)
        if not out.strip():
            return []
        entries = json.loads(out)
        if not isinstance(entries, list):
            return []
        return [str(e.get("path") or "") for e in entries
                if isinstance(e, dict) and e.get("type") == "file"
                and str(e.get("name") or "").endswith(".md")]

    def read_file(self, path: str, ref: str) -> tuple[str | None, str | None]:
        out = self._gh(["api", f"repos/{self.repo}/contents/{path}?ref={ref}"], "read", allow_404=True)
        if not out.strip():
            return None, None
        d = self._json(out)
        raw = base64.b64decode(str(d.get("content") or "")).decode(errors="replace")
        return raw, (str(d["sha"]) if d.get("sha") else None)

    def commit_file(self, branch: str, expected_head: str, path: str, text: str, message: str) -> str:
        payload = {"query": _COMMIT_MUTATION, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": self.repo, "branchName": branch},
            "expectedHeadOid": expected_head,
            "message": {"headline": message},
            "fileChanges": {"additions": [
                {"path": path, "contents": base64.b64encode(text.encode()).decode()}]},
        }}}
        d = self._json(self._gh(["api", "graphql", "--input", "-"], "commit", input=json.dumps(payload)))
        errors = d.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            msg = str(first.get("message") or errors)
            stale = str(first.get("type") or "") in _STALE_TYPES or any(k in msg for k in _STALE_HEAD)
            raise GhWriteError("commit", msg[:300], conflict=stale)
        oid = str((((d.get("data") or {}).get("createCommitOnBranch") or {}).get("commit") or {}).get("oid") or "")
        if not oid:
            raise GhWriteError("commit", f"no commit oid in {json.dumps(d)[:200]}")
        return oid

    # -- pull requests --------------------------------------------------------
    def open_pr(self, branch: str, base: str, title: str, body: str) -> PrState:
        d = self._json(self._gh(["api", "-X", "POST", f"repos/{self.repo}/pulls",
                                 "-f", f"head={branch}", "-f", f"base={base}",
                                 "-f", f"title={title}", "-F", "draft=false", "-F", "body=@-"],
                                "pr", input=body))
        return _pr_state(d)

    def read_pr(self, number: int) -> PrState:
        return _pr_state(self._json(self._gh(["api", f"repos/{self.repo}/pulls/{number}"], "pr_read")))

    def close_pr(self, number: int) -> None:
        self._gh(["api", "-X", "PATCH", f"repos/{self.repo}/pulls/{number}", "-f", "state=closed"], "close")

    def update_branch(self, number: int) -> None:
        self._gh(["api", "-X", "PUT", f"repos/{self.repo}/pulls/{number}/update-branch"], "update_branch")
