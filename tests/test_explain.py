"""解释强度单测：等级判据、因果措辞检测、越界处理。"""

from analytics_copilot import explain, verifier


def _executed(rows, index=1):
    return [{"index": index, "rows": [[v] for v in rows], "row_count": len(rows)}]


def test_level1_for_single_dimension_without_time():
    plan = {"steps": [{"sql": "SELECT country, SUM(line_revenue) FROM v_sales"}]}
    assert explain.evidence_level(plan, _executed([1.0])) == 1


def test_level2_for_dimension_plus_time_series():
    sql = "SELECT country, DATE_TRUNC('month', invoice_ts) FROM v_sales"
    assert explain.evidence_level({"steps": [{"sql": sql}]}, _executed([1.0])) == 2


def test_level2_for_two_dimensions():
    plan = {
        "steps": [
            {"sql": "SELECT country FROM v_sales"},
            {"sql": "SELECT stock_code FROM v_sales"},
        ]
    }
    assert explain.evidence_level(plan, _executed([1.0])) == 2


def test_level_never_exceeds_two():
    """Level 3（因果）在本项目永不允许，因此等级函数不会返回 3。"""
    plan = {
        "steps": [
            {"sql": "SELECT country, DATE_TRUNC('month', invoice_ts) FROM v_sales"},
            {"sql": "SELECT stock_code FROM v_sales"},
        ]
    }
    assert explain.evidence_level(plan, _executed([1.0])) == 2


def test_causal_wording_detected():
    assert explain.has_causal_wording("英国下降导致总额下降")
    assert explain.has_causal_wording("可能是因为促销")
    assert not explain.has_causal_wording("英国占比 84%")


def test_level_caveats_carry_fixed_wording():
    """Level 2 必须带固定边界措辞；Level 1 也要带禁止因果的说明。"""
    assert explain.LEVEL2_CAVEAT in explain.level_caveats(2)
    assert explain.LEVEL3_CAVEAT in explain.level_caveats(1)


def test_v4_marks_causal_claim_as_level_violation():
    plan = {"steps": [{"sql": "SELECT country, SUM(line_revenue) FROM v_sales"}]}
    executed = _executed(["United Kingdom", 100.0])
    claims = [
        {"text": "英国下降导致总额下降", "numbers": [], "dimension_values": ["英国"]}
    ]
    verdict = verifier.verify("为什么销售额下降", plan, executed, claims)
    assert verdict.explanation_level == 1
    assert verifier.level_violations(verdict)
    assert any(explain.LEVEL3_CAVEAT in item for item in verdict.caveats)


def test_causal_topic_detection():
    """因果类提问要被识别出来（v1.1 不允许直接拒答）。"""
    assert explain.is_causal_topic("为什么销售额下降")
    assert explain.is_causal_topic("这个问题属于因果推断类")
    assert not explain.is_causal_topic("各国销售额占比")
