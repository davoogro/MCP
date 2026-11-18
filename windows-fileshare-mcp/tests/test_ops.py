"""Tests de operaciones con tmp_path (sin red)."""

from __future__ import annotations

import os
import time

import pytest

from windows_fileshare_mcp import server
from windows_fileshare_mcp.config import reload_settings
from windows_fileshare_mcp import ops
from windows_fileshare_mcp.ops import (
    ToolError,
    add_root,
    compare_dirs,
    dir_stats,
    find_files,
    grep_text,
    list_directory,
    read_document,
    read_file,
    recent_files,
    run_timed,
    tail_file,
)

@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("UNC_ROOTS", f"test={tmp_path}")
    monkeypatch.setenv("UNC_DEFAULT_ROOT", "test")
    monkeypatch.setenv("UNC_ALLOW_LOCAL", "true")
    monkeypatch.setenv("UNC_ALLOW_WRITE", "false")
    monkeypatch.setenv("UNC_OP_TIMEOUT", "5")
    monkeypatch.setenv("UNC_SCAN_TIMEOUT", "5")
    reload_settings()
    yield tmp_path


def test_list_directory_sort_limit(_env):
    (_env / "b.txt").write_text("bb")
    (_env / "a.txt").write_text("aaa")
    (_env / "c.txt").write_text("c")
    out = list_directory(path="test:", sort="size", desc=True, limit=2)
    assert out["total"] == 3
    assert out["truncated"] is True
    assert out["returned"] == 2
    assert out["entries"][0]["size"] >= out["entries"][1]["size"]


def test_read_file_utf8_sig(_env):
    p = _env / "sig.txt"
    p.write_bytes(b"\xef\xbb\xbfline1\nline2\nline3\n")
    out = read_file(path=str(p), offset=2, limit=1)
    assert out["lines"] == ["line2"]
    assert out["total_lines"] == 3


def test_read_file_cp1252(_env):
    p = _env / "win.txt"
    p.write_bytes("café\n".encode("cp1252"))
    out = read_file(path=str(p))
    assert "caf" in out["lines"][0]


def test_read_file_binary(_env):
    p = _env / "bin.dat"
    p.write_bytes(b"\x00\x01\x02")
    out = server.read_file(path=str(p))
    assert out["ok"] is True
    assert out.get("binary") is True
    assert "hint" in out
    out2 = read_file(path=str(p), as_hex=True)
    assert "content" in out2


def test_read_file_byte_offset(_env):
    p = _env / "bytes.txt"
    p.write_bytes(b"abcdefgh")
    out = read_file(path=str(p), byte_offset=2, length=3)
    assert out["content"] == "cde"


def test_tail_large_file(_env, monkeypatch):
    p = _env / "big.log"
    with open(p, "wb") as fh:
        for _ in range(200):
            fh.write(b"x" * 100 + b"\n")
        fh.write(b"last line\n")
    monkeypatch.setenv("UNC_MAX_READ_BYTES", "100")
    reload_settings()
    out = tail_file(path=str(p), lines=1)
    assert out["lines"] == ["last line"]


def test_grep_large_file(_env):
    p = _env / "big.txt"
    with open(p, "wb") as fh:
        for _ in range(5000):
            fh.write(b"noise\n")
        fh.write(b"needle here\n")
    out = grep_text(path=str(p), pattern="needle")
    assert out["match_count"] == 1
    assert out["skipped"] == []


def test_grep_skips_binary(_env):
    p = _env / "bin.dat"
    p.write_bytes(b"\x00\x01")
    out = grep_text(path=str(p), pattern="x")
    assert out["matches"] == []
    assert out["skipped"][0]["reason"] == "binary"


def test_add_root(_env):
    extra = _env / "extra"
    extra.mkdir()
    out = add_root(alias="extra", unc_path=str(extra))
    assert "extra" in out["roots"]
    out2 = list_directory(path="extra:")
    assert out2["total"] >= 0


def test_compare_dirs(_env):
    a = _env / "a"
    b = _env / "b"
    a.mkdir()
    b.mkdir()
    (a / "only.txt").write_text("1")
    (a / "both.txt").write_text("same")
    (b / "both.txt").write_text("different-size")
    out = compare_dirs(path_a=str(a), path_b=str(b))
    assert "only.txt" in out["only_in_a"]
    assert out["count_different"] == 1


def test_recent_files(_env):
    p = _env / "fresh.log"
    p.write_text("new")
    out = recent_files(path="test:", hours=1, pattern="*.log")
    assert out["returned"] >= 1
    assert out["files"][0]["name"] == "fresh.log"


def test_read_document_csv(_env):
    p = _env / "data.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    out = read_document(path=str(p))
    assert out["format"] == "csv"
    assert out["rows"][0] == ["a", "b"]


def test_tail_file(_env):
    p = _env / "log.txt"
    p.write_text("\n".join(f"line{i}" for i in range(50)))
    out = tail_file(path=str(p), lines=3)
    assert out["lines"] == ["line47", "line48", "line49"]


def test_find_files_filters(_env):
    sub = _env / "sub"
    sub.mkdir()
    f1 = sub / "a.log"
    f1.write_text("x")
    f2 = sub / "b.txt"
    f2.write_text("y")
    os.utime(f1, (time.time(), time.time()))
    out = find_files(path="test:", pattern="*.log", type="file", limit=10)
    assert out["matched"] == 1
    assert out["matches"][0]["name"] == "a.log"


def test_grep_context(_env):
    p = _env / "g.txt"
    p.write_text("one\ntwo\nthree\n")
    out = grep_text(path=str(p), pattern="two", context=1)
    assert out["matches"][0]["before"] == ["one"]
    assert out["matches"][0]["after"] == ["three"]


def test_dir_stats(_env):
    (_env / "a.dat").write_bytes(b"x" * 100)
    (_env / "b.dat").write_bytes(b"y" * 200)
    out = dir_stats(path="test:", top=5)
    assert out["total_files"] == 2
    assert out["total_bytes"] == 300


def test_read_redact(_env, monkeypatch):
    monkeypatch.setenv("UNC_REDACT_SECRETS", "true")
    reload_settings()
    p = _env / "app.env"
    p.write_text("API_TOKEN=abc\nOK=1\n")
    out = read_file(path=str(p))
    assert out.get("redacted") is True
    assert "abc" not in "\n".join(out["lines"])


def test_scan_timeout_returns_partial(monkeypatch, _env):
    """Escaneo lento: al vencer UNC_SCAN_TIMEOUT devuelve parcial, no error Timeout."""
    monkeypatch.setenv("UNC_SCAN_TIMEOUT", "1")
    reload_settings()
    real_iter = ops._iter_scan

    def slow_iter(base, recursive, max_depth, state, deadline):
        for item in real_iter(base, recursive, max_depth, state, deadline):
            time.sleep(0.15)
            yield item

    monkeypatch.setattr(ops, "_iter_scan", slow_iter)
    for i in range(30):
        (_env / f"f{i}.txt").write_text("x")
    out = server.recent_files(path="test:", hours=720, max_results=3)
    assert out["ok"] is True
    assert out.get("scan_truncated") is True
    assert out.get("scanned_entries", 0) >= 1
    assert "hint" in out
    assert out["returned"] <= 3
    assert out.get("error_code") != "Timeout"


def test_recent_files_heap_keeps_newest(_env):
    sub = _env / "deep"
    sub.mkdir()
    now = time.time()
    for i, age in enumerate([10, 100, 50, 200, 5]):
        p = sub / f"file{i}.log"
        p.write_text("x")
        os.utime(p, (now - age, now - age))
    out = recent_files(path=str(sub), hours=720, pattern="*.log", max_results=2)
    assert out["returned"] == 2
    mtimes = [f["modified"] for f in out["files"]]
    assert mtimes == sorted(mtimes, reverse=True)


def test_timeout(monkeypatch):
    monkeypatch.setenv("UNC_OP_TIMEOUT", "1")
    reload_settings()

    def slow():
        time.sleep(3)
        return "done"

    with pytest.raises(ToolError) as exc:
        run_timed(slow, timeout=0.2)
    assert exc.value.code == "Timeout"


def test_write_tools_not_registered_by_default():
    import windows_fileshare_mcp.server as srv

    expected = 13
    assert srv._expected_tool_count() == expected


def test_write_ops_when_enabled(monkeypatch, _env):
    from windows_fileshare_mcp.ops import copy_file, make_directory, move_file, write_file

    monkeypatch.setenv("UNC_ALLOW_WRITE", "true")
    reload_settings()
    p = _env / "new.txt"
    out = write_file(path=str(p), content="hola", mode="create")
    assert out["bytes_written"] == 4
    assert p.read_text(encoding="utf-8") == "hola"
    d = _env / "nd"
    make_directory(path=str(d))
    assert d.is_dir()
    dst = _env / "copy.txt"
    copy_file(src=str(p), dst=str(dst))
    assert dst.read_text(encoding="utf-8") == "hola"
    moved = _env / "moved.txt"
    move_file(src=str(dst), dst=str(moved))
    assert moved.is_file()
    assert not dst.exists()
