from fabric_sql_mcp.export import _parse_s3, _slug


def test_slug():
    assert _slug("SELECT * FROM dbo.t") != ""


def test_parse_s3():
    b, k = _parse_s3("s3://my-bucket/path/out.csv")
    assert b == "my-bucket"
    assert k == "path/out.csv"
