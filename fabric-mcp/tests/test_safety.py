import pytest

from fabric_mcp.config import Connection, discover_connections, normalize_fabric_server
from fabric_mcp.safety import quote_ident, validate_readonly


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "  select top 10 * from dbo.t  ;",
    "WITH x AS (SELECT 1 a) SELECT * FROM x",
    "SELECT * FROM t WHERE accion = 'DELETE'",
    "SELECT [Update], [drop] FROM t",
    "SELECT Set, [Transaction], Begin FROM dbo.t",
    "-- comentario DROP\nSELECT 1",
    "SELECT /* exec xp_cmdshell */ 1",
    "EXEC sp_help 'dbo.t'",
    "EXEC [sys].[sp_columns] 't'",
    "(SELECT 1) UNION ALL (SELECT 2)",
])
def test_readonly_allows(sql):
    assert validate_readonly(sql) is None


@pytest.mark.parametrize("sql", [
    "",
    "DELETE FROM t",
    "SELECT 1; DROP TABLE t",
    "SELECT * INTO nueva FROM t",
    "WITH x AS (SELECT 1 a) DELETE FROM x",
    "EXEC xp_cmdshell 'dir'",
    "UPDATE t SET a = 1",
    "COPY INTO t FROM 'https://x'",
    "DECLARE @a int",
    "SELECT * FROM OPENROWSET(BULK 'x')",
    "SELECT 1 /* sin cerrar",
    "/* */ DELETE FROM t",
    "SELECT * FROM t WITH (UPDLOCK)",
    "WAITFOR DELAY '00:00:01'",
])
def test_readonly_blocks(sql):
    assert validate_readonly(sql) is not None


def test_quote_ident():
    assert quote_ident("tabla") == "[tabla]"
    assert quote_ident("a]b") == "[a]]b]"
    with pytest.raises(ValueError):
        quote_ident("")


def test_normalize_server():
    s = "tcp:abc.datawarehouse.fabric.microsoft.com.datawarehouse.fabric.microsoft.com,1433"
    assert normalize_fabric_server(s) == "abc.datawarehouse.fabric.microsoft.com"


def test_discover_connections(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("FABRIC"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("FABRIC_SERVER", "a.datawarehouse.fabric.microsoft.com")
    monkeypatch.setenv("FABRIC_USER", "u")
    monkeypatch.setenv("FABRIC_PASSWORD", "p")
    monkeypatch.setenv("FABRIC2_SERVER", "b.datawarehouse.fabric.microsoft.com")
    monkeypatch.setenv("FABRIC2_NAME", "ventas")
    monkeypatch.setenv("FABRIC2_AUTH", "ActiveDirectoryInteractive")
    monkeypatch.setenv("FABRIC_MCP_DEFAULT", "x")  # no debe confundirse con una conexión
    conns = discover_connections()
    assert list(conns) == ["default", "ventas"]
    assert conns["default"].readonly is True
    assert conns["default"].problems() == []
    assert conns["ventas"].problems() == []  # Interactive no exige user/password
    monkeypatch.setenv("FABRIC_PASSWORD", "s3cr3t-value")
    assert "s3cr3t-value" not in str(discover_connections()["default"].public_info())


def test_missing_credentials():
    c = Connection("x", "FABRIC_", "s", "db", "1433", "", "", "ActiveDirectoryPassword",
                   "", True, 120, 0, True)
    assert c.problems() == ["FABRIC_USER", "FABRIC_PASSWORD"]
