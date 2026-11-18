"""Tests de utilidades (sin WSL)."""

from __future__ import annotations

from wsl_mcp.util import clamp_timeout, decode_output, linux_path, shell_path, truncate


def test_linux_path_windows_drive():
    assert linux_path(r"C:\foo\bar") == "/mnt/c/foo/bar"
    assert linux_path("C:/x") == "/mnt/c/x"


def test_linux_path_passthrough():
    assert linux_path("/home/k8s") == "/home/k8s"


def test_shell_path_tilde():
    assert shell_path("~") == '"$HOME"'
    assert shell_path("~/a").startswith('"$HOME"/')
    assert "a" in shell_path("~/a")


def test_truncate():
    text = "a" * 100
    out, was = truncate(text, 40)
    assert was
    assert "truncado" in out
    assert len(out) < 100


def test_decode_output_utf8():
    assert decode_output(b"hello") == "hello"


def test_clamp_timeout():
    assert clamp_timeout(None, 120, 3600) == 120
    assert clamp_timeout(0, 120, 3600) == 120
    assert clamp_timeout(5000, 120, 3600) == 3600
    assert clamp_timeout(30, 120, 3600) == 30
