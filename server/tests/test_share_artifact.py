"""`share_artifact`: the session tool that puts a link, URL, local path or image in
front of the user as an always-visible transcript card, instead of leaving it in
prose or inside a collapsed tool result.

The handler validates every kind; for `image` it ALSO acts — it ingests the file
from the session host into the content-addressed attachment store so the edge can
serve it at `/api/attachments/{id}` (the daemon owning the session has the file;
the browser does not).
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from viberpit.engines import AgentEngine

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def _ctx(session_id="s1", **kw):
    from server.sessions.tools.context import SessionToolContext
    rec = SimpleNamespace(
        id=session_id, name="Fix #12", repo="o/n", branch="x", cwd="/tmp/w",
        origin=None, origin_ref=None, engine=AgentEngine.CLAUDE_CODE, model="sonnet",
        permission_mode="acceptEdits", account="alpha", host="rocky",
        active_goal=None, inferred_goal=None, kind=None)
    kw.setdefault("signalling_tools", True)
    return SessionToolContext(session_id=session_id, identity="u@x", kind=None,
                              engine="claude_code", record=lambda: rec, host="rocky", **kw)


def _tool():
    from server.sessions.tools.tools import build_session_tools
    return next(t for t in build_session_tools(_ctx()) if t.name == "share_artifact")


def _data(out):
    assert "is_error" not in out, out
    return json.loads(out["content"][0]["text"])["data"]


# ── registry ─────────────────────────────────────────────────────────────────


def test_share_artifact_is_a_signal_tool_that_keeps_its_card():
    """SIGNAL: it moves something the user sees (a card) and must never raise a
    permission card an unattended session has nobody to answer. NOT a turn signal:
    the card IS the feature, so neither transcript builder may suppress it."""
    from server.mcp_spec import ToolClass
    from server.sessions.tools.registry import (SESSION_TOOL_SPECS, TURN_SIGNAL_NAMES,
                                                mounted_names)
    assert SESSION_TOOL_SPECS["share_artifact"].tool_class is ToolClass.SIGNAL
    assert "share_artifact" not in TURN_SIGNAL_NAMES
    assert "share_artifact" in mounted_names(signalling_tools=True, self_model_switch=False)
    assert "share_artifact" not in mounted_names(signalling_tools=False, self_model_switch=False)


def test_share_artifact_is_named_in_the_system_prompt():
    from server.sessions.tools.instruction import session_instructions
    text = session_instructions(session_tools=True, signalling_tools=True,
                                self_model_switch=False)
    assert "`share_artifact`" in text


# ── url / path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_url_artifact_is_acknowledged_with_its_fields(isolated_db):
    out = await _tool().handler({"kind": "url", "value": "https://github.com/o/n/pull/7",
                                 "label": "PR #7", "note": "auto-merge armed"})
    assert _data(out) == {"kind": "url", "value": "https://github.com/o/n/pull/7",
                          "label": "PR #7", "note": "auto-merge armed"}


@pytest.mark.asyncio
async def test_a_shared_github_pr_url_is_reported_to_the_completion_bar(isolated_db):
    from server.sessions.tools.tools import build_session_tools
    seen = []

    async def note(repo, number):
        seen.append((repo, number))
    tool = next(t for t in build_session_tools(_ctx(note_shared_pr=note))
                if t.name == "share_artifact")
    await tool.handler({"kind": "url", "value": "https://github.com/o/other/pull/2662/files",
                        "label": "PR"})
    await tool.handler({"kind": "url", "value": "https://github.com/o/other/issues/5",
                        "label": "issue"})
    assert seen == [("o/other", 2662)]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x/y", "github.com/o/n", ""])
async def test_url_artifact_rejects_non_http_schemes(isolated_db, bad):
    out = await _tool().handler({"kind": "url", "value": bad, "label": "x"})
    assert out.get("is_error") is True
    assert "http" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_path_artifact_must_be_absolute(isolated_db):
    ok = await _tool().handler({"kind": "path", "value": "/tmp/w/report.md", "label": "Report"})
    assert _data(ok)["value"] == "/tmp/w/report.md"
    bad = await _tool().handler({"kind": "path", "value": "report.md", "label": "Report"})
    assert bad.get("is_error") is True and "absolute" in bad["content"][0]["text"]


@pytest.mark.asyncio
async def test_label_is_required_and_kind_is_closed(isolated_db):
    out = await _tool().handler({"kind": "url", "value": "https://x.y"})
    assert out.get("is_error") is True and "label" in out["content"][0]["text"]
    out = await _tool().handler({"kind": "video", "value": "https://x.y", "label": "v"})
    assert out.get("is_error") is True and "kind" in out["content"][0]["text"]


# ── image ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_image_artifact_is_ingested_into_the_attachment_store(isolated_db, tmp_path,
                                                                    monkeypatch):
    """The daemon has the file; the browser does not. The handler stores the bytes
    content-addressed under the (host-shared) attachments dir, binds an Attachment
    row to the session, and hands back the edge URL the card renders."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Attachment, AuditEvent
    from server.sessions.attachments import storage_path
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    out = await _tool().handler({"kind": "image", "value": str(shot), "label": "Settings page"})
    data = _data(out)
    assert data["kind"] == "image" and data["label"] == "Settings page"
    assert data["value"] == str(shot)
    async with db_session() as db:
        row = (await db.execute(select(Attachment))).scalar_one()
        assert data["url"] == f"/api/attachments/{row.id}"
        assert row.session_id == "s1" and row.media_type == "image/png"
        assert row.filename == "shot.png" and row.uploaded_by == "u@x"
        assert storage_path(row.sha256, row.media_type).read_bytes() == PNG
        types = {a.type for a in (await db.execute(select(AuditEvent))).scalars()}
        assert "session.artifact" in types


@pytest.mark.asyncio
async def test_image_artifact_reports_a_missing_file_to_the_model(isolated_db, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    out = await _tool().handler({"kind": "image", "value": str(tmp_path / "nope.png"),
                                 "label": "x"})
    assert out.get("is_error") is True and "no such file" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_image_artifact_rejects_bytes_that_are_not_an_image(isolated_db, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    f = tmp_path / "notes.png"
    f.write_text("just text, wrong extension")
    out = await _tool().handler({"kind": "image", "value": str(f), "label": "x"})
    assert out.get("is_error") is True and "not an image" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_image_artifact_rejects_an_oversize_file(isolated_db, tmp_path, monkeypatch):
    from server.sessions.attachments import MAX_BYTES
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))
    f = tmp_path / "big.png"
    f.write_bytes(PNG + b"\x00" * MAX_BYTES)
    out = await _tool().handler({"kind": "image", "value": str(f), "label": "x"})
    assert out.get("is_error") is True and "bytes" in out["content"][0]["text"]


def test_eval_case_covers_share_artifact():
    from pathlib import Path
    covered: set[str] = set()
    for line in Path("docs/superpowers/evals/session-tools/cases.jsonl").read_text().splitlines():
        if line.strip():
            covered.update(json.loads(line)["expect_tools"])
    assert "share_artifact" in covered
