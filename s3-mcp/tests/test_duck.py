import io
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from s3_mcp import server as s3server
from s3_mcp.duck import query_data, sample_rows, schema_of_prefix, validate_query_sql
from s3_mcp.ops import ToolError

download_object = s3server.download_object
freshness = s3server.freshness


def test_validate_query_sql_allows_select():
    validate_query_sql("SELECT 1")
    validate_query_sql("WITH t AS (SELECT 1 AS x) SELECT * FROM t")
    validate_query_sql("DESCRIBE SELECT * FROM read_parquet('s3://b/k.parquet')")
    validate_query_sql("SUMMARIZE SELECT * FROM read_parquet('s3://b/k.parquet')")


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "INSERT INTO t VALUES (1)",
        "SELECT 1; SELECT 2",
        "COPY x TO 's3://b/out'",
        "CREATE TABLE t AS SELECT 1",
        "SELECT read_text('C:/tmp/x.txt')",
        "SELECT * FROM read_parquet('/local/x.parquet')",
        "INSTALL httpfs",
    ],
)
def test_validate_query_sql_rejects(sql):
    with pytest.raises(ToolError):
        validate_query_sql(sql)


def _put_parquet(aws, key, columns=None):
    table = pa.table(columns or {"id": [1, 2, 3], "name": ["a", "b", "c"]})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    aws.put_object(Bucket="demo", Key=key, Body=buf.getvalue())


def test_schema_of_prefix_and_drift(aws):
    _put_parquet(aws, "lake/a.parquet", {"id": [1], "name": ["x"]})
    _put_parquet(aws, "lake/b.parquet", {"id": [2], "name": ["y"]})
    _put_parquet(aws, "lake/c.parquet", {"id": [3], "extra": [9]})
    out = schema_of_prefix(bucket="demo", prefix="lake/", sample_files=10)
    assert out["files_sampled"] == 3
    assert len(out["canonical_schema"]) == 2
    assert out["schema_drift"]
    assert len(out["schema_drift"][0]["keys"]) == 1


def test_query_data_local_fallback(aws, monkeypatch):
    _put_parquet(aws, "q/t.parquet", {"n": [10, 20, 30]})
    monkeypatch.setattr("s3_mcp.duck._configure_httpfs", lambda _conn: False)
    result = query_data("SELECT sum(n) AS total FROM read_parquet('s3://demo/q/t.parquet')")
    assert result["rows"][0]["total"] == 60
    assert result["mode"] == "local_download"


def test_sample_rows_limit(aws, monkeypatch):
    _put_parquet(aws, "s/t.parquet", {"v": list(range(100))})
    monkeypatch.setattr("s3_mcp.duck._configure_httpfs", lambda _conn: False)
    out = sample_rows(bucket="demo", key_or_prefix="s/t.parquet", n=5, random=False)
    assert out["rows_returned"] == 5
    assert out["format"] == "parquet"


def test_download_object(aws, tmp_path):
    aws.put_object(Bucket="demo", Key="dl/hello.txt", Body=b"payload")
    out = download_object(bucket="demo", key="dl/hello.txt", local_dir=str(tmp_path))
    assert out["ok"] is True
    assert Path(out["local_path"]).read_bytes() == b"payload"
    assert out["bytes"] == 7


def test_freshness_groups(aws):
    from tests.test_ops import _set_last_modified

    aws.put_object(Bucket="demo", Key="loads/2024/a.parquet", Body=b"x")
    aws.put_object(Bucket="demo", Key="loads/2024/b.parquet", Body=b"y")
    aws.put_object(Bucket="demo", Key="loads/2025/c.parquet", Body=b"z")
    _set_last_modified("demo", "loads/2024/a.parquet", datetime(2020, 1, 1))
    now = datetime.now(timezone.utc)
    _set_last_modified("demo", "loads/2024/b.parquet", now)
    _set_last_modified("demo", "loads/2025/c.parquet", now)
    out = freshness(bucket="demo", prefixes="loads/", group_by_depth=1)
    assert out["ok"] is True
    groups = {item["group"]: item for item in out["groups"]}
    assert groups["2024"]["object_count"] == 2
    assert groups["2025"]["object_count"] == 1
    assert groups["2024"]["objects_last_24h"] >= 1


def test_handshake_seventeen_tools():
    import asyncio
    from mcp import Client

    async def go():
        async with Client(s3server.mcp) as client:
            listed = await client.list_tools()
            return {tool.name for tool in listed.tools}

    names = asyncio.run(go())
    for tool in (
        "query_data",
        "schema_of_prefix",
        "sample_rows",
        "freshness",
        "download_object",
    ):
        assert tool in names
    assert len(names) == 17
