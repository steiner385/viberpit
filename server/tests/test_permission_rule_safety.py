import pytest

from server.automations.autopilot_policy import _BASELINE_DENY, matches_denylist
from server.permission_rules import safety
from server.permission_rules.safety import (
    assess,
    needs_confirmation,
    offered_suggestions,
)


@pytest.mark.parametrize(
    "pat,scope,level",
    [
        ("Bash", "session", "refuse"),
        ("Bash(*)", "session", "refuse"),
        ("Bash(git *)", "session", "refuse"),
        ("Bash(git:*)", "repo", "refuse"),
        ("Bash(git push:*)", "session", "refuse"),
        ("Bash(rm *)", "session", "refuse"),
        ("Bash(rm:*)", "session", "refuse"),
        ("Bash(sudo:*)", "session", "refuse"),
        ("Bash(git status:*)", "session", "ok"),
        ("Bash(npm test:*)", "global", "ok"),
        ("Bash(npm:*)", "session", "warn"),
        ("Bash(npm test)", "session", "ok"),
        ("Edit(src/**)", "repo", "ok"),
        ("Edit(**)", "session", "warn"),
        ("Edit(**)", "global", "refuse"),
        ("Edit", "global", "refuse"),
        ("Edit", "repo", "warn"),
        ("Edit(/etc/**)", "session", "refuse"),
        ("Edit(../x/**)", "session", "refuse"),
        ("Edit(**/.env*)", "session", "refuse"),
        ("Edit(.claude/**)", "session", "refuse"),
        ("Edit(.github/**)", "session", "warn"),
        ("WebFetch(domain:*)", "session", "refuse"),
        ("WebFetch", "session", "warn"),
        ("WebFetch(domain:docs.python.org)", "global", "ok"),
        ("mcp__gh", "session", "warn"),
        ("mcp__gh__list", "session", "ok"),
        ("Bash(", "session", "refuse"),
        ("Bash(echo a > b)", "session", "refuse"),
        ("Bash(echo $(x))", "session", "refuse"),
        ("Bash(a | b)", "session", "refuse"),
        ("WebSearch", "session", "warn"),
        ("TodoWrite", "session", "ok"),
        ("Read", "global", "refuse"),
        ("Read", "repo", "warn"),
        ("Read(~/x)", "session", "refuse"),
    ],
)
def test_assess(pat, scope, level):
    assert assess(pat, scope).level == level


def test_unknown_scope_raises():
    with pytest.raises(ValueError):
        assess("Bash(npm test)", "galaxy")


def test_probes_cover_the_baseline_denylist():
    hit = {matches_denylist("Bash", {"command": p}, []) for p in safety._DANGER_PROBES}
    assert {n for n, _ in _BASELINE_DENY} - {"secret_exfil"} <= hit


def test_global_always_needs_confirmation():
    assert needs_confirmation(assess("Bash(npm test)", "global"), "global")
    assert not needs_confirmation(assess("Bash(npm test)", "session"), "session")


def test_offered_suggestions_drop_refusals():
    out = offered_suggestions("Bash", {"command": "git push --force"}, "/w")
    assert all(s["level"] != "refuse" for s in out)
    assert [s["pattern"] for s in out] == ["Bash(git push --force)"] or out == []


def test_offered_suggestions_shape():
    out = offered_suggestions("Bash", {"command": "npm test"}, "/w")
    assert out and set(out[0]) == {"pattern", "description", "level"}
    assert out[0]["pattern"] == "Bash(npm test)"


@pytest.mark.parametrize(
    "pat",
    [
        "Bash(sudo -n:*)",
        "Bash(command sudo:*)",
        "Bash(env bash:*)",
        "Bash(timeout 5 bash:*)",
        "Bash(rm -r:*)",
        "Bash(git push origin:*)",
        "Bash(git push origin master:*)",
        "Bash(git -c x=y push:*)",
        "Bash(env:*)",
        "Bash(xargs:*)",
        "Bash(/usr/bin/sudo:*)",
        "Bash(/bin/rm:*)",
        "Bash(git push --force)",
        "Bash(rm -rf /)",
        "mcp__x__",
        "Edit(.git/**)",
        "Edit(.git/hooks/*)",
        "Edit(.aws/*)",
        "Edit(**/*.pem)",
        "Edit(.npmrc)",
        "Edit(.husky/**)",
        "Edit(.mcp.json)",
        "Edit(secrets/**)",
        "Edit(**/credentials*)",
        "WebFetch(domain:*.com)",
        "WebFetch(domain:com)",
        "WebFetch(domain:localhost)",
        "WebFetch(domain:127.0.0.1)",
        "WebFetch(domain:foo.local)",
        "WebFetch(domain:a.internal)",
    ],
)
def test_bypasses_refused(pat):
    assert assess(pat, "session").level == "refuse"


@pytest.mark.parametrize(
    "pat",
    [
        "Bash(git status:*)",
        "Bash(npm test:*)",
        "Bash(rm build/x.txt)",
        "Edit(src/**)",
        "WebFetch(domain:docs.python.org)",
    ],
)
def test_still_ok(pat):
    assert assess(pat, "session").level == "ok"


@pytest.mark.parametrize(
    "pat,level",
    [
        ("Bash(python -m pytest:*)", "ok"),
        ("Bash(python3 -m pip list:*)", "ok"),
        ("Bash(python:*)", "refuse"),
        ("Bash(python -c:*)", "refuse"),
        ("Bash(python3 -c:*)", "refuse"),
        ("Bash(node:*)", "refuse"),
        ("Bash(node -e:*)", "refuse"),
        ("Bash(perl *)", "refuse"),
        ("Edit(.git)", "refuse"),
        ("Edit(**/.git)", "refuse"),
    ],
)
def test_interpreters_and_git_file(pat, level):
    assert assess(pat, "session").level == level


@pytest.mark.parametrize(
    "pat,level",
    [
        ("Bash(git * origin)", "refuse"),
        ("Bash(git * --force)", "refuse"),
        ("Bash(* sudo)", "refuse"),
        ("Bash(* bash -c x)", "refuse"),
        ("Bash(python3.12 -c:*)", "refuse"),
        ("Bash(/usr/bin/python3.11 -c:*)", "refuse"),
        ("Bash(python3.11:*)", "refuse"),
        ("Bash(nodejs:*)", "refuse"),
        ("Bash(pythonw:*)", "refuse"),
        ("Bash(deno:*)", "refuse"),
        ("Bash(bun:*)", "refuse"),
        ("Bash(pwsh:*)", "refuse"),
        ("Bash(osascript:*)", "refuse"),
        ("Bash(\\sudo:*)", "refuse"),
        ("WebFetch(domain:localhost.)", "refuse"),
        ("WebFetch(domain:foo.local.)", "refuse"),
        ("Edit(a/.git/hooks/pre-commit)", "refuse"),
        ("Edit(x/.husky/*)", "refuse"),
        ("Edit(x/.aws/credentials)", "refuse"),
        ("Edit(x/.ssh/*)", "refuse"),
        ("Bash(npm *)", "ok"),
        ("Bash(python -m pytest:*)", "ok"),
        ("Bash(git status:*)", "ok"),
        ("Bash(npm test:*)", "ok"),
        ("Edit(src/**)", "ok"),
        ("WebFetch(domain:docs.python.org)", "ok"),
    ],
)
def test_round2(pat, level):
    assert assess(pat, "session").level == level
