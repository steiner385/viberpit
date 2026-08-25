"""Guards issue #751's actual deliverable: "the eleventh site cannot reintroduce the bug."

Nine LLM helper call sites used to each build their own SDK env by hand
(``{**os.environ, "CLAUDE_CONFIG_DIR": config_dir}`` or ``dict(os.environ,
CLAUDE_CONFIG_DIR=config_dir)``), which silently dropped an account's setup-token whenever its
browser-OAuth refresh was dead. The sweep collapsed all nine onto ``account_auth.helper_env``
(and, for session launch, ``account_auth.oauth_env_for`` via ``sdk_factory``'s ``extra_env``).

Before this test, the only thing enforcing "route through helper_env instead of building the
dict yourself" was a one-off grep pasted into a planning report — nothing would fail if a tenth
or eleventh call site reintroduced the bare construction. This test greps ``server/`` (excluding
tests) for BOTH forms of the construction and fails if either form appears anywhere outside the
explicit allowlist below.
"""
from __future__ import annotations

import re
from pathlib import Path

_SERVER_ROOT = Path(__file__).resolve().parent.parent

# Files allowed to construct the SDK env directly, and why:
#   account_auth.py — the shared helper itself (helper_env/oauth_env_for live here).
#   sdk_factory.py   — session launch's own env (extra_env, built by orchestrator's _oat_env,
#                       which already calls oauth_env_for) plus the two SDK factories
#                       (real_suggestion_factory/real_summary_factory) that call
#                       account_auth.oauth_env_for directly rather than through helper_env.
_ALLOWLIST = {
    _SERVER_ROOT / "sessions" / "account_auth.py",
    _SERVER_ROOT / "sessions" / "sdk_factory.py",
}

# Both forms of the bare construction this sweep replaced:
#   {"CLAUDE_CONFIG_DIR": ...}          (dict-literal / dict-merge form)
#   CLAUDE_CONFIG_DIR=...               (dict(os.environ, CLAUDE_CONFIG_DIR=config_dir) kwarg form)
_QUOTED_FORM = re.compile(r'"CLAUDE_CONFIG_DIR"\s*:')
_KWARG_FORM = re.compile(r'\bCLAUDE_CONFIG_DIR\s*=')

_FAILURE_HINT = (
    "\n\nThis matched a raw SDK-env construction for CLAUDE_CONFIG_DIR outside the allowlist in "
    "test_env_construction_no_reintroduction.py. Do NOT add yourself to the allowlist — that "
    "reintroduces the exact bug issue #751 fixed (an account whose browser-OAuth refresh is "
    "dead but whose setup-token still works would silently lose auth). Instead, call "
    "server.sessions.account_auth.helper_env(config_dir) to build the env, the way all nine "
    "other LLM helper call sites do."
)


def _iter_server_py_files():
    for path in _SERVER_ROOT.rglob("*.py"):
        if "tests" in path.relative_to(_SERVER_ROOT).parts:
            continue
        yield path


def test_no_stray_quoted_config_dir_construction():
    hits = []
    for path in _iter_server_py_files():
        if path in _ALLOWLIST:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _QUOTED_FORM.search(line):
                hits.append(f"{path}:{lineno}: {line.strip()}")
    assert not hits, "Found un-allowlisted CLAUDE_CONFIG_DIR dict-literal construction:\n" + "\n".join(hits) + _FAILURE_HINT


def test_no_stray_kwarg_config_dir_construction():
    hits = []
    for path in _iter_server_py_files():
        if path in _ALLOWLIST:
            continue
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _KWARG_FORM.search(line):
                hits.append(f"{path}:{lineno}: {line.strip()}")
    assert not hits, "Found un-allowlisted CLAUDE_CONFIG_DIR kwarg construction:\n" + "\n".join(hits) + _FAILURE_HINT


def test_allowlisted_files_still_exist():
    """Guards against the allowlist silently going stale (e.g. a file rename)."""
    for path in _ALLOWLIST:
        assert path.is_file(), f"allowlisted path no longer exists: {path}"


def test_allowlisted_files_actually_contain_the_construction():
    """Sanity check the test itself: the allowlisted files should still contain at least one
    of the two forms, or the allowlist entry is dead weight that would hide a real regression
    if that file's construction were ever deleted and reintroduced elsewhere."""
    for path in _ALLOWLIST:
        text = path.read_text()
        assert _QUOTED_FORM.search(text) or _KWARG_FORM.search(text), (
            f"{path} is allowlisted but no longer contains either CLAUDE_CONFIG_DIR "
            "construction form — check if the allowlist entry is still needed"
        )
