"""时间窗解析测试：明确的"N 个月"才生效，模糊的"最近"不猜。"""

from analytics_copilot import window

PROFILE = {"ts_min": "2010-12-01 08:26:00", "ts_max": "2011-12-09 12:50:00"}


def test_explicit_months_resolve_to_calendar_months():
    got = window.resolve("最近三个月销售额为什么下降", PROFILE)
    assert got["applied"] is True
    assert got["requested"] == "最近三个月"
    assert got["resolved_range"] == ["2011-10-01", "2011-12-09"]
    # 末月只到 12-09，属不完整月
    assert got["complete_period"] is False


def test_anchor_is_dataset_max_not_today():
    """锚点必须是数据集最后一天，否则会查空。"""
    got = window.resolve("最近一个月销售额", PROFILE)
    assert got["anchor"] == "dataset_max_date"
    assert got["resolved_range"][1] == "2011-12-09"
    assert got["resolved_range"][0] == "2011-12-01"


def test_other_units_and_writing_styles():
    assert window.resolve("近 3 个月销售额", PROFILE)["n"] == 3
    assert window.resolve("最近两周的退货", PROFILE)["unit"] == "week"
    assert window.resolve("最近7天销售额", PROFILE)["unit"] == "day"
    assert window.resolve("近三月销售额", PROFILE)["n"] == 3


def test_vague_recent_is_not_guessed():
    """只有「最近」两个字时不许猜——否则对比类问题会被误过滤成短窗口。"""
    assert window.resolve("最近哪个国家的销售额下降最多", PROFILE) is None
    assert window.resolve("最近情况怎么样", PROFILE) is None
    fallback = window.full_period(PROFILE)
    assert fallback["applied"] is False
    assert window.where_clause(fallback) == ""
    assert "全期" in window.describe(fallback)


def test_where_clause_and_description_share_one_source():
    got = window.resolve("最近三个月销售额", PROFILE)
    where = window.where_clause(got)
    assert "2011-10-01" in where and "2011-12-09 12:50:00" in where
    described = window.describe(got)
    assert "2011-10-01" in described and "2011-12-09" in described
    assert "末月不完整" in described
