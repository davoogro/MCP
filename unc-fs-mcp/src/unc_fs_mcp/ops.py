"""Operaciones de filesystem (UNC/local) con timeouts en hilos."""

from __future__ import annotations

import heapq
import os
import re
import stat
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from unc_fs_mcp.config import add_runtime_root, get_settings, merged_roots, settings_generation
from unc_fs_mcp import documents as doc_ops
from unc_fs_mcp.util import (
    PathError,
    ResolvedPath,
    current_user,
    decode_bytes,
    detect_encoding,
    extension_of,
    glob_match,
    human_size,
    looks_binary,
    parse_datetime,
    parse_size,
    redact_secrets_text,
    resolve_path,
    should_redact_filename,
    to_iso_local,
)

_executor: Optional[ThreadPoolExecutor] = None
_settings_gen = -1

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_BASE64_MAX_BYTES = 2_097_152


class ToolError(Exception):
    def __init__(self, message: str, code: str = "IOError", **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor, _settings_gen
    gen = settings_generation()
    if _executor is None or gen != _settings_gen:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
        _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="unc-fs")
        _settings_gen = gen
    return _executor


def run_timed(fn: Callable[[], Any], timeout: Optional[float] = None) -> Any:
    settings = get_settings()
    limit = timeout if timeout is not None else settings.op_timeout
    future = _ensure_executor().submit(fn)
    try:
        return future.result(timeout=limit)
    except FuturesTimeout:
        future.cancel()
        raise ToolError(f"operación expiró tras {limit}s", "Timeout")
    except ToolError:
        raise
    except PathError as exc:
        raise ToolError(str(exc), exc.code) from exc
    except PermissionError as exc:
        raise ToolError(str(exc) or "acceso denegado", "AccessDenied") from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc) or "no encontrado", "NotFound") from exc
    except OSError as exc:
        if exc.errno == 2:
            raise ToolError(str(exc), "NotFound") from exc
        if exc.errno in (13, 5):
            raise ToolError(str(exc), "AccessDenied") from exc
        raise ToolError(str(exc), "IOError") from exc
    except Exception as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}", "IOError") from exc


_SCAN_TRUNCATED_HINT = (
    "Escaneo incompleto por tiempo o límite de entradas; acote path, pattern o max_depth."
)


@dataclass
class _ScanState:
    entries_seen: int = 0
    scan_truncated: bool = False
    timed_out: bool = False


def run_scan(fn: Callable[[], Any]) -> Any:
    """Ejecuta un escaneo con tope interno (UNC_SCAN_TIMEOUT); no falla por timeout externo."""
    future = _ensure_executor().submit(fn)
    try:
        return future.result()
    except ToolError:
        raise
    except PathError as exc:
        raise ToolError(str(exc), exc.code) from exc
    except PermissionError as exc:
        raise ToolError(str(exc) or "acceso denegado", "AccessDenied") from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc) or "no encontrado", "NotFound") from exc
    except OSError as exc:
        if exc.errno == 2:
            raise ToolError(str(exc), "NotFound") from exc
        if exc.errno in (13, 5):
            raise ToolError(str(exc), "AccessDenied") from exc
        raise ToolError(str(exc), "IOError") from exc
    except Exception as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}", "IOError") from exc


def _scan_meta(state: _ScanState) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "scanned_entries": state.entries_seen,
        "entries_scanned": state.entries_seen,
        "scan_truncated": state.scan_truncated,
    }
    if state.scan_truncated or state.timed_out:
        meta["hint"] = _SCAN_TRUNCATED_HINT
    return meta


def _resolved(path: Optional[str], root: Optional[str] = None) -> ResolvedPath:
    try:
        return resolve_path(path, root=root)
    except PathError as exc:
        raise ToolError(str(exc), exc.code) from exc


def _map_os_error(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except PathError as exc:
        raise ToolError(str(exc), exc.code) from exc
    except PermissionError as exc:
        raise ToolError(str(exc) or "acceso denegado", "AccessDenied") from exc
    except FileNotFoundError as exc:
        raise ToolError(str(exc) or "no encontrado", "NotFound") from exc
    except OSError as exc:
        if exc.errno == 2:
            raise ToolError(str(exc), "NotFound") from exc
        if exc.errno in (13, 5):
            raise ToolError(str(exc), "AccessDenied") from exc
        raise ToolError(str(exc), "IOError") from exc


def _entry_type(entry: os.DirEntry) -> str:
    try:
        if entry.is_symlink():
            return "link"
        if entry.is_dir(follow_symlinks=False):
            return "dir"
    except OSError:
        pass
    return "file"


def _stat_times(st: os.stat_result) -> dict[str, Optional[str]]:
    return {
        "created": to_iso_local(getattr(st, "st_birthtime", None) or st.st_ctime),
        "modified": to_iso_local(st.st_mtime),
        "accessed": to_iso_local(st.st_atime),
    }


def _win_attrs(st: os.stat_result) -> dict[str, bool]:
    attrs = getattr(st, "st_file_attributes", 0) or 0
    return {
        "readonly": bool(attrs & FILE_ATTRIBUTE_READONLY),
        "hidden": bool(attrs & FILE_ATTRIBUTE_HIDDEN),
    }


def list_roots() -> dict[str, Any]:
    settings = get_settings()

    def probe(alias: str, root_path: str) -> tuple[str, str, bool]:
        def check() -> bool:
            return os.path.isdir(root_path)

        try:
            ok = run_timed(check, timeout=5.0)
            return alias, root_path, bool(ok)
        except ToolError:
            return alias, root_path, False

    items = []
    for alias, root_path in merged_roots().items():
        a, p, reachable = probe(alias, root_path)
        items.append(
            {
                "alias": a,
                "path": p,
                "reachable": reachable,
                "default": a == settings.default_root,
            }
        )
    return {
        "roots": items,
        "default_root": settings.default_root,
        "allow_write": settings.allow_write,
        "allowlist_only": settings.allowlist_only,
        "allow_any_unc": settings.allow_any_unc,
        "allow_local": settings.allow_local,
    }


def add_root(alias: str, unc_path: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        try:
            roots = add_runtime_root(alias, unc_path)
        except ValueError as exc:
            raise ToolError(str(exc), "InvalidPath") from exc
        return {"alias": alias.strip(), "path": roots[alias.strip()], "roots": roots}

    return run_timed(work)


def test_access(path: Optional[str] = None) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        p = res.path
        t0 = time.perf_counter()
        exists = os.path.exists(p)
        is_dir = os.path.isdir(p) if exists else False
        is_file = os.path.isfile(p) if exists else False
        can_list = os.access(p, os.R_OK) if exists else False
        if is_dir and exists:
            try:
                with os.scandir(p) as it:
                    next(iter(it), None)
                can_list = True
            except OSError:
                can_list = False
        can_write = os.access(p, os.W_OK) if exists else os.access(os.path.dirname(p) or p, os.W_OK)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "path": res.path,
            "display": res.display,
            "exists": exists,
            "is_dir": is_dir,
            "is_file": is_file,
            "can_list": can_list,
            "can_write": bool(can_write),
            "response_ms": elapsed_ms,
            "user": current_user(),
        }

    return run_timed(work)


def list_directory(
    path: Optional[str] = None,
    pattern: str = "*",
    include_hidden: bool = False,
    sort: str = "name",
    desc: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isdir(res.path):
            raise ToolError("no es un directorio", "NotFound" if not os.path.exists(res.path) else "InvalidPath")
        entries: list[dict[str, Any]] = []
        total = 0
        files_count = 0
        dirs_count = 0
        bytes_sum = 0
        with os.scandir(res.path) as scan:
            for entry in scan:
                name = entry.name
                if not include_hidden and name.startswith("."):
                    continue
                if not glob_match(name, pattern):
                    continue
                total += 1
                try:
                    st = entry.stat(follow_symlinks=False)
                    size = st.st_size if entry.is_file(follow_symlinks=False) else 0
                except OSError:
                    size = 0
                etype = _entry_type(entry)
                if etype == "file":
                    files_count += 1
                    bytes_sum += size
                elif etype == "dir":
                    dirs_count += 1
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    mtime = 0
                entries.append(
                    {
                        "name": name,
                        "type": etype,
                        "size": size,
                        "size_human": human_size(size),
                        "modified": to_iso_local(mtime),
                    }
                )
        sort_key = sort.lower()
        if sort_key == "size":
            entries.sort(key=lambda e: e["size"], reverse=desc)
        elif sort_key == "modified":
            entries.sort(key=lambda e: e.get("modified") or "", reverse=desc)
        else:
            entries.sort(key=lambda e: e["name"].lower(), reverse=desc)
        truncated = total > limit
        returned = entries[: max(0, limit)]
        return {
            "path": res.path,
            "display": res.display,
            "entries": returned,
            "total": total,
            "returned": len(returned),
            "truncated": truncated,
            "files": files_count,
            "dirs": dirs_count,
            "bytes_listed": bytes_sum,
        }

    return run_timed(work)


def file_info(path: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.exists(res.path):
            raise ToolError("no encontrado", "NotFound")
        st = os.stat(res.path)
        is_dir = stat.S_ISDIR(st.st_mode)
        is_link = stat.S_ISLNK(st.st_mode)
        etype = "dir" if is_dir else ("link" if is_link else "file")
        info: dict[str, Any] = {
            "path": res.path,
            "display": res.display,
            "name": os.path.basename(res.path),
            "type": etype,
            "size": st.st_size,
            "size_human": human_size(st.st_size),
            "extension": extension_of(os.path.basename(res.path)),
            **_stat_times(st),
            **_win_attrs(st),
        }
        settings = get_settings()
        if etype == "file" and st.st_size <= settings.max_read_bytes:
            with open(res.path, "rb") as fh:
                sample = fh.read(min(st.st_size, settings.max_read_bytes))
            if not looks_binary(sample[:8192]):
                enc = detect_encoding(sample[: min(len(sample), 65536)])
                text, _ = decode_bytes(sample, enc if enc != "utf-8-sig" else None)
                info["encoding"] = enc
                info["line_count"] = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        return info

    return run_timed(work)


def read_file(
    path: str,
    offset: int = 1,
    limit: int = 500,
    encoding: Optional[str] = None,
    as_hex: bool = False,
    byte_offset: int = 0,
    length: Optional[int] = None,
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isfile(res.path):
            raise ToolError("no es un archivo", "NotFound" if not os.path.exists(res.path) else "InvalidPath")
        settings = get_settings()
        size = os.path.getsize(res.path)
        name = os.path.basename(res.path)
        b_off = max(0, byte_offset)
        byte_mode = length is not None or b_off > 0 or (encoding or "").lower() == "base64"

        def read_slice(max_bytes: int) -> bytes:
            cap = min(max_bytes, max(0, size - b_off))
            if cap <= 0:
                return b""
            with open(res.path, "rb") as fh:
                fh.seek(b_off)
                return fh.read(cap)

        if encoding and encoding.lower() == "base64":
            want = min(length or _BASE64_MAX_BYTES, _BASE64_MAX_BYTES)
            raw = read_slice(want)
            import base64

            return {
                "path": res.path,
                "display": res.display,
                "encoding": "base64",
                "content": base64.standard_b64encode(raw).decode("ascii"),
                "bytes": len(raw),
                "byte_offset": b_off,
                "total_size": size,
                "truncated": b_off + len(raw) < size,
            }

        if as_hex:
            raw = read_slice(min(length or 512, settings.max_read_bytes))
            return {
                "path": res.path,
                "display": res.display,
                "encoding": "hex",
                "content": raw.hex(),
                "bytes": len(raw),
                "byte_offset": b_off,
                "total_size": size,
            }

        probe = read_slice(min(8192, settings.max_read_bytes))
        is_bin = looks_binary(probe[:8192])

        if byte_mode:
            want = length if length is not None else settings.max_read_bytes
            want = min(max(1, want), settings.max_read_bytes)
            raw = read_slice(want)
            if is_bin:
                return {
                    "path": res.path,
                    "display": res.display,
                    "binary": True,
                    "total_size": size,
                    "byte_offset": b_off,
                    "bytes_read": len(raw),
                    "extension": extension_of(name),
                    "hint": "Archivo binario; use encoding='base64' (máx. 2 MB) o as_hex=true.",
                }
            text, enc_used = decode_bytes(raw, encoding)
            if settings.redact_secrets and should_redact_filename(name):
                text, red = redact_secrets_text(text)
            else:
                red = False
            out: dict[str, Any] = {
                "path": res.path,
                "display": res.display,
                "encoding": enc_used,
                "byte_offset": b_off,
                "length": len(raw),
                "content": text,
                "total_size": size,
                "truncated": b_off + len(raw) < size,
            }
            if red:
                out["redacted"] = True
            return out

        raw = read_slice(settings.max_read_bytes)
        if is_bin:
            return {
                "path": res.path,
                "display": res.display,
                "binary": True,
                "total_size": size,
                "extension": extension_of(name),
                "hint": "Archivo binario; use encoding='base64' (máx. 2 MB) o as_hex=true.",
            }
        text, enc_used = decode_bytes(raw, encoding)
        if settings.redact_secrets and should_redact_filename(name):
            text, red = redact_secrets_text(text)
        else:
            red = False
        lines = text.splitlines()
        total_lines = len(lines)
        start = max(1, offset) - 1
        end = start + max(0, limit)
        slice_lines = lines[start:end]
        truncated = end < total_lines or size > len(raw)
        out = {
            "path": res.path,
            "display": res.display,
            "encoding": enc_used,
            "offset": offset,
            "limit": limit,
            "lines": slice_lines,
            "returned_lines": len(slice_lines),
            "total_lines": total_lines,
            "truncated": truncated,
            "file_bytes_read": len(raw),
            "total_size": size,
        }
        if red:
            out["redacted"] = True
        return out

    return run_timed(work)


def tail_file(path: str, lines: int = 100) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isfile(res.path):
            raise ToolError("no es un archivo", "NotFound")
        settings = get_settings()
        size = os.path.getsize(res.path)
        block = 8192
        wanted = max(1, lines)
        with open(res.path, "rb") as fh:
            pos = size
            buffer = b""
            while pos > 0 and buffer.count(b"\n") <= wanted:
                read_size = min(block, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                buffer = chunk + buffer
            raw = buffer
        if looks_binary(raw[:8192]):
            raise ToolError("archivo binario", "Binary")
        text, enc = decode_bytes(raw)
        if settings.redact_secrets and should_redact_filename(os.path.basename(res.path)):
            text, red = redact_secrets_text(text)
        else:
            red = False
        all_lines = text.splitlines()
        tail_lines = all_lines[-wanted:]
        out: dict[str, Any] = {
            "path": res.path,
            "display": res.display,
            "encoding": enc,
            "lines": tail_lines,
            "returned_lines": len(tail_lines),
            "requested_lines": wanted,
        }
        if red:
            out["redacted"] = True
        return out

    return run_timed(work)


def _iter_scan(
    base: str,
    recursive: bool,
    max_depth: int,
    state: _ScanState,
    deadline: float,
) -> Iterator[tuple[str, os.DirEntry, int]]:
    settings = get_settings()
    stack: list[tuple[str, int]] = [(base, 0)]
    while stack:
        if time.monotonic() > deadline:
            state.timed_out = True
            state.scan_truncated = True
            return
        current, depth = stack.pop()
        try:
            with os.scandir(current) as scan:
                for entry in scan:
                    if time.monotonic() > deadline:
                        state.timed_out = True
                        state.scan_truncated = True
                        return
                    state.entries_seen += 1
                    if state.entries_seen > settings.max_scan_entries:
                        state.scan_truncated = True
                        return
                    child_path = entry.path
                    yield child_path, entry, depth
                    if recursive and entry.is_dir(follow_symlinks=False) and depth < max_depth:
                        stack.append((child_path, depth + 1))
        except OSError:
            continue


def find_files(
    path: Optional[str] = None,
    pattern: str = "*",
    recursive: bool = True,
    max_depth: int = 10,
    type: str = "any",
    min_size: Optional[Any] = None,
    max_size: Optional[Any] = None,
    modified_after: Optional[str] = None,
    modified_before: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isdir(res.path):
            raise ToolError("no es un directorio", "NotFound")
        settings = get_settings()
        min_b = parse_size(min_size) if min_size is not None else None
        max_b = parse_size(max_size) if max_size is not None else None
        after = parse_datetime(modified_after) if modified_after else None
        before = parse_datetime(modified_before) if modified_before else None
        state = _ScanState()
        deadline = time.monotonic() + settings.scan_timeout
        matches: list[dict[str, Any]] = []
        type_f = type.lower()
        for abspath, entry, _depth in _iter_scan(res.path, recursive, max_depth, state, deadline):
            name = entry.name
            if not glob_match(name, pattern):
                continue
            etype = _entry_type(entry)
            if type_f == "file" and etype != "file":
                continue
            if type_f == "dir" and etype != "dir":
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if etype == "file":
                if min_b is not None and st.st_size < min_b:
                    continue
                if max_b is not None and st.st_size > max_b:
                    continue
            if after or before:
                mdt = datetime_from_timestamp(st.st_mtime)
                if after and mdt < after:
                    continue
                if before and mdt > before:
                    continue
            rel = os.path.relpath(abspath, res.path)
            matches.append(
                {
                    "path": rel,
                    "name": name,
                    "type": etype,
                    "size": st.st_size if etype == "file" else 0,
                    "size_human": human_size(st.st_size) if etype == "file" else human_size(0),
                    "modified": to_iso_local(st.st_mtime),
                }
            )
            if len(matches) >= limit and not state.scan_truncated:
                break
        out = {
            "path": res.path,
            "display": res.display,
            "matches": matches[:limit],
            "matched": len(matches),
            "returned": min(len(matches), limit),
            "truncated": len(matches) > limit,
        }
        out.update(_scan_meta(state))
        return out

    return run_scan(work)


def datetime_from_timestamp(ts: float):
    from datetime import datetime

    return datetime.fromtimestamp(ts).astimezone()


def grep_text(
    path: str,
    pattern: str,
    regex: bool = False,
    ignore_case: bool = True,
    file_pattern: str = "*",
    recursive: bool = True,
    context: int = 0,
    max_matches: int = 200,
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        settings = get_settings()
        flags = re.MULTILINE
        if ignore_case:
            flags |= re.IGNORECASE
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), flags) if regex else None
        except re.error as exc:
            raise ToolError(f"patrón inválido: {exc}", "InvalidPath") from exc

        def line_matches(line: str) -> bool:
            if regex:
                return rx.search(line) is not None  # type: ignore[union-attr]
            if ignore_case:
                return pattern.lower() in line.lower()
            return pattern in line

        skipped: list[dict[str, str]] = []

        def rel_path(file_path: str) -> str:
            if os.path.isdir(res.path):
                return os.path.relpath(file_path, res.path)
            return os.path.basename(file_path)

        def search_file(file_path: str) -> list[dict[str, Any]]:
            if not glob_match(os.path.basename(file_path), file_pattern):
                return []
            rel = rel_path(file_path)
            try:
                with open(file_path, "rb") as fh:
                    head = fh.read(8192)
                    if looks_binary(head):
                        skipped.append({"path": rel, "reason": "binary"})
                        return []
                    fh.seek(0)
                    line_no = 0
                    hits: list[dict[str, Any]] = []
                    while True:
                        raw_line = fh.readline()
                        if not raw_line:
                            break
                        line_no += 1
                        try:
                            line = raw_line.decode("utf-8")
                        except UnicodeDecodeError:
                            line, _ = decode_bytes(raw_line)
                        if settings.redact_secrets and should_redact_filename(os.path.basename(file_path)):
                            line, _ = redact_secrets_text(line)
                        stripped = line.rstrip("\r\n")
                        if line_matches(stripped):
                            hits.append(
                                {
                                    "file": rel,
                                    "line_no": line_no,
                                    "line": stripped,
                                    "before": [],
                                    "after": [],
                                }
                            )
                        if len(hits) >= max_matches:
                            break
                    return hits
            except OSError as exc:
                skipped.append({"path": rel, "reason": str(exc) or "IOError"})
                return []

        def fill_context(hits: list[dict[str, Any]], file_path: str) -> None:
            if context <= 0 or not hits:
                return
            try:
                with open(file_path, encoding="utf-8", errors="replace") as fh:
                    all_lines = fh.read().splitlines()
            except OSError:
                return
            for hit in hits:
                i = hit["line_no"]
                hit["before"] = all_lines[max(0, i - 1 - context) : i - 1]
                hit["after"] = all_lines[i : min(len(all_lines), i + context)]

        results: list[dict[str, Any]] = []
        if os.path.isfile(res.path):
            file_hits = search_file(res.path)
            if context:
                fill_context(file_hits, res.path)
            results.extend(file_hits)
        elif os.path.isdir(res.path):
            state = _ScanState()
            deadline = time.monotonic() + settings.scan_timeout
            for abspath, entry, _ in _iter_scan(res.path, recursive, 50, state, deadline):
                if time.monotonic() > deadline:
                    state.timed_out = True
                    state.scan_truncated = True
                    break
                if not entry.is_file(follow_symlinks=False):
                    continue
                batch = search_file(abspath)
                if context:
                    fill_context(batch, abspath)
                results.extend(batch)
                if len(results) >= max_matches:
                    break
        else:
            raise ToolError("no encontrado", "NotFound")
        out = {
            "path": res.path,
            "display": res.display,
            "matches": results[:max_matches],
            "match_count": len(results),
            "truncated": len(results) > max_matches,
            "skipped": skipped,
        }
        if os.path.isdir(res.path):
            out.update(_scan_meta(state))
        return out

    return run_scan(work)


def dir_stats(path: Optional[str] = None, max_depth: Optional[int] = None, top: int = 20) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isdir(res.path):
            raise ToolError("no es un directorio", "NotFound")
        settings = get_settings()
        state = _ScanState()
        deadline = time.monotonic() + settings.scan_timeout
        depth_limit = max_depth if max_depth is not None else 10_000
        total_files = 0
        total_dirs = 0
        total_bytes = 0
        by_ext: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        largest: list[tuple[int, str, float]] = []
        subdir_bytes: dict[str, int] = defaultdict(int)
        newest: Optional[tuple[float, str]] = None
        oldest: Optional[tuple[float, str]] = None

        for abspath, entry, depth in _iter_scan(res.path, True, depth_limit, state, deadline):
            etype = _entry_type(entry)
            if etype == "dir":
                total_dirs += 1
                continue
            if etype != "file":
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            total_files += 1
            total_bytes += st.st_size
            ext = extension_of(entry.name) or "(sin ext)"
            by_ext[ext][0] += 1
            by_ext[ext][1] += st.st_size
            largest.append((st.st_size, abspath, st.st_mtime))
            rel = os.path.relpath(abspath, res.path)
            top_dir = rel.split(os.sep)[0] if os.sep in rel else "."
            subdir_bytes[top_dir] += st.st_size
            if newest is None or st.st_mtime > newest[0]:
                newest = (st.st_mtime, abspath)
            if oldest is None or st.st_mtime < oldest[0]:
                oldest = (st.st_mtime, abspath)

        largest.sort(key=lambda x: x[0], reverse=True)
        top_files = [
            {
                "path": os.path.relpath(p, res.path),
                "size": sz,
                "size_human": human_size(sz),
                "modified": to_iso_local(mt),
            }
            for sz, p, mt in largest[:top]
        ]
        sub_sorted = sorted(subdir_bytes.items(), key=lambda x: x[1], reverse=True)[:top]
        ext_breakdown = [
            {"extension": k, "count": v[0], "bytes": v[1], "size_human": human_size(v[1])}
            for k, v in sorted(by_ext.items(), key=lambda x: x[1][1], reverse=True)[:top]
        ]
        out: dict[str, Any] = {
            "path": res.path,
            "display": res.display,
            "total_files": total_files,
            "total_dirs": total_dirs,
            "total_bytes": total_bytes,
            "total_size_human": human_size(total_bytes),
            "by_extension": ext_breakdown,
            "largest_files": top_files,
            "top_subdirs": [
                {"name": name, "bytes": b, "size_human": human_size(b)} for name, b in sub_sorted
            ],
        }
        out.update(_scan_meta(state))
        if newest:
            out["newest"] = {"path": os.path.relpath(newest[1], res.path), "modified": to_iso_local(newest[0])}
        if oldest:
            out["oldest"] = {"path": os.path.relpath(oldest[1], res.path), "modified": to_iso_local(oldest[0])}
        return out

    return run_scan(work)


def write_file(
    path: str,
    content: str,
    mode: str = "create",
    encoding: str = "utf-8",
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        settings = get_settings()
        if not settings.allow_write:
            raise ToolError("escritura deshabilitada", "NotAllowed")
        res = _resolved(path)
        data = content.encode(encoding)
        if len(data) > settings.max_write_bytes:
            raise ToolError("contenido demasiado grande", "InvalidPath")
        exists = os.path.exists(res.path)
        m = mode.lower()
        if m == "create":
            if exists:
                raise ToolError("el archivo ya existe", "IOError")
            flags = "xb"
        elif m == "overwrite":
            flags = "wb"
        elif m == "append":
            flags = "ab"
        else:
            raise ToolError(f"modo inválido: {mode}", "InvalidPath")
        os.makedirs(os.path.dirname(res.path) or ".", exist_ok=True)
        with open(res.path, flags) as fh:
            fh.write(data)
        return {"path": res.path, "display": res.display, "bytes_written": len(data), "mode": m}

    return run_timed(work)


def make_directory(path: str, parents: bool = True) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        settings = get_settings()
        if not settings.allow_write:
            raise ToolError("escritura deshabilitada", "NotAllowed")
        res = _resolved(path)
        os.makedirs(res.path, exist_ok=parents)
        return {"path": res.path, "display": res.display, "created": True}

    return run_timed(work)


def move_file(src: str, dst: str) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        import shutil

        settings = get_settings()
        if not settings.allow_write:
            raise ToolError("escritura deshabilitada", "NotAllowed")
        s = _resolved(src)
        d = _resolved(dst)
        if not os.path.exists(s.path):
            raise ToolError("origen no encontrado", "NotFound")
        if os.path.exists(d.path):
            raise ToolError("destino ya existe", "IOError")
        os.makedirs(os.path.dirname(d.path) or ".", exist_ok=True)
        shutil.move(s.path, d.path)
        size = os.path.getsize(d.path) if os.path.isfile(d.path) else 0
        return {"src": s.path, "dst": d.path, "bytes": size, "size_human": human_size(size)}

    return run_timed(work)


def copy_file(src: str, dst: str, overwrite: bool = False) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        import shutil

        settings = get_settings()
        if not settings.allow_write:
            raise ToolError("escritura deshabilitada", "NotAllowed")
        s = _resolved(src)
        d = _resolved(dst)
        if not os.path.isfile(s.path):
            raise ToolError("origen no es archivo", "NotFound")
        if os.path.exists(d.path) and not overwrite:
            raise ToolError("destino ya existe", "IOError")
        os.makedirs(os.path.dirname(d.path) or ".", exist_ok=True)
        shutil.copy2(s.path, d.path)
        size = os.path.getsize(d.path)
        return {
            "src": s.path,
            "dst": d.path,
            "bytes": size,
            "size_human": human_size(size),
            "overwrite": overwrite,
        }

    return run_timed(work)


def read_document(path: str, sheet: Optional[str] = None, max_rows: int = 200) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isfile(res.path):
            raise ToolError("no es un archivo", "NotFound")
        try:
            data = doc_ops.read_document(res.path, sheet=sheet, max_rows=max_rows)
        except ValueError as exc:
            raise ToolError(str(exc), "InvalidPath") from exc
        data["path"] = res.path
        data["display"] = res.display
        return data

    return run_timed(work)


def _dir_index(
    base: str,
    recursive: bool,
    deadline: float,
    state: _ScanState,
    max_depth: int = 10_000,
) -> dict[str, tuple[int, float]]:
    """Mapa ruta relativa -> (size, mtime). Solo archivos."""
    index: dict[str, tuple[int, float]] = {}
    for abspath, entry, _depth in _iter_scan(base, recursive, max_depth, state, deadline):
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        rel = os.path.relpath(abspath, base)
        index[rel.replace("\\", "/")] = (st.st_size, st.st_mtime)
    return index


def compare_dirs(path_a: str, path_b: str, recursive: bool = True) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        a = _resolved(path_a)
        b = _resolved(path_b)
        if not os.path.isdir(a.path):
            raise ToolError("path_a no es directorio", "NotFound")
        if not os.path.isdir(b.path):
            raise ToolError("path_b no es directorio", "NotFound")
        settings = get_settings()
        deadline = time.monotonic() + settings.scan_timeout
        state_a = _ScanState()
        state_b = _ScanState()
        idx_a = _dir_index(a.path, recursive, deadline, state_a)
        idx_b = _dir_index(b.path, recursive, deadline, state_b)
        set_a = set(idx_a)
        set_b = set(idx_b)
        only_a = sorted(set_a - set_b)
        only_b = sorted(set_b - set_a)
        different: list[dict[str, Any]] = []
        for key in sorted(set_a & set_b):
            sa, ma = idx_a[key]
            sb, mb = idx_b[key]
            if sa != sb or abs(ma - mb) > 1.0:
                different.append(
                    {
                        "path": key,
                        "size_a": sa,
                        "size_b": sb,
                        "modified_a": to_iso_local(ma),
                        "modified_b": to_iso_local(mb),
                    }
                )
        combined = _ScanState(
            entries_seen=state_a.entries_seen + state_b.entries_seen,
            scan_truncated=state_a.scan_truncated or state_b.scan_truncated,
            timed_out=state_a.timed_out or state_b.timed_out,
        )
        out = {
            "path_a": a.path,
            "path_b": b.path,
            "only_in_a": only_a,
            "only_in_b": only_b,
            "different": different,
            "count_only_a": len(only_a),
            "count_only_b": len(only_b),
            "count_different": len(different),
        }
        out.update(_scan_meta(combined))
        return out

    return run_scan(work)


def recent_files(
    path: Optional[str] = None,
    hours: float = 24,
    pattern: str = "*",
    max_results: int = 200,
    max_depth: Optional[int] = None,
) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        res = _resolved(path)
        if not os.path.isdir(res.path):
            raise ToolError("no es un directorio", "NotFound")
        settings = get_settings()
        cutoff = time.time() - max(0.0, hours) * 3600.0
        state = _ScanState()
        deadline = time.monotonic() + settings.scan_timeout
        depth_limit = max_depth if max_depth is not None else 10_000
        cap = max(1, max_results)
        heap: list[tuple[float, int, dict[str, Any]]] = []
        tie = 0
        matched = 0
        for abspath, entry, _ in _iter_scan(res.path, True, depth_limit, state, deadline):
            if not entry.is_file(follow_symlinks=False):
                continue
            if not glob_match(entry.name, pattern):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            matched += 1
            rel = os.path.relpath(abspath, res.path)
            item = {
                "path": rel.replace("\\", "/"),
                "name": entry.name,
                "size": st.st_size,
                "size_human": human_size(st.st_size),
                "modified": to_iso_local(st.st_mtime),
            }
            tie += 1
            if len(heap) < cap:
                heapq.heappush(heap, (st.st_mtime, tie, item))
            elif st.st_mtime > heap[0][0]:
                heapq.heapreplace(heap, (st.st_mtime, tie, item))
        items = [pair[2] for pair in sorted(heap, key=lambda x: (x[0], x[1]), reverse=True)]
        out = {
            "path": res.path,
            "display": res.display,
            "hours": hours,
            "max_depth": depth_limit if max_depth is not None else None,
            "files": items,
            "returned": len(items),
            "matched": matched,
            "truncated": matched > cap,
        }
        out.update(_scan_meta(state))
        return out

    return run_scan(work)
