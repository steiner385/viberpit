"""Tests for extracting user prompts from a Claude transcript JSONL.

These feed the AI title generator (highest-signal, lowest-token input).
"""
import json
from pathlib import Path

from server.sessions.titles import read_user_prompts_from_file


def _write(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(x) for x in lines))


def test_collects_string_content_user_prompts(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"type": "user", "message": {"content": "fix the login bug"}},
        {"type": "user", "message": {"content": "now add tests"}},
    ])
    assert read_user_prompts_from_file(p) == ["fix the login bug", "now add tests"]


def test_collects_block_content_user_prompts(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hello there"}]}},
    ])
    assert read_user_prompts_from_file(p) == ["hello there"]


def test_skips_tool_result_user_entries(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"type": "user", "message": {"content": "real prompt"}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "abc", "content": "output"}]}},
    ])
    assert read_user_prompts_from_file(p) == ["real prompt"]


def test_skips_assistant_messages(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [
        {"type": "user", "message": {"content": "the question"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer"}]}},
    ])
    assert read_user_prompts_from_file(p) == ["the question"]


def test_missing_file_returns_empty(tmp_path):
    assert read_user_prompts_from_file(tmp_path / "nope.jsonl") == []


def test_empty_path_returns_empty(tmp_path):
    assert read_user_prompts_from_file("") == []


def test_malformed_lines_do_not_raise(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{not json\n{"type": "user", "message": {"content": "ok"}}\n')
    assert read_user_prompts_from_file(p) == ["ok"]
