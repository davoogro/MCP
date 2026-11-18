import asyncio
import gzip
import io
import json
from datetime import datetime

import boto3
from botocore.stub import Stubber

from s3_readonly_mcp import server as s3server
from s3_readonly_mcp.ops import activate_settings

compare_prefixes = s3server.compare_prefixes
find_objects = s3server.find_objects
get_bucket_info = s3server.get_bucket_info
get_object_metadata = s3server.get_object_metadata
list_buckets = s3server.list_buckets
list_object_versions = s3server.list_object_versions
list_objects = s3server.list_objects
prefix_stats = s3server.prefix_stats
preview_table = s3server.preview_table
read_object = s3server.read_object
count_rows = s3server.count_rows


def _ok(out):
    assert out["ok"] is True, out
    json.dumps(out)
    return out


def test_connection_masks_secrets(aws):
    out = _ok(s3server.test_connection())
    blob = json.dumps(out)
    assert "s3-secret-value-xyz" not in blob
    assert "AKIATESTING1234" not in blob
    assert out["access_key_masked"] == "AKIA...1234"
    assert out["identity"]["account"]
    assert out["identity_error"] is None
    assert out["list_buckets_allowed"] is True
    assert out["endpoint_url"] is None
    demo = next(item for item in out["buckets"] if item["name"] == "demo")
    assert demo["accessible"] is True
    assert demo["region"] == "us-east-1"


def test_list_buckets_union_and_region(aws, monkeypatch):
    aws.create_bucket(Bucket="extra")
    monkeypatch.setenv("S3_BUCKETS", "demo,missing")
    monkeypatch.delenv("S3_DEFAULT_BUCKET", raising=False)
    activate_settings()
    out = _ok(list_buckets(include_region=True))
    rows = {item["name"]: item for item in out["buckets"]}
    assert rows["demo"]["origin"] == "configured"
    assert rows["demo"]["creation_date"]
    assert rows["demo"]["region"] == "us-east-1"
    assert rows["missing"]["origin"] == "configured"
    assert rows["missing"]["creation_date"] is None
    assert rows["extra"]["origin"] == "account"
    plain = _ok(list_buckets())
    assert "region" not in plain["buckets"][0]


def test_bucket_info_tolerant(aws):
    aws.put_bucket_versioning(
        Bucket="demo", VersioningConfiguration={"Status": "Enabled"}
    )
    aws.put_bucket_tagging(
        Bucket="demo", Tagging={"TagSet": [{"Key": "env", "Value": "test"}]}
    )
    aws.put_bucket_encryption(
        Bucket="demo",
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    out = _ok(get_bucket_info("demo"))
    assert out["region"] == "us-east-1"
    assert out["versioning"]["status"] == "Enabled"
    assert out["tagging"]["env"] == "test"
    assert out["encryption"]["rules"][0]["algorithm"] == "AES256"
    assert out["lifecycle"] is None
    assert "lifecycle" in out["unavailable"]
    assert isinstance(out["unavailable"], dict)


def test_list_objects_folders_files_and_uri(aws):
    aws.put_object(Bucket="demo", Key="docs/readme.md", Body=b"hello docs")
    aws.put_object(Bucket="demo", Key="docs/img/a.png", Body=b"png")
    aws.put_object(Bucket="demo", Key="notes.txt", Body=b"note")
    root = _ok(list_objects(bucket="demo", prefix="", delimiter="/"))
    assert sorted(root["folders"]) == ["docs/"]
    assert [item["key"] for item in root["files"]] == ["notes.txt"]
    assert root["files"][0]["name"] == "notes.txt"
    assert root["files"][0]["size"] == 4
    assert root["files"][0]["size_human"]
    assert root["files"][0]["last_modified"].endswith("Z")
    assert '"' not in (root["files"][0]["etag"] or "")
    assert root["files_count"] == 1
    assert root["page_size_total"] == 4

    docs = _ok(list_objects(bucket="demo", prefix="docs/", delimiter="/"))
    assert docs["folders"] == ["docs/img/"]
    assert docs["files"][0]["key"] == "docs/readme.md"
    assert docs["files"][0]["name"] == "readme.md"

    flat = _ok(list_objects(bucket="s3://demo/docs/", prefix="", delimiter=""))
    assert flat["folders"] == []
    assert sorted(item["key"] for item in flat["files"]) == ["docs/img/a.png", "docs/readme.md"]

    page = _ok(list_objects(bucket="demo", prefix="", delimiter="", max_keys=1))
    assert page["is_truncated"] is True
    assert page["next_continuation_token"]
    nxt = _ok(list_objects(
        bucket="demo",
        prefix="",
        delimiter="",
        max_keys=1,
        continuation_token=page["next_continuation_token"],
    ))
    assert nxt["files"]
    assert nxt["files"][0]["key"] != page["files"][0]["key"]


def test_object_metadata_and_folder_hint(aws):
    aws.put_object(
        Bucket="demo",
        Key="a.txt",
        Body=b"hola",
        ContentType="text/plain",
        Metadata={"origen": "unit"},
        Tagging="env=test",
    )
    aws.put_object(Bucket="demo", Key="docs/readme.md", Body=b"x")
    out = _ok(get_object_metadata(bucket="demo", key="a.txt"))
    assert out["size"] == 4
    assert out["size_human"]
    assert out["content_type"] == "text/plain"
    assert out["metadata"]["origen"] == "unit"
    assert out["tags"]["env"] == "test"
    assert out["etag"]
    assert out["last_modified"].endswith("Z")
    assert out["storage_class"] == "STANDARD"

    via_uri = _ok(get_object_metadata(key="s3://demo/a.txt"))
    assert via_uri["key"] == "a.txt"

    missing = get_object_metadata(bucket="demo", key="docs")
    assert missing["ok"] is False
    assert "list_objects" in missing["suggestion"]
    gone = get_object_metadata(bucket="demo", key="nope.txt")
    assert gone["ok"] is False
    assert "suggestion" not in gone
    assert "Traceback" not in json.dumps(gone)


def test_prefix_stats_totals_and_groups(aws):
    aws.put_object(Bucket="demo", Key="data/2024/01/a.txt", Body=b"a" * 100)
    aws.put_object(Bucket="demo", Key="data/2024/01/b.txt", Body=b"b" * 50)
    aws.put_object(Bucket="demo", Key="data/2025/c.csv", Body=b"c" * 10)
    aws.put_object(Bucket="demo", Key="data/readme.txt", Body=b"r" * 5)
    aws.put_object(Bucket="demo", Key="data/empty.dat", Body=b"")
    out = _ok(prefix_stats(bucket="demo", prefix="data/", group_by_depth=1, top_n=2))
    assert out["object_count"] == 5
    assert out["total_bytes"] == 165
    assert out["total_bytes_human"]
    assert out["average_bytes"] == 33.0
    assert out["min_size"] == 0
    assert out["max_size"] == 100
    assert out["zero_byte_count"] == 1
    assert out["scan_truncated"] is False
    assert out["scanned"] == 5
    groups = {item["prefix"]: item for item in out["by_prefix"]}
    assert groups["2024"]["count"] == 2
    assert groups["2024"]["bytes"] == 150
    assert groups["2025"]["bytes"] == 10
    assert groups["(raíz)"]["count"] == 2
    assert groups["(raíz)"]["bytes"] == 5
    exts = {item["extension"]: item for item in out["by_extension"]}
    assert exts["txt"]["bytes"] == 155
    assert exts["csv"]["count"] == 1
    assert out["largest"][0]["key"].endswith("a.txt")
    assert out["largest"][0]["size"] == 100
    assert out["oldest"]["key"]
    assert out["oldest"]["last_modified"].endswith("Z")
    assert out["newest"][0]["last_modified"].endswith("Z")
    assert len(out["newest"]) <= 2

    deep = _ok(prefix_stats(bucket="demo", prefix="data/", group_by_depth=2, top_n=1))
    deep_groups = {item["prefix"]: item["bytes"] for item in deep["by_prefix"]}
    assert deep_groups["2024/01"] == 150

    partial = _ok(prefix_stats(bucket="demo", prefix="data/", max_objects=2))
    assert partial["scan_truncated"] is True
    assert partial["scanned"] == 2
    assert partial["total_bytes"] == 150

    empty = _ok(prefix_stats(bucket="demo", prefix="no-such/"))
    assert empty["object_count"] == 0
    assert empty["total_bytes"] == 0
    assert empty["average_bytes"] is None
    assert empty["oldest"] is None


def test_find_objects_filters(aws):
    aws.put_object(Bucket="demo", Key="alpha/A.TXT", Body=b"a" * 100)
    aws.put_object(Bucket="demo", Key="alpha/b.csv", Body=b"b" * 20)
    aws.put_object(Bucket="demo", Key="alpha/c.csv.gz", Body=b"c" * 30)
    aws.put_object(Bucket="demo", Key="alpha/d.bin", Body=b"d" * 5)
    aws.put_object(
        Bucket="demo", Key="alpha/cold.bin", Body=b"z" * 8, StorageClass="STANDARD_IA"
    )

    txt = _ok(find_objects(bucket="demo", prefix="alpha/", pattern="*.txt"))
    assert txt["matched_total"] == 1
    assert txt["matches"][0]["name"] == "A.TXT"

    nested = _ok(find_objects(bucket="demo", prefix="", pattern="alpha/*.csv"))
    assert [item["key"] for item in nested["matches"]] == ["alpha/b.csv"]

    csv_only = _ok(find_objects(bucket="demo", prefix="alpha/", extensions=["csv"]))
    assert [item["name"] for item in csv_only["matches"]] == ["b.csv"]
    double = _ok(find_objects(bucket="demo", prefix="alpha/", extensions=["CSV.GZ"]))
    assert [item["name"] for item in double["matches"]] == ["c.csv.gz"]

    bins = _ok(find_objects(bucket="demo", prefix="alpha/", regex=r"\.bin$"))
    assert bins["matched_total"] == 2

    big = _ok(find_objects(bucket="demo", prefix="alpha/", min_size="50B"))
    assert [item["name"] for item in big["matches"]] == ["A.TXT"]
    small = _ok(find_objects(bucket="demo", prefix="alpha/", max_size=10))
    assert {item["name"] for item in small["matches"]} == {"d.bin", "cold.bin"}

    cold = _ok(find_objects(bucket="demo", prefix="alpha/", storage_class="standard_ia"))
    assert [item["name"] for item in cold["matches"]] == ["cold.bin"]

    future = _ok(find_objects(bucket="demo", prefix="alpha/", modified_after="2099-01-01"))
    assert future["matched_total"] == 0
    past = _ok(find_objects(bucket="demo", prefix="alpha/", modified_after="2000-01-01"))
    assert past["matched_total"] == 5

    top = _ok(find_objects(
        bucket="demo", prefix="alpha/", sort_by="size", descending=True, limit=1
    ))
    assert top["matches"][0]["name"] == "A.TXT"
    assert top["matched_total"] == 5
    assert top["matched_total_bytes"] == 100 + 20 + 30 + 5 + 8

    bad = find_objects(bucket="demo", prefix="alpha/", regex="(")
    assert bad["ok"] is False
    assert bad["error_code"] == "InvalidRegex"
    bad_sort = find_objects(bucket="demo", prefix="alpha/", sort_by="foo")
    assert bad_sort["ok"] is False
    assert bad_sort["error_code"] == "InvalidArgument"

    capped = _ok(find_objects(bucket="demo", prefix="alpha/", max_objects=2, pattern="*"))
    assert capped["scan_truncated"] is True
    assert capped["scanned"] == 2


def test_read_object_range_gzip_and_binary(aws):
    body = b"0123456789abcdefghijklmnopqrstuvwxyz"
    aws.put_object(Bucket="demo", Key="blob.txt", Body=body, ContentType="text/plain")
    out = _ok(read_object(bucket="demo", key="blob.txt", offset=10, max_bytes=5))
    assert out["content"] == "abcde"
    assert out["bytes_returned"] == 5
    assert out["object_size"] == len(body)
    assert out["truncated"] is True
    assert out["next_offset"] == 15
    assert out["is_binary"] is False
    assert out["content_type"] == "text/plain"

    payload = "abcdefghij" * 20
    raw = gzip.compress(payload.encode())
    aws.put_object(Bucket="demo", Key="saludo.txt.gz", Body=raw, ContentType="application/gzip")
    full = _ok(read_object(bucket="demo", key="saludo.txt.gz", decompress=True))
    assert full["content"] == payload
    assert full["decompressed"] is True
    assert full["truncated"] is False
    window = _ok(read_object(bucket="demo", key="saludo.txt.gz", offset=10, max_bytes=10))
    assert window["content"] == payload[10:20]
    assert window["truncated"] is True
    assert window["next_offset"] == 20

    aws.put_object(Bucket="demo", Key="bin.dat", Body=b"\x00\x01\x02\xffBIN")
    binary = _ok(read_object(bucket="demo", key="bin.dat"))
    assert binary["is_binary"] is True
    assert binary["content"] is None
    assert binary["content_hex"].startswith("000102")

    missing = read_object(bucket="demo", key="missing.txt")
    assert missing["ok"] is False
    assert missing["error_code"]


def test_preview_table_csv_jsonl_json_parquet(aws):
    csv_body = (
        "name,qty,ratio,active,day,note\n"
        "ada,3,1.5,true,2024-05-06," + ("n" * 600) + "\n"
        "grace,10,2.0,false,2020-01-31,ok\n"
    )
    aws.put_object(Bucket="demo", Key="people.csv", Body=csv_body.encode())
    table = _ok(preview_table(bucket="demo", key="people.csv", rows=20))
    assert table["format"] == "csv"
    assert table["partial_read"] is False
    assert table["rows_returned"] == 2
    types = {col["name"]: col["type"] for col in table["columns"]}
    assert types["name"] == "string"
    assert types["qty"] == "int"
    assert types["ratio"] == "float"
    assert types["active"] == "bool"
    assert types["day"] == "date"
    assert table["rows"][0]["qty"] == 3
    assert table["rows"][0]["active"] is True
    assert table["rows"][1]["active"] is False
    assert len(table["rows"][0]["note"]) <= 501

    gz = gzip.compress(csv_body.encode())
    aws.put_object(Bucket="demo", Key="people.csv.gz", Body=gz)
    zipped = _ok(preview_table(bucket="demo", key="people.csv.gz", rows=1))
    assert zipped["format"] == "csv"
    assert zipped["rows_returned"] == 1
    assert zipped["rows_truncated"] is True

    aws.put_object(
        Bucket="demo",
        Key="events.jsonl",
        Body=b'{"name":"ada","n":1}\n{"name":"grace","n":2}\n',
    )
    lines = _ok(preview_table(bucket="demo", key="events.jsonl"))
    assert lines["format"] == "jsonl"
    assert lines["rows"] == [{"name": "ada", "n": 1}, {"name": "grace", "n": 2}]
    assert {col["name"]: col["type"] for col in lines["columns"]}["n"] == "int"

    aws.put_object(
        Bucket="demo",
        Key="events.json",
        Body=json.dumps([{"name": "ada", "n": 1}, {"name": "grace", "n": 2}]).encode(),
    )
    doc = _ok(preview_table(bucket="demo", key="events.json", columns=["name"]))
    assert doc["format"] == "json"
    assert doc["rows"] == [{"name": "ada"}, {"name": "grace"}]

    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"name": ["ada", "grace"], "n": [1, 2]}), buf)
    aws.put_object(Bucket="demo", Key="t.parquet", Body=buf.getvalue())
    parq = _ok(preview_table(bucket="demo", key="t.parquet", rows=1))
    assert parq["format"] == "parquet"
    assert parq["num_rows"] == 2
    assert parq["num_row_groups"] >= 1
    assert parq["rows_returned"] == 1
    assert {col["name"] for col in parq["schema"]} == {"name", "n"}
    assert parq["rows"][0]["name"] == "ada"


_SEMICOLON_QUOTE_CSV = (
    "'FECHA';'PLMN_ID';'DN';'ALARM_NUMBER';'ALARM_TEXT';'SUPPLEMENTARY_INFO';'CONCURRENCIAS'\r\n"
    "'20260817';'rc10';'PLMN-PLMN/BSC-407163/BCF-296';7705;'D-CHANNEL FAILURE';;2776\r\n"
    "'20260817';'rc10';'PLMN-PLMN/MRBTS-787336/EQM_R-1';7115;"
    "'BASE STATION INFORMATION';'No connection to unit';1940\r\n"
)


def _assert_semicolon_quote_table(table):
    assert table["format"] == "csv"
    assert table["delimiter"] == ";"
    assert [col["name"] for col in table["columns"]] == [
        "FECHA",
        "PLMN_ID",
        "DN",
        "ALARM_NUMBER",
        "ALARM_TEXT",
        "SUPPLEMENTARY_INFO",
        "CONCURRENCIAS",
    ]
    assert table["rows_returned"] == 2
    first, second = table["rows"]
    assert first == {
        "FECHA": 20260817,
        "PLMN_ID": "rc10",
        "DN": "PLMN-PLMN/BSC-407163/BCF-296",
        "ALARM_NUMBER": 7705,
        "ALARM_TEXT": "D-CHANNEL FAILURE",
        "SUPPLEMENTARY_INFO": None,
        "CONCURRENCIAS": 2776,
    }
    assert second["ALARM_TEXT"] == "BASE STATION INFORMATION"
    assert second["SUPPLEMENTARY_INFO"] == "No connection to unit"
    assert second["CONCURRENCIAS"] == 1940
    assert second["FECHA"] == 20260817


def test_preview_table_semicolon_single_quotes(aws, monkeypatch):
    aws.put_object(Bucket="demo", Key="alarms.csv", Body=_SEMICOLON_QUOTE_CSV.encode())
    plain = _ok(preview_table(bucket="demo", key="alarms.csv"))
    _assert_semicolon_quote_table(plain)
    explicit = _ok(preview_table(bucket="demo", key="alarms.csv", quotechar="'"))
    _assert_semicolon_quote_table(explicit)

    gz = gzip.compress(_SEMICOLON_QUOTE_CSV.encode())
    aws.put_object(Bucket="demo", Key="alarms.csv.gz", Body=gz)
    zipped = _ok(preview_table(bucket="demo", key="alarms.csv.gz"))
    _assert_semicolon_quote_table(zipped)

    def sniff_without_quote(self, sample, delimiters=None):
        class dialect:
            delimiter = ";"
            quotechar = '"'

        return dialect

    monkeypatch.setattr("s3_readonly_mcp.ops.csv.Sniffer.sniff", sniff_without_quote)
    fallback = _ok(preview_table(bucket="demo", key="alarms.csv"))
    _assert_semicolon_quote_table(fallback)


def test_allowlist(aws, monkeypatch):
    aws.create_bucket(Bucket="extra")
    monkeypatch.setenv("S3_BUCKET_ALLOWLIST_ONLY", "true")
    monkeypatch.setenv("S3_BUCKETS", "demo")
    monkeypatch.setenv("S3_DEFAULT_BUCKET", "demo")
    activate_settings()
    listed = _ok(list_buckets())
    assert [item["name"] for item in listed["buckets"]] == ["demo"]
    denied = list_objects(bucket="extra")
    assert denied["ok"] is False
    assert denied["error_code"] == "BucketNotAllowed"
    denied_uri = read_object(key="s3://extra/a.txt")
    assert denied_uri["error_code"] == "BucketNotAllowed"


def test_response_truncated(aws, monkeypatch):
    for index in range(40):
        aws.put_object(Bucket="demo", Key=f"wide/{index:02d}-{'k' * 80}.txt", Body=b"hello world")
    monkeypatch.setenv("S3_MAX_RESPONSE_CHARS", "2500")
    activate_settings()
    out = _ok(list_objects(bucket="demo", prefix="wide/", delimiter="", max_keys=100))
    assert out.get("response_truncated") is True
    assert len(out["files"]) < 40
    assert len(json.dumps(out, ensure_ascii=False)) <= 2500


def test_versions_and_compare(aws):
    aws.put_bucket_versioning(Bucket="demo", VersioningConfiguration={"Status": "Enabled"})
    aws.put_object(Bucket="demo", Key="v.txt", Body=b"one")
    aws.put_object(Bucket="demo", Key="v.txt", Body=b"two")
    aws.delete_object(Bucket="demo", Key="v.txt")
    versions = _ok(list_object_versions(bucket="demo", prefix="v.txt"))
    assert len(versions["versions"]) >= 2
    assert versions["versions"][0]["last_modified"].endswith("Z")
    assert "size" in versions["versions"][0]
    assert len(versions["delete_markers"]) >= 1
    assert versions["delete_markers"][0]["is_delete_marker"] is True

    aws.create_bucket(Bucket="other")
    aws.put_object(Bucket="demo", Key="a/x.txt", Body=b"same")
    aws.put_object(Bucket="demo", Key="a/only_a.txt", Body=b"a")
    aws.put_object(Bucket="demo", Key="a/x2.txt", Body=b"original")
    aws.put_object(Bucket="other", Key="b/x.txt", Body=b"same")
    aws.put_object(Bucket="other", Key="b/only_b.txt", Body=b"b")
    aws.put_object(Bucket="other", Key="b/x2.txt", Body=b"changed")
    out = _ok(compare_prefixes(prefix_a="a/", prefix_b="b/", bucket="demo", bucket_b="other"))
    assert out["same_count"] == 1
    assert [item["key"] for item in out["only_in_a"]] == ["only_a.txt"]
    assert [item["key"] for item in out["only_in_b"]] == ["only_b.txt"]
    assert [item["key"] for item in out["different"]] == ["x2.txt"]
    assert out["only_in_a_total"] == 1
    assert out["different_total"] == 1
    assert out["scan_truncated"] is False


def test_stubber_access_denied(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKIATESTING1234")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "s3-secret-value-xyz")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_BUCKET_ALLOWLIST_ONLY", "false")
    monkeypatch.setenv("S3_BUCKETS", "demo")
    activate_settings()
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="AKIATESTING1234",
        aws_secret_access_key="s3-secret-value-xyz",
    )
    stubber = Stubber(client)
    stubber.add_client_error(
        "list_objects_v2",
        service_error_code="AccessDenied",
        service_message="Access Denied",
        http_status_code=403,
    )
    stubber.activate()
    monkeypatch.setattr("s3_readonly_mcp.ops.client_for_bucket", lambda bucket: client)
    out = list_objects(bucket="demo", prefix="p/")
    assert out["ok"] is False
    assert out["error_code"] == "AccessDenied"
    assert "Traceback" not in json.dumps(out)
    stubber.deactivate()


def test_region_header_on_error():
    from s3_readonly_mcp.ops import normalize_location, region_from_response

    response = {
        "Error": {"Code": "PermanentRedirect", "Message": "moved"},
        "ResponseMetadata": {"HTTPHeaders": {"x-amz-bucket-region": "eu-west-1"}},
    }
    assert region_from_response(response) == "eu-west-1"
    assert normalize_location(None) == "us-east-1"
    assert normalize_location("EU") == "eu-west-1"
    assert normalize_location("us-west-2") == "us-west-2"


def test_handshake_lists_twelve_tools():
    from mcp import Client

    expected = {
        "compare_prefixes",
        "count_rows",
        "download_object",
        "find_objects",
        "freshness",
        "get_bucket_info",
        "get_object_metadata",
        "list_buckets",
        "list_object_versions",
        "list_objects",
        "prefix_stats",
        "preview_table",
        "query_data",
        "read_object",
        "sample_rows",
        "schema_of_prefix",
        "test_connection",
    }

    async def go():
        async with Client(s3server.mcp) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            for tool in listed.tools:
                ann = tool.annotations
                data = ann if isinstance(ann, dict) else ann.model_dump(by_alias=True)
                assert data["readOnlyHint"] is True
                assert data["openWorldHint"] is True
            return names

    assert asyncio.run(go()) == expected


def _set_last_modified(bucket, key, when):
    from moto.s3.models import s3_backends

    for parts in s3_backends.values():
        backend = parts.get("aws") if hasattr(parts, "get") else None
        if backend is None:
            continue
        try:
            fake = backend.get_bucket(bucket).keys[key]
        except Exception:
            continue
        fake.last_modified = when
        return
    raise AssertionError(f"no está {bucket}/{key} en el backend de moto")


def _large_parquet_bytes():
    import pyarrow as pa
    import pyarrow.parquet as pq

    blob = "m" * 800

    def make(start, count):
        return pa.table({
            "id": pa.array(range(start, start + count), type=pa.int64()),
            "blob": pa.array([blob] * count),
        })

    schema = make(0, 1).schema.with_metadata({b"origin": b"fix2"})
    buf = io.BytesIO()
    writer = pq.ParquetWriter(buf, schema, compression="NONE", use_dictionary=False)
    writer.write_table(make(0, 1500))
    writer.write_table(make(1500, 8000))
    writer.write_table(make(9500, 1500))
    writer.close()
    return buf.getvalue()


def test_preview_parquet_ranges_and_count(aws, monkeypatch):
    payload = _large_parquet_bytes()
    assert len(payload) > 5_000_000
    aws.put_object(Bucket="demo", Key="big.parquet", Body=payload)

    preview = _ok(preview_table(bucket="demo", key="big.parquet", rows=2))
    assert preview["format"] == "parquet"
    assert preview["num_rows"] == 11000
    assert preview["num_row_groups"] >= 3
    assert preview["rows_returned"] == 2
    assert preview["rows"][0]["id"] == 0
    assert preview["rows"][1]["id"] == 1
    assert preview["partial_read"] is False
    assert preview["bytes_fetched"] < preview["object_size"]
    assert {col["name"] for col in preview["schema"]} == {"id", "blob"}
    assert preview["metadata"]["origin"] == "fix2"
    assert isinstance(preview["created_by"], str) and preview["created_by"]
    full_fetched = preview["bytes_fetched"]

    only_id = _ok(preview_table(bucket="demo", key="big.parquet", rows=1, columns=["id"]))
    assert only_id["rows"] == [{"id": 0}]
    assert only_id["bytes_fetched"] < full_fetched
    assert only_id["num_rows"] == 11000

    counted = _ok(count_rows(bucket="demo", key="big.parquet"))
    assert counted["files_counted"] == 1
    assert counted["all_exact"] is True
    assert counted["total_rows"] == 11000
    footer = counted["files"][0]
    assert footer["method"] == "parquet_footer"
    assert footer["exact"] is True
    assert footer["rows"] == 11000
    assert footer["error"] is None
    assert footer["bytes_fetched"] < footer["size"]
    assert footer["bytes_fetched"] < full_fetched

    monkeypatch.setenv("S3_MAX_PARQUET_READ_BYTES", "1500000")
    activate_settings()
    limited = _ok(preview_table(bucket="demo", key="big.parquet", rows=5))
    assert limited["num_rows"] == 11000
    assert limited["num_row_groups"] >= 3
    assert limited["rows"] == []
    assert limited["rows_returned"] == 0
    assert limited["partial_read"] is True
    assert limited["aviso"]
    assert limited["bytes_fetched"] <= 1_500_000
    assert limited["created_by"]


_QUOTE_CSV = (
    "'FECHA';'NOTE'\r\n"
    "'20260817';'linea uno\nlinea dos'\r\n"
    "'20260818';'ok'\r\n"
)


def test_count_rows_text_prefix_and_limit(aws, monkeypatch):
    aws.put_object(Bucket="demo", Key="plain.csv", Body=b"h1,h2\na,b\nc,d\n")
    plain = _ok(count_rows(bucket="demo", key="plain.csv"))
    assert plain["total_rows"] == 2
    assert plain["files"][0]["method"] == "csv_stream"
    assert plain["files"][0]["exact"] is True
    assert plain["all_exact"] is True

    gz = gzip.compress(_QUOTE_CSV.encode())
    aws.put_object(Bucket="demo", Key="alarms.csv.gz", Body=gz)
    sniffed = _ok(count_rows(bucket="demo", key="alarms.csv.gz"))
    assert sniffed["files"][0]["rows"] == 2
    assert sniffed["files"][0]["method"] == "csv_stream"
    assert sniffed["files"][0]["exact"] is True
    explicit = _ok(count_rows(
        bucket="demo", key="alarms.csv.gz", delimiter=";", quotechar="'",
    ))
    assert explicit["files"][0]["rows"] == 2

    aws.put_object(
        Bucket="demo",
        Key="events.jsonl",
        Body=b'{"n":1}\n\n{"n":2}\n{"n":3}\n',
    )
    lines = _ok(count_rows(bucket="demo", key="events.jsonl"))
    assert lines["files"][0]["rows"] == 3
    assert lines["files"][0]["method"] == "jsonl_stream"
    assert lines["files"][0]["exact"] is True

    aws.put_object(
        Bucket="demo",
        Key="events.json",
        Body=json.dumps([{"n": 1}, {"n": 2}]).encode(),
    )
    doc = _ok(count_rows(bucket="demo", key="events.json"))
    assert doc["files"][0]["rows"] == 2
    assert doc["files"][0]["method"] == "json_array"

    aws.put_object(Bucket="demo", Key="data/events.jsonl", Body=b'{"n":1}\n{"n":2}\n{"n":3}\n')
    aws.put_object(Bucket="demo", Key="data/new.csv", Body=b"h\n1\n2\n3\n4\n")
    aws.put_object(Bucket="demo", Key="data/old.csv", Body=b"h\n1\n2\n")
    aws.put_object(Bucket="demo", Key="data/skip.txt", Body=b"no es tabla\n")
    aws.put_object(Bucket="demo", Key="other/x.csv", Body=b"h\n9\n")
    _set_last_modified("demo", "data/events.jsonl", datetime(2024, 7, 1))
    _set_last_modified("demo", "data/new.csv", datetime(2024, 6, 1))
    _set_last_modified("demo", "data/old.csv", datetime(2020, 6, 1))
    _set_last_modified("demo", "data/skip.txt", datetime(2024, 7, 2))

    recent = _ok(count_rows(bucket="demo", prefix="data/", modified_after="2023-01-01"))
    by_key = {item["key"]: item for item in recent["files"]}
    assert set(by_key) == {"data/events.jsonl", "data/new.csv", "data/skip.txt"}
    assert by_key["data/new.csv"]["rows"] == 4
    assert by_key["data/events.jsonl"]["rows"] == 3
    assert by_key["data/skip.txt"]["error"]
    assert recent["total_rows"] == 7
    assert recent["files_counted"] == 2
    assert recent["files_with_errors"] == 1
    assert recent["all_exact"] is False
    assert recent["files_truncated"] is False
    assert [item["key"] for item in recent["files"]] == sorted(by_key)

    old = _ok(count_rows(bucket="demo", prefix="data/", modified_before="2021-01-01"))
    assert [item["key"] for item in old["files"]] == ["data/old.csv"]
    assert old["total_rows"] == 2
    assert old["all_exact"] is True

    csv_only = _ok(count_rows(
        bucket="demo",
        prefix="data/",
        extensions=["csv"],
        modified_after="2023-01-01",
    ))
    assert [item["key"] for item in csv_only["files"]] == ["data/new.csv"]
    assert csv_only["total_rows"] == 4

    capped = _ok(count_rows(bucket="demo", prefix="data/", max_files=1, modified_after="2000-01-01"))
    assert capped["files_truncated"] is True
    assert len(capped["files"]) == 1
    assert capped["files"][0]["key"] == "data/events.jsonl"
    assert capped["elapsed_seconds"] >= 0

    both = count_rows(bucket="demo", key="plain.csv", prefix="data/")
    assert both["ok"] is False
    assert both["error_code"] == "InvalidArgument"
    neither = count_rows(bucket="demo")
    assert neither["ok"] is False

    wide = "name,note\n" + "".join(f"n{i},{'x' * 40}\n" for i in range(30))
    aws.put_object(Bucket="demo", Key="wide.csv", Body=wide.encode())
    monkeypatch.setenv("S3_MAX_COUNT_BYTES", "80")
    activate_settings()
    partial = _ok(count_rows(bucket="demo", key="wide.csv"))
    entry = partial["files"][0]
    assert entry["partial"] is True
    assert entry["exact"] is False
    assert entry["error"] is None
    assert entry["rows"] < 30
    assert partial["all_exact"] is False
