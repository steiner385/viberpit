"""A `hold:human` PR is merged by its owner only (#3101): the PreToolUse guard finds
every command shape that merges a PR or arms its auto-merge, resolves the PR, and
denies when it carries the label — whoever opened it."""
import pytest

from server.sessions.draft_hold_guard import ReadyCall, TargetPr
from server.sessions.merge_hold_guard import (
    HIDDEN_QUERY_REASON, MUTATION_REASON, UNRESOLVED_REASON, held_reason, merge_hold_guard,
    scan_command)


@pytest.fixture(autouse=True)
def _no_inherited_gh_repo(monkeypatch):
    """A GH_REPO in the runner's environment would make every unqualified call
    ambiguous (``env_repo_ambiguous``)."""
    monkeypatch.delenv("GH_REPO", raising=False)


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


_ENQ = "enqueue" + "PullRequest"   # split so this file's own text is not the shape


@pytest.mark.parametrize("command, node_id", [
    # The incident recipe (#3200): a variable bound by -F / -f.
    (f"gh api graphql -f query='mutation($id:ID!){{{_ENQ}(input:{{pullRequestId:$id,"
     f"jump:true}}){{mergeQueueEntry{{position state}}}}}}' -F id=PR_kwDOabc123", "PR_kwDOabc123"),
    (f"gh api graphql -F id=PR_x -f query='mutation($id:ID!){{{_ENQ}(input:"
     f"{{jump:true, pullRequestId: $id}}){{clientMutationId}}}}'", "PR_x"),
    # An inline literal, in a chain after the lock step.
    (f"gh api graphql -f query='mutation{{q:{_ENQ}(input:{{pullRequestId:\"PR_y\"}})"
     f"{{clientMutationId}}}}' | jq .", "PR_y"),
])
def test_an_enqueue_naming_one_readable_node_id_is_judged(command, node_id):
    scan = scan_command(command)
    assert scan.deny_reason is None
    assert scan.calls == (ReadyCall(None, None, None, node_id),)


def test_an_enqueue_is_judged_alongside_other_merge_calls():
    command = (f"gh pr merge 17 --auto && gh api graphql -f query='mutation{{{_ENQ}"
               f"(input:{{pullRequestId:\"PR_y\"}}){{clientMutationId}}}}'")
    assert scan_command(command).calls == (
        ReadyCall("17", None, None), ReadyCall(None, None, None, "PR_y"))


def test_gh_repo_does_not_make_a_node_id_ambiguous(monkeypatch):
    monkeypatch.setenv("GH_REPO", "o/r")
    command = (f"gh api graphql -f query='mutation{{{_ENQ}(input:{{pullRequestId:\"PR_y\"}})"
               f"{{clientMutationId}}}}'")
    assert scan_command(command).calls == (ReadyCall(None, None, None, "PR_y"),)


_Q = f"mutation($id:ID!){{{_ENQ}(input:{{pullRequestId:$id}}){{clientMutationId}}}}"


@pytest.mark.parametrize("command", [
    # The id is a shell expansion, a file, missing, or bound twice (gh keeps the last).
    f"gh api graphql -f query='{_Q}' -F id=$PR",
    f"gh api graphql -f query='{_Q}' -F id=\"$(gh pr view 1 --json id -q .id)\"",
    f"gh api graphql -f query='{_Q}' -F id=@id.txt",
    f"gh api graphql -f query='{_Q}'",
    f"gh api graphql -f query='{_Q}' -F id=PR_a -F id=PR_b",
    f"gh api graphql -f query='{_Q}' -F id=PR_a -Fid=PR_b",
    f"gh api graphql -f query='{_Q}' -F id=PR_a --field=id=PR_b",
    # The input is a variable, or the id sits outside the enqueue's input.
    f"gh api graphql -f query='mutation($i:{_ENQ}Input!){{{_ENQ}(input:$i){{x}}}}' "
    "-F i[pullRequestId]=PR_a",
    f"gh api graphql -f query='mutation{{{_ENQ}(input:$i){{x}} c:addComment(input:"
    "{pullRequestId:\"PR_a\"}){x}}'",
    # A second pullRequestId — even in a comment or a string — hides which id is read.
    f"gh api graphql -f query='mutation{{{_ENQ}(input:{{#pullRequestId:\"PR_a\"\n"
    "pullRequestId:\"PR_b\"}){x}}'",
    f"gh api graphql -f query='mutation{{{_ENQ}(input:{{clientMutationId:\"pullRequestId:\","
    "pullRequestId:\"PR_b\"}){x}}'",
    # Two enqueues, or an enqueue plus another merge mutation.
    f"gh api graphql -f query='mutation{{a:{_ENQ}(input:{{pullRequestId:\"PR_a\"}}){{x}} "
    f"b:{_ENQ}(input:{{pullRequestId:\"PR_b\"}}){{x}}}}'",
    f"gh api graphql -f query='mutation{{{_ENQ}(input:{{pullRequestId:\"PR_a\"}}){{x}} "
    "m:merge''PullRequest(input:{pullRequestId:\"PR_b\"}){x}}'",
    # The query is not exactly the single-quoted text the shell passes.
    f"gh api graphql -f query=\"{_Q}\" -F id=PR_a",
    f"gh api graphql -f query='{_Q}'$EXTRA -F id=PR_a",
    f"bash -c \"gh api graphql -f query='{_Q}' -F id=PR_a\"",
    f"gh api graphql -fquery='{_Q}' -F id=PR_a",
    # Not a gh api graphql call the guard reads.
    f"curl -d '{_Q}' https://api.github.com/graphql",
    f"gh api --hostname x graphql -f query='{_Q}' -F id=PR_a",
    f"gh api graphql --hostname x -f query='{_Q}' -F id=PR_a",
    # GraphQL is case-sensitive: a near-miss spelling is not the mutation it reads.
    "gh api graphql -f query='mutation{ENQUEUEPullRequest(input:{pullRequestId:\"PR_a\"})"
    "{x}}'",
])
def test_an_enqueue_the_guard_cannot_read_exactly_is_refused(command):
    assert scan_command(command).deny_reason == MUTATION_REASON


@pytest.mark.asyncio
async def test_the_merge_queue_jump_on_an_unheld_pr_passes_and_on_a_held_one_is_refused():
    command = (f"gh api graphql -f query='mutation($id:ID!){{{_ENQ}(input:{{pullRequestId:$id,"
               "jump:true}){mergeQueueEntry{position}}}' -F id=PR_x")
    seen: list = []

    def resolve(call, cwd):
        seen.append(call)
        return TargetPr("o/r", 20177, "fix/x", False, held)
    held = False
    assert await merge_hold_guard(resolve)(_call(command), "tu", None) == {}
    held = True
    assert _denied(await merge_hold_guard(resolve)(_call(command), "tu", None)) == \
        held_reason("o/r", 20177)
    assert [c.node_id for c in seen] == ["PR_x", "PR_x"]


@pytest.mark.asyncio
async def test_an_unresolvable_node_id_is_refused():
    command = f"gh api graphql -f query='{_Q}' -F id=PR_x"
    hook = merge_hold_guard(_resolver(found=False))
    assert _denied(await hook(_call(command), "tu", None)) == UNRESOLVED_REASON


@pytest.mark.parametrize("command", [
    "gh api graphql -F query=@arm.graphql -f id=PR_x",
    "gh api graphql --input body.json",
    "bash -c 'gh api graphql -F query=@q'",
    'gh api graph""ql -F "query=@q"',
    'gh api graphql -f query="$Q"',
    "gh api graphql -f query=$(cat arm.graphql)",
    "gh api graphql -Fquery=@q",
    "gh api graphql --raw-field=query=`cat q`",
    "gh api graphql --input=body.json",
])
def test_a_graphql_query_the_guard_cannot_read_is_refused(command):
    assert scan_command(command).deny_reason == HIDDEN_QUERY_REASON


@pytest.mark.parametrize("command", [
    "gh api graphql -f query='query($n:Int!){viewer{login}}' -F n=1",
    "gh api graphql -f query='{viewer{login}}'",
])
def test_an_inline_graphql_query_without_a_merge_mutation_passes(command):
    scan = scan_command(command)
    assert scan.deny_reason is None and scan.calls == ()


@pytest.mark.parametrize("command", [
    "gh api graphql -f query='mutation{enable''PullRequestAutoMerge(input:{}){x}}'",
    # …and inside a command string a shell will run.
    """bash -c "gh api graphql -f query='mutation{enable''PullRequestAutoMerge{x}}'" """,
])
def test_a_quote_split_mutation_name_is_still_refused(command):
    assert scan_command(command).deny_reason == MUTATION_REASON


@pytest.mark.parametrize("command", [
    """bash -c "gh api https://api.github.com/graphql -f query='$Q'" """,
    """eval "gh api graphql -F 'query=@q'" """,
])
def test_a_nested_hidden_graphql_query_is_refused(command):
    assert scan_command(command).deny_reason == HIDDEN_QUERY_REASON


def test_a_nested_quote_split_gh_repo_is_ambiguous():
    command = """bash -c 'GH_R""EPO=o/r gh pr merge 17 --auto'"""
    assert scan_command(command).deny_reason == UNRESOLVED_REASON


@pytest.mark.parametrize("command", [
    "GH_REPO=o/r gh pr merge 17 --auto",
    "export GH_REPO=o/r; gh pr merge 17 --auto",
    "env GH_REPO=o/r gh pr merge 17 --auto",
    'GH_R""EPO=o/r gh pr merge 17 --auto',
])
def test_gh_repo_without_an_explicit_repo_is_refused(command):
    """gh would merge in GH_REPO while the lookup reads the working directory's
    repo — two different PRs — so the call must name --repo."""
    assert scan_command(command).deny_reason == UNRESOLVED_REASON


def test_gh_repo_inherited_from_the_session_environment_is_ambiguous(monkeypatch):
    monkeypatch.setenv("GH_REPO", "o/r")
    assert scan_command("gh pr merge 17 --auto").deny_reason == UNRESOLVED_REASON
    assert scan_command("gh pr merge 17 --auto -R o/r").calls == (
        ReadyCall("17", "o/r", None),)


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


@pytest.mark.parametrize("payload, expected", [
    ({"url": "https://github.com/O/R/pull/20177", "number": 20177, "headRefName": "fix/x",
      "isDraft": False, "labels": [{"name": "hold:human"}]},
     TargetPr("O/R", 20177, "fix/x", False, True)),
    ({"url": "https://github.com/O/R/pull/20177", "number": 20177, "headRefName": "fix/x",
      "isDraft": False, "labels": []}, TargetPr("O/R", 20177, "fix/x", False, False)),
    # The id is some other node type: no PR url, so the guard cannot vouch for it.
    ({"url": None, "number": None, "headRefName": None, "isDraft": None, "labels": None}, None),
])
def test_resolve_pr_maps_a_node_id_to_its_pr(monkeypatch, payload, expected):
    import json
    import subprocess
    from server.sessions import draft_hold_guard as guard
    seen = {}

    def run(args, **kw):
        seen["args"], seen["cwd"] = args, kw.get("cwd")
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
    monkeypatch.setattr(guard.subprocess, "run", run)
    pr = guard.resolve_pr(ReadyCall(None, None, "/nowhere", "PR_kwDOabc"), "/nowhere")
    assert pr == expected
    assert seen["args"][:3] == ["gh", "api", "graphql"]
    assert "id=PR_kwDOabc" in seen["args"] and seen["cwd"] is None
