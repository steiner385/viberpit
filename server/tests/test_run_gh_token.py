import os

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


def test_gh_env_strips_ambient_gh_token(monkeypatch):
    """A stray GH_TOKEN in the ambient env (e.g. a KinDash bot login) must not
    leak into ccswitch gh calls — it would override the pinned account."""
    monkeypatch.setenv("GH_TOKEN", "ghs_BOT_LEAK")
    monkeypatch.delenv("CCSWITCH_GH_CONFIG_DIR", raising=False)
    env = gitrepos._gh_env()
    assert "GH_TOKEN" not in env


def test_gh_env_pins_config_dir_when_set(monkeypatch, tmp_path):
    """CCSWITCH_GH_CONFIG_DIR isolates gh auth from the shared ~/.config/gh that
    KinDash's bot mutates, so the daemon's steiner385 identity can't be hijacked."""
    monkeypatch.setenv("CCSWITCH_GH_CONFIG_DIR", str(tmp_path / "gh"))
    env = gitrepos._gh_env()
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "gh")


def test_gh_env_expands_config_dir(monkeypatch):
    monkeypatch.setenv("CCSWITCH_GH_CONFIG_DIR", "~/.ccswitch/gh")
    env = gitrepos._gh_env()
    assert env["GH_CONFIG_DIR"] == os.path.expanduser("~/.ccswitch/gh")


def test_gh_env_no_config_dir_when_unset(monkeypatch):
    monkeypatch.delenv("CCSWITCH_GH_CONFIG_DIR", raising=False)
    env = gitrepos._gh_env()
    assert "GH_CONFIG_DIR" not in env


def test_run_gh_token_wins_alongside_config_dir(monkeypatch, tmp_path):
    """The per-owner App token (GH_TOKEN) still applies with a pinned config dir —
    isolation must not break the App-installation-token path."""
    monkeypatch.setenv("CCSWITCH_GH_CONFIG_DIR", str(tmp_path / "gh"))
    captured = {}
    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        class P: returncode = 0; stdout = "{}"; stderr = ""
        return P()
    monkeypatch.setattr(gitrepos.subprocess, "run", fake_run)
    gitrepos._run_gh(["api", "graphql"], token="ghs_APP")
    assert captured["env"]["GH_TOKEN"] == "ghs_APP"
    assert captured["env"]["GH_CONFIG_DIR"] == str(tmp_path / "gh")
