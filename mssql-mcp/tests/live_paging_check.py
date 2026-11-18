from mssql_mcp import server as s

SQL = (
    "WITH n AS ("
    " SELECT TOP (12000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS rn"
    " FROM sys.all_objects a CROSS JOIN sys.all_objects b"
    ") SELECT rn, CAST(rn AS sysname) AS label FROM n ORDER BY rn"
)
r = s.query(SQL, page_size=5000, connection="sql01")
seen = [row[0] for row in r["rows"]]


def info(r):
    return {k: r.get(k) for k in (
        "rows_returned", "rows_buffered", "has_more", "truncated",
        "next_cursor", "response_truncated", "error",
    )}


print(info(r))
for _ in range(20):
    c = r.get("next_cursor")
    if not c:
        break
    r = s.fetch_more(c, page_size=5000, connection="sql01")
    print(info(r))
    seen += [row[0] for row in r.get("rows", [])]

contiguous = seen == list(range(1, len(seen) + 1))
print(
    "total", len(seen), "unique", len(set(seen)),
    "min", min(seen) if seen else None, "max", max(seen) if seen else None,
    "contiguous", contiguous,
)
