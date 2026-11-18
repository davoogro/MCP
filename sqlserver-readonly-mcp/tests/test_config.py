import pytest

from sqlserver_readonly_mcp.config import DEFAULT_SERVERS, Settings, parse_servers


def test_parse_default():
    s = parse_servers(DEFAULT_SERVERS)
    assert list(s) == ["sql01", "sql05"]
    assert s["sql01"].address == "192.0.2.61,49152"
    assert s["sql05"].host == "192.0.2.65"


def test_parse_variants():
    s = parse_servers(" a = h1 ; b=h2,1500;\n")
    assert s["a"].port == 1433 and s["b"].port == 1500
    with pytest.raises(ValueError):
        parse_servers("sinalias")
    with pytest.raises(ValueError):
        parse_servers("a=h,abc")


def test_settings_env(monkeypatch):
    monkeypatch.setenv("MSSQL_USER", "usuario")
    monkeypatch.setenv("MSSQL_PASS", "x")
    monkeypatch.setenv("MSSQL_SERVERS", "uno=h,1;dos=h2,2")
    monkeypatch.setenv("MSSQL_DEFAULT_CONNECTION", "noexiste")
    st = Settings.from_env()
    assert st.default_connection == "uno"
    assert st.problems() == []
    assert "x" not in st.masked_user() and st.masked_user().startswith("us")
