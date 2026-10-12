"""`server.github.pr_files.fetch_pr_files` — a PR's changed files plus per-file patch,
paginated off gh REST and capped, with every cut reported (assistant get_pr_files)."""
from __future__ import annotations

import json
import subprocess

import pytest

from server.github import pr_files
from server.github.pr_files import PrFilesError, PrFilesNotFound, fetch_pr_files


def _proc(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(["gh"], rc, stdout=stdout, stderr=stderr)


def _file(i: int, patch: str | None = "@@ -1 +1 @@\n-a\n+b\n", **extra) -> dict:
    row = {"filename": f"src/f{i}.py", "status": "modified", "additions": 1, "deletions": 1,
           "changes": 2, "sha": "x" * 40, "blob_url": "https://example/blob",
           **({"patch": patch} if patch is not None else {}), **extra}
    return row


class _FakeGh:
    """Serves `repos/{repo}/pulls/{n}` and its paginated `/files` from canned rows."""

    def __init__(self, files: list[dict], *, changed_files: int | None = None):
        self.files = files
        self.changed_files = len(files) if changed_files is None else changed_files
        self.calls: list[list[str]] = []
        self.tokens: list[str | None] = []

    def __call__(self, args, token=None, **_kw):
        self.calls.append(args)
        self.tokens.append(token)
        path = args[1]
        if path == "repos/acme/billing/pulls/7":
            return _proc(json.dumps({"number": 7, "changed_files": self.changed_files,
                                     "additions": 10, "deletions": 4}))
        if path.startswith("repos/acme/billing/pulls/7/files?"):
            q = dict(kv.split("=") for kv in path.split("?", 1)[1].split("&"))
            per, page = int(q["per_page"]), int(q["page"])
            return _proc(json.dumps(self.files[(page - 1) * per: page * per]))
        return _proc("", 1, "gh: Not Found (HTTP 404)")


def _fetch(gh, **kw):
    return fetch_pr_files("acme/billing", 7, run=gh, token_for=lambda owner: f"tok-{owner}", **kw)


def test_paginates_every_file_and_keeps_only_the_review_fields():
    gh = _FakeGh([_file(i) for i in range(150)])
    out = _fetch(gh)
    assert [c[1] for c in gh.calls] == [
        "repos/acme/billing/pulls/7",
        "repos/acme/billing/pulls/7/files?per_page=100&page=1",
        "repos/acme/billing/pulls/7/files?per_page=100&page=2",
    ]
    # The owner's App installation token, like every other server-side PR read.
    assert set(gh.tokens) == {"tok-acme"}
    assert out["totalFiles"] == 150 and len(out["files"]) == 150
    assert out["additions"] == 10 and out["deletions"] == 4
    assert set(out["files"][0]) == {"filename", "status", "additions", "deletions", "patch"}
    assert out["cut"] == {"filesOmitted": 0, "patchesTruncated": 0, "patchesOmitted": 0,
                          "patchBytesOmitted": 0, "maxFiles": pr_files.FILES_MAX,
                          "maxPatchBytes": pr_files.PATCH_BYTES_MAX}


def test_renamed_file_carries_its_previous_name():
    gh = _FakeGh([_file(1, status="renamed", previous_filename="src/old.py")])
    row = _fetch(gh)["files"][0]
    assert row["status"] == "renamed" and row["previousFilename"] == "src/old.py"


def test_file_cap_stops_paging_and_reports_how_many_files_were_left_out():
    gh = _FakeGh([_file(i) for i in range(412)])
    out = _fetch(gh, max_files=150)
    # Two pages cover 150 files; the third is never requested.
    assert len(gh.calls) == 3
    assert len(out["files"]) == 150
    assert out["totalFiles"] == 412
    assert out["cut"]["filesOmitted"] == 262 and out["cut"]["maxFiles"] == 150


def test_patch_budget_truncates_on_a_line_boundary_then_omits_and_counts_the_bytes():
    big = "".join(f"+line {i:04d}\n" for i in range(400))       # 4400 bytes
    small = "@@ -1 +1 @@\n-a\n+b\n"
    gh = _FakeGh([_file(0, patch=small), _file(1, patch=big), _file(2, patch=small)])
    out = _fetch(gh, max_patch_bytes=3000)
    f0, f1, f2 = out["files"]
    assert f0["patch"] == small
    # The second patch is cut where the budget ran out, at a whole line, and says so.
    assert f1["patchTruncated"] is True
    assert big.startswith(f1["patch"]) and f1["patch"].endswith("\n")
    assert len(f1["patch"].encode()) <= 3000 - len(small.encode())
    # Nothing is left for the third: dropped, not silently missing.
    assert f2["patch"] is None and f2["patchOmitted"] is True
    cut = out["cut"]
    assert cut["patchesTruncated"] == 1 and cut["patchesOmitted"] == 1
    assert cut["patchBytesOmitted"] == (len(big) - len(f1["patch"])) + len(small)


def test_a_patch_github_did_not_send_is_marked_unavailable_not_cut():
    gh = _FakeGh([_file(0, patch=None)])
    out = _fetch(gh)
    row = out["files"][0]
    assert row["patch"] is None and row["patchUnavailable"] is True
    assert out["cut"]["patchesOmitted"] == 0


def test_missing_pr_is_not_found():
    gh = _FakeGh([])
    with pytest.raises(PrFilesNotFound):
        fetch_pr_files("acme/billing", 8, run=gh, token_for=lambda o: None)


def test_other_gh_failure_is_an_error_naming_gh_stderr():
    def run(args, token=None, **_kw):
        return _proc("", 1, "gh: API rate limit exceeded (HTTP 403)")
    with pytest.raises(PrFilesError, match="rate limit"):
        fetch_pr_files("acme/billing", 7, run=run, token_for=lambda o: None)


def test_unparseable_page_is_an_error_not_a_crash():
    def run(args, token=None, **_kw):
        if args[1].endswith("/pulls/7"):
            return _proc(json.dumps({"changed_files": 1}))
        return _proc("<html>")
    with pytest.raises(PrFilesError):
        fetch_pr_files("acme/billing", 7, run=run, token_for=lambda o: None)
