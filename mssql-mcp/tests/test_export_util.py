from mssql_mcp.export import _parse_s3, _slug


def test_s3_parse():
    b, k = _parse_s3("s3://my-bucket/path/out.parquet")
    assert b == "my-bucket" and k == "path/out.parquet"


def test_slug():
    assert _slug("SELECT * FROM dbo.Celdas") == "SELECT___FROM"
