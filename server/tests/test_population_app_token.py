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
