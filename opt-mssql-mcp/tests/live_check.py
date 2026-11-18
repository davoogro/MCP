"""Prueba en vivo contra winsrv01 (requiere OPT_USER/OPT_PASS). Solo escribe en una tabla #temporal.

    set PYTHONPATH=src
    C:\\DVB\\python\\python.exe tests\\live_check.py
"""

import json

from opt_mssql_mcp import server as S


def show(name, out, keys=None, n=700):
    data = out if keys is None else {k: out.get(k) for k in keys}
    print(f"== {name}: ok={out.get('ok')} {json.dumps(data, default=str, ensure_ascii=False)[:n]}\n")


show("test_connection", S.test_connection(), None, 1500)
dbs = S.list_databases()
show("list_databases", dbs, ["databases"], 1200)

# Escritura real en tempdb con una tabla #temporal (desaparece al cerrar la sesión).
script = """
CREATE TABLE #mcp_probe (id int IDENTITY PRIMARY KEY, nombre nvarchar(50));
INSERT INTO #mcp_probe (nombre) VALUES (N'uno'), (N'dos'), (N'tres');
UPDATE #mcp_probe SET nombre = N'DOS' WHERE id = 2;
DELETE FROM #mcp_probe WHERE id = 3;
PRINT 'probe ok';
SELECT * FROM #mcp_probe ORDER BY id;
"""
show("execute_sql temp", S.execute_sql(script, database="tempdb"),
     ["status", "rows_affected", "result_sets", "messages", "classification", "error"], 1200)
show("dry_run temp", S.execute_sql(script, database="tempdb", dry_run=True),
     ["status", "rows_affected", "notes", "error"])
show("GO batches", S.execute_sql("SELECT 1 AS a\nGO\nSELECT 2 AS b", database="tempdb"),
     ["status", "batches", "error"], 900)
show("rollback on error", S.execute_sql(
    "CREATE TABLE #x (a int);\nINSERT #x VALUES (1);\nGO\nSELECT 1/0;", database="tempdb"), ["error"])
show("confirm required", S.execute_sql("DROP TABLE dbo.no_existe_mcp", database="tempdb"),
     ["error_code", "classification"])
show("classify", S.classify_sql("UPDATE t SET a = 1"), None)
show("query read-only", S.query("DELETE FROM t", database="tempdb"), ["error"])
show("audit", S.write_audit_log(5), ["count", "entries"], 900)
