from ccswitch import gitrepos


def test_run_gh_sets_gh_token_when_provided(monkeypatch):
    captured = {}
    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class P: returncode = 0; stdout = "{}"; stderr = ""
        return P()
    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)
    gitrepos._run_gh(["api", "graphql", "-f", "query=x"], token="ghs_ABC")
    assert captured["env"]["GH_TOKEN"] == "ghs_ABC"


def test_run_gh_no_token_strips_github_token(monkeypatch):
    captured = {}
    monkeypatch.setenv("GITHUB_TOKEN", "stale")
    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class P: returncode = 0; stdout = "{}"; stderr = ""
        return P()
    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)
    gitrepos._run_gh(["api", "graphql"])
    assert "GITHUB_TOKEN" not in captured["env"]
    assert "GH_TOKEN" not in captured["env"]
