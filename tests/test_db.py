"""护栏测试：白名单、单语句、只读、行数上限——四条都要真的挡得住。"""

import duckdb
import pytest

from analytics_copilot import config, db


def test_non_whitelisted_table_is_rejected():
    """原始表不在白名单里，不能绕过口径列直接查。"""
    with pytest.raises(db.SqlRejected) as exc:
        db.check_sql("SELECT * FROM raw_online_retail")
    assert exc.value.code == "NOT_WHITELISTED"


def test_multi_statement_is_rejected():
    with pytest.raises(db.SqlRejected) as exc:
        db.check_sql("SELECT 1 FROM v_sales; SELECT 2 FROM v_sales")
    assert exc.value.code == "MULTI_STATEMENT"


def test_write_keyword_is_rejected():
    with pytest.raises(db.SqlRejected) as exc:
        db.check_sql("SELECT 1 FROM v_sales UNION SELECT 2 -- drop")
    assert exc.value.code == "FORBIDDEN_KEYWORD"


def test_non_select_is_rejected():
    with pytest.raises(db.SqlRejected) as exc:
        db.check_sql("FROM v_sales SELECT 1")
    assert exc.value.code == "NOT_SELECT"


def test_row_limit_truncates():
    """超上限时截断并标记，不能把整表拉回来。"""
    result = db.run_sql("SELECT * FROM v_sales", max_rows=50)
    assert len(result.rows) == 50
    assert result.truncated is True


def test_connection_is_physically_read_only():
    """物理只读：即使绕过 SQL 检查，连接本身也写不进去。"""
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    try:
        with pytest.raises(duckdb.Error):
            con.execute("CREATE TABLE should_not_exist (x INT)")
    finally:
        con.close()
