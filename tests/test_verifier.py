"""验证层单测：三类检查都要能真的判对、判错，且不误杀现象描述。"""

from analytics_copilot import verifier


def _executed(rows, index=1, columns=("dimension_value", "share")):
    return [
        {
            "index": index,
            "name": "维度贡献",
            "sql": "SELECT country, share FROM v_sales",
            "columns": list(columns),
            "rows": rows,
            "row_count": len(rows),
        }
    ]


def test_v1_passes_when_number_matches_source():
    claims = [
        {
            "text": "英国贡献 84%",
            "numbers": [{"value": 0.84, "step": 1}],
            "dimension_values": [],
        }
    ]
    statuses, violations = verifier.verify_claims(
        claims, _executed([["United Kingdom", 0.84]])
    )
    assert statuses[0]["supported"] is True
    assert not violations


def test_v1_blocks_when_number_not_supported_by_result():
    claims = [
        {
            "text": "英国贡献 90%",
            "numbers": [{"value": 0.90, "step": 1}],
            "dimension_values": [],
        }
    ]
    statuses, violations = verifier.verify_claims(
        claims, _executed([["United Kingdom", 0.84]])
    )
    assert statuses[0]["supported"] is False
    assert any(item.kind == "V1" for item in violations)


def test_v1_blocks_undeclared_number_in_text():
    """结论里出现没声明来源的数字 → 不支持。"""
    claims = [
        {
            "text": "退货率上升到 12%",
            "numbers": [],
            "dimension_values": [],
        }
    ]
    statuses, _violations = verifier.verify_claims(
        claims, _executed([["Germany", 0.5]])
    )
    assert statuses[0]["supported"] is False


def test_v2_blocks_dimension_value_not_in_result():
    claims = [
        {
            "text": "德国退货最多",
            "numbers": [],
            "dimension_values": ["德国"],
        }
    ]
    statuses, violations = verifier.verify_claims(
        claims, _executed([["United Kingdom", 0.5]])
    )
    assert statuses[0]["supported"] is False
    assert any(item.kind == "V2" for item in violations)


def test_v2_accepts_chinese_alias_present_in_result():
    claims = [
        {
            "text": "德国销售额最高",
            "numbers": [],
            "dimension_values": ["德国"],
        }
    ]
    statuses, _violations = verifier.verify_claims(
        claims, _executed([["Germany", 0.5]])
    )
    assert statuses[0]["supported"] is True


def test_v3_flags_attribution_with_single_dimension():
    plan = {"steps": [{"sql": "SELECT country, SUM(line_revenue) FROM v_sales"}]}
    violations, caveats = verifier.verify_sufficiency(
        "销售额为什么下降", plan, _executed([["Germany", 0.5]])
    )
    assert any(item.kind == "V3" for item in violations)
    assert any("不能支持因果归因" in item for item in caveats)


def test_v3_flags_empty_step():
    executed = _executed([])
    violations, caveats = verifier.verify_sufficiency("各国销售额", {"steps": []}, executed)
    assert any("返回 0 行" in item for item in caveats)
    assert any(item.kind == "V3" for item in violations)


def test_overall_status_unsupported_when_all_claims_blocked():
    claims = [
        {"text": "英国 90%", "numbers": [{"value": 0.9, "step": 1}],
         "dimension_values": []}
    ]
    verdict = verifier.verify(
        "各国销售额", {"steps": []}, _executed([["UK", 0.84]]), claims
    )
    assert verdict.status == "unsupported"
    assert verdict.blocked()


def test_overall_status_not_degraded_for_clean_claims():
    claims = [
        {"text": "英国 84%", "numbers": [{"value": 0.84, "step": 1}],
         "dimension_values": ["United Kingdom"]}
    ]
    verdict = verifier.verify(
        "各国销售额占比",
        {"steps": [{"sql": "SELECT country FROM v_sales"}]},
        _executed([["United Kingdom", 0.84]]),
        claims,
    )
    assert verdict.status == "supported"
    assert not verdict.violations
