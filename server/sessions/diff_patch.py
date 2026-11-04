"""The ``format=patch`` view of a session diff (D-12): unified-diff text per file,
held to one byte budget and redacted, for the assistant's ``get_session_diff``.

The D-4 ``SessionDiff`` carries structured hunks (a dict per line) for the Changes
pane. A model reads text, and the hunk form costs several times the bytes of the text
it describes, so this view renders it back — and, unlike the pane, which shows the
operator their own worktree, it can leave the box through a model call, so every patch
goes through the same credential redactor D-11's transcript search uses.
"""
from __future__ import annotations

from typing import Any

from viberpit import worktree_diff

from server.audit import redact_text
from server.github.pr_files import PATCH_BYTES_MAX, PatchBudget

#: Ceilings the route enforces; a caller may ask for less, never more. The file cap is
#: the diff's own; the byte cap is get_pr_files' (``server/github/pr_files.py``).
PATCH_FILES_MAX = worktree_diff.MAX_FILES
PATCH_VIEW_BYTES_MAX = PATCH_BYTES_MAX
#: The smallest budget worth asking for: below it nothing but a cut is returned.
PATCH_VIEW_BYTES_MIN = 1_000

_FILE_KEYS = ("path", "oldPath", "status", "additions", "deletions", "binary", "tooLarge")
_LINE_PREFIX = {"add": "+", "del": "-"}


def render_patch(f: dict[str, Any]) -> str:
    """A ``FileDiff``'s hunks as unified-diff text: ``@@ -a,b +c,d @@ header`` and one
    ``+``/``-``/space line per row."""
    out: list[str] = []
    for h in f.get("hunks") or []:
        if not isinstance(h, dict):
            continue
        header = f" {h['header']}" if h.get("header") else ""
        out.append(f"@@ -{h.get('oldStart')},{h.get('oldLines')} "
                   f"+{h.get('newStart')},{h.get('newLines')} @@{header}\n")
        out.extend(f"{_LINE_PREFIX.get(str(ln.get('kind')), ' ')}{ln.get('text', '')}\n"
                   for ln in h.get("lines") or [] if isinstance(ln, dict))
    return "".join(out)


def patch_view(diff: dict[str, Any], *, max_files: int, max_patch_bytes: int) -> dict[str, Any]:
    """``{base, head, additions, deletions, truncated, files, cut}`` from a full
    ``SessionDiff``. Each file row drops ``hunks`` for ``patch``: the redacted text,
    or None when it is binary / too large for the diff to carry (``patchUnavailable``)
    or the budget ran out (``patchOmitted``; ``patchTruncated`` for a line-boundary
    cut). ``cut`` has get_pr_files' keys, so both diff tools report alike."""
    files_in = [f for f in diff.get("files") or [] if isinstance(f, dict)]
    budget = PatchBudget(max_patch_bytes)
    files: list[dict[str, Any]] = []
    for f in files_in[:max_files]:
        row: dict[str, Any] = {k: f[k] for k in _FILE_KEYS if k in f}
        row["patch"] = None
        if f.get("binary") or f.get("tooLarge") or not f.get("hunks"):
            row["patchUnavailable"] = True
        elif budget.left <= 0:
            # Spent: count it without paying for a render + redaction of text nobody reads.
            row["patch"], flag = budget.take(render_patch(f))
            row[flag or "patchOmitted"] = True
        else:
            row["patch"], flag = budget.take(redact_text(render_patch(f)))
            if flag:
                row[flag] = True
        files.append(row)
    return {
        **{k: diff.get(k) for k in ("base", "head", "additions", "deletions", "truncated")},
        "files": files,
        "cut": {"filesOmitted": len(files_in) - len(files),
                "patchesTruncated": budget.truncated, "patchesOmitted": budget.omitted,
                "patchBytesOmitted": budget.bytes_omitted, "maxFiles": max_files,
                "maxPatchBytes": max_patch_bytes},
    }
