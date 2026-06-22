"""Real-SDK spike: pin slash-command behavior before building the dispatcher.

Opt in with CCSWITCH_SPIKE=1 and a usable account config dir in CCSWITCH_SPIKE_CONFIG_DIR.
These hit the real `claude` engine and spend a little quota; they are NOT part of CI.
"""
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CCSWITCH_SPIKE") != "1",
    reason="real-SDK spike; set CCSWITCH_SPIKE=1 to run",
)


@pytest.mark.anyio
async def test_server_info_commands_shape():
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    cfg = os.environ["CCSWITCH_SPIKE_CONFIG_DIR"]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
    async with ClaudeSDKClient(options=ClaudeAgentOptions(env=env, cwd=os.getcwd())) as c:
        info = await c.get_server_info()
        assert info is not None, "get_server_info returned None — not in streaming mode?"
        cmds = info.get("commands")
        print("COMMANDS SAMPLE:", (cmds or [])[:5])
        assert isinstance(cmds, list) and cmds, "no commands enumerated"


@pytest.mark.anyio
async def test_custom_command_expands_via_query(tmp_path):
    # Create a throwaway .claude/commands/spikeecho.md in cwd and confirm /spikeecho
    # produces an assistant turn (i.e. the template was expanded, not echoed verbatim).
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage
    cwd = tmp_path
    cmds_dir = cwd / ".claude" / "commands"
    cmds_dir.mkdir(parents=True)
    (cmds_dir / "spikeecho.md").write_text("Reply with exactly the word: PONG")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.environ["CCSWITCH_SPIKE_CONFIG_DIR"])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(env=env, cwd=str(cwd))) as c:
        await c.query("/spikeecho")
        saw_assistant = False
        async for m in c.receive_response():
            if isinstance(m, AssistantMessage):
                saw_assistant = True
        assert saw_assistant, "custom command did not produce an assistant turn"


@pytest.mark.anyio
async def test_compact_behavior():
    # Record whether query("/compact") is accepted and what it streams back.
    # The VERDICT (works vs no-op) decides whether /compact ships. Print, don't assert.
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.environ["CCSWITCH_SPIKE_CONFIG_DIR"])
    async with ClaudeSDKClient(options=ClaudeAgentOptions(env=env, cwd=os.getcwd())) as c:
        await c.query("/compact")
        kinds = []
        async for m in c.receive_response():
            kinds.append(type(m).__name__)
        print("COMPACT STREAM:", kinds)
