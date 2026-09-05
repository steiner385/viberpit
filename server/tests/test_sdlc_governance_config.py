"""Drift + behaviour guards for the `.claude/` governance control plane (#805).

Unlike the pure-core tests, these read the ACTUAL committed config files and run
the ACTUAL hook script, so the enforcement layer can't silently diverge from the
governance decisions it is supposed to wire in.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from server.sdlc.governance import (
    PROTECTED_EDIT_PATHS,
    managed_deny_patterns,
    missing_managed_rules,
)

_PROTECTED_PATHS = PROTECTED_EDIT_PATHS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE = _REPO_ROOT / ".claude"
_CODEX = _REPO_ROOT / ".codex"
_HOOK = _CLAUDE / "hooks" / "prod-deploy-gate.py"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# Managed non-negotiables — the file must carry every MANAGED_DENY rule        #
# --------------------------------------------------------------------------- #

def test_managed_settings_file_honours_every_non_negotiable():
    settings = _load(_CLAUDE / "managed-settings.json")
    assert missing_managed_rules(settings) == [], (
        "managed-settings.json dropped a non-negotiable; it must stay in sync with "
        "server.sdlc.governance.MANAGED_DENY"
    )


def test_managed_settings_has_no_extra_deny_rules():
    # Exact match (not just superset): an unexplained extra deny is drift too.
    settings = _load(_CLAUDE / "managed-settings.json")
    assert settings["permissions"]["deny"] == managed_deny_patterns()


# --------------------------------------------------------------------------- #
# settings.json wires the prod-deploy gate                                     #
# --------------------------------------------------------------------------- #

def test_settings_wires_prod_deploy_gate_hook():
    settings = _load(_CLAUDE / "settings.json")
    pretooluse = settings["hooks"]["PreToolUse"]
    commands = [
        h["command"]
        for group in pretooluse
        for h in group.get("hooks", [])
    ]
    assert any("prod-deploy-gate.py" in c for c in commands)


def test_codex_config_wires_governance_hook_for_shell_and_edits():
    with (_CODEX / "config.toml").open("rb") as handle:
        settings = tomllib.load(handle)
    pretooluse = settings["hooks"]["PreToolUse"]
    assert any("Bash" in group["matcher"] and "apply_patch" in group["matcher"]
               for group in pretooluse)
    commands = [
        hook["command"]
        for group in pretooluse
        for hook in group.get("hooks", [])
    ]
    assert any("prod-deploy-gate.py" in command for command in commands)


def test_codex_rules_forbid_unleased_force_pushes():
    rules = (_CODEX / "rules" / "default.rules").read_text()
    assert 'pattern = ["git", "push", "--force"]' in rules
    assert 'pattern = ["git", "push", "-f"]' in rules
    assert 'decision = "forbidden"' in rules


# --------------------------------------------------------------------------- #
# The hook script actually blocks / allows the right commands                  #
# --------------------------------------------------------------------------- #

def _run_hook_event(event: dict, env_extra: dict[str, str]) -> dict | None:
    import os
    # Start from a clean slate for the governance vars so the test runner's own
    # environment can't leak an approval token in, then apply only what the case sets.
    env = {k: v for k, v in os.environ.items()
           if k not in ("RELEASE_APPROVAL", "CCSWITCH_DEPLOY_ENV",
                        "CCSWITCH_RELEASE_AUTHOR", "CCSWITCH_RELEASE_APPROVER")}
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(event), text=True, capture_output=True,
        cwd=str(_REPO_ROOT), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _run_hook(command: str, env_extra: dict[str, str]) -> dict | None:
    """Run the gate with a Bash event; return its decision or None when allowed."""
    return _run_hook_event(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env_extra,
    )


def test_hook_blocks_prod_deploy_without_approval():
    decision = _run_hook("./scripts/deploy.sh --prod", {})
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "RELEASE_APPROVAL" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allows_prod_deploy_with_approval():
    decision = _run_hook("./scripts/deploy.sh --prod", {"RELEASE_APPROVAL": "incident-42"})
    assert decision is None  # allowed → no deny emitted


def test_hook_blocks_self_approved_prod_deploy():
    decision = _run_hook(
        "deploy production",
        {
            "RELEASE_APPROVAL": "incident-42",
            "CCSWITCH_RELEASE_AUTHOR": "backlog_runner",
            "CCSWITCH_RELEASE_APPROVER": "backlog_runner",
        },
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_ignores_non_deploy_commands():
    assert _run_hook("git log --oneline | grep deploy", {}) is None


def test_hook_ignores_non_prod_deploy():
    assert _run_hook("./scripts/deploy.sh --staging", {}) is None


def test_hook_blocks_codex_patch_to_constitution():
    decision = _run_hook_event(
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "*** Begin Patch\n*** Update File: .specify/memory/constitution.md\n",
            },
        },
        {},
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ".specify/memory/constitution.md" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allows_codex_patch_to_regular_file():
    decision = _run_hook_event(
        {
            "tool_name": "apply_patch",
            "tool_input": {"command": "*** Begin Patch\n*** Update File: README.md\n"},
        },
        {},
    )
    assert decision is None


# --------------------------------------------------------------------------- #
# Native Claude edit tools carry `file_path`, not `command`                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
@pytest.mark.parametrize("path", sorted(_PROTECTED_PATHS))
def test_hook_blocks_native_edit_tool_on_protected_path(tool_name, path):
    """``Edit``/``Write`` events describe their target with ``file_path``.

    Reading ``tool_input["command"]`` for these tools finds nothing and silently
    allows — a fail-open in a security gate. The managed deny rules are the only
    other thing standing in the way, so this branch must deny on its own.
    """
    decision = _run_hook_event(
        {"tool_name": tool_name, "tool_input": {"file_path": path}}, {},
    )
    assert decision is not None, f"{tool_name} on {path} was allowed (fail-open)"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert path in decision["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
def test_hook_allows_native_edit_tool_on_regular_file(tool_name):
    decision = _run_hook_event(
        {"tool_name": tool_name, "tool_input": {"file_path": "server/app.py"}}, {},
    )
    assert decision is None


def test_hook_blocks_native_edit_tool_on_absolute_protected_path():
    decision = _run_hook_event(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(_REPO_ROOT / ".specify/memory/constitution.md")},
        },
        {},
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


# --------------------------------------------------------------------------- #
# Shell writes to protected paths (the gap the deny patterns cannot express)   #
# --------------------------------------------------------------------------- #

_SHELL_WRITES = [
    "sed -i 's/1.2.0/9.9.9/' .specify/memory/constitution.md",
    "sed --in-place 's/a/b/' .specify/memory/constitution.md",
    "cat > .specify/memory/constitution.md",
    "echo x >> .specify/memory/constitution.md",
    "printf 'x' | tee .specify/memory/constitution.md",
    "python3 -c \"open('.specify/memory/constitution.md','w').write('x')\"",
    "perl -i -pe 's/a/b/' .specify/memory/constitution.md",
    "cp /tmp/evil.md .specify/memory/constitution.md",
    "mv /tmp/evil.md .specify/memory/constitution.md",
    "truncate -s 0 .specify/memory/constitution.md",
    "cat > .claude/managed-settings.json",
]


@pytest.mark.parametrize("command", _SHELL_WRITES)
def test_hook_blocks_shell_write_to_protected_path(command):
    decision = _run_hook(command, {})
    assert decision is not None, f"shell write was allowed: {command}"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


_SHELL_READS = [
    "grep -n 'Principle' .specify/memory/constitution.md",
    "sed -n '1,20p' .specify/memory/constitution.md",
    "head -40 .specify/memory/constitution.md",
    "wc -l .specify/memory/constitution.md",
    "awk 'length($0)>100' .specify/memory/constitution.md",
    "git show HEAD:.specify/memory/constitution.md",
    "git diff .specify/memory/constitution.md",
    "cat .specify/memory/constitution.md",
    "grep -c Principle .specify/memory/constitution.md | sort",
]


@pytest.mark.parametrize("command", _SHELL_READS)
def test_hook_allows_shell_read_of_protected_path(command):
    """Reading these files is ordinary work and must stay frictionless."""
    assert _run_hook(command, {}) is None, f"read-only command was denied: {command}"


def test_hook_ignores_protected_path_names_in_unrelated_commands():
    assert _run_hook("git log --oneline -5", {}) is None


def test_hook_protected_paths_match_governance_core():
    """The hook's bare-worktree fallback must not drift from the core tuple.

    ``_load_protected_paths`` prefers the imported ``PROTECTED_EDIT_PATHS`` but
    falls back to a literal set when ``server/`` isn't importable. A stale
    fallback would quietly under-protect exactly in the environment that has no
    other defence.
    """
    source = _HOOK.read_text()
    fallback = re.search(
        r"return frozenset\(\{(.*?)\}\)", source, re.DOTALL,
    )
    assert fallback, "fallback literal set not found in the hook"
    literals = set(re.findall(r"[\"']([^\"']+)[\"']", fallback.group(1)))
    assert literals == set(PROTECTED_EDIT_PATHS), (
        "hook fallback drifted from server.sdlc.governance.PROTECTED_EDIT_PATHS"
    )


def test_codeowners_covers_guardrails():
    """Every guardrail path is claimed in CODEOWNERS.

    CODEOWNERS mirrors ``GUARDRAIL_PATHS``; a path in one and not the other means
    a file whose change alters enforcement is unowned, or an owned file the gate
    does not check.
    """
    from server.sdlc.governance import GUARDRAIL_PATHS

    codeowners = _REPO_ROOT / ".github" / "CODEOWNERS"
    patterns = [
        line.split()[0].lstrip("/")
        for line in codeowners.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    def claimed(path: str) -> bool:
        return any(
            path.startswith(pattern) if pattern.endswith("/") else path == pattern
            for pattern in patterns
        )

    unclaimed = [p for p in (*GUARDRAIL_PATHS, *PROTECTED_EDIT_PATHS) if not claimed(p)]
    assert not unclaimed, f"paths not claimed in CODEOWNERS: {unclaimed}"


def test_constitution_workflow_has_no_paths_filter():
    """A required check that is skipped leaves unrelated PRs pending forever."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "constitution.yml").read_text()
    pull_request_block = workflow.split("on:", 1)[1].split("jobs:", 1)[0]
    assert "paths:" not in pull_request_block, (
        "constitution.yml must run on every pull request — it is a required check, "
        "and a skipped required check never reports a conclusion"
    )


def test_constitution_workflow_reruns_on_body_edit():
    """The acknowledgment lives in the PR body, so `edited` must be a trigger.

    GitHub's default ``pull_request`` types are opened/synchronize/reopened. Without
    ``edited``, adding the acknowledgment to a failing pull request would not re-run
    the gate and the only remedy would be an empty commit.
    """
    text = (_REPO_ROOT / ".github" / "workflows" / "constitution.yml").read_text()
    trigger = text.split("on:", 1)[1].split("permissions:", 1)[0]
    assert "edited" in trigger, "constitution.yml must re-run when the PR body is edited"


def test_managed_deny_covers_every_protected_path():
    """Every protected path gets both a Write and an Edit deny pattern."""
    patterns = set(managed_deny_patterns())
    for path in PROTECTED_EDIT_PATHS:
        assert f"Write({path})" in patterns
        assert f"Edit({path})" in patterns
