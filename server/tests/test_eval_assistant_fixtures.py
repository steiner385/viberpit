"""Offline fixture contracts and immutable benchmark criteria."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scripts import eval_assistant as ev


def client(fixture=None):
    return httpx.Client(base_url="http://eval.local", transport=httpx.MockTransport(
        (fixture or ev.EvalFixture()).handle))


# Full case SHA before metadata repair: b5ca691336d554d80b63a8b46843ccd56f4a1c3cdeed260c432b293be0ac4288
# After setup-only repair: b224764dd87ceb21761e4f026cd9c858209a7a9f87847363f61d527cfb0d75ae
def test_semantic_projection_unchanged():
    rows = [json.loads(s) for s in ev.DEFAULT_CASES_PATH.read_text().splitlines() if s.strip()]
    keys = ("id", "prompt", "route", "rubric", "expect_tools", "forbid_tools", "operator")
    projection = [{k: r.get(k, False if k == "operator" else [] if k.endswith("tools") else "")
                   for k in keys} for r in rows]
    digest = hashlib.sha256(json.dumps(projection, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    assert digest == "db4445721e49cce3cdc8670da408943fb460b6daf3d05b45af3634d14155120b"
    assert len(rows) == 125
    assert sum(bool(r.get("operator")) for r in rows) == 5


def test_catalogs_render_and_missing_routes():
    with client() as c:
        assert c.get("/api/presets").json()[0]["permissionMode"]
        assert c.get("/api/permission-profiles").json()[0]["patterns"]
        assert c.get("/api/templates?tag=bugfix").json()
        assert c.get("/api/templates?tag=missing").json() == []
        assert c.post("/api/templates/standup/render", json={"variables": {"date": "2026-09-14", "extra": "ignored"}}).json() == {"rendered": "Standup for 2026-09-14: {{updates}}"}
        assert c.post("/api/templates/missing/render", json={}).status_code == 404
        assert c.post("/api/templates/standup/render", json={"variables": []}).status_code == 422


def test_watch_contract_state_and_validation():
    with client() as c:
        before = c.get("/api/assistant/watches").json()
        response = c.post("/api/assistant/watches", json={"kind": "account", "target": "any", "condition": "limited"})
        assert response.status_code == 201
        watch = response.json()
        assert watch["eventClass"] == "account_limited"
        assert watch["scopeAccount"] is None
        assert set(watch) == {"id", "eventClass", "scopeRepo", "scopeAutomation", "scopeAccount", "scopeSessionId", "once", "ownerSessionId", "expiresAt", "createdAt"}
        assert len(c.get("/api/assistant/watches").json()) == len(before) + 1
        assert c.delete("/api/assistant/watches/" + watch["id"]).json() == {"ok": True}
        assert c.get("/api/assistant/watches").json() == before
        assert c.delete("/api/assistant/watches/" + watch["id"]).status_code == 404
        assert c.post("/api/assistant/watches", json={"kind": "account", "target": "any", "condition": "invented"}).status_code == 422
        assert c.post("/api/assistant/watches", json={}).status_code == 422


def test_batches_are_stateful_and_partial_failures_are_visible():
    fixture = ev.EvalFixture()
    with client(fixture) as c:
        rows = c.get("/api/sessions").json()
        done = [r["id"] for r in rows if r["status"] == "done"]
        limited = [r["id"] for r in rows if r["status"] == "limited"]
        assert len(done) >= 2 and len(limited) >= 2
        result = c.post("/api/sessions/batch/archive", json={"ids": done}).json()
        assert result == {"succeeded": done, "failed": []}
        assert set(done) <= {r["id"] for r in c.get("/api/sessions/archived").json()}
        result = c.post("/api/sessions/batch/unarchive", json={"ids": [done[0], ev.S1, "missing"]}).json()
        assert result["succeeded"] == [done[0]]
        assert len(result["failed"]) == 2
        assert c.get("/api/sessions/" + done[0]).json()["archivedAt"] is None
        assert c.post("/api/sessions/batch/rotate", json={"ids": limited}).json()["succeeded"] == limited
        assert c.post("/api/sessions/batch/unarchive", json={"ids": [done[1]]}).json() == {"succeeded": [done[1]], "failed": []}
        assert c.post("/api/sessions/batch/unarchive", json={"ids": []}).status_code == 422
    assert any(r["id"] == done[0] for r in ev.EvalFixture().live())


def test_declarative_restore_setup_and_no_leakage():
    case = next(c for c in ev.load_cases() if c.id == "unarchive-sessions-batch")
    with client(ev.EvalFixture(case.fixture_state)) as c:
        assert not {ev.S1, ev.S2} & {r["id"] for r in c.get("/api/sessions").json()}
        assert c.get("/api/sessions/" + ev.S1).json()["name"] == "Fix auth"
        assert c.post("/api/sessions/batch/unarchive", json={"ids": [ev.S1, ev.S2]}).json() == {"succeeded": [ev.S1, ev.S2], "failed": []}
        assert {ev.S1, ev.S2} <= {r["id"] for r in c.get("/api/sessions").json()}
    assert ev.EvalFixture().rows[ev.S1]["name"] == "Refactor billing auth"
    with pytest.raises(ValueError):
        ev.EvalFixture({"sessions": {ev.S1: {"instructions": "pass"}}})


@pytest.mark.parametrize("anchor", [datetime(2026, 9, 14, 12, tzinfo=timezone.utc), datetime(2026, 9, 21, 0, tzinfo=timezone.utc)])
def test_clock_dates_and_schedule_boundaries(anchor):
    f = ev.EvalFixture(now=anchor)
    with client(f) as c:
        dates = [datetime.fromisoformat(r["archivedAt"]) for r in c.get("/api/sessions/archived").json()]
        monday = anchor.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=anchor.weekday())
        assert sum(monday - timedelta(days=7) <= d < monday for d in dates) >= 2
        assert any(d >= monday for d in dates)
        assert any(d < monday - timedelta(days=7) for d in dates)
        runs = c.post("/api/automations/schedule/preview", json={"schedule": {"cron": "0 8 * * *"}}).json()["nextRuns"]
        assert all(datetime.fromisoformat(d) > anchor and datetime.fromisoformat(d).hour == 8 for d in runs)
        assert c.post("/api/automations/schedule/preview", json={"schedule": {"cron": "bad"}}).status_code == 422
        assert c.post("/api/notifications/test", json={}).json()["deliver"] is True


@pytest.mark.asyncio
async def test_clock_instrumentation_changes_only_success_envelope(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from server.assistant import tools

    before = datetime.now(timezone.utc)
    envelope = json.loads(tools.wrap_untrusted({"safe": "value"}, truncated=2))
    success = {"content": [{"type": "text", "text": json.dumps(envelope)}]}
    error = {"isError": True, "content": [{"type": "text", "text": "failure"}]}

    async def ok(args):
        return deepcopy(success)

    async def fail(args):
        return deepcopy(error)

    monkeypatch.setattr(tools, "build_tools", lambda *args, **kwargs: [
        SimpleNamespace(name="ok", handler=ok), SimpleNamespace(name="fail", handler=fail)])
    first = ev.EvalFixture()
    second = ev.EvalFixture(now=first.now + timedelta(days=7))
    wrapped = ev._instrumented_tools([], fixture=first)
    alternate = ev._instrumented_tools([], fixture=second)
    result = await wrapped[0].handler({})
    changed = json.loads(result["content"][0]["text"])
    assert changed == {**envelope, "now": first.now.isoformat(timespec="seconds")}
    assert json.loads((await alternate[0].handler({}))["content"][0]["text"])["now"] == second.now.isoformat(timespec="seconds")
    assert await wrapped[1].handler({}) == error
    after = json.loads(tools.wrap_untrusted({}))
    assert datetime.fromisoformat(after["now"]) >= before.replace(microsecond=0)
    assert json.loads(success["content"][0]["text"]) == envelope


def test_singular_mutations_share_batch_state_and_search_scope():
    with client() as c:
        assert c.delete("/api/sessions/" + ev.S7).status_code == 204
        assert ev.S7 not in c.get("/api/sessions/search?q=Old&scope=live").json()["ids"]
        assert ev.S7 in c.get("/api/sessions/search?q=Old&scope=archived").json()["ids"]
        assert c.post("/api/sessions/" + ev.S7 + "/unarchive").status_code == 200
        assert c.post("/api/sessions/" + ev.S7 + "/unarchive").status_code == 404
        assert c.delete("/api/sessions/" + ev.S7 + "/permanent").status_code == 204
        assert c.get("/api/sessions/" + ev.S7).status_code == 404
        assert c.delete("/api/sessions/missing").status_code == 404


def test_case_loader_rejects_unknown_setup_before_running(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps({"id": "arbitrary", "prompt": "anything", "route": "/sessions",
                                "fixture_state": {"sessions": {"unknown": {"archived": True}}}}))
    with pytest.raises(ValueError, match="override"):
        ev.load_cases(path)


def test_model_and_threshold_defaults_remain_unchanged():
    import ast
    import inspect

    module = ast.parse(inspect.getsource(ev))
    defaults = {node.args[0].value: next((kw.value.value for kw in node.keywords if kw.arg == "default" and isinstance(kw.value, ast.Constant)), None)
                for node in ast.walk(module) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant)}
    assert defaults["--model"] == "sonnet"
    assert defaults["--threshold"] == 0.90
    judge = ast.parse(inspect.getsource(ev.judge_rubric))
    assert any(kw.arg == "model" and isinstance(kw.value, ast.Constant) and kw.value.value == "haiku"
               for node in ast.walk(judge) if isinstance(node, ast.Call) for kw in node.keywords)


def test_seed_watch_is_deletable_and_create_ids_never_reuse():
    with client() as c:
        assert c.get("/api/assistant/watches").json()[0]["id"] == "watch-1"
        assert c.delete("/api/assistant/watches/watch-1").json() == {"ok": True}
        body = {"kind": "pr", "target": "acme/billing", "condition": "breaks"}
        first = c.post("/api/assistant/watches", json=body).json()
        second = c.post("/api/assistant/watches", json=body).json()
        assert [first["id"], second["id"]] == ["watch-2", "watch-3"]
        assert len(c.get("/api/assistant/watches").json()) == 2


def test_resume_batch_has_realistic_targets_and_idempotent_results():
    f = ev.EvalFixture()
    with client(f) as c:
        eligible = [r for r in c.get("/api/sessions").json() if r.get("interruptedByRestart")]
        assert len(eligible) >= 2
        ids = [r["id"] for r in eligible]
        assert all("restart" in r["summary"] for r in eligible)
        assert all(c.get("/api/sessions/" + sid).json()["interruptedByRestart"] for sid in ids)
        result = c.post("/api/sessions/batch/resume", json={"ids": ids + [ev.S1, "missing", ev.S_ARCHIVED]}).json()
        assert result == {"succeeded": ids, "failed": [
            {"id": ev.S1, "error": "skipped:not-interrupted"},
            {"id": "missing", "error": "skipped:not-found"},
            {"id": ev.S_ARCHIVED, "error": "skipped:not-found"}]}
        assert c.post("/api/sessions/batch/resume", json={"ids": [ids[0]]}).json() == {
            "succeeded": [], "failed": [{"id": ids[0], "error": "skipped:not-interrupted"}]}
        assert not any(r.get("interruptedByRestart") for r in c.get("/api/sessions").json())
        assert all(c.get("/api/sessions/" + sid).json()["status"] == "in_progress" for sid in ids)
        assert c.post("/api/sessions/batch/resume", json={"ids": []}).status_code == 422
    assert any(r.get("interruptedByRestart") for r in ev.EvalFixture().live())


def test_resume_does_not_fabricate_success_for_ineligible_or_driver_failure():
    f = ev.EvalFixture()
    ids = [r["id"] for r in f.live() if r.get("interruptedByRestart")]
    f.rows[ids[0]]["interruptedFromStatus"] = "waiting_input"
    f.resume_driver_ids.remove(ids[1])
    with client(f) as c:
        result = c.post("/api/sessions/batch/resume", json={"ids": ids}).json()
        assert result == {"succeeded": [], "failed": [
            {"id": ids[0], "error": "skipped:not-resumable-status"},
            {"id": ids[1], "error": "no_driver"}]}
        assert all(f.rows[sid]["interruptedByRestart"] for sid in ids)


@pytest.mark.parametrize("sid", [ev.S7, ev.S_ARCHIVED])
def test_permanent_delete_accepts_live_and_archived_records(sid):
    # Production test_archive::test_permanent_delete_live_session_archives_then_deletes
    # establishes that the client need not archive a known live session first.
    with client() as c:
        assert c.delete(f"/api/sessions/{sid}/permanent").status_code == 204
        assert c.get(f"/api/sessions/{sid}").status_code == 404
        assert c.delete(f"/api/sessions/{sid}/permanent").status_code == 404
        assert c.get(f"/api/sessions/{ev.S1}").status_code == 200


def test_permanent_delete_batch_mixed_states_and_missing():
    with client() as c:
        assert c.post("/api/sessions/batch/permanent-delete", json={"ids": [ev.S7, ev.S_ARCHIVED, "missing"]}).json() == {
            "succeeded": [ev.S7, ev.S_ARCHIVED], "failed": [{"id": "missing", "error": "not found"}]}
        for route in ("/api/sessions", "/api/sessions/archived"):
            assert not {ev.S7, ev.S_ARCHIVED} & {r["id"] for r in c.get(route).json()}
        assert c.get(f"/api/sessions/{ev.S1}").status_code == 200


@pytest.mark.asyncio
async def test_automation_event_contract_matches_canonical_serializer(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from server.automations import context

    with client() as c:
        rows = c.get("/api/automations/backlog-runner/events").json()
    assert isinstance(rows, list)
    db_rows = [SimpleNamespace(ts=datetime.fromisoformat(r["ts"]), level=r["level"], kind=r["kind"],
                              summary=r["summary"], target=r["target"], session_id=r["sessionId"]) for r in rows]

    @asynccontextmanager
    async def fake_db():
        async def execute(_statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: db_rows))
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(context, "db_session", fake_db)
    assert await context.AutomationContext("backlog-runner").list_events() == rows
    assert rows[0]["summary"] == ev._INJECTION
    assert all(datetime.fromisoformat(r["ts"]) < ev.FIXTURE_NOW for r in rows)
    assert rows[0]["ts"] > rows[1]["ts"]


def test_event_filters_limits_empty_and_unknown():
    with client() as c:
        rows = c.get("/api/automations/backlog-runner/events").json()
        assert c.get("/api/automations/backlog-runner/events?limit=1").json() == rows[:1]
        assert c.get("/api/automations/backlog-runner/events?limit=0").json() == []
        assert c.get("/api/automations/backlog-runner/events?limit=bad").status_code == 422
        assert c.get("/api/automations/backlog-runner/events?kind=claim").json() == [rows[1]]
        assert c.get("/api/automations/backlog-runner/events?target=acme/billing%231201").json() == [rows[1]]
        assert c.get("/api/automations/backlog-runner/events?kind=absent").json() == []
        assert c.get("/api/automations/pr-doctor/events").json() == []
        assert c.get("/api/automations/unknown/events").status_code == 404
