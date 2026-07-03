import json
import subprocess
from server.deploy import steps


def test_publish_success_returns_none():
    calls = []
    def gh(path, body):
        calls.append((path, json.loads(body)))
        if path.endswith("/deployments"):
            return json.dumps({"id": 99})
        return json.dumps({"id": 1234})
    assert steps.publish_deployment("abc", gh_api_json=gh) is None
    assert calls[0][1]["required_contexts"] == []
    assert "deployments/99/statuses" in calls[1][0]


def test_publish_failure_returns_reason_not_raises():
    def gh(path, body):
        raise RuntimeError("403")
    assert steps.publish_deployment("abc", gh_api_json=gh) == "403"


def test_publish_failure_prefers_stderr_tail():
    """A CalledProcessError-style failure surfaces its stderr tail (the 307 rename case)."""
    def gh(path, body):
        raise subprocess.CalledProcessError(
            1, ["gh"], output='{"message":"Moved Permanently"}', stderr="gh: HTTP 307\n")
    assert steps.publish_deployment("abc", gh_api_json=gh) == "gh: HTTP 307"


def test_publish_missing_id_returns_reason():
    reason = steps.publish_deployment("abc", gh_api_json=lambda p, b: json.dumps({}))
    assert reason == "no deployment id in response"
