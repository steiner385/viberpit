import asyncio
import pytest
from server.sessions.x11manager import DisplayAllocator, PoolExhausted

@pytest.mark.asyncio
async def test_alloc_release_roundtrip():
    a = DisplayAllocator(start=100, count=2, port_base=6100)
    l1 = await a.alloc(); l2 = await a.alloc()
    assert {l1.display, l2.display} == {100, 101}
    assert l1.rfbport == 6100 + (l1.display - 100)
    assert a.in_use() == 2
    await a.release(l1)
    assert a.in_use() == 1
    l3 = await a.alloc()
    assert l3.display == l1.display  # reused

@pytest.mark.asyncio
async def test_exhaustion_raises():
    a = DisplayAllocator(start=100, count=1, port_base=6100)
    await a.alloc()
    with pytest.raises(PoolExhausted):
        await a.alloc()

@pytest.mark.asyncio
async def test_no_double_alloc_under_concurrency():
    a = DisplayAllocator(start=100, count=50, port_base=6100)
    leases = await asyncio.gather(*[a.alloc() for _ in range(50)])
    assert len({lease.display for lease in leases}) == 50
