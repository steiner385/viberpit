import pytest

from server.codex.capabilities import CODEX_CAPABILITIES
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.commands.catalog import (
    APP,
    APP_COMMANDS,
    VENDOR,
    app_command,
    build_catalog,
    classify,
)
from server.sessions.engine import AgentCapabilities

CLAUDE = ClaudeAgentDriver.capabilities


def test_classify_buckets():
    assert classify("help") == "client"
    assert classify("context") == "client"
    assert classify("model") == "control"
    assert classify("clear") == "control"
    assert classify("review") == "passthrough"   # unknown → custom/passthrough


def test_build_catalog_normalizes_and_filters():
    info = {"commands": [
        {"name": "help", "description": "List commands"},
        {"name": "/model", "description": "Switch model", "argumentHint": "<model>"},
        {"name": "review", "description": "Review a PR", "argumentHint": "<pr#>"},
        {"name": "login", "description": "Sign in"},          # excluded (interactive)
        "barecmd",                                             # string form tolerated
        {"description": "no name"},                            # dropped (no name)
    ]}
    cat = build_catalog(info, CLAUDE)
    by = {c.name: c for c in cat}
    assert "login" not in by                                  # filtered
    assert by["model"].kind == "control"
    assert by["model"].argument_hint == "<model>"
    assert by["help"].kind == "client"
    assert by["review"].kind == "passthrough"
    assert by["barecmd"].kind == "passthrough"
    assert by["model"].to_wire() == {
        "name": "model", "description": "Switch model",
        "argumentHint": "<model>", "kind": "control", "source": "vendor",
    }


def test_build_catalog_handles_none():
    # build_catalog always includes the app-owned commands this engine supports,
    # so even empty/None vendor input returns a usable palette.
    names = {c.name for c in build_catalog(None, CLAUDE)}
    assert "account" in names
    names_empty = {c.name for c in build_catalog({}, CLAUDE)}
    assert "account" in names_empty


def test_vendor_entry_wins_a_name_collision_with_an_app_command():
    """When an engine publishes a command of its own, that name means what the
    engine says it means — the app-owned fallback must not overwrite its
    description or re-classify it."""
    info = {"commands": [{"name": "model", "description": "the CLI's own /model"}]}
    by = {c.name: c for c in build_catalog(info, CLAUDE)}
    assert by["model"].description == "the CLI's own /model"
    assert by["model"].source == VENDOR
    assert sum(1 for c in build_catalog(info, CLAUDE) if c.name == "model") == 1


# --- #1286: a truthful catalog for an engine with no Claude slash syntax -----


def test_codex_catalog_offers_the_app_commands_it_actually_supports():
    """The regression this issue names: a Codex session's palette held ONE entry
    (`/account`) while `/model` was silently dispatchable, because the app-owned
    half was a single hardcoded synthetic command."""
    names = {c.name for c in build_catalog(None, CODEX_CAPABILITIES)}
    assert {"help", "context", "clear", "model", "account"} <= names
    assert all(
        c.source == APP for c in build_catalog(None, CODEX_CAPABILITIES)
    ), "with no vendor answer, every entry is ccswitch's own"


def test_codex_vendor_entries_are_skills_not_slash_passthrough():
    """A Codex catalog entry must never be `passthrough`: passthrough writes
    `/name` into the prompt for the ENGINE to parse, and the app-server does not
    parse slash syntax — it would reach the model as literal text."""
    info = {"commands": [{"name": "pr-review", "description": "Review a PR"}]}
    by = {c.name: c for c in build_catalog(info, CODEX_CAPABILITIES)}
    assert by["pr-review"].kind == "skill"
    assert by["pr-review"].source == VENDOR
    assert not any(
        c.kind == "passthrough" for c in build_catalog(info, CODEX_CAPABILITIES)
    )


def test_an_engine_without_a_vendor_catalog_ignores_vendor_entries():
    """`vendor_commands=False` means "this engine publishes no such list", so a
    payload carrying one is a stale cache, not a fact about the engine."""
    caps = AgentCapabilities(models=True)
    info = {"commands": [{"name": "ghost", "description": "from a stale cache"}]}
    names = {c.name for c in build_catalog(info, caps)}
    assert "ghost" not in names
    assert "model" in names


def test_model_is_withheld_from_an_engine_that_cannot_switch_models():
    caps = AgentCapabilities(models=False, account_rotation=True)
    names = {c.name for c in build_catalog(None, caps)}
    assert "model" not in names
    assert "account" in names


@pytest.mark.parametrize("caps", [CLAUDE, CODEX_CAPABILITIES])
def test_every_app_command_offered_is_also_dispatchable(caps):
    """`app_command` is the gate `run_command` consults; the palette and the
    dispatcher must agree, or a command is visible-but-refused (or worse,
    dispatchable-but-invisible, which is what #1286 found)."""
    offered = {c.name for c in build_catalog(None, caps)}
    declared = {entry.command.name for entry in APP_COMMANDS}
    for name in declared:
        assert (app_command(name, caps) is not None) == (name in offered)


def test_app_command_tolerates_a_leading_slash():
    assert app_command("/model", CLAUDE) is not None
    assert app_command("model", CLAUDE) is not None
    assert app_command("nonesuch", CLAUDE) is None
