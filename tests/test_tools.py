"""工具层测试：元数据、贡献度分解、以及两类机械检查。"""

from analytics_copilot import tools
from analytics_copilot.db import QueryResult


def test_metadata_tool_lists_whitelist_and_caliber():
    call = tools.metadata_tool()
    assert call.ok
    assert set(call.extra["allowed_views"]) == {"v_sales", "v_returns"}
    assert len(call.extra["caliber"]) >= 5


def test_key_uniqueness_flags_non_unique_keys():
    stats = tools.key_uniqueness()
    assert stats["v_sales"]["rows"] > 500000
    # 一张发票有多行 → invoice_no 在 v_sales 里不唯一
    assert stats["v_sales"]["invoice_no"] < 1.0


def test_decompose_shares_sum_to_one():
    sql = tools.decompose_sql(dimension="country")
    result, checks, _call = tools.sql_tool(sql, "贡献度分解")
    shares = [row[2] for row in result.rows if row[2] is not None]
    assert abs(sum(shares) - 1.0) < 0.05
    assert all(item.ok for item in checks)


def test_share_check_flags_wrong_denominator():
    """占比列合计明显偏离 1 时必须报警。"""
    fake = QueryResult(
        columns=["dimension_value", "share"],
        rows=[["a", 0.2], ["b", 0.2]],
        truncated=False,
        sql="SELECT 1",
    )
    check = tools.check_share_sum(fake)
    assert check.ok is False
    assert "偏离 1" in check.detail


def test_share_check_skips_truncated_result():
    """被截断的结果无法判定占比合计，不应误报。"""
    fake = QueryResult(
        columns=["dimension_value", "share"],
        rows=[["a", 0.2]],
        truncated=True,
        sql="SELECT 1",
    )
    assert tools.check_share_sum(fake).ok is True


def test_fanout_check_flags_join_with_aggregate():
    sql = (
        "SELECT SUM(v_sales.line_revenue) FROM v_sales "
        "JOIN v_returns ON v_sales.invoice_no = v_returns.invoice_no"
    )
    assert tools.check_join_fanout(sql).ok is False
