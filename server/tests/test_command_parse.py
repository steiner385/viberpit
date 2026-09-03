"""Unit tests for parse_command_line — the backend mirror of the Composer's
runCommandFromText slash detection (web/src/components/session/Composer.tsx).

Keeping the two in lockstep is what lets the initial-prompt panel route slash
commands through the SAME dispatch path the interactive composer uses.
"""
from server.sessions.commands.catalog import parse_command_line


def test_splits_name_and_args():
    assert parse_command_line("/model opus") == ("model", "opus")


def test_no_args_gives_empty_string():
    assert parse_command_line("/compact") == ("compact", "")


def test_collapses_whitespace_before_args_but_preserves_internal():
    assert parse_command_line("/model   opus  --after-turn") == (
        "model", "opus  --after-turn")


def test_non_slash_text_is_not_a_command():
    assert parse_command_line("just a normal prompt") is None


def test_bare_slash_is_not_a_command():
    assert parse_command_line("/") is None


def test_multiline_is_not_a_command():
    # A leading slash on a multi-line prompt is prose (e.g. a path), not a command
    # — the Composer rejects it the same way (raw.includes('\n')).
    assert parse_command_line("/model opus\nand do the thing") is None
