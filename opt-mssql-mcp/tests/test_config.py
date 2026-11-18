import pytest

from opt_mssql_mcp.config import DEFAULT_SERVERS, Settings, parse_servers


def test_parse_default():
    s = parse_servers(DEFAULT_SERVERS)
    assert list(s) == ["winsrv01"]
    assert s["winsrv01"].address == "127.0.0.1,15439"


def test_parse_variants():
    s = parse_servers(" a = h1 ; b=h2,1500;\n")
    assert s["a"].port == 1433 and s["b"].port == 1500
    with pytest.raises(ValueError):
        parse_servers("sinalias")
    with pytest.raises(ValueError):
        parse_servers("a=h,abc")


def test_settings_env(monkeypatch):
    monkeypatch.setenv("OPT_USER", "usuario")
    monkeypatch.setenv("OPT_PASS", "x")
    monkeypatch.setenv("OPT_SERVERS", "uno=h,1;dos=h2,2")
    monkeypatch.setenv("OPT_DEFAULT_CONNECTION", "noexiste")
    st = Settings.from_env()
    assert st.default_connection == "uno"
    assert st.problems() == []
    assert "x" not in st.masked_user() and st.masked_user().startswith("us")
