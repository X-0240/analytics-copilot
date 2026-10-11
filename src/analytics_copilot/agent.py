"""最小分析链路：任务理解 → 缺失信息检测 → 澄清(≤2) → 计划 → 查询 → 检查 → 结论+证据。

M0 的"理解"不是分类器，而是一组**可检查的状态谓词**（缺时间窗 / 缺对比基准 /
指标不明），因此不需要训练也不需要 LLM 也能验证；LLM 只负责把问题映射成谓词
与计划（M0 用确定性桩，接真实模型时替换 `StubLLM` 即可）。
"""

import re
from dataclasses import dataclass, field

from analytics_copilot import config, llm, planner, tools, verifier

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


class Refused(Exception):
    """超出能力边界（预测/因果/无成本字段等）：明确拒答并给原因，不硬答。"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


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
    # 只有在问题本身要求"比较/拆解"时才把缺维度当成澄清理由；
    # 单纯看趋势的问题不需要维度，默认按总量趋势回答并记假设。
    needs_dimension = bool(comparison) or any(
        word in text for word in ("哪个", "哪些", "主要", "拆解", "贡献", "排名", "前十")
    )
    missing = []
    if not metric:
        missing.append("指标（看销售额、销量还是退货？）")
    if not dimensions and needs_dimension:
        missing.append("分析维度（按国家、商品还是客户看？）")
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


def analyze(question, session, client=None, verify_layer=True):
    """跑完一次分析；`session` 用于记澄清轮数与假设。

    两条路径共用前半段（理解 + 澄清预算），只在"规划"处分叉：
    - 无 key / 桩：走 M0 的规则计划（离线可复现，测试默认走这条）；
    - 有 key：走受控 LLM 规划（`planner`），SQL 仍受 M0 护栏约束。
    """
    client = client or planner.build_client()
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

    if not understanding.time_window:
        # 缺时间窗不澄清，按全部时间处理并显式记成假设
        understanding.time_window = "全部时间（未指定，按 2010-12 ~ 2011-12）"
        understanding.assumptions.append(understanding.time_window)

    if isinstance(client, llm.StubLLM) or config.FORCE_STUB:
        return _rule_path(question, understanding)
    return _llm_path(client, question, understanding, verify_layer)


def _rule_path(question, understanding):
    """M0 的确定性路径：规则计划 + 规则结论（保持回归测试不变）。"""
    hint = planner.scope_hint(question)
    if hint:
        # 能力边界（机械可判）：与 LLM 路径保持同一套拒答口径
        raise Refused(
            "问题涉及 %s，超出本项目能力边界：只做历史数据的探索/比较/异常/拆解，"
            "不做预测与因果推断，且数据集没有成本字段。" % "、".join(hint)
        )
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
        "mode": "rule",
        "understanding": understanding,
        "conclusion": Conclusion(
            claim=claim, evidence=evidence, confidence=confidence, caveats=caveats
        ),
        "tool_calls": calls,
        "checks": checks,
        "plan": {"scope": "ok", "steps": plan(understanding), "refuse_reason": ""},
        "cost_cny": 0.0,
    }


def _llm_path(client, question, understanding, verify_layer=True):
    """受控 LLM 路径：LLM 出计划与结论，护栏决定能不能执行。"""
    plan_obj = planner.ask_plan(
        client, question, understanding, understanding.assumptions
    )
    if plan_obj["scope"] == "refuse":
        raise Refused(plan_obj["refuse_reason"])

    executed, calls, checks, evidence = [], [], [], []
    for step in plan_obj["steps"]:
        result, step_checks, call = tools.sql_tool(step["sql"], step["name"])
        calls.append(call)
        checks.extend(step_checks)
        row = {
            "index": step["index"],
            "name": step["name"],
            "sql": step["sql"],
            "columns": result.columns,
            "rows": result.rows[:5],
            "row_count": len(result.rows),
            "checks": [
                {"name": item.name, "ok": item.ok, "detail": item.detail}
                for item in step_checks
            ],
        }
        executed.append(row)
        evidence.append(
            {
                "step": "%d %s" % (step["index"], step["name"]),
                "sql": result.sql,
                "columns": result.columns,
                "sample_rows": result.rows[:3],
                "row_count": len(result.rows),
                "truncated": result.truncated,
            }
        )

    conclusion_obj = planner.ask_conclusion(client, question, executed)
    risky = [item for item in checks if not item.ok]
    caveats = list(conclusion_obj["caveats"]) + [item.detail for item in risky]
    caveats.extend(understanding.assumptions)
    if any(item["truncated"] for item in evidence):
        caveats.append("至少一步结果被截断，结论可能不完整")
    claims = conclusion_obj["claims"]
    verdict = None
    if verify_layer:
        # 验证层 v1：逐条机械判定结论是否被证据支持
        verdict = verifier.verify(question, plan_obj, executed, claims)
        caveats.extend(verdict.caveats)
    # 机械优先：护栏或验证层发现问题时，不允许结论自称高置信
    confidence = conclusion_obj["confidence"]
    if (risky or (verdict and verdict.blocked())) and confidence in (
        "high",
        "medium",
    ):
        confidence = "low"
    claim_text = conclusion_obj["claim"]
    if verdict and verdict.status == "unsupported":
        # 拿不到证据支持时**不输出强结论**，只说明现状与缺什么
        claim_text = (
            "本问题当前拿不到被证据支持的结论：结论中的关键数字无法追溯到查询结果，"
            "已按验证层判定降级，仅列出证据与不确定性。"
        )
        confidence = "low"
    return {
        "mode": "llm",
        "understanding": understanding,
        "conclusion": Conclusion(
            claim=claim_text,
            evidence=evidence,
            confidence=confidence,
            caveats=caveats,
        ),
        "tool_calls": calls,
        "checks": checks,
        "plan": plan_obj,
        # 供评测层离线复评（服务层不返回这个内部字段）
        "executed": executed,
        "claims": claims,
        "verification": (
            {
                "status": verdict.status,
                "blocked": len(verdict.blocked()),
                "downgraded": len(verdict.downgraded()),
                "claim_status": verdict.claim_status,
            }
            if verdict
            else {"status": "not_run", "blocked": 0, "downgraded": 0,
                  "claim_status": []}
        ),
        "cost_cny": round(
            plan_obj.get("cost_cny", 0.0) + conclusion_obj.get("cost_cny", 0.0), 6
        ),
    }


def named_dimension(question):
    """从问题里抠出用户点名的维度值（如"德国"），供后续验证使用。"""
    return next(
        (word for word in re.findall(r"[\u4e00-\u9fa5]{2,4}", question) if word in
         ("德国", "法国", "荷兰", "英国")),
        "",
    )
