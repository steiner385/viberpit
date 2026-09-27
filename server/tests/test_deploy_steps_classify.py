from server.deploy import steps


def test_all_edge_files_is_edge_only():
    assert steps.classify_change(["web/src/App.tsx", "server/api/x.py", "docs/y.md"]) == "edge_only"


def test_shared_daemon_module_forces_daemon_changed():
    assert steps.classify_change(["web/src/App.tsx", "server/config.py"]) == "daemon_changed"


def test_orchestrator_change_is_daemon_changed():
    assert steps.classify_change(["server/sessions/orchestrator.py"]) == "daemon_changed"


def test_none_diff_fails_safe_to_daemon_changed():
    assert steps.classify_change(None) == "daemon_changed"


def test_app_py_is_edge_only_but_db_is_not():
    assert steps.classify_change(["server/app.py"]) == "edge_only"
    assert steps.classify_change(["server/db/models.py"]) == "daemon_changed"


def test_repo_root_tooling_config_is_edge_only():
    """Repo-root dotdirs/dotfiles are agent, CI and spec-kit tooling config that nothing
    under server/ or src/ imports. A commit touching only them needs no daemon restart —
    and, because the reconciler's SHA fence reuses this classifier, must not fence the
    boards either: three such merges in a row kept every board write refused."""
    files = [".agents/instructions/core.md", ".claude/settings.json", ".claude/hooks/gate.py",
             ".codex/agents/viberpit-reviewer.toml", ".specify/memory/constitution.md",
             ".brand-ratchet.toml", ".env.example", ".gitignore", ".superpowers/x.json",
             "tests/test_ledger.py", "AGENTS.md"]
    assert steps.classify_change(files) == "edge_only"
    assert steps.daemon_changing_paths(files) == []


def test_dot_env_is_the_one_root_dotfile_the_daemon_loads():
    """server/config.py reads ``.env`` at startup (pydantic ``env_file``): a hidden root
    path, but one the daemon consumes, so it stays daemon-changing."""
    assert steps.classify_change([".env"]) == "daemon_changed"
    assert steps.classify_change([".env/x"]) == "daemon_changed"


def test_dependency_and_core_changes_stay_daemon_changed():
    for f in ("pyproject.toml", "uv.lock", "src/viberpit/ledger.py", "server/ipc/serve.py",
              "scripts/check_constitution_amendment.py", "evals/x.yaml", "alembic.ini"):
        assert steps.classify_change([f]) == "daemon_changed", f


def test_daemon_changing_paths_names_only_the_offenders():
    """The fence's refusal reason is built from this list, so it must name the paths
    that closed it and nothing else."""
    files = [".claude/settings.json", "server/db/models.py", "docs/a.md", "uv.lock"]
    assert steps.daemon_changing_paths(files) == ["server/db/models.py", "uv.lock"]
