"""Control-command echoes must not manufacture autonomous model turns (#2844)."""
import pytest
from claude_agent_sdk import TextBlock, UserMessage

from server.sessions.sdk_contract import is_turn_message


@pytest.mark.parametrize("text", [
    "<local-command-caveat>Context only</local-command-caveat>",
    "<command-name>/model</command-name>",
    "<local-command-stdout>Set model to `opus (claude-opus-5-5)`</local-command-stdout>",
    "<local-command-stderr>Unknown model</local-command-stderr>",
])
@pytest.mark.parametrize("blocks", [False, True])
def test_local_command_echo_is_not_a_turn(text, blocks):
    content = [TextBlock(text=text)] if blocks else text
    assert not is_turn_message(UserMessage(content=content))


@pytest.mark.parametrize("text", [
    "<task-notification>Background task finished</task-notification>",
    "Please continue",
])
def test_real_user_or_background_continuation_still_opens_turn(text):
    assert is_turn_message(UserMessage(content=text))
