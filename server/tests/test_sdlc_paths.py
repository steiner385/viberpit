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


def test_paths_module_is_pure():
    """Importing it must not drag in the DB, automations or session packages."""
    assert_pure("server.sdlc.paths")


def test_prompts_reexports_the_same_function():
    from server.sdlc import paths, prompts
    assert prompts.artifact_path is paths.artifact_path
    assert prompts.SDLC_DOCS_DIR == paths.SDLC_DOCS_DIR
