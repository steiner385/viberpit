"""Drift + behaviour guards for the `.claude/` governance control plane (#805).

Unlike the pure-core tests, these read the ACTUAL committed config files and run
the ACTUAL hook script, so the enforcement layer can't silently diverge from the
governance decisions it is supposed to wire in.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from server.sdlc.governance import managed_deny_patterns, missing_managed_rules

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
