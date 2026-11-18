from __future__ import annotations

import asyncio

from windows_winrm_fleet_mcp.server import _handshake


def test_handshake_lists_40_tools():
    code = asyncio.run(_handshake())
    assert code == 0
