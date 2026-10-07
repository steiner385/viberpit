"""Only paired successful shell creation results establish PR ownership."""
import pytest

from server.sessions.events import TextDelta, ToolCall, ToolKind, ToolResult


def _events(command, *, name="Bash", key="command", ok=True, payload=None):
    return [ToolCall("t", "call", name, ToolKind.BASH, {key: command}),
            ToolResult("call", ok, payload or {"content": "https://github.com/o/r/pull/12"})]


@pytest.mark.parametrize("command", [
    "gh pr create --head fix/x --body-file /tmp/body",
    "cd /tmp/extra && gh pr create --head fix/x",
    'U=$(gh pr create --head fix/x); echo "$U"',
    "cat > /tmp/body <<'EOF'\nbody text\nEOF\ngh pr create --body-file /tmp/body",
])
def test_successful_shell_create_recovers_ref(command):
    from server.sessions.artifacts import created_pr_refs
    assert created_pr_refs(_events(command)) == {("o/r", 12)}


@pytest.mark.parametrize("command", [
    'echo "gh pr create"',
    "gh pr view 12",
    "cat > /tmp/body <<'EOF'\ngh pr create\nEOF\necho done",
])
def test_mentions_are_not_creation(command):
    from server.sessions.artifacts import created_pr_refs
    assert created_pr_refs(_events(command)) == set()


def test_failure_unpaired_result_and_assistant_prose_are_not_evidence():
    from server.sessions.artifacts import created_pr_refs
    assert not created_pr_refs(_events("gh pr create", ok=False))
    assert not created_pr_refs(_events("gh pr create", payload={"exitCode": 1, "output": "https://github.com/o/r/pull/12"}))
    assert not created_pr_refs([ToolResult("missing", True, {"content": "https://github.com/o/r/pull/12"})])
    assert not created_pr_refs([TextDelta("t", "I opened https://github.com/o/r/pull/12")])


def test_exec_command_and_functions_wrapper_results():
    from server.sessions.artifacts import created_pr_refs
    assert created_pr_refs(_events("gh pr create", name="functions.exec_command", key="cmd"))
    command = 'text(await tools.exec_command({"cmd":"gh pr create --head fix/x"}));'
    assert created_pr_refs(_events(command, name="functions.exec", key="code"))


def test_multiline_success_output_and_nested_json_ref():
    from server.sessions.artifacts import created_pr_refs
    import json
    output = "remote: \nhttps://github.com/o/r/pull/12\n2026-10-07T10:55:57Z\n"
    assert created_pr_refs(_events("gh pr create", payload={"content": output})) == {("o/r", 12)}
    payload = {"content": [{"type": "text", "text": json.dumps({"exit_code": 0, "output": output})}]}
    assert created_pr_refs(_events("gh pr create", payload=payload)) == {("o/r", 12)}
    payload["content"][0]["text"] = json.dumps({"exit_code": 1, "output": output})
    assert not created_pr_refs(_events("gh pr create", payload=payload))
