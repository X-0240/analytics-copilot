"""链路测试：完整问题给结论、缺信息要澄清、超预算带假设继续。"""

import pytest

from analytics_copilot import agent


def _session():
    return {"clarify_rounds": 0, "history": []}


def test_complete_question_returns_conclusion_with_evidence():
    outcome = agent.analyze("最近三个月销售额为什么下降？按国家看", _session())
    conclusion = outcome["conclusion"]
    assert conclusion.claim
    assert conclusion.evidence
    assert all(item["sql"] for item in conclusion.evidence)
    assert outcome["understanding"].metric == "line_revenue"


def test_missing_metric_triggers_clarification_twice_then_assumes():
    """澄清预算 2 轮：前两次问，第三次带显式假设继续。"""
    session = _session()
    for _ in range(2):
        with pytest.raises(agent.ClarifyNeeded):
            agent.analyze("为什么最近不行？", session)
    outcome = agent.analyze("为什么最近不行？", session)
    assert outcome["understanding"].assumptions
    assert outcome["conclusion"].caveats


def test_understanding_is_predicates_not_categories():
    """任务理解只判断「缺什么」，不带类别标签。"""
    got = agent.understand("看看德国的情况")
    assert "country" in got.dimensions
    assert "指标（看销售额、销量还是退货？）" in got.missing
    assert not hasattr(got, "category")
