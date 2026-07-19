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
    keys = [("cairnea/KinDash", 1), ("steiner385/cc-account-switcher", 2)]
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
    assert len(calls) == 2
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
    assert len(calls) == 1


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
    assert len(calls) == 2
    assert resp["data"]["repo"]["environments"]["nodes"]  # main intact
