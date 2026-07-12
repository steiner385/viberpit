import pytest
from server.automations.action_providers import PrDoctorActionProvider


class _Orch:
    def __init__(self, status):
        self._status = status

    async def _pr_doctor_consolidate(self, repo, numbers):
        return self._status, None


@pytest.mark.asyncio
@pytest.mark.parametrize("status,level", [
    # The synchronous fast path can only answer these — clone/model/spawn run
    # in a background task and report through the automation feed (issue #567).
    ("spawning", "ok"), ("duplicate", "warn"), ("too_few", "warn"),
    ("not_actionable", "warn"),
])
async def test_consolidate_maps_status_to_level(status, level):
    provider = PrDoctorActionProvider(_Orch(status))
    result = await provider.consolidate("o/r", [1, 2])
    assert result.status == status
    assert result.level == level
    assert result.message                      # non-empty human message


@pytest.mark.asyncio
async def test_consolidate_unknown_status_maps_to_error():
    provider = PrDoctorActionProvider(_Orch("mystery"))
    result = await provider.consolidate("o/r", [1, 2])
    assert result.level == "error"
    assert result.message
