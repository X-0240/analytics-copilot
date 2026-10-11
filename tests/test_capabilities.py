"""能力卡测试：事实来自 metadata，拒答理由引用它。"""

import pytest

from analytics_copilot import agent, capabilities, planner


def test_card_facts_come_from_metadata():
    card = capabilities.card()
    assert card["rows"] == 541909
    assert card["time_range"] == ["2010-12-01", "2011-12-09"]
    # 指标与维度是按只读视图的实际列派生的，不是手写清单
    assert "销售额" in card["metrics"] and "退货" in card["metrics"]
    assert {"国家", "商品", "客户"} <= set(card["dimensions"])
    labels = [item["label"] for item in card["unsupported"]]
    assert "成本 / 利润" in labels and "预测" in labels


def test_refusal_reason_cites_capability_card():
    with pytest.raises(agent.Refused) as excinfo:
        agent._rule_path("哪个月的利润最高", agent.understand("哪个月的利润最高"))
    reason = excinfo.value.reason
    assert "成本 / 利润" in reason
    assert "没有成本或利润字段" in reason
    assert "当前可分析" in reason


def test_capability_check_runs_before_clarification():
    """问"利润"时不能先反问"看哪个指标"——边界要先于澄清。"""
    with pytest.raises(agent.Refused) as excinfo:
        agent.analyze("哪个月的利润最高", {"clarify_rounds": 0, "history": []})
    assert "成本 / 利润" in excinfo.value.reason


def test_planner_word_list_is_single_sourced():
    """planner 的拒答词表必须来自能力卡，否则两处会打架。"""
    assert set(planner.REFUSE_WORDS) == set(capabilities.capability_words())
    assert planner.scope_hint("预测下个月的销售额")
    assert not planner.scope_hint("各国销售额占比")
