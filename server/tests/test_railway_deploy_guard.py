"""#3403: a live Railway mutation on KinDash prod that would rebuild from unverified
main is denied; the staged / redeploy / other-service paths pass."""
import asyncio

import pytest

from server.sessions import railway_deploy_guard as g

TOOL = "mcp__plugin_railway_railway__"
PROJ, ENV, SVC = g.KINDASH_PROJECT_ID, g.KINDASH_PROD_ENV_ID, g.KINDASH_PROD_SERVICE_ID
RED = g.DeployFacts("75a3cdc1", "95a1412f", "75a3cdc1", 12, ("#21009",))
GREEN = g.DeployFacts("95a1412f", "95a1412f", "95a1412f", 0, ())


def run(tool, tool_input, facts):
    hook = g.railway_deploy_guard(lambda: facts)
    return asyncio.run(hook({"tool_name": TOOL + tool, "tool_input": tool_input}))


def denied(out):
    return out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_replay_of_the_11_13_call_is_blocked():
    out = run("set-variables", {"projectId": PROJ, "serviceId": SVC, "variables": {"A": "1"}}, RED)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert denied(out) and "12 commit(s)" in reason and "#21009" in reason
    assert "staged: true" in reason and "redeploy" in reason


def test_omitted_environment_defaults_to_prod():
    assert denied(run("set-variables", {"projectId": PROJ, "variables": {}}, RED))


def test_staged_call_passes():
    assert run("set-variables", {"projectId": PROJ, "staged": True, "variables": {}}, RED) == {}
    assert run("update-service", {"projectId": PROJ, "serviceId": SVC, "staged": True}, RED) == {}


def test_skip_deploys_passes_for_variables_only():
    assert run("set-variables", {"projectId": PROJ, "skipDeploys": True, "variables": {}}, RED) == {}
    assert denied(run("update-service", {"projectId": PROJ, "serviceId": SVC, "skipDeploys": True}, RED))


def test_other_environment_service_or_project_passes():
    assert run("set-variables", {"projectId": PROJ, "environmentId": "staging-id", "variables": {}}, RED) == {}
    assert run("set-variables", {"projectId": PROJ, "serviceId": "postgres-id", "variables": {}}, RED) == {}
    assert run("set-variables", {"projectId": "other", "variables": {}}, RED) == {}


def test_verified_main_passes():
    assert run("set-variables", {"projectId": PROJ, "variables": {}}, GREEN) == {}


def test_unreadable_facts_fail_closed():
    assert denied(run("set-variables", {"projectId": PROJ, "variables": {}},
                      g.DeployFacts(None, None, None, None, ())))


def test_lookup_error_fails_closed():
    def boom():
        raise RuntimeError("gh down")
    hook = g.railway_deploy_guard(boom)
    out = asyncio.run(hook({"tool_name": TOOL + "set-variables",
                            "tool_input": {"projectId": PROJ, "variables": {}}}))
    assert denied(out)


@pytest.mark.parametrize("tool", ["redeploy", "list-projects"])
def test_other_railway_tools_pass(tool):
    assert run(tool, {"projectId": PROJ}, RED) == {}


def test_non_railway_tool_passes():
    hook = g.railway_deploy_guard(lambda: RED)
    assert asyncio.run(hook({"tool_name": "mcp__other__set-variables", "tool_input": {}})) == {}
