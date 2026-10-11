import pytest
from viberpit.brand import LEGACY_MCP_TOOL_PREFIX

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


@pytest.mark.parametrize(
    "pat",
    [
        "Bash('python' -c:*)",
        "Bash(py\\thon -c:*)",
        "Bash(${X}python -c:*)",
        'Bash("sudo":*)',
        "Bash(su\\do:*)",
        'Bash(s""udo:*)',
        "Bash($sudo:*)",
        "Bash(sudo\\ :*)",
        'Bash(git commit -m "msg")',
        "Bash(.:*)",
        "Bash(. x:*)",
        "Bash(source:*)",
        "Bash(SUDO:*)",
        "Bash(Python -c:*)",
        "WebFetch(domain:foo.local..)",
        "Edit(x/.docker/config.json)",
        "Edit(x/.kube/*)",
        "Edit(.pgpass)",
        "Bash(git -c x=y status:*)",
        "Bash(git config:*)",
    ],
)
def test_round3_refused(pat):
    assert assess(pat, "session").level == "refuse"


def test_quoted_request_keeps_prefix_suggestion():
    out = offered_suggestions("Bash", {"command": 'git commit -m "msg"'}, "/w")
    pats = [s["pattern"] for s in out]
    assert "Bash(git commit:*)" in pats
    assert 'Bash(git commit -m "msg")' not in pats


@pytest.mark.parametrize(
    "pat",
    [
        "Bash(git status:*)",
        "Bash(git commit:*)",
        "Bash(npm *)",
        "Bash(python -m pytest:*)",
    ],
)
def test_round3_usability(pat):
    assert assess(pat, "session").level == "ok"


@pytest.mark.parametrize(
    "pat",
    [
        # `-m` must be followed by a module word in the head.
        "Bash(python -m:*)",
        "Bash(python3 -m:*)",
        "Bash(python -m *)",
        "Bash(python -m -c:*)",
        # Modules that run inline code.
        "Bash(python -m timeit:*)",
        "Bash(python3 -m pdb:*)",
        "Bash(python -m code:*)",
        "Bash(python -m runpy:*)",
        "Bash(python -m cProfile:*)",
        "Bash(python -m profile:*)",
        "Bash(python3.12 -m trace:*)",
        "Bash(pythonw -m timeit:*)",
        # Command launchers with nothing (or only options) after them.
        "Bash(uv run:*)",
        "Bash(uv run *)",
        "Bash(npm exec:*)",
        "Bash(npm x:*)",
        "Bash(pnpm exec:*)",
        "Bash(pnpm dlx:*)",
        "Bash(yarn exec:*)",
        "Bash(yarn dlx:*)",
        "Bash(poetry run:*)",
        "Bash(pipx run:*)",
        "Bash(npx:*)",
        "Bash(bunx:*)",
        "Bash(npx -y:*)",
        "Bash(uv run --with x:*)",
        # ... and the rest of a launcher head is checked like a head of its own.
        "Bash(uv run python:*)",
        "Bash(uv run python -c:*)",
        "Bash(uv run python -m timeit:*)",
        "Bash(poetry run bash:*)",
        "Bash(npx node -e:*)",
        "Bash(uv run uv run:*)",
    ],
)
def test_module_and_launcher_heads_refused(pat):
    assert assess(pat, "session").level == "refuse"


@pytest.mark.parametrize(
    "pat",
    [
        "Bash(python -m pytest:*)",
        "Bash(python3 -m pip list:*)",
        "Bash(uv run pytest:*)",
        "Bash(poetry run pytest -q:*)",
        "Bash(npx tsc:*)",
        "Bash(pnpm exec vitest:*)",
        "Bash(uv run python -m pytest:*)",
    ],
)
def test_module_and_launcher_heads_ok(pat):
    assert assess(pat, "session").level == "ok"


def _offered(command):
    return [s["pattern"] for s in offered_suggestions("Bash", {"command": command}, "/w")]


def test_python_module_suggestion_names_the_module():
    pats = _offered("python -m pytest -q")
    assert "Bash(python -m pytest:*)" in pats
    assert "Bash(python -m:*)" not in pats


def test_launcher_suggestion_names_the_command():
    pats = _offered("uv run pytest -x")
    assert "Bash(uv run pytest:*)" in pats
    assert "Bash(uv run:*)" not in pats


@pytest.mark.parametrize("scope", ["session", "repo", "global"])
@pytest.mark.parametrize(
    "pat",
    [
        "mcp__viberpit",
        LEGACY_MCP_TOOL_PREFIX.removesuffix("__"),
        "mcp__viberpit__resolve_permission",
        LEGACY_MCP_TOOL_PREFIX + "set_permission_mode",
        "mcp__viberpit__resolve_model_approval",
        "mcp__viberpit__set_autopilot_paused",
        "ExitPlanMode",
        "AskUserQuestion",
    ],
)
def test_app_controls_never_ruled(pat, scope):
    assert assess(pat, scope).level == "refuse"


def test_app_server_refusal_names_the_reason():
    assert "permission controls" in assess("mcp__viberpit", "session").reasons[0]


def test_ordinary_app_tool_rule_still_ok():
    assert assess("mcp__viberpit__rename_session", "session").level == "ok"


@pytest.mark.parametrize(
    "pat",
    [
        "Bash(git -ccore.pager=sh status:*)",
        "Bash(git -calias.x=!sh x:*)",
        "Bash(git --config-env=core.pager=X status:*)",
        "Bash(git --config-env core.pager=X status:*)",
        "Bash(git --exec-path=/tmp/x status:*)",
        "Bash(git --exec-path /tmp/x status:*)",
    ],
)
def test_git_config_injection_forms_refused(pat):
    assert assess(pat, "session").level == "refuse"
