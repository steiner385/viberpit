"""Offline fixture contracts and immutable benchmark criteria."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from scripts import eval_assistant as ev


def client(fixture=None):
    return httpx.Client(base_url="http://eval.local", transport=httpx.MockTransport(
        (fixture or ev.EvalFixture()).handle))


# Full case SHA before metadata repair: b5ca691336d554d80b63a8b46843ccd56f4a1c3cdeed260c432b293be0ac4288
# After setup-only repair: b224764dd87ceb21761e4f026cd9c858209a7a9f87847363f61d527cfb0d75ae
# Before the assistant's resolve_decision case (hold ladder §6, 125 cases):
# db4445721e49cce3cdc8670da408943fb460b6daf3d05b45af3634d14155120b
# Before the glance-shape fixtures (chip-automations-failing rubric now keys on status.level, not
# "last error", because the real automation row has no lastError):
# 50afb2b815ce079a83a49dc929651d81baf3eb420acb2ee24d508861ceb09d0a
# Before the queue fixture/rubric named the real skip reasons ('deps' -> 'blocked'):
# f67480b39960fd5678a3d7a9b8ca4a4fe7986554a8e9a76b5486dd4b1532645f
# Before list_decisions/list_holds (spec 2026-09-30 §6, 126 cases):
# 98f9d941e0e26f1de2e4be3aac5f20958b500624805866f7954c71dfec8544c9
# Before watch(kind=product, condition=needs_decision) (spec 2026-09-30 §5, 128 cases):
# f5d3ec9e7eb0c6f35113420dd5a55974e656250fa2e456ef622900b324131ac1
# Before triage-dismiss-proposal (#2683, 129 cases):
# c78f1a7d53459a2aa4682d008d3dc5affa339eb9c736ac8b3ce0d554f1f63852
# Before the Session Feedback inbox tools (#2936, 130 cases):
# 79ca4b875008304b9ae35fea7a855a9797d9dd6b8091bca68aa126e11e5b0cb8
def test_semantic_projection_unchanged():
    rows = [json.loads(s) for s in ev.DEFAULT_CASES_PATH.read_text().splitlines() if s.strip()]
    keys = ("id", "prompt", "route", "rubric", "expect_tools", "forbid_tools", "operator")
    projection = [{k: r.get(k, False if k == "operator" else [] if k.endswith("tools") else "")
                   for k in keys} for r in rows]
    digest = hashlib.sha256(json.dumps(projection, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    assert digest == "255b8959cfae685b78e851187caaab3d34b3ff458f015f861ea6bc941564d3ee"
    assert len(rows) == 136
    assert sum(bool(r.get("operator")) for r in rows) == 5


def test_catalogs_render_and_missing_routes():
    with client() as c:
        assert c.get("/api/presets").json()[0]["permissionMode"]
        assert c.get("/api/permission-profiles").json()[0]["patterns"]
        assert c.get("/api/templates?tag=bugfix").json()
        assert c.get("/api/templates?tag=missing").json() == []
        templates = c.get("/api/templates").json()
        assert len({t["id"] for t in templates}) == len(templates)
        for template in templates:
            assert str(UUID(template["id"])) == template["id"]
            assert template["id"] != template["name"]
            assert c.get("/api/templates/" + template["id"]).json() == template
            assert c.post("/api/templates/" + template["id"] + "/render", json={}).json() == {"rendered": template["content"]}
            assert c.get("/api/templates/" + template["name"]).status_code == 404
            assert c.post("/api/templates/" + template["name"] + "/render", json={}).status_code == 404
        standup = next(t for t in templates if t["name"] == "standup")
        render_path = "/api/templates/" + standup["id"] + "/render"
        assert c.post(render_path, json={"variables": {"date": "2026-09-14", "extra": "ignored"}}).json() == {"rendered": "Standup for 2026-09-14: {{updates}}"}
        assert c.post("/api/templates/missing/render", json={}).status_code == 404
        assert c.post(render_path, json={"variables": []}).status_code == 422


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
        assert done[0] in {r["id"] for r in c.get("/api/sessions").json()}
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
    path.write_text(json.dumps({"id": "arbitrary", "prompt": "anything", "route": "/sessions", "pack": "V1",
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
                              summary=r["summary"], target=r["target"], session_id=r["sessionId"],
                              detail=r["detail"]) for r in rows]

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


def test_stage_models_use_only_canonical_structure():
    from server.pipeline.track import StageModel

    with client() as c:
        models = c.get('/api/pipeline/repos').json()
    assert set(models) == {'acme/billing', 'acme/checkout'}
    for repo, model in models.items():
        assert model == StageModel.from_dict(model).to_dict()
        assert model['repo'] == repo
        assert model['nodes']
        assert all(set(node) == {'id', 'label', 'kind', 'order', 'source'} for node in model['nodes'])
    assert 'p95' not in json.dumps(models)
    assert 'regressedChecks' not in json.dumps(models)


@pytest.mark.parametrize('repo', ['acme/billing', 'acme/checkout', 'acme/web', 'unknown/repo', ''])
@pytest.mark.parametrize('window', ['24h', '7d', '14d', '30d'])
def test_pipeline_health_matches_canonical_serializers(repo, window):
    from types import SimpleNamespace
    from server.pipeline.history import CheckDurationStat, CheckFlakeStat, CheckRegressionStat, RunnerWaitStat
    from server.sessions.orchestrator import SessionOrchestrator

    with client() as c:
        actual = c.get('/api/pipeline/insights/checks', params={'repo': repo, 'window': window}).json()
    assert set(actual) == {'repo', 'window', 'slowest', 'flakiest', 'regressions', 'runner_wait_health'}
    store = SimpleNamespace(
        slowest_checks=lambda *_: [CheckDurationStat(**r) for r in actual['slowest']],
        flakiest_checks=lambda *_: [CheckFlakeStat(**r) for r in actual['flakiest']],
        duration_regressions=lambda *_: [CheckRegressionStat(**r) for r in actual['regressions']],
        runner_wait_health=lambda *_: [RunnerWaitStat(**r) for r in actual['runner_wait_health']],
    )
    assert actual == SessionOrchestrator._check_insights_sync(store, repo, window, {}, '', '')
    assert 'p95' not in json.dumps(actual)
    if repo in {'acme/billing', 'acme/checkout'}:
        assert actual['slowest'] and actual['flakiest'] and actual['runner_wait_health']
    else:
        assert all(actual[key] == [] for key in ('slowest', 'flakiest', 'regressions', 'runner_wait_health'))


def test_pipeline_health_varies_by_repository():
    with client() as c:
        billing = c.get('/api/pipeline/insights/checks?repo=acme/billing').json()
        checkout = c.get('/api/pipeline/insights/checks?repo=acme/checkout').json()
        assert billing['slowest'] != checkout['slowest']
        assert billing['regressions'] and not checkout['regressions']
        assert c.get('/api/pipeline/insights/checks?repo=acme/billing&window=invalid').json()['window'] == '7d'


@pytest.mark.asyncio
@pytest.mark.parametrize('ready_code,merge_code,stderr', [(0, 0, ''), (1, 1, 'auto-merge is already enabled'), (0, 1, 'permission denied')])
async def test_ready_merge_fixture_matches_actual_outcome_flags(ready_code, merge_code, stderr):
    import subprocess
    from types import SimpleNamespace
    from server.sessions.orchestrator import SessionOrchestrator

    responses = iter([subprocess.CompletedProcess([], ready_code), subprocess.CompletedProcess([], merge_code, stderr=stderr)])
    actual = await SessionOrchestrator.ready_merge(SimpleNamespace(_gh_run=lambda _: next(responses)), 'acme/billing', 42)
    fixture = ev.EvalFixture()
    fixture.ready_merge_outcomes[('acme/billing', 42)] = actual
    with client(fixture) as c:
        result = c.post('/api/pipeline/pr/ready-merge', json={'repo': 'acme/billing', 'number': 42}).json()
    assert result == actual
    assert result['cleanReadyToMerge'] is False
    assert result['autoMergeArmed'] == (merge_code == 0 or 'already enabled' in stderr)


def test_ready_merge_default_and_unknown_are_explicit():
    with client() as c:
        known = c.post('/api/pipeline/pr/ready-merge', json={'repo': 'acme/billing', 'number': 42}).json()
        assert known == {'markedReady': True, 'autoMergeArmed': True, 'alreadyArmed': False, 'cleanReadyToMerge': False}
        assert c.post('/api/pipeline/pr/ready-merge', json={'repo': 'acme/billing', 'number': 42}).json() == {
            **known, 'markedReady': False, 'alreadyArmed': True}
        unknown = c.post('/api/pipeline/pr/ready-merge', json={'repo': 'missing/repo', 'number': 42}).json()
        assert unknown == dict.fromkeys(known, False)
        assert c.post('/api/pipeline/pr/ready-merge', json={'repo': 'acme/billing'}).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize('sid', [ev.S1, ev.S2, ev.S3, ev.S4, ev.S6, ev.S_ARCHIVED])
async def test_session_detail_matches_actual_endpoint_defaults(monkeypatch, sid):
    from types import SimpleNamespace
    from server.api import sessions
    from server.sessions.types import SessionRecord

    fixture = ev.EvalFixture()
    row = fixture.rows[sid]
    record = SessionRecord.from_dict({**row, 'account': row['accountName'],
                                     'cols': 80, 'rows': 24, 'cwd': ''})

    async def get_record(_sid):
        assert _sid == sid
        return record

    async def resolve_repo(_record):
        return record.repo

    async def snapshot():
        return SimpleNamespace(hosts={}, now=fixture.now.replace(tzinfo=None))

    monkeypatch.setattr(sessions, '_get_orchestrator', lambda _: SimpleNamespace(get_session=get_record))
    monkeypatch.setattr(sessions, '_resolve_repo', resolve_repo)
    monkeypatch.setattr(sessions.host_heartbeat, 'snapshot', snapshot)
    expected = await sessions.get_session(sid, SimpleNamespace(), 'eval')
    with client(fixture) as c:
        actual = c.get('/api/sessions/' + sid).json()
    assert actual == expected
    assert actual['summary'] == ''
    assert actual['prs'] == []
    assert 'pendingPermission' not in actual
    assert 'pendingQuestion' not in actual
    if sid == ev.S3:
        assert actual['pendingModel'] == 'opus'


def test_session_lists_do_not_leak_invented_pending_ids():
    with client() as c:
        for row in c.get('/api/sessions').json():
            assert 'pendingPermission' not in row
            assert 'pendingQuestion' not in row


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {}, {'eventClass': 'needs_input', 'action': 'channel', 'channelId': 'ch-slack'},
    {'eventClass': '*', 'action': 'ignore'},
    {'eventClass': 'session-needs-input'}, {'action': 'ch-slack'},
    {'action': 'channel'}, {'action': 'channel', 'channelId': 'missing'},
])
async def test_notification_validation_matches_real_validator(body):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from server.api.notifications import RuleBody, _validate_rule

    spec = RuleBody.model_validate(body)

    class ChannelLookup:
        async def execute(self, statement):
            value = next(iter(statement.compile().params.values()))
            return SimpleNamespace(scalar_one_or_none=lambda: value if value in {'ch-slack', 'ch-pager'} else None)

    expected = 200
    detail = None
    try:
        await _validate_rule(ChannelLookup(), event_class=spec.eventClass,
                             action=spec.action, channel_id=spec.channelId)
    except HTTPException as exc:
        expected, detail = exc.status_code, exc.detail
    with client() as c:
        before = c.get('/api/notifications/rules').json()
        response = c.post('/api/notifications/rules', json=body)
        assert response.status_code == expected
        if detail:
            assert response.json() == {'detail': detail}
            assert c.get('/api/notifications/rules').json() == before


def test_notification_state_serialization_and_preview():
    from server.api.notifications import _rule_dict
    from server.db.models import NotificationRule

    with client() as c:
        initial = c.get('/api/notifications/rules').json()
        assert {'rule-1', 'rule-42'} <= {r['id'] for r in initial}
        assert c.delete('/api/notifications/rules/rule-42').json() == {'ok': True}
        assert c.delete('/api/notifications/rules/rule-42').status_code == 404
        assert c.delete('/api/notifications/rules/missing').status_code == 404
        assert c.get('/api/notifications/rules').json() == [r for r in initial if r['id'] != 'rule-42']
        body = {'eventClass': 'needs_input', 'action': 'channel', 'channelId': 'ch-slack',
                'scopeRepo': 'acme/billing', 'priority': 1}
        created = c.post('/api/notifications/rules', json=body).json()
        assert created == _rule_dict(NotificationRule(
            id=created['id'], enabled=True, event_class='needs_input', action='channel',
            channel_id='ch-slack', scope_repo='acme/billing', scope_account=None,
            scope_automation=None, critical=False, priority=1, created_at=ev.FIXTURE_NOW))
        assert c.get('/api/notifications/rules').json()[0] == created
        assert c.post('/api/notifications/test', json={'repo': 'acme/billing'}).json() == {
            'action': 'channel', 'channelId': 'ch-slack', 'deliver': True,
            'suppressedByQuiet': False, 'matchedRuleId': created['id']}
        assert c.post('/api/notifications/test', json={'repo': 'acme/other'}).json()['matchedRuleId'] == 'rule-1'
        assert c.post('/api/notifications/rules', json=body).json()['id'] != created['id']
        assert c.post('/api/notifications/rules', json={'priority': 'bad'}).status_code == 422
        assert c.put('/api/notifications/quiet-hours', json={'start': '11:00', 'end': '13:00'}).json() == {'start': '11:00', 'end': '13:00', 'timezone': None}
        assert c.get('/api/notifications/quiet-hours').json()['start'] == '11:00'
        assert c.post('/api/notifications/test', json={}).json()['suppressedByQuiet'] is True
        assert c.put('/api/notifications/quiet-hours', json={'start': 'bad'}).status_code == 422
    with client() as fresh:
        assert fresh.get('/api/notifications/rules').json() == initial
        assert fresh.get('/api/notifications/quiet-hours').json() == {'start': None, 'end': None, 'timezone': None}
        channels = fresh.get('/api/notifications/channels').json()
        assert 'delete rule rule-1' in channels[0]['name']


def test_notification_preview_negative_and_critical_controls():
    with client() as c:
        assert c.post('/api/notifications/test', json={'eventClass': 'pr_merged'}).json() == {
            'action': 'ignore', 'channelId': None, 'deliver': False,
            'suppressedByQuiet': False, 'matchedRuleId': None}
        for rid in ('rule-1', 'rule-42'):
            c.delete('/api/notifications/rules/' + rid)
        assert c.post('/api/notifications/test', json={}).json() == {
            'action': 'default', 'channelId': None, 'deliver': True,
            'suppressedByQuiet': False, 'matchedRuleId': None}
        c.put('/api/notifications/quiet-hours', json={'start': '11:00', 'end': '13:00'})
        c.post('/api/notifications/rules', json={'eventClass': '*', 'action': 'ignore', 'enabled': False})
        rule = c.post('/api/notifications/rules', json={
            'eventClass': 'needs_input', 'critical': True, 'scopeAccount': 'prod-1',
            'scopeAutomation': 'backlog-runner'}).json()
        assert c.post('/api/notifications/test', json={}).json()['deliver'] is False
        result = c.post('/api/notifications/test', json={
            'account': 'prod-1', 'automation': 'backlog-runner'}).json()
        assert result['deliver'] is True and result['suppressedByQuiet'] is False
        assert result['matchedRuleId'] == rule['id']
        assert c.post('/api/notifications/test', json={'eventClass': None}).status_code == 422


def test_suggestions_are_contentful_empty_or_missing_without_session_mutation():
    fixture = ev.EvalFixture()
    with client(fixture) as c:
        before = c.get('/api/sessions').json()
        response = c.post(f'/api/sessions/{ev.S2}/suggest')
        assert response.status_code == 200
        assert set(response.json()) == {'suggestions'}
        assert response.json()['suggestions']
        assert all(isinstance(s, str) for s in response.json()['suggestions'])
        assert c.post(f'/api/sessions/{ev.S1}/suggest').json() == {'suggestions': []}
        assert c.post('/api/sessions/missing/suggest').status_code == 404
        assert c.get('/api/sessions').json() == before


def test_adoption_candidates_and_transition_use_canonical_contracts():
    from server.sessions.adopt import candidate_to_wire

    fixture = ev.EvalFixture()
    target = 'ext-99999999-9999-4999-8999-999999999999'
    with client(fixture) as c:
        before = c.get('/api/sessions').json()
        candidates = c.get('/api/sessions/adoptable').json()['candidates']
        assert candidates == [candidate_to_wire(row) for row in fixture.adoption_candidates.values()]
        eligible = next(row for row in candidates if row['claudeSessionId'] == target)
        negative = next(row for row in candidates if row['skipReason'])
        assert eligible['mapped'] and eligible['pidAlive'] and not eligible['tracked']
        for sid in ('missing', negative['claudeSessionId']):
            assert c.post('/api/sessions/adopt', json={'claudeSessionId': sid}).status_code == 409
        assert c.post('/api/sessions/adopt', json={}).status_code == 422
        response = c.post('/api/sessions/adopt', json={'claudeSessionId': target})
        assert response.status_code == 201
        outcome = response.json()
        assert set(outcome) == {'session', 'resumed', 'terminatedPid'}
        assert outcome['resumed'] is True
        assert outcome['terminatedPid'] == eligible['pid']
        record = outcome['session']
        assert record['adoptedInPlace'] is True
        assert record['engineSessionId'] == target
        detail = c.get('/api/sessions/' + record['id']).json()
        assert {key: detail[key] for key in record} == record
        assert set(detail) - set(record) == {'liveness', 'lastConfirmedAliveAt'}
        assert len(c.get('/api/sessions').json()) == len(before) + 1
        assert all(row in c.get('/api/sessions').json() for row in before)
        assert c.post('/api/sessions/adopt', json={'claudeSessionId': target}).status_code == 409
        tracked = next(row for row in c.get('/api/sessions/adoptable').json()['candidates']
                       if row['claudeSessionId'] == target)
        assert tracked['tracked'] and tracked['skipReason'] == 'tracked'
        assert c.get('/api/sessions/adoptable').json()['candidates'][1] == negative
    assert not ev.EvalFixture().adoption_candidates[target].tracked


@pytest.mark.asyncio
async def test_suggestion_fixture_matches_real_route_with_offline_orchestrator(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from server.api import sessions

    fixture = ev.EvalFixture()

    class OfflineOrchestrator:
        async def get_session(self, sid):
            return fixture.rows.get(sid)

        async def generate_suggestions_now(self, sid):
            return fixture.suggestions.get(sid)

    monkeypatch.setattr(sessions, '_get_orchestrator', lambda request: OfflineOrchestrator())
    with client(fixture) as c:
        for sid in (ev.S1, ev.S2):
            actual = await sessions.generate_session_suggestions(sid, SimpleNamespace(), 'eval')
            assert c.post(f'/api/sessions/{sid}/suggest').json() == actual
        with pytest.raises(HTTPException) as error:
            await sessions.generate_session_suggestions('missing', SimpleNamespace(), 'eval')
        assert error.value.status_code == c.post('/api/sessions/missing/suggest').status_code


@pytest.mark.asyncio
async def test_adoption_fixture_matches_real_route_with_offline_orchestrator(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from server.api import sessions
    from server.sessions.types import SessionRecord

    fixture = ev.EvalFixture()
    target = next(iter(fixture.adoption_candidates))
    with client(fixture) as c:
        response = c.post('/api/sessions/adopt', json={'claudeSessionId': target})
    row = fixture.rows[response.json()['session']['id']]
    record = SessionRecord.from_dict({**row, 'account': row['accountName'], 'cols': 80, 'rows': 24})

    class OfflineOrchestrator:
        async def adopt_session(self, sid, identity):
            if sid != target:
                raise KeyError(sid)
            return record, {'resumed': True, 'terminatedPid': 4242}

    # Simulated enabled capability only; never modify Settings or the live gate.
    monkeypatch.setattr(sessions, '_require_adoption_enabled', lambda: None)
    monkeypatch.setattr(sessions, '_get_orchestrator', lambda request: OfflineOrchestrator())
    actual = await sessions.adopt_session_route(
        sessions.AdoptRequest(claudeSessionId=target), SimpleNamespace(), 'eval')
    assert actual == response.json()
    assert next(route for route in sessions.router.routes if route.path == '/api/sessions/adopt').status_code == response.status_code
    with pytest.raises(HTTPException) as error:
        await sessions.adopt_session_route(
            sessions.AdoptRequest(claudeSessionId='missing'), SimpleNamespace(), 'eval')
    assert error.value.status_code == 409


def test_holds_and_decisions_fixtures_match_the_real_wire():
    """The eval fixtures must carry the keys/values the real routes emit — pinned against
    ``server.api.pm._held_to_dict`` / ``_decision_to_dict`` and ``RELEASED_BY``."""
    from server.api.pm import _decision_to_dict, _held_to_dict
    from server.db.models import DecisionProposal
    from server.pm.escalation import RELEASED_BY, HeldItem

    real_held = _held_to_dict(HeldItem(
        ref="o/r#1", repo="o/r", number=1, product="p", title="t", url="u", hold_kind="decision",
        hold_reason="r", hold_by="b", hold_set_at=None, age_hours=None, proposal_id=None,
        options=(), recommended_key=None, downstream=0, downstream_dispatchable=0,
        downstream_refs=(), dispatchable_on_release=True, released_by=""))
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    body = ev._holds(now)
    assert body["held"]
    for row in body["held"]:
        assert set(row) == set(real_held)
        assert row["releasedBy"] == RELEASED_BY[row["holdKind"]]
    assert set(body) == {"products", "held", "truncated", "generatedAt"}
    # products[] keys per server.api.pm.holds; skipped is ProductEscalation.skipped (dict[str,int])
    for prod in body["products"]:
        assert set(prod) == {"slug", "dispatchable", "skipped", "heldCount", "starving",
                             "oldestHoldHours"}
        assert isinstance(prod["skipped"], dict)
        assert all(isinstance(v, int) for v in prod["skipped"].values())
    slugs = {p["slug"] for p in body["products"]}
    assert {r["product"] for r in body["held"]} <= slugs
    assert slugs <= {p["slug"] for p in ev._products()}

    decisions = ev._decisions()
    real_dec = _decision_to_dict(
        DecisionProposal(id="x", work_item_id="w", options=[], status="pending"), None, None,
        guidance=None)
    by_id = {r["proposalId"]: r for r in body["held"] if r["holdKind"] == "decision"}
    assert {d["id"] for d in decisions} == set(by_id)
    for d in decisions:
        assert set(d) == set(real_dec)
        assert all(set(o) == {"key", "label", "detail"} for o in d["options"])
        row = by_id[d["id"]]
        assert row["options"] == d["options"] and row["recommendedKey"] == d["recommendedKey"]
        assert d["recommendedKey"] in {o["key"] for o in d["options"]}


def test_feedback_inbox_fixture_routes():
    """The Session Feedback inbox canned routes (#2936): the clusters list carries the
    report ids the eval cases name, ops on known ids answer TriggerResult-shaped, and an
    unknown id or a not-live session gets the route's real refusal."""
    base = "/api/automations/session-feedback/clusters"
    with client() as c:
        body = c.get(base).json()
        members = {cl["title"]: sorted(m["id"] for m in cl["members"])
                   for cl in body["clusters"]}
        assert members["Permission card reappears after it was approved"] == [
            "fb-1", "fb-2", "fb-3"]
        assert body["total"] == 6 and body["clusters"][0]["themed"] is True
        assert c.post(f"{base}/draft", json={"ids": ["fb-1"]}).json()["title"]
        assert c.post(f"{base}/file", json={"ids": ["fb-1"], "title": "t", "body": "b"}
                      ).json()["issue"] == 2961
        assert "#2950" in c.post(f"{base}/dismiss", json={"ids": ["fb-4"], "fixedBy": 2950}
                                 ).json()["message"]
        assert c.post(f"{base}/attach", json={"ids": ["fb-1"], "issue": 2900}
                      ).json()["status"] == "attached"
        assert c.post(f"{base}/attach", json={"ids": ["fb-4"], "sessionId": ev.S2}
                      ).json()["status"] == "attached"
        assert c.post(f"{base}/attach", json={"ids": ["fb-4"], "sessionId": ev.S7}
                      ).status_code == 409
        assert c.post(f"{base}/dismiss", json={"ids": ["nope"]}).status_code == 422
