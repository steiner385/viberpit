# server/tests/test_github_deployments.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from server.github import deployments as mod


def _node(env="kindash / admin", sha="a" * 40, state="FAILURE", created="2026-09-12T21:12:22Z",
          status_at="2026-09-12T21:15:00Z", log_url="https://railway.app/x", creator="railway-app"):
    return {
        "databaseId": 1, "createdAt": created, "latestEnvironment": env, "commitOid": sha,
        "ref": {"name": "main"}, "creator": {"login": creator},
        "latestStatus": {"state": state, "createdAt": status_at, "logUrl": log_url,
                         "environmentUrl": "https://admin.example"},
        "commit": {"committedDate": created},
    }


def test_query_aliases_one_block_per_repo_and_json_encodes_names():
    q = mod.build_deployments_query(["cairnea/KinDash", 'o/we"ird'])
    assert q.startswith("query {")
    assert 'r0: repository(owner: "cairnea", name: "KinDash")' in q
    assert 'r1: repository(owner: "o", name: "we\\"ird")' in q
    assert "first: 50" in q
    assert "latestStatus { state createdAt logUrl environmentUrl }" in q


def test_query_empty_repos_is_empty_string():
    assert mod.build_deployments_query([]) == ""


def test_parse_iso_handles_z_and_none():
    dt = mod.parse_iso("2026-09-12T21:12:22Z")
    assert dt == datetime(2026, 9, 12, 21, 12, 22, tzinfo=timezone.utc)
    assert mod.parse_iso(None) is None
    assert mod.parse_iso("garbage") is None


def test_parse_response_keeps_order_drops_malformed_and_nulls_forbidden_repo():
    data = {"data": {
        "r0": {"deployments": {"nodes": [
            _node(sha="1" * 40), {"latestEnvironment": None, "commitOid": "x"},
            _node(env="kindash / qa", sha="2" * 40, state="success"),
            {"latestEnvironment": "kindash / qa", "commitOid": "3" * 40, "latestStatus": None},
        ]}},
        "r1": None,
    }}
    out = mod.parse_deployments_response(data, ["cairnea/KinDash", "o/forbidden"])
    deps = out["cairnea/KinDash"]
    assert [d.sha[0] for d in deps] == ["1", "2", "3"]
    assert deps[0].status_state == "FAILURE" and deps[0].creator == "railway-app"
    assert deps[0].ref == "main" and deps[0].log_url == "https://railway.app/x"
    assert deps[0].env_url == "https://admin.example"
    assert deps[1].status_state == "SUCCESS"          # upper-cased
    assert deps[2].status_state == "" and deps[2].status_at is None  # null latestStatus
    assert out["o/forbidden"] is None


def test_parse_response_missing_alias_is_none():
    assert mod.parse_deployments_response({"data": {}}, ["a/b"]) == {"a/b": None}
    assert mod.parse_deployments_response({}, ["a/b"]) == {"a/b": None}


def test_by_env_groups_preserving_newest_first():
    deps = mod.parse_deployments_response({"data": {"r0": {"deployments": {"nodes": [
        _node(env="qa", sha="1" * 40), _node(env="admin", sha="2" * 40),
        _node(env="qa", sha="3" * 40)]}}}}, ["a/b"])["a/b"]
    grouped = mod.by_env(deps)
    assert [d.sha[0] for d in grouped["qa"]] == ["1", "3"]
    assert [d.sha[0] for d in grouped["admin"]] == ["2"]


def test_fetch_groups_by_token_and_maps_failures_to_none(monkeypatch):
    from viberpit import gitrepos
    monkeypatch.setattr(gitrepos, "app_token_for_repo",
                        lambda repo: "tok-c" if repo.startswith("cairnea/") else None)
    calls: list[tuple[list[str], str | None]] = []

    def run(args, token=None):
        calls.append((args, token))
        if token == "tok-c":
            body = {"data": {"r0": {"deployments": {"nodes": [_node()]}}}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    out = mod.fetch_deployments(["cairnea/KinDash", "steiner385/viberpit"], run=run)
    assert len(calls) == 2
    assert {t for _, t in calls} == {"tok-c", None}
    assert out["steiner385/viberpit"] is None
    assert out["cairnea/KinDash"][0].env == "kindash / admin"


def test_fetch_never_raises_on_bad_json_or_exception():
    def bad_json(args, token=None):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    def explode(args, token=None):
        raise RuntimeError("gh missing")

    assert mod.fetch_deployments(["a/b"], run=bad_json) == {"a/b": None}
    assert mod.fetch_deployments(["a/b"], run=explode) == {"a/b": None}
    assert mod.fetch_deployments([], run=explode) == {}


def _failing(stderr):
    return lambda args, token=None: SimpleNamespace(returncode=1, stdout="", stderr=stderr)


def test_fetch_edge_5xx_logs_below_warning(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="server.github.deployments"):
        out = mod.fetch_deployments(["a/b", "a/c"], run=_failing("gh: HTTP 504"))
    assert out == {"a/b": None, "a/c": None}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_fetch_non_transient_failure_still_warns(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="server.github.deployments"):
        out = mod.fetch_deployments(["a/b"], run=_failing("gh: HTTP 404"))
    assert out == {"a/b": None}
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
