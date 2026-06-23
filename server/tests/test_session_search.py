from server.sessions.search import transcript_text, parse_terms, session_matches


def _write(tmp_path, *lines: str) -> str:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_transcript_text_extracts_user_assistant_thinking(tmp_path):
    path = _write(
        tmp_path,
        '{"type":"user","message":{"role":"user","content":"fix the auth bug"}}',
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"thinking","thinking":"considering tokens"},'
        '{"type":"text","text":"here is the patch"}]}}',
    )
    text = transcript_text(path)
    assert "fix the auth bug" in text
    assert "considering tokens" in text
    assert "here is the patch" in text


def test_transcript_text_excludes_tooljson_images_and_sidechains(tmp_path):
    path = _write(
        tmp_path,
        '{"type":"user","isSidechain":true,"message":{"role":"user","content":"SECRET sidechain"}}',
        '{"type":"assistant","message":{"role":"assistant","content":['
        '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"NEEDLE_CMD"}}]}}',
        '{"type":"user","message":{"role":"user","content":['
        '{"type":"tool_result","tool_use_id":"t1","content":"NEEDLE_RESULT"},'
        '{"type":"image","source":{"type":"base64","media_type":"image/png","data":"NEEDLE_B64"}}]}}',
    )
    text = transcript_text(path)
    assert "NEEDLE_CMD" not in text
    assert "NEEDLE_RESULT" not in text
    assert "NEEDLE_B64" not in text
    assert "sidechain" not in text


def test_transcript_text_missing_file_returns_empty():
    assert transcript_text("/no/such/file.jsonl") == ""


def test_transcript_text_skips_malformed_lines(tmp_path):
    path = _write(
        tmp_path,
        '{"type":"user","message":{"role":"user","content":"good line"}}',
        "this is not json",
    )
    assert "good line" in transcript_text(path)


def test_parse_terms_lowercases_and_splits():
    assert parse_terms("  Auth   BUG ") == ["auth", "bug"]
    assert parse_terms("") == []


def test_session_matches_requires_all_terms_case_insensitive():
    assert session_matches(["auth", "bug"], "My Session", "fixing the Auth Bug now") is True
    assert session_matches(["auth", "bug"], "Auth work", "no match here") is False  # 'bug' missing
    assert session_matches(["auth"], "AUTH session", "") is True                    # name-only match
    assert session_matches([], "anything", "anything") is True                      # no terms → matches
