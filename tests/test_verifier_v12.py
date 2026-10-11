"""v1.2 验收用例：两处收窄（实体编码、单位换算）既不误拦、也不降低标准。"""

from analytics_copilot import verifier

EXECUTED = [
    {
        "index": 1,
        "columns": ["stock_code", "description", "value", "share"],
        "rows": [
            ["23084", "RABBIT NIGHT LIGHT", 56289.81, 0.019],
            ["22086", "PAPER CHAIN KIT", 45173.14, 0.0152],
        ],
    },
    {"index": 2, "columns": ["total"], "rows": [[9747747.93]]},
]


def _verdict(text, numbers, dims=None):
    claims = [{"text": text, "numbers": numbers, "dimension_values": dims or []}]
    statuses, violations = verifier.verify_claims(claims, EXECUTED)
    return statuses[0], violations


def test_entity_code_present_in_results_is_not_a_number_assertion():
    """用例 1：真实存在的商品编码不算数值断言。"""
    status, _ = _verdict(
        "商品 23084（RABBIT NIGHT LIGHT）销售额为 56289.81",
        [{"value": 56289.81, "step": 1}],
        ["23084"],
    )
    assert status["supported"] is True


def test_fabricated_code_is_still_rejected():
    """收窄不等于放宽：结果里没有的编号照样判失败。

    注意带前缀的编号（"商品 99999"）在 v1.1 就被当作标识剔除了，
    这里要测的是**没有前缀、且结果里不存在**的编号。
    """
    status, _ = _verdict(
        "编码 99999 的销售额为 56289.81",
        [{"value": 56289.81, "step": 1}],
    )
    assert status["supported"] is False
    assert any("99999" in item for item in status["problems"])


def test_real_code_does_not_excuse_a_fake_metric_number():
    """真编码被跳过，同一句里编造的指标数字仍要被抓住。"""
    status, _ = _verdict(
        "商品 23084 的销售额为 99999",
        [{"value": 56289.81, "step": 1}],
    )
    assert status["supported"] is False
    assert any("99999" in item for item in status["problems"])


def test_code_in_comparison_context_does_not_excuse_the_ratio():
    """用例 2：编码参与比较时，比较出来的数字仍必须可核对。"""
    status, _ = _verdict(
        "商品 23084 比 22086 高 20%",
        [{"value": 56289.81, "step": 1}],
    )
    assert status["supported"] is False
    assert any("20%" in item for item in status["problems"])


def test_unit_conversion_with_matching_evidence_passes():
    """用例 3：9747747.93 写成「974.77 万」→ 存在确定换算，通过。"""
    status, _ = _verdict(
        "总销售额约 974.77 万", [{"value": 9747747.93, "step": 2}]
    )
    assert status["supported"] is True


def test_unit_conversion_without_source_still_fails():
    """用例 4：没有对应原始数字的「约 100 万」照样判没有来源。"""
    status, _ = _verdict("销售额约 100 万", [])
    assert status["supported"] is False
    assert any("100万" in item for item in status["problems"])


def test_wrong_unit_is_not_rescued():
    """用例 5：把单位换成「千」后换算不上，仍判失败。"""
    status, _ = _verdict(
        "总销售额约 974.77 千", [{"value": 9747747.93, "step": 2}]
    )
    assert status["supported"] is False
