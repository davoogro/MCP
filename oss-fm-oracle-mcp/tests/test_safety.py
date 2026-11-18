"""Pruebas de la capa de seguridad SQL — sin base de datos ni driver Oracle."""

from __future__ import annotations

import pytest

from oss_fm_oracle_mcp.safety import (
    SqlSafetyError,
    ensure_row_limit,
    validate_readonly_sql,
)


def test_allow_select():
    v = validate_readonly_sql("SELECT COUNT(*) FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL")
    assert v.cleaned.upper().startswith("SELECT")
    assert v.has_row_limit is False


def test_allow_with():
    sql = """
    WITH active AS (
      SELECT * FROM ALARMS.ALARM_LOG WHERE CANCEL_TIME IS NULL
    )
    SELECT PERCEIVED_SEVERITY, COUNT(*) AS N FROM active GROUP BY PERCEIVED_SEVERITY
    """
    v = validate_readonly_sql(sql)
    assert v.cleaned.upper().lstrip().startswith("WITH")


def test_allow_select_with_trailing_semicolon():
    v = validate_readonly_sql("SELECT 1 FROM dual;")
    assert "SELECT" in v.cleaned.upper()


def test_reject_update():
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("UPDATE ALARMS.ALARM_LOG SET ALARM_TEXT = 'x' WHERE 1=0")
    assert "UPDATE" in str(ei.value).upper() or "prohibida" in str(ei.value).lower()


def test_reject_drop():
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("DROP TABLE ALARMS.ALARM_LOG")
    msg = str(ei.value).upper()
    assert "DROP" in msg or "SELECT" in msg


def test_reject_delete():
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("DELETE FROM ALARMS.ALARM_LOG WHERE 1=0")


def test_reject_insert():
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("INSERT INTO ALARMS.ALARM_LOG (ALARM_NUMBER) VALUES (1)")


def test_reject_plsql_begin():
    # Sin ';' internos: debe caer por prefijo BEGIN / keyword / marcador PL/SQL
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("BEGIN UPDATE ALARMS.ALARM_LOG SET ALARM_TEXT='x' END")
    msg = str(ei.value).upper()
    assert "BEGIN" in msg or "PL/SQL" in msg or "SELECT" in msg or "PROHIBIDA" in msg


def test_reject_plsql_declare():
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("DECLARE x NUMBER BEGIN x := 1 END")


def test_reject_plsql_assignment_marker():
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("SELECT 1 FROM dual WHERE 1=1 AND dummy := 'x'")
    assert "PL/SQL" in str(ei.value) or ":=" in str(ei.value)


def test_reject_multi_statement():
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("SELECT 1 FROM dual; SELECT 2 FROM dual")
    assert "múltiples" in str(ei.value).lower() or "multiple" in str(ei.value).lower()


def test_reject_empty():
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("   ")


def test_reject_comment_only():
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("-- solo comentario")


def test_reject_set_transaction_from_client():
    """SET está prohibido en SQL del cliente (la sesión RO la fija el driver)."""
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("SET TRANSACTION READ ONLY")


def test_select_with_forbidden_keyword_in_body():
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql("SELECT * FROM ALARMS.ALARM_LOG; DROP TABLE ALARMS.ALARM_LOG")


def test_ensure_row_limit_wraps():
    sql, wrapped = ensure_row_limit("SELECT * FROM ALARMS.ALARM_LOG", 100, has_row_limit=False)
    assert wrapped is True
    # max_rows + 1: la fila extra permite detectar truncated=True
    assert "ROWNUM <= 101" in sql


def test_ensure_row_limit_respects_existing_fetch():
    src = "SELECT * FROM ALARMS.ALARM_LOG FETCH FIRST 10 ROWS ONLY"
    v = validate_readonly_sql(src)
    assert v.has_row_limit is True
    sql, wrapped = ensure_row_limit(v.cleaned, 100, v.has_row_limit)
    assert wrapped is False
    assert sql.upper().startswith("SELECT")
