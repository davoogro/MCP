import json
from datetime import datetime, timezone

import pytest

from s3_mcp.config import load_env_files, reload_settings
from s3_mcp.util import (
    bound_response,
    group_label,
    human_size,
    looks_binary,
    mask_secret_id,
    parse_datetime,
    parse_s3_uri,
    parse_size,
)


def test_parse_s3_uri():
    assert parse_s3_uri("s3://my-bucket/path/to/file.txt") == ("my-bucket", "path/to/file.txt")
    assert parse_s3_uri("s3://my-bucket") == ("my-bucket", "")
    assert parse_s3_uri("s3://my-bucket/") == ("my-bucket", "")
    assert parse_s3_uri("s3://my-bucket/a/b/") == ("my-bucket", "a/b/")
    assert parse_s3_uri("S3://Bucket/Key") == ("Bucket", "Key")
    assert parse_s3_uri("plain/key") == (None, None)
    assert parse_s3_uri(None) == (None, None)
    assert parse_s3_uri("s3://") == (None, None)


def test_parse_size():
    assert parse_size(10) == 10
    assert parse_size("2048") == 2048
    assert parse_size("10MB") == 10 * 1024**2
    assert parse_size("1.5 GB") == int(round(1.5 * 1024**3))
    assert parse_size("2kb") == 2048
    assert parse_size("100 B") == 100
    assert parse_size(None) is None
    assert parse_size("") is None
    with pytest.raises(ValueError):
        parse_size("nope")
    with pytest.raises(ValueError):
        parse_size(-1)
    with pytest.raises(ValueError):
        parse_size(True)


def test_parse_datetime():
    assert parse_datetime("2026-01-31") == datetime(2026, 1, 31, tzinfo=timezone.utc)
    noon = parse_datetime("2026-01-31T15:04:05")
    assert noon == datetime(2026, 1, 31, 15, 4, 5, tzinfo=timezone.utc)
    assert parse_datetime("2026-01-31 15:04:05") == noon
    shifted = parse_datetime("2026-01-31T15:04:05-05:00")
    assert shifted == datetime(2026, 1, 31, 20, 4, 5, tzinfo=timezone.utc)
    assert parse_datetime("2026-01-31T15:04:05Z") == noon
    with pytest.raises(ValueError):
        parse_datetime("ayer")


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1536) == "1.5 KB"
    assert human_size(int(12.4 * 1024 * 1024)) == "12.4 MB"
    assert human_size(1024**2) == "1.0 MB"


def test_looks_binary():
    assert looks_binary(b"") is False
    assert looks_binary(b"hola mundo") is False
    assert looks_binary("áéíóú ñ".encode()) is False
    assert looks_binary(b"hel\x00lo") is True
    assert looks_binary(bytes([0xFF, 0xFE, 0x80, 0x81] * 50)) is True
    assert looks_binary(b"\x89PNG\r\n\x1a\n" + b"\xff" * 40) is True


def test_mask_and_group():
    assert mask_secret_id(None) is None
    assert mask_secret_id("") is None
    assert mask_secret_id("short") == "..."
    assert mask_secret_id("AKIAS301XXXX") == "AKIA...XXXX"
    assert group_label("data/2024/01/a.txt", "data/", 1) == "2024"
    assert group_label("data/2024/01/a.txt", "data/", 2) == "2024/01"
    assert group_label("data/readme.txt", "data/", 1) == "(raíz)"


def test_bound_response_trims_lists_and_text():
    payload = {"ok": True, "files": [{"key": f"k{i:02d}", "blob": "x" * 200} for i in range(40)]}
    out = bound_response(payload, limit=1500)
    assert out["response_truncated"] is True
    assert out["ok"] is True
    assert len(out["files"]) < 40
    assert json.dumps(out, ensure_ascii=False) 
    assert len(json.dumps(out, ensure_ascii=False)) <= 1500

    small = {"ok": True, "files": [1]}
    assert "response_truncated" not in bound_response(small, limit=80000)

    text = bound_response({"ok": True, "content": "a" * 10000}, limit=500)
    assert text["response_truncated"] is True
    assert len(text["content"]) < 10000
    assert len(json.dumps(text, ensure_ascii=False)) <= 500


def test_credential_precedence_and_defaults(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAWS0XXXX")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws-session-token")
    monkeypatch.setenv("access_key", "AKIAlow0XXXX")
    monkeypatch.setenv("secret_key", "low-secret-value")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKIAS301XXXX")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "s3-secret-value-xyz")
    monkeypatch.setenv("S3_SESSION_TOKEN", "")
    settings = reload_settings()
    assert settings.access_key_id == "AKIAS301XXXX"
    assert settings.secret_access_key == "s3-secret-value-xyz"
    assert settings.session_token == ""
    blob = json.dumps(settings.public_dict())
    assert "s3-secret-value-xyz" not in blob
    assert "AKIAS301XXXX" not in blob
    assert settings.public_dict()["access_key_masked"] == "AKIA...XXXX"
    assert "s3-secret-value-xyz" not in repr(settings)

    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    settings = reload_settings()
    assert settings.access_key_id == "AKIAlow0XXXX"
    assert settings.secret_access_key == "low-secret-value"
    assert settings.session_token == ""

    monkeypatch.delenv("access_key", raising=False)
    monkeypatch.delenv("secret_key", raising=False)
    settings = reload_settings()
    assert settings.access_key_id == "AKIAAWS0XXXX"
    assert settings.session_token == "aws-session-token"
    assert "aws-secret-value" not in json.dumps(settings.public_dict())

    for name in (
        "S3_REGION",
        "S3_ENDPOINT_URL",
        "S3_BUCKETS",
        "S3_BUCKET_ALLOWLIST_ONLY",
        "S3_DEFAULT_BUCKET",
        "S3_MAX_SCAN_OBJECTS",
        "S3_MAX_READ_BYTES",
        "S3_MAX_PARQUET_READ_BYTES",
        "S3_MAX_COUNT_BYTES",
        "S3_MAX_RESPONSE_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_BUCKETS", "alpha, beta")
    settings = reload_settings()
    assert settings.region == "us-east-1"
    assert settings.endpoint_url is None
    assert settings.buckets == ("alpha", "beta")
    assert settings.default_bucket == "alpha"
    assert settings.allowlist_only is False
    assert settings.max_scan_objects == 200_000
    assert settings.max_read_bytes == 20 * 1024 * 1024
    assert settings.max_parquet_read_bytes == 256 * 1024 * 1024
    assert settings.max_count_bytes == 2 * 1024 * 1024 * 1024
    assert settings.max_response_chars == 80_000


def test_load_explicit_env_file(monkeypatch, tmp_path):
    env = tmp_path / "custom.env"
    env.write_text("S3_REGION=eu-west-1\n", encoding="utf-8")
    monkeypatch.setenv("S3_MCP_ENV_FILE", str(env))
    monkeypatch.delenv("S3_REGION", raising=False)
    loaded = load_env_files()
    assert env.resolve() in [path.resolve() for path in loaded]
    assert __import__("os").environ["S3_REGION"] == "eu-west-1"
