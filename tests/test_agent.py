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


def test_execution_failure_degrades_to_unable_to_complete(monkeypatch):
    """执行期 SQL 失败要降级为"无法完成"，不是系统错误。"""
    from analytics_copilot import config, db, tools

    def boom(sql, context=""):
        raise db.SqlRejected("BAD_SQL", "列不存在")

    monkeypatch.setattr(tools, "sql_tool", boom)
    # 默认 AC_FORCE_STUB=1 会走确定性规则路径，这里显式打开 LLM 路径
    monkeypatch.setattr(config, "FORCE_STUB", False)

    class FakePlanClient:
        name = "fake"

        def complete_json(self, system, user, max_tokens=900):
            import json as _json

            payload = {
                "scope": "ok",
                "assumptions": [],
                "steps": [{"name": "x", "purpose": "y", "sql": "SELECT 1 FROM v_sales"}],
            }
            return _json.dumps(
                {"content": _json.dumps(payload, ensure_ascii=False), "cost_cny": 0}
            )

    with pytest.raises(agent.UnableToComplete):
        agent.analyze("最近销售额", _session(), client=FakePlanClient())


def test_causal_refusal_becomes_level2_answer(monkeypatch):
    """因果类提问不再直接拒答：给证据允许的等级 + 固定边界措辞。"""
    from analytics_copilot import config

    monkeypatch.setattr(config, "FORCE_STUB", False)

    class RefuseClient:
        name = "fake"

        def complete_json(self, system, user, max_tokens=900):
            import json as _json

            payload = {
                "scope": "refuse",
                "refuse_reason": "这是因果推断问题，超出能力边界",
                "assumptions": [],
                "steps": [],
            }
            return _json.dumps(
                {"content": _json.dumps(payload, ensure_ascii=False), "cost_cny": 0}
            )

    outcome = agent.analyze(
        "最近三个月销售额为什么下降？按国家看", _session(), client=RefuseClient()
    )
    assert outcome["mode"] == "llm_causal_fallback"
    assert outcome["explanation_level"] in (1, 2)
    assert outcome["conclusion"].confidence == "low"
    caveats = " ".join(outcome["conclusion"].caveats)
    assert "不能证明因果" in caveats


def test_broken_conclusion_json_degrades_to_unable_to_complete(monkeypatch):
    """结论 JSON 解析失败也走结构化降级（与计划失败同一口径）。"""
    from analytics_copilot import config

    monkeypatch.setattr(config, "FORCE_STUB", False)

    class BrokenConclusionClient:
        name = "fake"

        def __init__(self):
            self.calls = 0

        def complete_json(self, system, user, max_tokens=900):
            import json as _json

            self.calls += 1
            if self.calls == 1:
                payload = {
                    "scope": "ok",
                    "assumptions": [],
                    "steps": [
                        {
                            "name": "总量趋势",
                            "purpose": "看走势",
                            "sql": (
                                "SELECT DATE_TRUNC('month', invoice_ts) AS month, "
                                "ROUND(SUM(line_revenue), 2) AS value FROM v_sales "
                                "GROUP BY 1 ORDER BY 1"
                            ),
                        }
                    ],
                }
                content = _json.dumps(payload, ensure_ascii=False)
            else:
                content = "{这不是合法 JSON"
            return _json.dumps({"content": content, "cost_cny": 0})

    with pytest.raises(agent.UnableToComplete):
        agent.analyze(
            "最近三个月销售额走势怎么样", _session(), client=BrokenConclusionClient()
        )


def test_rule_path_carries_level_boundary_wording():
    """无 key 的规则路径同样受等级约束：Level 2 必须带固定边界措辞。"""
    outcome = agent.analyze("最近三个月销售额走势怎么样", _session())
    caveats = " ".join(outcome["conclusion"].caveats)
    if outcome["explanation_level"] == 2:
        assert "不能证明因果关系" in caveats
    assert "不支持因果结论" in caveats


def test_causal_fallback_gives_checkable_facts_and_next_steps(monkeypatch):
    """兜底回答要给可核查事实、写清口径、给下一步问法，而不是只说"我不能回答"。"""
    from analytics_copilot import config

    monkeypatch.setattr(config, "FORCE_STUB", False)

    class RefuseClient:
        name = "fake"

        def complete_json(self, system, user, max_tokens=900):
            import json as _json

            payload = {
                "scope": "refuse",
                "refuse_reason": "因果推断超出边界",
                "assumptions": [],
                "steps": [],
            }
            return _json.dumps(
                {"content": _json.dumps(payload, ensure_ascii=False), "cost_cny": 0}
            )

    outcome = agent.analyze(
        "最近三个月销售额为什么下降？按国家看", _session(), client=RefuseClient()
    )
    claims = outcome["claims"]
    assert len(claims) >= 2
    # 每条事实都必须被验证层判为"有证据支持"（数字来源对得上结果行）
    assert all(item["supported"] for item in outcome["verification"]["claim_status"])
    assert outcome["verification"]["blocked"] == 0
    caveats = " ".join(outcome["conclusion"].caveats)
    assert "不能证明因果" in caveats
    assert "口径" in caveats
    assert len(outcome["next_questions"]) == 2


def test_time_window_is_applied_to_sql_and_declared():
    """「最近三个月」必须进 SQL，并且口径写进答案（P0-1）。"""
    outcome = agent.analyze("最近三个月销售额为什么下降？按国家看", _session())
    sqls = " ".join(step["sql"] for step in outcome["plan"]["steps"])
    assert "invoice_ts" in sqls
    assert "2011-10-01" in sqls
    assert outcome["time_window"]["applied"] is True
    caveats = " ".join(outcome["conclusion"].caveats)
    assert "2011-10-01" in caveats and "末月不完整" in caveats


def test_vague_recent_stays_full_period():
    """只有「最近」时不加时间过滤（避免误伤对比类问题），但口径要如实声明。"""
    outcome = agent.analyze("最近哪个国家的销售额下降最多？", _session())
    sqls = " ".join(step["sql"] for step in outcome["plan"]["steps"])
    assert outcome["time_window"]["applied"] is False
    assert "invoice_ts >= DATE" not in sqls
    assert "全期" in " ".join(outcome["conclusion"].caveats)
