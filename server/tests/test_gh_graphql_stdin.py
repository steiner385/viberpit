"""Regression: a large GraphQL query must go to `gh` via stdin, not argv.

The PR poller batches one sub-query per tracked (repo, branch) pair into a single
GraphQL document. With enough pairs the query string exceeds the per-argument
kernel limit (MAX_ARG_STRLEN, 128 KiB), and passing it as `-f query=<huge>` makes
`subprocess.run(["gh", ...])` raise ``OSError: [Errno 7] Argument list too long``,
which killed the PR poll cycle (``pr_poller.refresh_failed``). Sending the query on
stdin via ``gh api graphql -F query=@-`` sidesteps the argv size limit entirely.
"""
from __future__ import annotations

from viberpit import gitrepos
from server.github import pr_poller


def test_run_gh_forwards_input_to_stdin(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["input"] = kw.get("input")
        class P:  # noqa: E306
            returncode = 0
            stdout = "{}"
            stderr = ""
        return P()

    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)
    gitrepos._run_gh(["api", "graphql", "-F", "query=@-"], input="the-query")
    assert captured["input"] == "the-query"


def test_default_query_sends_query_via_stdin_not_argv(monkeypatch):
    """The query text must never appear in argv (that is what blows ARG_MAX)."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        class P:  # noqa: E306
            returncode = 0
            stdout = '{"data": {}}'
            stderr = ""
        return P()

    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)

    # A realistic multi-pair batch — the actual query text is opaque here; what
    # matters is where it lands.
    pairs = [("owner/repo", f"branch-{i}") for i in range(50)]
    pr_poller._default_query(pairs)

    query_text = captured["input"]
    assert query_text, "query must be passed on stdin"
    # gh reads the value from stdin
    assert "query=@-" in captured["cmd"]
    # and the query text must NOT be embedded in any argv element
    assert not any(query_text in arg for arg in captured["cmd"]), (
        "query text leaked into argv — this reintroduces the ARG_MAX crash"
    )


def _failing_gh(stderr):
    import subprocess

    def fake_run_gh(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", stderr)

    return fake_run_gh


def test_default_query_transient_503_logs_info_not_a_warning(caplog, monkeypatch):
    """#2669: a GitHub-side 503 retries every tick; it must not file an auto-error."""
    import logging

    monkeypatch.setattr(gitrepos, "_run_gh", _failing_gh(
        "gh: No server is currently available to service your request. (HTTP 503)"))
    with caplog.at_level(logging.DEBUG):
        pr_poller._default_query([("o/r", "b")])

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any("pr_poller.query_failed" in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO)


def test_default_query_non_transient_failure_still_warns(caplog, monkeypatch):
    import logging

    monkeypatch.setattr(gitrepos, "_run_gh", _failing_gh("gh: not found (HTTP 404)"))
    with caplog.at_level(logging.DEBUG):
        pr_poller._default_query([("o/r", "b")])

    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert "pr_poller.query_failed" in warned[0].getMessage()
