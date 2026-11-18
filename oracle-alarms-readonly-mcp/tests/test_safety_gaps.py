"""Huecos de la capa de seguridad: literales, comentarios, FOR UPDATE, paquetes, PL/SQL."""

from __future__ import annotations

import json

import pytest

from oracle_alarms_readonly_mcp.formatting import fit_payload
from oracle_alarms_readonly_mcp.safety import (
    SqlSafetyError,
    ensure_row_limit,
    lex_sql,
    validate_readonly_sql,
)

# --- falsos positivos que ahora se aceptan --------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM ALARMS.ALARM_LOG WHERE ALARM_TEXT LIKE '%UPDATE%'",
        "SELECT * FROM ALARMS.ALARM_LOG WHERE ALARM_TEXT = 'a; DROP TABLE x'",
        "SELECT 'BEGIN x := 1; END;' AS s FROM dual",
        "SELECT q'[it's; DELETE]' AS s FROM dual",
        'SELECT 1 AS "COMMENT" FROM dual',
        "SELECT * FROM V$LOCK WHERE ROWNUM <= 5",
        "SELECT REPLACE(ALARM_TEXT, 'a', 'b') FROM ALARMS.ALARM_LOG",
        "SELECT DBMS_LOB.SUBSTR(ADDITIONAL_TEXT, 100, 1) FROM ALARMS.ALARM_LOG",
        "SELECT LISTAGG(x, ',' ON OVERFLOW TRUNCATE) WITHIN GROUP (ORDER BY x) FROM t",
        "(SELECT 1 FROM dual) UNION ALL (SELECT 2 FROM dual)",
        "SELECT /*+ PARALLEL(4) */ COUNT(*) FROM ALARMS.ALARM_LOG",
        "-- comentario inicial\nSELECT 1 FROM dual",
    ],
)
def test_allows_readonly_variants(sql):
    validate_readonly_sql(sql)


def test_literal_with_double_dash_is_preserved_for_execution():
    v = validate_readonly_sql("SELECT * FROM t WHERE c = 'a--b' -- real comment")
    assert "'a--b'" in v.cleaned
    assert "real comment" not in v.cleaned


def test_optimizer_hint_is_kept_but_regular_comment_dropped():
    v = validate_readonly_sql("SELECT /*+ INDEX(a) */ /* nota */ 1 FROM dual a")
    assert "/*+ INDEX(a) */" in v.cleaned
    assert "nota" not in v.cleaned


def test_lex_masks_literals_and_identifiers():
    exe, masked = lex_sql("SELECT 'x''y', \"Col\" FROM t -- c")
    assert "'x''y'" in exe
    assert "x''y" not in masked
    assert "Col" not in masked


# --- huecos que ahora se rechazan -----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM ALARMS.ALARM_LOG FOR UPDATE",
        "SELECT * FROM ALARMS.ALARM_LOG FOR UPDATE NOWAIT",
        "SELECT DBMS_PIPE.SEND_MESSAGE('x') FROM dual",
        "SELECT SYS.DBMS_LOCK.SLEEP(10) FROM dual",
        "SELECT UTL_HTTP.REQUEST('http://evil') FROM dual",
        "SELECT HTTPURITYPE('http://evil').GETCLOB() FROM dual",
        "SELECT DBMS_SCHEDULER.GENERATE_JOB_NAME FROM dual",
        "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f FROM dual",
        "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1 END SELECT f FROM dual",
        "SELECT 1 FROM dual; /* x */ DELETE FROM t",
        "SELECT 1 FROM dual /* unterminated",
        "SELECT 'unterminated FROM dual",
        "/* SELECT */ DELETE FROM t",
        "EXPLAIN PLAN FOR SELECT 1 FROM dual",
        "CALL p()",
        "SELECT 1 FROM dual WHERE EXISTS (SELECT 1 FROM t) MINUS SELECT 1 FROM dual FOR UPDATE",
    ],
)
def test_rejects_unsafe(sql):
    with pytest.raises(SqlSafetyError):
        validate_readonly_sql(sql)


def test_for_update_message_is_explicit():
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("SELECT * FROM t FOR UPDATE")
    assert "FOR UPDATE" in str(ei.value)


def test_package_message_names_package():
    with pytest.raises(SqlSafetyError) as ei:
        validate_readonly_sql("SELECT DBMS_PIPE.SEND_MESSAGE('x') FROM dual")
    assert "DBMS_PIPE" in str(ei.value)


# --- tope de filas / truncado ---------------------------------------------------------


def test_wrap_fetches_one_extra_row_to_detect_truncation():
    sql, wrapped = ensure_row_limit("SELECT * FROM t", 100, has_row_limit=False)
    assert wrapped
    assert "ROWNUM <= 101" in sql


def test_rownum_inside_literal_does_not_count_as_limit():
    v = validate_readonly_sql("SELECT * FROM t WHERE c = 'ROWNUM'")
    assert v.has_row_limit is False


# --- recorte de respuesta -------------------------------------------------------------


def test_fit_payload_trims_rows_and_flags():
    rows = [{"A": "x" * 100, "B": i} for i in range(1000)]
    payload = {"ok": True, "rc": "rc1", "row_count": 1000, "truncated": False, "rows": rows}
    text = fit_payload(payload, 10_000)
    assert len(text) <= 10_000
    data = json.loads(text)
    assert data["response_truncated"] is True
    assert data["truncated"] is True
    assert data["row_count"] == len(data["rows"]) < 1000


def test_fit_payload_multi_rc():
    results = [
        {
            "ok": True,
            "rc": f"rc{i}",
            "row_count": 500,
            "truncated": False,
            "rows": [{"A": "y" * 80}] * 500,
        }
        for i in range(6)
    ]
    text = fit_payload({"ok": True, "results": results}, 20_000)
    data = json.loads(text)
    assert len(text) <= 20_000
    assert all(r["truncated"] for r in data["results"])


def test_fit_payload_small_is_untouched():
    payload = {"ok": True, "rows": [{"A": 1}]}
    assert json.loads(fit_payload(payload, 10_000)) == {"ok": True, "rows": [{"A": 1}]}
