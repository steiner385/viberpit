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

    # ensure_vnc() returns as soon as x11vnc is *spawned*; the process binds its
    # RFB port a moment later (~1s here), so an immediate connect races the bind
    # and gets ConnectionRefused. Poll until the banner arrives or we hit the
    # deadline — mirroring how a real VNC client would reconnect.
    async def _read_banner():
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.1)
                continue
            banner = await reader.read(12)
            if banner:
                return banner, writer
            writer.close()  # x11vnc accepted then dropped before ready — retry
            await asyncio.sleep(0.1)

    banner, writer = await asyncio.wait_for(_read_banner(), 10)
    assert banner.startswith(b"RFB "), f"expected RFB banner, got {banner!r}"
    writer.close()
    await mgr.stop_display("S")
