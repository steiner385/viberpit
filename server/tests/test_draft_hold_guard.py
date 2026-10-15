"""A draft PR is a hold: a session marks a draft ready only when it opened that PR.
The PreToolUse guard finds every command shape that marks a PR ready, resolves the
PR, and denies unless the session's ownership check says it is its own."""
import pytest

from server.sessions.draft_hold_guard import (
    MUTATION_REASON, UNRESOLVED_REASON, ReadyCall, TargetPr, draft_ready_guard,
    not_own_reason, scan_command)


@pytest.mark.parametrize("command, call", [
    ("gh pr ready 17", ReadyCall("17", None, None)),
    ("gh pr ready '#17'", ReadyCall("17", None, None)),
    # Unquoted, `#17` is a shell comment: bash runs a bare `gh pr ready`.
    ("gh pr ready #17", ReadyCall(None, None, None)),
    ('gh pr ready "17" --repo cairnea/KinDash', ReadyCall("17", "cairnea/KinDash", None)),
    ("gh pr ready -R o/r 17", ReadyCall("17", "o/r", None)),
    ("gh pr ready 17 --repo=o/r", ReadyCall("17", "o/r", None)),
    ("gh pr ready https://github.com/o/r/pull/17",
     ReadyCall("https://github.com/o/r/pull/17", None, None)),
    ("gh pr ready", ReadyCall(None, None, None)),
    ("gh pr ready feat/spec", ReadyCall("feat/spec", None, None)),
    ("/usr/bin/gh pr ready 17", ReadyCall("17", None, None)),
    ("cd /w/kd && gh pr ready 17", ReadyCall("17", None, "/w/kd")),
    ("git push && gh pr ready 17 && gh pr merge 17 --squash --auto",
     ReadyCall("17", None, None)),
    ("for n in 1; do gh pr ready 17; done", ReadyCall("17", None, None)),
    ("bash -c 'gh pr ready 17 --repo o/r'", ReadyCall("17", "o/r", None)),
    ('eval "gh pr ready 17"', ReadyCall("17", None, None)),
    ("gh api -X PATCH repos/o/r/pulls/17 -F draft=false", ReadyCall("17", "o/r", None)),
    ("gh api --method PATCH /repos/o/r/pulls/17 -f 'draft=false'",
     ReadyCall("17", "o/r", None)),
])
def test_ready_shapes_are_detected(command, call):
    scan = scan_command(command)
    assert scan.deny_reason is None
    assert scan.calls == (call,)


@pytest.mark.parametrize("command", [
    "gh pr ready 17 --undo",
    "gh pr ready --undo 17 --repo o/r",
    "gh pr ready --help",
    'echo "gh pr ready 17"',
    "gh pr comment 17 --body 'once CI is green, run gh pr ready 17'",
    'gh pr create --title t --body "Owner: click Ready, or gh pr ready 17"',
    "gh pr view 17 --json isDraft",
    "gh api repos/o/r/pulls/17",
    "gh api -X PATCH repos/o/r/pulls/17 -f title=x",
    "git commit -m 'teach sessions not to gh pr ready'",
])
def test_non_ready_commands_pass(command):
    scan = scan_command(command)
    assert scan.deny_reason is None and scan.calls == ()


@pytest.mark.parametrize("command", [
    "gh api graphql -f query='mutation{markPullRequestReadyForReview(input:"
    "{pullRequestId:\"PR_x\"}){clientMutationId}}'",
    # Raw-text match on the mutation name: prose quoting it is a re-phrase.
    "gh pr comment 1 --body 'do not call markPullRequestReadyForReview'",
])
def test_graphql_mutation_is_always_refused(command):
    assert scan_command(command).deny_reason == MUTATION_REASON


def test_gh_repo_without_an_explicit_repo_is_refused():
    assert scan_command("GH_REPO=o/r gh pr ready 17").deny_reason == UNRESOLVED_REASON
    assert scan_command("GH_REPO=x/y gh pr ready 17 -R o/r").calls == (
        ReadyCall("17", "o/r", None),)


def test_unbalanced_quoting_fails_closed_unless_undo():
    assert scan_command("gh pr ready 17 'oops").deny_reason == UNRESOLVED_REASON
    assert scan_command("gh pr ready 17 --undo 'oops").deny_reason is None


def _call(command, cwd="/w/s"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _resolver(*, draft=True, found=True, seen=None):
    def resolve(call, cwd):
        if seen is not None:
            seen.append((call, cwd))
        if not found:
            return None
        return TargetPr(call.repo or "o/r", int(call.selector or 17), "feat/x", draft)
    return resolve


def _owns(*own):
    async def owns_pr(repo, number, head_ref):
        return (repo, number) in own
    return owns_pr


def _denied(out):
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    return spec["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_own_draft_may_be_marked_ready():
    hook = draft_ready_guard(_owns(("o/r", 17)), _resolver())
    assert await hook(_call("gh pr ready 17"), "tu", None) == {}


@pytest.mark.asyncio
async def test_another_sessions_draft_is_refused_with_the_hand_back_advice():
    hook = draft_ready_guard(_owns(("o/r", 99)), _resolver())
    reason = _denied(await hook(_call("gh pr ready 17 --repo o/r"), "tu", None))
    assert reason == not_own_reason("o/r", 17)
    assert "hand back" in reason and "--undo" in reason


@pytest.mark.asyncio
async def test_without_an_ownership_check_no_draft_is_own():
    hook = draft_ready_guard(None, _resolver())
    assert _denied(await hook(_call("gh pr ready 17"), "tu", None))


@pytest.mark.asyncio
async def test_one_foreign_target_in_a_chain_denies_the_whole_call():
    hook = draft_ready_guard(_owns(("o/r", 17)), _resolver())
    assert _denied(await hook(_call("gh pr ready 17 && gh pr ready 18"), "tu", None))


@pytest.mark.asyncio
async def test_undo_and_prose_never_resolve_a_pr():
    seen: list = []
    hook = draft_ready_guard(_owns(), _resolver(seen=seen))
    for command in ("gh pr ready 17 --undo", 'echo "gh pr ready 17"', "gh pr view 17"):
        assert await hook(_call(command), "tu", None) == {}
    assert seen == []


@pytest.mark.asyncio
async def test_a_pr_that_is_not_a_draft_passes():
    hook = draft_ready_guard(_owns(), _resolver(draft=False))
    assert await hook(_call("gh pr ready 17"), "tu", None) == {}


@pytest.mark.asyncio
async def test_an_unresolvable_target_is_refused():
    hook = draft_ready_guard(_owns(("o/r", 17)), _resolver(found=False))
    assert _denied(await hook(_call("gh pr ready"), "tu", None)) == UNRESOLVED_REASON


@pytest.mark.asyncio
async def test_resolution_runs_where_the_command_runs():
    seen: list = []
    hook = draft_ready_guard(_owns(("o/r", 17)), _resolver(seen=seen))
    await hook(_call("cd ../other && gh pr ready 17", cwd="/w/s"), "tu", None)
    await hook(_call("cd /abs && gh pr ready 17", cwd="/w/s"), "tu", None)
    await hook(_call("gh pr ready 17", cwd="/w/s"), "tu", None)
    assert [cwd for _, cwd in seen] == ["/w/s/../other", "/abs", "/w/s"]


@pytest.mark.asyncio
async def test_a_slow_ownership_check_denies_rather_than_outlive_the_hook_timeout(
        monkeypatch):
    import asyncio
    from server.sessions import draft_hold_guard as guard
    monkeypatch.setattr(guard, "GUARD_BUDGET_S", 0.05)

    async def slow(repo, number, head_ref):
        await asyncio.sleep(5)
        return True
    hook = draft_ready_guard(slow, _resolver())
    assert _denied(await hook(_call("gh pr ready 17"), "tu", None)) == UNRESOLVED_REASON


@pytest.mark.asyncio
async def test_mutation_is_refused_before_any_lookup():
    hook = draft_ready_guard(_owns(("o/r", 17)), _resolver(seen=[]))
    out = await hook(_call("gh api graphql -f query=@q -F x=markPullRequestReadyForReview"),
                     "tu", None)
    assert _denied(out) == MUTATION_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("input_data", [
    {"tool_name": "Bash", "tool_input": {"command": "gh pr view 1"}},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": "not-a-dict"},
    {},
    None,
])
async def test_hook_passes_everything_else_through(input_data):
    hook = draft_ready_guard(None, _resolver(seen=[]))
    assert await hook(input_data, None, None) == {}


def test_resolve_pr_reads_gh_pr_view_in_the_command_directory(monkeypatch, tmp_path):
    import json
    import subprocess
    from server.sessions import draft_hold_guard as guard
    seen = {}

    def run(args, **kw):
        seen.update(args=args, cwd=kw.get("cwd"))
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "url": "https://github.com/O/R/pull/17", "number": 17,
            "headRefName": "feat/x", "isDraft": True}), "")
    monkeypatch.setattr(guard.subprocess, "run", run)
    monkeypatch.setattr(guard.gitrepos, "app_token_for_owner", lambda owner: None)
    pr = guard.resolve_pr(ReadyCall("17", None, None), str(tmp_path))
    assert pr == TargetPr("O/R", 17, "feat/x", True)
    assert seen["args"][:4] == ["gh", "pr", "view", "17"] and seen["cwd"] == str(tmp_path)
    monkeypatch.setattr(guard.subprocess, "run", lambda args, **kw:
                        subprocess.CompletedProcess(args, 1, "", "no pull requests found"))
    assert guard.resolve_pr(ReadyCall(None, "o/r", None), None) is None
    # An unknowable directory never falls back to the daemon's own repo.
    assert guard.resolve_pr(ReadyCall("17", None, None), str(tmp_path / "missing")) is None
