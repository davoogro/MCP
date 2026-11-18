"""Prueba en vivo opcional contra wsl.exe."""

from __future__ import annotations

import shutil

import pytest

from wsl_mcp import ops

pytestmark = pytest.mark.wsl


@pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="wsl.exe no disponible")
def test_list_distros_live():
    out = ops.op_list_distros()
    assert out["ok"] is True
    assert out["count"] >= 0
