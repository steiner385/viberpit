"""One pull request's changed files with their unified-diff patches, for
``GET /api/prs/{owner}/{name}/{number}/files`` (the assistant's ``get_pr_files``).

REST (``repos/{repo}/pulls/{n}/files``) rather than GraphQL: GraphQL's
``PullRequest.files`` has no patch text. Pages are requested explicitly
(``per_page``/``page``) instead of ``gh api --paginate`` so paging stops at the file cap
instead of downloading up to GitHub's 3000-file ceiling only to throw most of it away.

Everything this module cuts is reported in ``cut``: a reviewer who is shown 300 of 412
files, or a patch cut short, must be told so rather than review a partial diff as if it
were the whole one. Blocking (``gh`` subprocesses) — call it off the event loop.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from viberpit import gitrepos

from .host_failures import log_gh_failure

log = logging.getLogger(__name__)

# Ceilings the route enforces; a caller may ask for less, never more. 300 files is three
# REST pages; 60 kB of patch text is about what a spec or plan PR needs to be read whole.
FILES_MAX = 300
PATCH_BYTES_MAX = 60_000
_PER_PAGE = 100
# Below this many bytes of remaining budget a truncated patch is a fragment too short to
# review; the patch is dropped outright (and counted) instead.
_MIN_PARTIAL_PATCH = 1_000


class PrFilesNotFound(Exception):
    """GitHub has no such pull request (or this host's credential cannot see it)."""


class PrFilesError(Exception):
    """gh failed for any other reason; the message is gh's own stderr."""


def _gh_json(run: Callable[..., Any], path: str, token: str | None, repo: str,
             number: int) -> Any:
    proc = run(["api", path], token=token)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or "gh api failed"
        if "HTTP 404" in err:
            raise PrFilesNotFound(err)
        log_gh_failure(log, err, "github.pr_files_failed repo=%s #%s: %s", repo, number, err)
        raise PrFilesError(err)
    try:
        return json.loads(proc.stdout or "null")
    except ValueError as exc:
        raise PrFilesError(f"gh api {path} returned unparseable JSON") from exc


def _cut_patch(patch: str, budget: int) -> str:
    """The longest whole-line prefix of ``patch`` within ``budget`` UTF-8 bytes."""
    prefix = patch.encode()[:budget].decode(errors="ignore")
    nl = prefix.rfind("\n")
    return prefix[:nl + 1] if nl != -1 else ""


def fetch_pr_files(
    repo: str, number: int, *,
    max_files: int = FILES_MAX, max_patch_bytes: int = PATCH_BYTES_MAX,
    run: Callable[..., Any] | None = None,
    token_for: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """``{repo, number, totalFiles, additions, deletions, files, cut}``.

    Each file is ``{filename, status, additions, deletions, patch}`` plus
    ``previousFilename`` for a rename. ``patch`` is None when GitHub sent none (binary
    or too large for GitHub to diff: ``patchUnavailable``) or when the patch budget was
    spent before this file (``patchOmitted``); a patch cut short at a line boundary
    carries ``patchTruncated``. ``cut`` counts each of those plus the files past the
    file cap. Raises ``PrFilesNotFound`` / ``PrFilesError``.
    """
    # Resolved per call, not bound as defaults, so a patched seam is honoured.
    run = run or gitrepos._run_gh
    token = (token_for or gitrepos.app_token_for_owner)(repo.partition("/")[0])
    pr = _gh_json(run, f"repos/{repo}/pulls/{number}", token, repo, number)
    if not isinstance(pr, dict):
        raise PrFilesError(f"gh api repos/{repo}/pulls/{number} returned no pull request")
    total = int(pr.get("changed_files") or 0)

    raw: list[dict[str, Any]] = []
    page = 1
    while len(raw) < max_files:
        rows = _gh_json(run, f"repos/{repo}/pulls/{number}/files?per_page={_PER_PAGE}&page={page}",
                        token, repo, number)
        if not isinstance(rows, list):
            raise PrFilesError(f"gh api repos/{repo}/pulls/{number}/files returned no list")
        raw.extend(r for r in rows if isinstance(r, dict))
        if len(rows) < _PER_PAGE:
            break
        page += 1
    raw = raw[:max_files]
    total = max(total, len(raw))

    budget = max_patch_bytes
    truncated = omitted = bytes_omitted = 0
    files: list[dict[str, Any]] = []
    for r in raw:
        row: dict[str, Any] = {
            "filename": r.get("filename"), "status": r.get("status"),
            "additions": int(r.get("additions") or 0), "deletions": int(r.get("deletions") or 0),
            "patch": None,
        }
        if r.get("previous_filename"):
            row["previousFilename"] = r["previous_filename"]
        patch = r.get("patch")
        if not isinstance(patch, str):
            row["patchUnavailable"] = True
        else:
            size = len(patch.encode())
            if size <= budget:
                row["patch"] = patch
                budget -= size
            elif budget >= _MIN_PARTIAL_PATCH and (part := _cut_patch(patch, budget)):
                row["patch"] = part
                row["patchTruncated"] = True
                truncated += 1
                bytes_omitted += size - len(part.encode())
                budget = 0
            else:
                row["patchOmitted"] = True
                omitted += 1
                bytes_omitted += size
        files.append(row)

    return {
        "repo": repo, "number": number, "totalFiles": total,
        "additions": pr.get("additions"), "deletions": pr.get("deletions"),
        "files": files,
        "cut": {"filesOmitted": total - len(files), "patchesTruncated": truncated,
                "patchesOmitted": omitted, "patchBytesOmitted": bytes_omitted,
                "maxFiles": max_files, "maxPatchBytes": max_patch_bytes},
    }
