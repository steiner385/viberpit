"""Observe-only framework evidence: no submitted claim becomes an attestation."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from server.sdlc.framework_observe import load_policy_snapshot
from server.sdlc.gates import FrameworkPolicySnapshot as PolicySnapshot
from server.sdlc.gates import observe_framework_evidence
from server.sdlc.explain import explain, guidance_to_wire
from server.tests.test_sdlc_explain import _ctx, _view, _art


def snapshot(**kw):
    values = dict(repo="o/r", status="loaded", policy_revision="a" * 40,
                  contract_oid="c" * 40, lock_oid="d" * 40,
                  frameworks=({"id": "impeccable", "state": "candidate",
                               "concerns": ["ui-design"]},),
                  head_sha="b" * 40, author="alice")
    values.update(kw)
    return PolicySnapshot(**values)


def claim(**kw):
    values = dict(framework="impeccable", repo="o/r", headSha="b" * 40,
                  policyRevision="a" * 40, producer="trusted-runner", reviewer="bob")
    values.update(kw)
    return values


def test_missing_evidence_and_scope_are_not_a_pass():
    result = observe_framework_evidence(snapshot())
    row = result["frameworks"][0]
    assert row["applicability"] == "unknown"
    assert row["evidenceStatus"] == "missing"
    assert row["state"] == "candidate"
    assert result["blocking"] is False


@pytest.mark.parametrize("changes,reason", [
    ({"repo": "other/repo"}, "wrong_repository"),
    ({"headSha": "e" * 40}, "stale_head"),
    ({"policyRevision": "e" * 40}, "stale_policy"),
    ({"producer": "forged-admin"}, "unauthenticated_producer"),
    ({"reviewer": "alice"}, "self_review"),
])
def test_claim_diagnostics_never_authenticate(changes, reason):
    row = observe_framework_evidence(snapshot(), [claim(**changes)])["claims"][0]
    assert row["trusted"] is False
    assert reason in row["reasons"]


def test_matching_claim_is_still_untrusted_and_revocation_visible():
    s = snapshot(frameworks=({"id": "impeccable", "state": "revoked", "concerns": []},))
    result = observe_framework_evidence(s, [claim()])
    assert result["frameworks"][0]["state"] == "revoked"
    assert result["frameworks"][0]["evidenceStatus"] == "untrusted"
    assert "unauthenticated_producer" in result["claims"][0]["reasons"]


def test_unknown_subject_does_not_confuse_policy_revision_with_head():
    result = observe_framework_evidence(snapshot(head_sha=None, author=None), [claim()])
    assert "subject_revision_unknown" in result["claims"][0]["reasons"]
    assert "stale_head" not in result["claims"][0]["reasons"]


@pytest.mark.parametrize("tolerance", ["cautious", "balanced", "bold"])
def test_observations_do_not_change_existing_decisions(tolerance):
    ctx = _ctx(_view(), [_art("spec", sha="b")], tolerance=tolerance)
    before = guidance_to_wire(explain(ctx))
    after = guidance_to_wire(explain(replace(ctx, framework_observations=
        observe_framework_evidence(snapshot(), [claim(reviewer="alice")]))))
    assert after.pop("frameworkEvidence")["blocking"] is False
    assert after == before


def runner_with(responses):
    calls = []
    def run(argv, token=None, **kw):
        calls.append((argv, token))
        return SimpleNamespace(returncode=0, stdout=json.dumps(responses.pop(0)))
    return run, calls


def test_default_sha_pins_both_blobs_and_never_reads_pr_policy():
    run, calls = runner_with([
        {"data": {"repository": {"defaultBranchRef": {"target": {"oid": "a" * 40}},
                                 "pullRequest": {"headRefOid": "b" * 40, "author": {"login": "alice"}}}}},
        {"data": {"repository": {"object": {"oid": "c" * 40, "text": json.dumps({"frameworks": {"lock": ".agents/frameworks.lock.json"}})}}}},
        {"data": {"repository": {"object": {"oid": "d" * 40, "text": json.dumps({"schema_version": 1, "frameworks": [{"id": "impeccable", "state": "candidate", "concerns": ["ui-design"]}]})}}}},
    ])
    result = load_policy_snapshot("o/r", pull_request=42, run=run, token="isolated")
    assert result.status == "loaded"
    assert result.head_sha == "b" * 40
    assert result.policy_revision == "a" * 40
    assert all(token == "isolated" for _, token in calls)
    assert all("a" * 40 + ":.agents/" in " ".join(argv) for argv, _ in calls[1:])
    assert all("b" * 40 not in " ".join(argv) for argv, _ in calls[1:])


@pytest.mark.parametrize("blob,status", [(None, "no_contract"),
    ({"oid": "c", "text": "{}"}, "legacy_contract"),
    ({"oid": "c", "text": '{"frameworks":{"lock":"../../secret"}}'}, "unavailable"),
    ({"oid": "c", "text": "[]"}, "unavailable")])
def test_missing_legacy_and_malformed_policy(blob, status):
    run, _ = runner_with([
        {"data": {"repository": {"defaultBranchRef": {"target": {"oid": "a" * 40}}}}},
        {"data": {"repository": {"object": blob}}},
    ])
    assert load_policy_snapshot("o/r", run=run).status == status


def test_failed_read_is_unknown_without_leaking_error():
    def run(*args, **kwargs):
        raise RuntimeError("SECRET")
    result = load_policy_snapshot("o/r", run=run)
    assert result.status == "unavailable"
    assert "SECRET" not in repr(result)


def test_opt_in_and_diagnostic_api_leave_actions_unchanged(monkeypatch, tmp_path):
    from server.tests.test_sdlc_api import _client, _seed_guidance_case, IDENTITY_HEADERS
    calls = []
    def load(repo, **kw):
        calls.append((repo, kw))
        return snapshot()
    monkeypatch.setattr("server.api.sdlc.load_policy_snapshot", load)
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    _seed_guidance_case(db_url)
    with _client(db_url) as client:
        path = "/api/sdlc/initiatives/dark-mode"
        baseline = client.get(path, headers=IDENTITY_HEADERS).json()
        assert calls == []
        observed = client.get(path + "?includeFrameworkEvidence=true", headers=IDENTITY_HEADERS).json()
        assert observed["nextAction"] == baseline["nextAction"]
        assert observed["guidance"]["frameworkEvidence"]["frameworks"][0]["evidenceStatus"] == "missing"
        result = client.post(path + "/framework-observations", headers=IDENTITY_HEADERS,
                             json={"pullRequest": 42, "claims": [claim(reviewer="alice")]}).json()
        assert result["nextAction"] == baseline["nextAction"]
        assert "self_review" in result["guidance"]["frameworkEvidence"]["claims"][0]["reasons"]
        assert calls[-1] == ("o/r", {"pull_request": 42})
        assert client.post(path + "/framework-observations", json={}).status_code == 401
        assert client.post(path + "/framework-observations", headers=IDENTITY_HEADERS,
                           json={"claims": [claim()] * 51}).status_code == 422


@pytest.mark.parametrize("response", [
    {"errors": [{"message": "partial"}], "data": {"repository": {"object": None}}},
    {"data": {"repository": {"object": {"oid": "c", "text": "{}", "isTruncated": True}}}},
    {"data": {"repository": {"object": {"oid": "c", "text": "{}", "byteSize": 1000001}}}},
])
def test_partial_or_truncated_policy_never_becomes_legacy(response):
    run, _ = runner_with([
        {"data": {"repository": {"defaultBranchRef": {"target": {"oid": "a" * 40}}}}},
        response,
    ])
    assert load_policy_snapshot("o/r", run=run).status == "unavailable"


def test_selected_missing_lock_is_not_legacy():
    run, _ = runner_with([
        {"data": {"repository": {"defaultBranchRef": {"target": {"oid": "a" * 40}}}}},
        {"data": {"repository": {"object": {"oid": "c", "text": '{"frameworks":{"lock":".agents/absent.json"}}'}}}},
        {"data": {"repository": {"object": None}}},
    ])
    assert load_policy_snapshot("o/r", run=run).status == "unavailable"


@pytest.mark.parametrize("status", ["unavailable", "no_contract", "legacy_contract"])
def test_absent_policy_is_visible_not_satisfied(status):
    result = observe_framework_evidence(snapshot(status=status, frameworks=()))
    assert result["policyStatus"] == status
    assert result["frameworks"] == []
    assert result["blocking"] is False
