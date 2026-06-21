import json
from pathlib import Path

from server.sessions.titles import read_ai_title


def _write(root: Path, sid: str, lines: list[dict], sub: str = "encoded-cwd"):
    d = root / "projects" / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))


def test_reads_ai_title(tmp_path):
    _write(tmp_path, "sid1", [
        {"type": "user", "message": {"content": "hi"}},
        {"ai-title": "Fix the login bug"},
    ])
    assert read_ai_title(tmp_path, "sid1") == "Fix the login bug"


def test_custom_title_wins_over_ai_title(tmp_path):
    _write(tmp_path, "sid2", [
        {"ai-title": "auto title"},
        {"customTitle": "My Renamed Session"},
    ])
    assert read_ai_title(tmp_path, "sid2") == "My Renamed Session"


def test_summary_key_supported(tmp_path):
    _write(tmp_path, "sid4", [{"type": "summary", "summary": "Refactor parser"}])
    assert read_ai_title(tmp_path, "sid4") == "Refactor parser"


def test_missing_file_returns_none(tmp_path):
    assert read_ai_title(tmp_path, "nope") is None


def test_empty_session_id_returns_none(tmp_path):
    assert read_ai_title(tmp_path, "") is None


def test_malformed_lines_do_not_raise(tmp_path):
    d = tmp_path / "projects" / "x"
    d.mkdir(parents=True)
    (d / "sid3.jsonl").write_text('{not json\n{"ai-title": "ok"}\n')
    assert read_ai_title(tmp_path, "sid3") == "ok"
