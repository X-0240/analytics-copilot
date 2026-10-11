"""最小分析链路：任务理解 → 缺失信息检测 → 澄清(≤2) → 计划 → 查询 → 检查 → 结论+证据。

M0 的"理解"不是分类器，而是一组**可检查的状态谓词**（缺时间窗 / 缺对比基准 /
指标不明），因此不需要训练也不需要 LLM 也能验证；LLM 只负责把问题映射成谓词
与计划（M0 用确定性桩，接真实模型时替换 `StubLLM` 即可）。
"""

import re
from dataclasses import dataclass, field

from analytics_copilot import config, tools

PERIOD_WORDS = ("最近", "近", "本月", "上月", "今年", "去年", "季度", "周", "月")
COMPARE_WORDS = ("比", "对比", "相比", "下降", "上升", "增加", "减少", "原因")


@dataclass
class Understanding:
    """任务理解结果：五要素 + 缺失清单（缺失项决定是否要澄清）。"""

    question: str
    metric: str = ""
    time_window: str = ""
    dimensions: list[str] = field(default_factory=list)
    comparison_basis: str = ""
    missing: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass
class Conclusion:
    """结论：主张 + 支撑查询 + 结果引用 + 不确定性标注。"""

    claim: str
    evidence: list[dict]
    confidence: str
    caveats: list[str] = field(default_factory=list)


class ClarifyNeeded(Exception):
    """需要向用户澄清；缺什么、为什么缺都要带出来。"""

    def __init__(self, missing, question):
        super().__init__("需要澄清：%s" % ", ".join(missing))
        self.missing = missing
        self.question = question


def understand(question):
    """规则版任务理解：只判断"缺什么"，不判断"属于哪一类"。"""
    text = question.strip()
    metric = ""
    for name, words in (
        ("line_revenue", ("销售", "营收", "收入", "金额")),
        ("quantity", ("数量", "件数", "销量")),
        ("return", ("退货", "退款")),
    ):
        if any(word in text for word in words):
            metric = name
            break
    time_window = next(
        (word for word in PERIOD_WORDS if word in text), ""
    )
    dimensions = [
        name
        for name, words in (
            ("country", ("国家", "地区", "德国", "法国")),
            ("stock_code", ("商品", "产品", "SKU", "品类")),
            ("customer_id", ("客户", "用户", "买家")),
        )
        if any(word in text for word in words)
    ]
    comparison = "对比" if any(w in text for w in COMPARE_WORDS) else ""
    missing = []
    if not metric:
        missing.append("指标（看销售额、销量还是退货？）")
    if not time_window:
        missing.append("时间范围（最近多久？）")
    if not dimensions and not comparison:
        missing.append("分析维度或对比基准（按国家、商品还是客户看？）")
    return Understanding(
        question=text,
        metric=metric,
        time_window=time_window,
        dimensions=dimensions,
        comparison_basis=comparison,
        missing=missing,
    )


def plan(understanding):
    """把理解结果变成 1–3 条只读查询（M0 只覆盖两类常见问法）。"""
    metric = understanding.metric or "line_revenue"
    if metric == "return":
        metric_expr = "COUNT(*)"
        source = "v_returns"
    else:
        metric_expr = "SUM(%s)" % metric
        source = "v_sales"
    dimension = (understanding.dimensions or ["country"])[0]
    return [
        {
            "name": "总量趋势",
            "sql": (
                "SELECT DATE_TRUNC('month', invoice_ts) AS month, "
                "ROUND(%s, 2) AS value FROM %s GROUP BY 1 ORDER BY 1"
                % (metric_expr, source)
            ),
        },
        {
            "name": "维度贡献",
            # 不加 LIMIT：占比合计要能对上 1；行数上限由工具层统一兜住
            "sql": (
                "SELECT %s AS dimension_value, ROUND(SUM(line_revenue), 2) AS value, "
                "ROUND(SUM(line_revenue) / (SELECT SUM(line_revenue) FROM v_sales), 4) "
                "AS share FROM v_sales GROUP BY 1 ORDER BY value DESC"
                % dimension
            ),
        },
    ]


def analyze(question, session):
    """跑完一次分析；`session` 用于记澄清轮数与假设。"""
    understanding = understand(question)
    if understanding.missing:
        if session["clarify_rounds"] < config.MAX_CLARIFY_ROUNDS:
            session["clarify_rounds"] += 1
            raise ClarifyNeeded(understanding.missing, "；".join(understanding.missing))
        # 超过预算：带显式假设继续（冻结稿要求：标注不确定性）
        understanding.assumptions = [
            "按默认口径处理缺失项：" + "；".join(understanding.missing)
        ]
        understanding.time_window = understanding.time_window or "全部时间"
        understanding.dimensions = understanding.dimensions or ["country"]
        understanding.metric = understanding.metric or "line_revenue"

    calls, checks, evidence = [], [], []
    top_share = None
    for step in plan(understanding):
        result, step_checks, call = tools.sql_tool(step["sql"], step["name"])
        calls.append(call)
        checks.extend(step_checks)
        evidence.append(
            {
                "step": step["name"],
                "sql": result.sql,
                "columns": result.columns,
                "sample_rows": result.rows[:3],
                "row_count": len(result.rows),
                "truncated": result.truncated,
            }
        )
        if step["name"] == "维度贡献" and result.rows:
            top_share = (result.rows[0][0], result.rows[0][2])

    risky = [item for item in checks if not item.ok]
    caveats = [item.detail for item in risky] + understanding.assumptions
    if top_share:
        claim = "第一大贡献维度是 %s，占比 %.1f%%" % (
            top_share[0],
            (top_share[1] or 0) * 100,
        )
    else:
        claim = "已给出总量趋势，未找到可比较的维度贡献"
    if top_share and top_share[1] is not None and top_share[1] < 0.5:
        caveats.append(
            "最大维度占比不足 50%，结论不能只看单一因素（可能存在更大量级因素）"
        )
    confidence = "low" if (caveats or risky) else "medium"
    return {
        "understanding": understanding,
        "conclusion": Conclusion(
            claim=claim, evidence=evidence, confidence=confidence, caveats=caveats
        ),
        "tool_calls": calls,
        "checks": checks,
    }


def named_dimension(question):
    """从问题里抠出用户点名的维度值（如"德国"），供后续验证使用。"""
    return next(
        (word for word in re.findall(r"[\u4e00-\u9fa5]{2,4}", question) if word in
         ("德国", "法国", "荷兰", "英国")),
        "",
    )
