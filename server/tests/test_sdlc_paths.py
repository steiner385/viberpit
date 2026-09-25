"""The artifact layout lives in a pure module so the consequence explainer
(server/sdlc/explain.py) can name the file a session will add without importing
the prompt builders, which pull in session code."""
import subprocess
import sys

# Purity is checked in a FRESH interpreter: deleting entries from sys.modules in
# the test process would leave later tests in the same pytest worker holding
# duplicate class objects (isinstance mismatches).
_PURITY_CHECK = (
    "import sys, {module}; bad = sorted(m for m in sys.modules if m.startswith("
    "('server.db', 'server.automations', 'server.sessions'))); "
    "assert not bad, bad"
)


def assert_pure(module: str) -> None:
    r = subprocess.run([sys.executable, "-c", _PURITY_CHECK.format(module=module)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_artifact_path_layout():
    from server.sdlc.paths import SDLC_DOCS_DIR, artifact_path
    assert SDLC_DOCS_DIR == "docs/sdlc"
    assert artifact_path("dark-mode", "spec") == "docs/sdlc/dark-mode/spec.md"


def test_artifact_path_default_layout_is_explicit():
    from server.sdlc.paths import DEFAULT_ARTIFACT_LAYOUT, artifact_path
    assert DEFAULT_ARTIFACT_LAYOUT == "docs_sdlc"
    assert (artifact_path("dark-mode", "plan", "docs_sdlc")
            == artifact_path("dark-mode", "plan"))


def test_artifact_path_speckit_layout():
    from server.sdlc.paths import artifact_path
    assert artifact_path("007-dark-mode", "plan", "speckit") == "specs/007-dark-mode/plan.md"


def test_artifact_path_unknown_layout_falls_back_to_the_default():
    from server.sdlc.paths import artifact_path
    assert artifact_path("dark-mode", "spec", "no-such-layout") == "docs/sdlc/dark-mode/spec.md"


def test_paths_module_is_pure():
    """Importing it must not drag in the DB, automations or session packages."""
    assert_pure("server.sdlc.paths")


def test_prompts_reexports_the_same_function():
    from server.sdlc import paths, prompts
    assert prompts.artifact_path is paths.artifact_path
    assert prompts.SDLC_DOCS_DIR == paths.SDLC_DOCS_DIR
