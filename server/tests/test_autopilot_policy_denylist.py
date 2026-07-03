import pytest
from server.automations.autopilot_policy import matches_denylist


@pytest.mark.parametrize("cmd", [
    "git push --force",
    "git push -f origin main",
    "git reset --hard origin/master",
    "rm -rf /home/tony/GitHub/KinDash",
    "git clean -fdx",
    "gh pr merge 123 --squash",
    "git branch -D feature",
])
def test_dangerous_bash_is_denied(cmd):
    assert matches_denylist("Bash", {"command": cmd}, []) is not None


@pytest.mark.parametrize("cmd", [
    "git push --force-with-lease",
    "pytest -q",
    "git add -A && git commit -m x",
    "git push origin HEAD",
    "gh pr create --fill",
])
def test_safe_bash_is_allowed(cmd):
    assert matches_denylist("Bash", {"command": cmd}, []) is None


def test_extra_patterns_extend_denylist():
    assert matches_denylist("Bash", {"command": "terraform apply"}, ["terraform apply"]) is not None


def test_non_bash_tool_not_denied_by_default():
    assert matches_denylist("Read", {"file_path": "/etc/passwd"}, []) is None
