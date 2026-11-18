from sqlserver_readonly_mcp import query_history as qh


def test_query_history_ring():
    for i in range(105):
        qh.record(tool="query", sql=f"SELECT {i}", row_count=i)
    items = qh.list_history(100)
    assert len(items) == 100
    assert items[0]["sql"] == "SELECT 104"
