"""Tests de resolución de rutas y utilidades (sin red)."""

from __future__ import annotations

import os

import pytest

from unc_fs_mcp.config import reload_settings
from unc_fs_mcp.util import PathError, is_under_root, redact_secrets_text, resolve_path


@pytest.fixture(autouse=True)
def _local_roots(monkeypatch, tmp_path):
    root_a = tmp_path / "share"
    root_b = tmp_path / "share2"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "sub").mkdir()
    monkeypatch.setenv(
        "UNC_ROOTS",
        f"a={root_a};b={root_b}",
    )
    monkeypatch.setenv("UNC_DEFAULT_ROOT", "a")
    monkeypatch.setenv("UNC_ALLOW_LOCAL", "true")
    monkeypatch.setenv("UNC_ALLOWLIST_ONLY", "false")
    reload_settings()
    yield {"a": str(root_a), "b": str(root_b), "tmp": tmp_path}


def test_alias_and_relative():
    r = resolve_path("sub/file.txt")
    assert r.root_alias == "a"
    assert r.display == "a:sub/file.txt"
    assert r.path.endswith(os.path.join("share", "sub", "file.txt"))


def test_alias_prefix():
    r = resolve_path("a:sub")
    assert r.display == "a:sub"
    assert os.path.isdir(r.path)


def test_alias_only():
    r = resolve_path("a:")
    assert r.display == "a:"
    assert os.path.isdir(r.path)


def test_parent_escape():
    with pytest.raises(PathError) as exc:
        resolve_path("..")
    assert exc.value.code == "InvalidPath"


def test_allowlist_by_components(monkeypatch, _local_roots):
    monkeypatch.setenv("UNC_ALLOWLIST_ONLY", "true")
    reload_settings()
    r = resolve_path("a:sub")
    assert os.path.isdir(r.path)
    with pytest.raises(PathError) as exc:
        resolve_path(str(_local_roots["tmp"] / "outside"))
    assert exc.value.code == "NotAllowed"


def test_share_prefix_trap():
    assert not is_under_root(r"\\s\share2\file", r"\\s\share")


def test_device_path_rejected():
    with pytest.raises(PathError):
        resolve_path(r"\\?\C:\secret")


def test_slash_normalized(_local_roots):
    r = resolve_path("a:sub/file.txt")
    assert "sub" in r.path


def test_local_when_allowed(_local_roots):
    p = _local_roots["tmp"] / "local.txt"
    p.write_text("hola", encoding="utf-8")
    r = resolve_path(str(p))
    assert r.path == str(p)


def test_local_denied(monkeypatch, _local_roots):
    monkeypatch.setenv("UNC_ALLOW_LOCAL", "false")
    reload_settings()
    p = _local_roots["tmp"] / "x.txt"
    p.write_text("x")
    with pytest.raises(PathError) as exc:
        resolve_path(str(p))
    assert exc.value.code == "NotAllowed"


def test_unc_any_disabled(monkeypatch, _local_roots):
    monkeypatch.setenv("UNC_ALLOW_ANY_UNC", "false")
    monkeypatch.setenv("UNC_ALLOWLIST_ONLY", "false")
    reload_settings()
    with pytest.raises(PathError) as exc:
        resolve_path(r"\\other\share\file.txt")
    assert exc.value.code == "NotAllowed"
    r = resolve_path("a:sub")
    assert os.path.isdir(r.path)


def test_redact_env():
    text = "DB_PASSWORD=secret123\nHOST=localhost\n"
    out, changed = redact_secrets_text(text)
    assert changed
    assert "secret123" not in out
    assert "***" in out
    assert "HOST=localhost" in out
