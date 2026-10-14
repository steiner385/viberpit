"""A session can never remove ``hold:human`` (#3088): the PreToolUse guard denies
every command shape that removes the label and passes everything else through."""
import pytest

from server.sessions.human_hold_guard import (
    DENY_REASON, human_hold_pre_tool_use, removes_human_hold)


@pytest.mark.parametrize("command", [
    # The incident shape: edit + comment in one call.
    "gh pr edit 19061 --repo cairnea/KinDash --remove-label hold:human "
    "&& gh pr comment 19061 --body-file /tmp/r18.md",
    'gh pr edit 19061 --remove-label "hold:human"',
    "gh issue edit 19061 --remove-label docs,HOLD:HUMAN",
    "gh api -X DELETE repos/cairnea/KinDash/issues/19061/labels/hold:human",
    "gh api --method DELETE repos/o/r/issues/1/labels/hold%3Ahuman",
    "gh label delete hold:human --repo o/r --yes",
    "gh api graphql -f query='mutation{removeLabelsFromLabelable(input:{labelableId:\"X\","
    "labelIds:[\"L\"]}){clientMutationId}}' # hold:human",
])
def test_removal_shapes_are_detected(command):
    assert removes_human_hold(command)


@pytest.mark.parametrize("command", [
    "gh pr view 19061 --json labels --jq '.labels[].name' | grep hold:human",
    "gh pr edit 19061 --add-label hold:human",
    "gh pr edit 19061 --remove-label needs-triage",
    "gh pr comment 19061 --body-file /tmp/r18.md",
    "git push origin HEAD:feature",
])
def test_non_removals_pass(command):
    assert not removes_human_hold(command)


@pytest.mark.asyncio
async def test_hook_denies_a_removal_with_the_reason():
    out = await human_hold_pre_tool_use(
        {"tool_name": "Bash",
         "tool_input": {"command": "gh pr edit 1 --remove-label hold:human"}},
        "tu1", None)
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    assert spec["permissionDecisionReason"] == DENY_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("input_data", [
    {"tool_name": "Bash", "tool_input": {"command": "gh pr view 1"}},
    {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Bash", "tool_input": "not-a-dict"},
    {},
    None,
])
async def test_hook_passes_everything_else_through(input_data):
    assert await human_hold_pre_tool_use(input_data, None, None) == {}
