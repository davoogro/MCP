import pytest

from mssql_mcp.safety import like_pattern, quote_ident, validate_fragment, validate_readonly

ACCEPTED = [
    "SELECT 1",
    "select top (10) * from dbo.Celdas",
    "SELECT * FROM t;",
    "WITH x AS (SELECT a FROM t) SELECT * FROM x",
    "SELECT a, COUNT(*) FROM t GROUP BY a ORDER BY 2 DESC OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY",
    "SELECT * FROM t FOR JSON PATH",
    "SELECT * FROM t WITH (NOLOCK)",
    "SELECT * FROM t WHERE accion = 'DELETE; DROP TABLE x'",
    "SELECT [update], [set] FROM t",
    "SELECT Set, [Transaction], Begin, Save FROM dbo.t",
    "SELECT alias_name = Update FROM t",
    "SELECT 'it''s' AS s -- comentario con DROP\n FROM t",
    "SELECT /* exec xp_cmdshell */ 1",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "SELECT * FROM [ANALYTICS_4G].[dbo].[tabla]",
    "SELECT name FROM sys.dm_exec_sessions",
    "SELECT * FROM t OPTION (MAXDOP 1)",
]

REJECTED = [
    "WITH c AS (SELECT 1 a) DELETE FROM t",
    "WITH c AS (SELECT id FROM t) UPDATE t SET a = 1 WHERE id IN (SELECT id FROM c)",
    "SELECT 1 DELETE FROM t",
    "SELECT 1 DROP TABLE t",
    "SELECT 1 EXEC sp_who",
    "SELECT * FROM t WHERE a IN (SELECT a FROM s) INSERT INTO t VALUES (1)",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE;",
    "TRUNCATE TABLE t",
    "DROP TABLE t",
    "CREATE TABLE t (a int)",
    "ALTER TABLE t ADD b int",
    "EXEC sp_who",
    "EXECUTE ('select 1')",
    "SELECT * INTO nueva FROM t",
    "SELECT 1; DROP TABLE t",
    "SELECT 1 ;DROP TABLE t",
    "SELECT 1\nGO\nDROP TABLE t",
    "WAITFOR DELAY '00:00:10'",
    "SELECT 1 WAITFOR DELAY '00:00:10'",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'select 1')",
    "SELECT * FROM OPENQUERY(lnk, 'delete from t')",
    "SELECT * FROM OPENDATASOURCE('x','y').db.dbo.t",
    "SELECT xp_cmdshell",
    "DECLARE @a int; SELECT @a",
    "SET NOCOUNT ON",
    "BEGIN TRAN; SELECT 1",
    "SELECT 1 COMMIT",
    "USE master",
    "DBCC CHECKDB",
    "BACKUP DATABASE x TO DISK='c:/x.bak'",
    "GRANT SELECT ON t TO u",
    "SELECT * FROM t WITH (UPDLOCK)",
    "SELECT * FROM t WITH (TABLOCKX)",
    "SELECT NEXT VALUE FOR dbo.seq",
    "SELECT 1 /* sin cerrar",
    "SELECT 'sin cerrar",
    "/* */ DELETE FROM t",
    "-- comentario\nDELETE FROM t",
    "SELECT 1 -- '\n; DELETE FROM t",
    "",
    "   ",
    "SHUTDOWN",
    "KILL 52",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql):
    assert validate_readonly(sql) is None, validate_readonly(sql)


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql):
    assert validate_readonly(sql) is not None


def test_fragments():
    assert validate_fragment("fecha >= '2026-01-01' AND a IN (1,2)", "where") is None
    assert validate_fragment("fecha DESC, id", "order_by") is None
    assert validate_fragment(None, "where") is None
    assert validate_fragment("1=1; DROP TABLE t", "where") is not None
    assert validate_fragment("1=1) UNION SELECT (1", "where") is not None
    assert validate_fragment("(SELECT 1)", "order_by") is not None
    assert validate_fragment("1=1 WAITFOR DELAY '0:0:5'", "where") is not None


def test_quote_ident():
    assert quote_ident("tabla") == "[tabla]"
    assert quote_ident("[tabla]") == "[tabla]"
    assert quote_ident("mi tabla") == "[mi tabla]"
    for bad in ["a];DROP TABLE t--", "x'y", "", "a" * 200, None]:
        with pytest.raises(ValueError):
            quote_ident(bad)


def test_like_pattern():
    assert like_pattern("cell") == "%cell%"
    assert like_pattern("cell*") == "cell%"
    assert like_pattern("%id") == "%id"
