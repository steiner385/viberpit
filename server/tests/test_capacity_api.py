"""GET /api/capacity smoke test (spec 2026-09-19 §5)."""
from __future__ import annotations

from server.tests.test_pm_api import IDENTITY_HEADERS, _client
from server.tests.test_pm_api import canonical_mutation_repositories  # noqa: F401


def test_get_capacity_returns_the_ceiling_stack():
    with _client() as c:
        resp = c.get("/api/capacity", headers=IDENTITY_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for key in ("global", "hosts", "accounts", "runner", "products", "binding", "defects", "plan"):
            assert key in body, key
