import json
from server.deploy import steps


def test_publish_success_returns_true():
    calls = []
    def gh(path, body):
        calls.append((path, json.loads(body)))
        if path.endswith("/deployments"):
            return json.dumps({"id": 99})
        return json.dumps({"id": 1234})
    assert steps.publish_deployment("abc", gh_api_json=gh) is True
    assert calls[0][1]["required_contexts"] == []
    assert "deployments/99/statuses" in calls[1][0]


def test_publish_failure_returns_false_not_raises():
    def gh(path, body):
        raise RuntimeError("403")
    assert steps.publish_deployment("abc", gh_api_json=gh) is False


def test_publish_missing_id_returns_false():
    assert steps.publish_deployment("abc", gh_api_json=lambda p, b: json.dumps({})) is False
