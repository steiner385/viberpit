"""A `hold:human` PR is merged by its owner only (#3101): the PreToolUse guard finds
every command shape that merges a PR or arms its auto-merge, resolves the PR, and
denies when it carries the label — whoever opened it."""
import pytest

from server.sessions.draft_hold_guard import ReadyCall, TargetPr
from server.sessions.merge_hold_guard import (
    HIDDEN_QUERY_REASON, MUTATION_REASON, UNRESOLVED_REASON, held_reason, merge_hold_guard,
    scan_command)


@pytest.mark.parametrize("command, call", [
    ("gh pr merge 19061 --squash --auto", ReadyCall("19061", None, None)),
    ("gh pr merge '#17' --auto", ReadyCall("17", None, None)),
    ("gh pr merge --squash --auto", ReadyCall(None, None, None)),
    ("gh pr merge 17 --admin --merge", ReadyCall("17", None, None)),
    ("gh pr merge -R o/r 17 --auto", ReadyCall("17", "o/r", None)),
    ("gh pr merge --repo=o/r 17", ReadyCall("17", "o/r", None)),
    # A flag's value is never read as the selector.
    ("gh pr merge --squash -t 'fix it' -b body --auto 17", ReadyCall("17", None, None)),
    ("gh pr merge --match-head-commit abc123 --auto", ReadyCall(None, None, None)),
    ("gh pr merge https://github.com/o/r/pull/17 --auto",
     ReadyCall("https://github.com/o/r/pull/17", None, None)),
    ("cd /w/kd && git push && gh pr merge 17 --squash --auto",
     ReadyCall("17", None, "/w/kd")),
    ("bash -c 'gh pr merge 17 --auto --repo o/r'", ReadyCall("17", "o/r", None)),
    ("gh api -X PUT repos/o/r/pulls/17/merge -f merge_method=squash",
     ReadyCall("17", "o/r", None)),
    ("gh api -X PUT 'repos/o/r/pulls/17/merge?merge_method=squash'",
     ReadyCall("17", "o/r", None)),
    ("gh api -X PUT https://api.github.com/repos/o/r/pulls/17/merge",
     ReadyCall("17", "o/r", None)),
    # An explicit --repo beats GH_REPO in gh, so the call is still judged.
    ("GH_REPO=x/y gh pr merge 17 --auto --repo o/r", ReadyCall("17", "o/r", None)),
])
def test_merge_shapes_are_detected(command, call):
    scan = scan_command(command)
    assert scan.deny_reason is None
    assert scan.calls == (call,)


@pytest.mark.parametrize("command", [
    "gh pr merge 17 --disable-auto",
    "gh pr merge --help",
    'echo "gh pr merge 17 --auto"',
    "gh pr comment 17 --body 'then run gh pr merge 17 --squash --auto'",
    "gh pr view 17 --json autoMergeRequest",
    "gh api repos/o/r/pulls/17",
    "git merge origin/master",
])
def test_non_merge_commands_pass(command):
    scan = scan_command(command)
    assert scan.deny_reason is None and scan.calls == ()


@pytest.mark.parametrize("mutation", [
    "enablePullRequestAutoMerge", "mergePullRequest", "enqueuePullRequest"])
def test_graphql_merge_mutations_are_always_refused(mutation):
    command = f"gh api graphql -f query='mutation{{{mutation}(input:{{}}){{clientMutationId}}}}'"
    assert scan_command(command).deny_reason == MUTATION_REASON


@pytest.mark.parametrize("command", [
    "gh api graphql -F query=@arm.graphql -f id=PR_x",
    "gh api graphql --input body.json",
    "bash -c 'gh api graphql -F query=@q'",
])
def test_a_graphql_query_the_guard_cannot_read_is_refused(command):
    assert scan_command(command).deny_reason == HIDDEN_QUERY_REASON


@pytest.mark.parametrize("command", [
    "GH_REPO=o/r gh pr merge 17 --auto",
    "export GH_REPO=o/r; gh pr merge 17 --auto",
    "env GH_REPO=o/r gh pr merge 17 --auto",
])
def test_gh_repo_without_an_explicit_repo_is_refused(command):
    """gh would merge in GH_REPO while the lookup reads the working directory's
    repo — two different PRs — so the call must name --repo."""
    assert scan_command(command).deny_reason == UNRESOLVED_REASON


def test_unbalanced_quoting_fails_closed_unless_disarming():
    assert scan_command("gh pr merge 17 --auto 'oops").deny_reason == UNRESOLVED_REASON
    assert scan_command("gh pr merge 17 --disable-auto 'oops").deny_reason is None


def _call(command, cwd="/w/s"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _resolver(*held, found=True, seen=None):
    def resolve(call, cwd):
        if seen is not None:
            seen.append((call, cwd))
        if not found:
            return None
        number = int(call.selector or 17)
        return TargetPr(call.repo or "o/r", number, "feat/x", False, number in held)
    return resolve


def _denied(out):
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse" and spec["permissionDecision"] == "deny"
    return spec["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_arming_a_held_pr_is_refused_with_the_hand_back_advice():
    hook = merge_hold_guard(_resolver(19061))
    reason = _denied(await hook(_call("gh pr merge 19061 --squash --auto"), "tu", None))
    assert reason == held_reason("o/r", 19061)
    assert "hold:human" in reason and "hand back" in reason and "--disable-auto" in reason


@pytest.mark.asyncio
async def test_an_unheld_pr_may_be_armed():
    hook = merge_hold_guard(_resolver())
    assert await hook(_call("gh pr merge 17 --squash --auto"), "tu", None) == {}


@pytest.mark.asyncio
async def test_one_held_target_in_a_chain_denies_the_whole_call():
    hook = merge_hold_guard(_resolver(18))
    assert _denied(await hook(
        _call("gh pr merge 17 --auto && gh pr merge 18 --auto"), "tu", None))


@pytest.mark.asyncio
async def test_an_unresolvable_target_is_refused():
    hook = merge_hold_guard(_resolver(found=False))
    assert _denied(await hook(_call("gh pr merge --auto"), "tu", None)) == UNRESOLVED_REASON


@pytest.mark.asyncio
async def test_disarm_and_prose_never_resolve_a_pr():
    seen: list = []
    hook = merge_hold_guard(_resolver(17, seen=seen))
    for command in ("gh pr merge 17 --disable-auto", 'echo "gh pr merge 17"', "gh pr view 17"):
        assert await hook(_call(command), "tu", None) == {}
    assert seen == []


@pytest.mark.asyncio
async def test_resolution_runs_where_the_command_runs():
    seen: list = []
    hook = merge_hold_guard(_resolver(seen=seen))
    await hook(_call("cd ../kd && gh pr merge 17 --auto", cwd="/w/s"), "tu", None)
    await hook(_call("gh pr merge 17 --auto", cwd="/w/s"), "tu", None)
    assert [cwd for _, cwd in seen] == ["/w/s/../kd", "/w/s"]


@pytest.mark.asyncio
async def test_a_slow_lookup_denies_rather_than_outlive_the_hook_timeout(monkeypatch):
    import time
    from server.sessions import draft_hold_guard
    monkeypatch.setattr(draft_hold_guard, "GUARD_BUDGET_S", 0.05)

    def slow(call, cwd):
        time.sleep(0.2)
        return TargetPr("o/r", 17, "feat/x", False, False)
    hook = merge_hold_guard(slow)
    assert _denied(await hook(_call("gh pr merge 17 --auto"), "tu", None)) == UNRESOLVED_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("input_data", [
    {"tool_name": "Bash", "tool_input": {"command": "gh pr view 1"}},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": "not-a-dict"},
    {},
    None,
])
async def test_hook_passes_everything_else_through(input_data):
    hook = merge_hold_guard(_resolver(17, seen=[]))
    assert await hook(input_data, None, None) == {}


def test_resolve_pr_reads_the_hold_label(monkeypatch):
    import json
    import subprocess
    from server.sessions import draft_hold_guard as guard
    seen = {}

    def run(args, **kw):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, json.dumps({
            "url": "https://github.com/O/R/pull/17", "number": 17, "headRefName": "feat/x",
            "isDraft": False, "labels": [{"name": "Hold:Human"}, {"name": "bug"}]}), "")
    monkeypatch.setattr(guard.subprocess, "run", run)
    monkeypatch.setattr(guard.gitrepos, "app_token_for_owner", lambda owner: None)
    pr = guard.resolve_pr(ReadyCall("17", "O/R", None), None)
    assert pr == TargetPr("O/R", 17, "feat/x", False, True)
    assert "labels" in seen["args"][-1].split(",")
