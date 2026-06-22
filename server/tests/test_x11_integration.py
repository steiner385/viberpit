"""Real-binary integration smoke test: Xvfb + x11vnc + RFB handshake.

Skips automatically when Xvfb or x11vnc are not installed on the host.
"""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not all(shutil.which(b) for b in ("Xvfb", "x11vnc")),
    reason="no Xvfb/x11vnc binaries installed",
)


@pytest.mark.asyncio
async def test_real_xvfb_x11vnc_rfb_handshake(tmp_path):
    """Start a real Xvfb+x11vnc, verify the RFB banner arrives."""
    import asyncio

    from server.sessions.x11manager import DisplayAllocator, X11Manager

    mgr = X11Manager(
        DisplayAllocator(190, 2, 6190),
        resolution="640x480x24",
        max_browsers=1,
    )
    spec = await mgr.start_display("S")
    assert spec is not None

    port = await mgr.ensure_vnc("S")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    banner = await asyncio.wait_for(reader.read(12), 5)
    assert banner.startswith(b"RFB "), f"expected RFB banner, got {banner!r}"
    writer.close()
    await mgr.stop_display("S")
