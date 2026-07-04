import pytest
from server.automations.action_providers import PrDoctorActionProvider


class _Orch:
    def __init__(self, status):
        self._status = status

    async def _pr_doctor_consolidate(self, repo, numbers):
        return self._status, ("sid" if self._status == "spawned" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,level", [
    ("spawned", "ok"), ("duplicate", "warn"), ("too_few", "warn"),
    ("not_actionable", "warn"), ("ceiling", "warn"),
    ("clone_failed", "error"), ("spawn_failed", "error"),
])
async def test_consolidate_maps_status_to_level(status, level):
    provider = PrDoctorActionProvider(_Orch(status))
    result = await provider.consolidate("o/r", [1, 2])
    assert result.status == status
    assert result.level == level
    assert result.message                      # non-empty human message
