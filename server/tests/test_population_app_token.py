from server.github import pr_population as P


def test_default_query_passes_owner_token(monkeypatch):
    calls = {}
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: f"tok-{owner}")
    def fake_run(args, token=None):
        calls["token"] = token
        class R: returncode = 0; stdout = '{"data":{}}'; stderr = ""
        return R()
    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    P._default_query("cairnea/KinDash", retention_days=7)
    assert calls["token"] == "tok-cairnea"


def test_eta_query_grouped_by_owner_merges_with_global_aliases(monkeypatch):
    # two owners → two gh calls; aliases remapped to the original key order
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: f"tok-{owner}")
    seen = []
    def fake_run(args, token=None):
        seen.append(token)
        # Return distinct nodes per owner to catch cross-owner alias swaps
        number = 1 if token == "tok-cairnea" else 2
        out = '{"data":{"r0":{"pullRequest":{"number":%d}}}}' % number
        class R: returncode = 0; stdout = out; stderr = ""
        return R()
    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    keys = [("cairnea/KinDash", 1), ("steiner385/viberpit", 2)]
    resp = P._default_eta_query(keys)
    # one call per owner, each with that owner's token
    assert set(seen) == {"tok-cairnea", "tok-steiner385"}
    # subset alias r0 remapped: cairnea key idx 0 → r0, steiner key idx 1 → r1
    assert resp["data"]["r0"]["pullRequest"]["number"] == 1   # cairnea, global idx 0
    assert resp["data"]["r1"]["pullRequest"]["number"] == 2   # steiner, global idx 1


import json


def _dep(env, *, status="SUCCESS", date="2026-07-18T20:00:00Z"):
    return {"latestEnvironment": env, "commitOid": "c",
            "latestStatus": {"state": status}, "commit": {"committedDate": date}}


def _population_response(deploy_nodes, env_names):
    return {"data": {"repo": {
        "deployments": {"nodes": deploy_nodes},
        "environments": {"nodes": [{"name": n} for n in env_names]},
        "pullRequests": {"nodes": []},
    }, "merged": {"nodes": []}}}


def test_default_query_supplements_evicted_envs(monkeypatch):
    # Main window holds only qa churn; production is listed but has no SUCCESS
    # in-window → a second, environment-filtered query restores it.
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: "tok")
    main = _population_response([_dep("kindash / qa")],
                                ["kindash / qa", "kindash / production"])
    supp = {"data": {"r0": {"e0": {"nodes": [_dep("kindash / production")]}}}}
    calls = []

    def fake_run(args, token=None, input=None):
        calls.append(args)
        out = json.dumps(main if len(calls) == 1 else supp)
        class R: returncode = 0; stdout = out; stderr = ""
        return R()

    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    resp = P._default_query("cairnea/KinDash", retention_days=7)
    # open query, env-supplement re-fetch, merged-PR search: 3 separate gh calls.
    assert len(calls) == 3
    envs = [d["latestEnvironment"]
            for d in resp["data"]["repo"]["deployments"]["nodes"]]
    assert "kindash / production" in envs


def test_default_query_no_supplement_when_all_envs_active(monkeypatch):
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: "tok")
    main = _population_response(
        [_dep("qa"), _dep("production")], ["qa", "production"])
    calls = []

    def fake_run(args, token=None, input=None):
        calls.append(args)
        class R: returncode = 0; stdout = json.dumps(main); stderr = ""
        return R()

    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    P._default_query("cairnea/KinDash", retention_days=7)
    # open query + merged-PR search; no env-supplement re-fetch needed.
    assert len(calls) == 2


def test_default_query_supplement_failure_keeps_main_response(monkeypatch):
    # The supplement is best-effort: a failed second call must not lose the
    # main response (behavior degrades to the pre-fix window, not to an error).
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: "tok")
    main = _population_response([_dep("kindash / qa")],
                                ["kindash / qa", "kindash / production"])
    calls = []

    def fake_run(args, token=None, input=None):
        calls.append(args)
        class R:
            returncode = 0 if len(calls) == 1 else 1
            stdout = json.dumps(main) if len(calls) == 1 else ""
            stderr = "boom"
        return R()

    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    resp = P._default_query("cairnea/KinDash", retention_days=7)
    # open query, failed env-supplement re-fetch, failed merged-PR search — all
    # three attempted; the two best-effort failures never take down the main response.
    assert len(calls) == 3
    assert resp["data"]["repo"]["environments"]["nodes"]  # main intact


def test_default_query_tolerates_forbidden_deploys(monkeypatch):
    """Deployments/Environments forbidden → GraphQL nulls the `deploys` alias and
    gh exits non-zero, but the PR population (repo.pullRequests + merged search) is
    intact and must still be returned. Graceful degradation, not a blank board."""
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: "tok")
    pr = {"number": 811, "url": "u", "title": "t", "state": "OPEN",
          "isDraft": False, "updatedAt": "2026-09-01T10:00:00Z"}
    partial = {
        "data": {"repo": {"pullRequests": {"nodes": [pr]}},
                 "deploys": None, "merged": {"nodes": []}},
        "errors": [{"message": "Resource not accessible by integration"}],
    }

    def fake_run(args, token=None, input=None):
        class R:
            returncode = 1
            stdout = json.dumps(partial)
            stderr = "gh: Resource not accessible by integration"
        return R()

    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    resp = P._default_query("steiner385/viberpit", retention_days=7)
    from server.github.pr_status import parse_population_response
    out = parse_population_response(resp, "steiner385/viberpit")
    assert [p.number for p in out] == [811]


def test_default_query_never_combines_open_and_merged_in_one_call(monkeypatch):
    """Regression: cairnea/KinDash disappeared from the pipeline board because the
    open-PR block and the merged-PR search used to share ONE GraphQL document —
    both select the full, expensive node fields (files + check contexts), and for
    a repo with enough open PRs and big enough diffs the combined document's
    execution time exceeded GitHub's edge timeout: every poll got back a 504 with
    no usable data at all (verified live against cairnea/KinDash: reproduced 3/3).

    This fake models exactly that failure mode — a query containing BOTH an open
    `pullRequests(states: OPEN` block and a `merged: search(` block 504s; either
    one alone succeeds — so this test fails the moment the two are ever
    recombined into a single request, not just today."""
    monkeypatch.setattr(P.app_auth, "token_for_owner", lambda owner: "tok")
    open_pr = {"number": 16934, "url": "u", "title": "t", "state": "OPEN",
               "isDraft": False, "updatedAt": "2026-09-24T10:00:00Z"}

    def fake_run(args, token=None, input=None):
        query = " ".join(args)
        combined = "pullRequests(states: OPEN" in query and "merged: search(" in query
        if combined:
            class R: returncode = 1; stdout = ""; stderr = "gh: HTTP 504"
            return R()
        if "pullRequests(states: OPEN" in query:
            out = json.dumps({"data": {"repo": {"pullRequests": {"nodes": [open_pr]}}}})
        else:
            out = json.dumps({"data": {"merged": {"nodes": []}}})
        class R: returncode = 0; stdout = out; stderr = ""
        return R()

    monkeypatch.setattr(P.gitrepos, "_run_gh", fake_run)
    resp = P._default_query("cairnea/KinDash", retention_days=7)
    from server.github.pr_status import parse_population_response
    out = parse_population_response(resp, "cairnea/KinDash")
    assert [p.number for p in out] == [16934]
