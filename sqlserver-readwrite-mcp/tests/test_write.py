import pytest

from sqlserver_readwrite_mcp.tunnel import parse_tunnel
from sqlserver_readwrite_mcp.write import classify, split_batches


def test_split_batches():
    assert split_batches("SELECT 1\nGO\nSELECT 2\ngo  \nSELECT 3") == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert split_batches("INSERT t VALUES (1)\nGO 3\n") == ["INSERT t VALUES (1)"] * 3
    assert split_batches("SELECT 'GO' AS x") == ["SELECT 'GO' AS x"]
    assert split_batches("SELECT 1 -- GO\n") == ["SELECT 1 -- GO"]
    with pytest.raises(ValueError):
        split_batches("SELECT 1\nGO 5000")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t",
    "WITH x AS (SELECT 1 a) SELECT * FROM x",
])
def test_read(sql):
    assert classify(sql).risk == "read"


@pytest.mark.parametrize("sql", [
    "INSERT INTO t (a) VALUES (1)",
    "UPDATE t SET a = 1 WHERE id = 5",
    "DELETE FROM t WHERE fecha < '2026-01-01'",
    "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a WHEN NOT MATCHED THEN INSERT (a) VALUES (s.a);",
    "CREATE TABLE dbo.x (id int)",
    "ALTER TABLE dbo.x ADD b int",
    "EXEC dbo.miProc @a = 1",
    "SELECT * INTO dbo.copia FROM t",
    "CREATE INDEX ix ON t(a)",
    "UPDATE STATISTICS dbo.t",
    "WITH c AS (SELECT id FROM t) DELETE FROM t WHERE id IN (SELECT id FROM c)",
    "UPDATE t SET a = 1 WHERE id IN (SELECT id FROM s)\nGO\nINSERT t (a) SELECT a FROM s",
    "CREATE TRIGGER tr ON t AFTER UPDATE AS BEGIN SET NOCOUNT ON; INSERT INTO log (a) SELECT a FROM inserted; END",
    "DELETE FROM t WHERE descripcion = 'DROP TABLE x'",
])
def test_write(sql):
    c = classify(sql)
    assert c.risk == "write", c.as_dict()


@pytest.mark.parametrize("sql,reason", [
    ("DELETE FROM t", "DELETE sin WHERE"),
    ("delete t", "DELETE sin WHERE"),
    ("UPDATE t SET a = 1", "UPDATE sin WHERE"),
    ("UPDATE t SET a = (SELECT MAX(b) FROM s WHERE s.id = 1)", "UPDATE sin WHERE"),
    ("WITH c AS (SELECT 1 a) DELETE FROM t", "DELETE sin WHERE"),
    ("DROP TABLE dbo.t", "DROP de objeto"),
    ("TRUNCATE TABLE dbo.t", "TRUNCATE TABLE"),
    ("ALTER TABLE t DROP COLUMN b", "ALTER TABLE ... DROP"),
    ("ALTER DATABASE x SET OFFLINE", "ALTER de base"),
    ("GRANT SELECT ON t TO u", "permisos"),
    ("EXEC xp_cmdshell 'dir'", "xp_"),
    ("EXEC sp_configure 'show advanced options', 1", "configuración"),
    ("KILL 55", "KILL"),
    ("DBCC SHRINKFILE (1)", "DBCC"),
    ("EXEC sp_rename 't', 't2'", "administrativo"),
    ("INSERT t VALUES (1)\nGO\nDELETE FROM t", "DELETE sin WHERE"),
    ("MERGE t USING s ON t.id=s.id WHEN NOT MATCHED BY SOURCE THEN DELETE;", "NOT MATCHED BY SOURCE"),
])
def test_dangerous(sql, reason):
    c = classify(sql)
    assert c.risk == "dangerous", c.as_dict()
    assert any(reason in r for r in c.reasons), c.reasons


def test_autocommit_detection():
    assert classify("CREATE DATABASE prueba").requires_autocommit
    assert classify("BACKUP DATABASE x TO DISK='c:/x.bak'").requires_autocommit
    assert not classify("CREATE TABLE t (a int)").requires_autocommit


def test_empty_and_unclosed():
    with pytest.raises(ValueError):
        classify("  ")
    with pytest.raises(ValueError):
        classify("UPDATE t SET a = 'sin cerrar")


def test_parse_tunnel():
    assert parse_tunnel("winsrv01:127.0.0.1:49152") == ("winsrv01", "127.0.0.1", 49152)
    for bad in ["winsrv01", "a:b:c", "a::1"]:
        with pytest.raises(ValueError):
            parse_tunnel(bad)


def test_module_body_not_dangerous():
    c = classify("CREATE OR ALTER PROCEDURE dbo.limpia AS BEGIN DELETE FROM staging; DROP TABLE #x; END")
    assert c.risk == "write" and c.verbs == ["CREATE PROCEDURE"]
    assert classify("CREATE PROC p AS SELECT 1\nGO\nDROP TABLE t").risk == "dangerous"
