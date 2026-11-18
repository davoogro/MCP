from fabric_sql_mcp import query_history as qh


def test_record_and_list():
    qh.record(tool="query", sql="SELECT 1", connection="default", row_count=1, elapsed_ms=5)
    items = qh.list_history(10)
    assert items[0]["tool"] == "query"
    assert items[0]["sql"] == "SELECT 1"
    assert items[0]["row_count"] == 1
