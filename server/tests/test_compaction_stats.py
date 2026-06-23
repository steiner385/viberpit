from server.sessions.compaction_stats import fold, DEFAULT_ESTIMATE_MS, MIN_MS, MAX_MS


def test_fold_seeds_with_first_clamped_sample():
    assert fold(None, 9000) == 9000
    assert fold(None, 100) == MIN_MS        # below floor clamps up
    assert fold(None, 999999) == MAX_MS     # above ceiling clamps down


def test_fold_ewma_weights_recent_at_alpha_0_3():
    # 0.3*sample + 0.7*prev
    assert fold(10000, 20000) == 0.3 * 20000 + 0.7 * 10000


def test_fold_clamps_sample_before_folding():
    # a 5-minute outlier sample is clamped to MAX_MS before folding
    assert fold(10000, 300000) == 0.3 * MAX_MS + 0.7 * 10000


import pytest
from server.db import init_db
from server.sessions import compaction_stats as cs


@pytest.mark.asyncio
async def test_record_and_estimate_roundtrip(tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    assert await cs.current_estimate() == cs.DEFAULT_ESTIMATE_MS  # cold
    await cs.record_duration(9000)
    assert await cs.current_estimate() == 9000                    # seeded
    await cs.record_duration(19000)
    assert await cs.current_estimate() == 0.3 * 19000 + 0.7 * 9000
