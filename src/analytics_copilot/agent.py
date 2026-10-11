"""最小分析链路：任务理解 → 缺失信息检测 → 澄清(≤2) → 计划 → 查询 → 检查 → 结论+证据。

M0 的"理解"不是分类器，而是一组**可检查的状态谓词**（缺时间窗 / 缺对比基准 /
指标不明），因此不需要训练也不需要 LLM 也能验证；LLM 只负责把问题映射成谓词
与计划（M0 用确定性桩，接真实模型时替换 `StubLLM` 即可）。
"""

import json
import re
from dataclasses import dataclass, field

from analytics_copilot import (
    config,
    db,
    explain,
    llm,
    planner,
    tools,
    verifier,
    window,
)

PERIOD_WORDS = ("最近", "近", "本月", "上月", "今年", "去年", "季度", "周", "月")
COMPARE_WORDS = ("比", "对比", "相比", "下降", "上升", "增加", "减少", "原因")


@dataclass
class Understanding:
    """任务理解结果：五要素 + 缺失清单（缺失项决定是否要澄清）。"""

    question: str
    metric: str = ""
    time_window: str = ""
    # 可解析的时间窗短语（如「最近三个月」）；空串表示没有明确窗口
    window_request: str = ""
    dimensions: list[str] = field(default_factory=list)
    # 维度是"用户点名"还是"系统默认"：P0-2 要求默认可见，所以必须记下来
    dimension_source: str = ""
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

    def __init__(self, reason, cost_cny=0.0):
        super().__init__(reason)
        self.reason = reason
        # 拒答前可能已经调过模型：成本要跟着异常走，否则预算会被低估
        self.cost_cny = cost_cny


class UnableToComplete(Exception):
    """两次都拿不到可执行计划/合法结论：**降级为"无法完成"**，不是系统错误。"""

    def __init__(self, reason, code="UNABLE_TO_COMPLETE", cost_cny=0.0):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.cost_cny = cost_cny


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
    window_request = window.requested_window(text)
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
        window_request=window_request,
        dimensions=dimensions,
        dimension_source="explicit" if dimensions else "",
        comparison_basis=comparison,
        missing=missing,
    )


def resolve_window(understanding):
    """解析时间窗；解析不出来时返回"全期"口径对象（口径必须显式，不猜）。"""
    profile = (db.load_metadata() or {}).get("profile") or {}
    resolved = window.resolve(
        understanding.window_request or understanding.question, profile
    )
    return resolved or window.full_period(profile)


def analysis_context(understanding, win):
    """把"系统实际分析的是什么"写成一个显式对象（P0-2：不禁止默认，禁止隐藏默认）。

    返回结构化字段 + 一句可直接展示的文案；口径文案与 SQL 用的是同一个时间窗对象。
    """
    dimension = (understanding.dimensions or ["country"])[0]
    source = understanding.dimension_source or (
        "explicit" if understanding.dimensions else "default"
    )
    metric = understanding.metric or "line_revenue"
    metric_label = METRIC_LABELS.get(metric, metric)
    dimension_label = DIMENSION_LABELS.get(dimension, dimension)
    window_label = win.get("requested") or "全期"
    start, end = win["resolved_range"]
    source_text = "问题中指定" if source == "explicit" else "未指定，按默认"
    text = (
        "分析口径：指标=%s；维度=%s（%s）；时间范围=%s（%s ~ %s）"
        % (metric_label, dimension_label, source_text, window_label, start, end)
    )
    if source != "explicit":
        text += "；可指定维度：国家 / 商品 / 客户"
    return {
        "metric": metric,
        "metric_label": metric_label,
        "dimension": dimension,
        "dimension_label": dimension_label,
        "dimension_source": source,
        "time_window": window_label,
        "time_range": list(win["resolved_range"]),
        "text": text,
    }


def plan(understanding, win=None):
    """把理解结果变成 1–3 条只读查询（M0 只覆盖两类常见问法）。

    `win` 是 `window.resolve()` 的结果：有时间窗时两个步骤都要带过滤，
    占比分母也必须用同一过滤条件，否则占比合计不等于 1。
    """
    metric = understanding.metric or "line_revenue"
    if metric == "return":
        metric_expr = "COUNT(*)"
        source = "v_returns"
    else:
        metric_expr = "SUM(%s)" % metric
        source = "v_sales"
    dimension = (understanding.dimensions or ["country"])[0]
    where = window.where_clause(win)
    filtered = (" WHERE " + where) if where else ""
    denominator = "SELECT SUM(line_revenue) FROM v_sales" + filtered
    return [
        {
            "name": "总量趋势",
            "sql": (
                "SELECT DATE_TRUNC('month', invoice_ts) AS month, "
                "ROUND(%s, 2) AS value FROM %s%s GROUP BY 1 ORDER BY 1"
                % (metric_expr, source, filtered)
            ),
        },
        {
            "name": "维度贡献",
            # 不加 LIMIT：占比合计要能对上 1；行数上限由工具层统一兜住
            "sql": (
                "SELECT %s AS dimension_value, ROUND(SUM(line_revenue), 2) AS value, "
                "ROUND(SUM(line_revenue) / (%s), 4) "
                "AS share FROM v_sales%s GROUP BY 1 ORDER BY value DESC"
                % (dimension, denominator, filtered)
            ),
        },
    ]


def analyze(question, session, client=None, verify_layer=True, meter=None):
    """跑完一次分析；`session` 用于记澄清轮数与假设。

    两条路径共用前半段（理解 + 澄清预算），只在"规划"处分叉：
    - 无 key / 桩：走 M0 的规则计划（离线可复现，测试默认走这条）；
    - 有 key：走受控 LLM 规划（`planner`），SQL 仍受 M0 护栏约束。

    `meter`（`llm.CostMeter`）用于把**本次请求的全部模型调用**记账，
    包括失败重试与拒答前的那几次；服务层拿它做日预算熔断。
    """
    meter = meter if meter is not None else llm.CostMeter()
    client = client or planner.build_client(meter)
    if getattr(client, "meter", None) is None:
        # 外部传入的客户端也要记账（评测 runner 会传桩或真实客户端）
        client.meter = meter
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
        understanding.dimension_source = (
            understanding.dimension_source or "default"
        )
        understanding.metric = understanding.metric or "line_revenue"

    if not understanding.time_window:
        # 缺时间窗不澄清，按全部时间处理并显式记成假设
        understanding.time_window = "全部时间（未指定，按 2010-12 ~ 2011-12）"
        understanding.assumptions.append(understanding.time_window)

    if isinstance(client, llm.StubLLM) or config.FORCE_STUB:
        return _rule_path(question, understanding)
    outcome = _llm_path(client, question, understanding, verify_layer, meter)
    # 以计量器为准：成功路径的 cost_cny 只覆盖"最后两次调用"，
    # 而预算要覆盖"整次请求真正花了多少"
    if meter.calls:
        outcome["cost_cny"] = round(meter.total, 6)
    return outcome


def _rule_path(question, understanding):
    """M0 的确定性路径：规则计划 + 规则结论（保持回归测试不变）。"""
    hint = planner.scope_hint(question)
    if hint:
        # 能力边界（机械可判）：与 LLM 路径保持同一套拒答口径
        raise Refused(
            "问题涉及 %s，超出本项目能力边界：只做历史数据的探索/比较/异常/拆解，"
            "不做预测与因果推断，且数据集没有成本字段。" % "、".join(hint)
        )
    win = resolve_window(understanding)
    calls, checks, evidence = [], [], []
    top_share = None
    for step in plan(understanding, win):
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
    # 口径必须显式：SQL 用的窗口与答案写的窗口来自同一个对象
    caveats = [window.describe(win)] + [item.detail for item in risky]
    caveats.extend(understanding.assumptions)
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
    rule_plan = plan(understanding, win)
    level = explain.evidence_level(
        {"steps": rule_plan},
        [{"row_count": ev["row_count"]} for ev in evidence],
    )
    # v1.1：等级边界措辞对所有出口生效（含无 key 的规则路径），
    # 否则 Level 2 的结论会缺"不能证明因果关系"这句必须有的声明。
    caveats.extend(explain.level_caveats(level))
    return {
        "mode": "rule",
        "understanding": understanding,
        "conclusion": Conclusion(
            claim=claim, evidence=evidence, confidence=confidence, caveats=caveats
        ),
        "tool_calls": calls,
        "checks": checks,
        "plan": {"scope": "ok", "steps": rule_plan, "refuse_reason": ""},
        "time_window": win,
        "analysis_context": analysis_context(understanding, win),
        "explanation_level": level,
        "level_violations": [],
        "cost_cny": 0.0,
    }


def _llm_path(client, question, understanding, verify_layer=True, meter=None):
    """受控 LLM 路径：LLM 出计划与结论，护栏决定能不能执行。"""
    win = resolve_window(understanding)
    try:
        plan_obj = planner.ask_plan(
            client, question, understanding, understanding.assumptions, win
        )
    except (planner.PlanInvalid, db.SqlRejected, json.JSONDecodeError) as exc:
        # 修不好就不再抛异常：给用户一个明确的"我做不到 + 原因"
        raise UnableToComplete(
            "两次都没能生成可执行的分析计划：%s" % exc,
            cost_cny=(meter.total if meter else 0.0),
        ) from exc
    if plan_obj["scope"] == "refuse":
        if explain.is_causal_topic(question, plan_obj["refuse_reason"]):
            # 因果类提问不允许直接拒答（v1.1）：改走确定性计划，
            # 只给证据允许的事实与相关变化，并强制附加边界措辞。
            return _causal_fallback(question, understanding, plan_obj)
        raise Refused(
            plan_obj["refuse_reason"], cost_cny=(meter.total if meter else 0.0)
        )

    executed, calls, checks, evidence = [], [], [], []
    for step in plan_obj["steps"]:
        try:
            result, step_checks, call = tools.sql_tool(step["sql"], step["name"])
        except db.SqlRejected as exc:
            # 执行期才暴露的 SQL 问题（列不存在、类型不匹配等）同样按"做不到"降级，
            # 不让用户体验到系统错误。
            raise UnableToComplete(
                "查询执行失败（第 %d 步）：%s" % (step["index"], exc),
                cost_cny=(meter.total if meter else 0.0),
            ) from exc
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

    try:
        conclusion_obj = planner.ask_conclusion(client, question, executed)
    except (planner.PlanInvalid, json.JSONDecodeError) as exc:
        raise UnableToComplete(
            "结论输出不合法：%s" % exc,
            cost_cny=(meter.total if meter else 0.0),
        ) from exc
    risky = [item for item in checks if not item.ok]
    caveats = list(conclusion_obj["caveats"]) + [item.detail for item in risky]
    caveats.extend(understanding.assumptions)
    # 时间窗口径：计划里没有 invoice_ts 过滤就如实说明，别让用户以为已经过滤
    if win.get("applied"):
        sqls = " ".join(str(step.get("sql", "")) for step in plan_obj["steps"]).lower()
        if "invoice_ts" in sqls:
            caveats.append(window.describe(win))
        else:
            caveats.append(
                "计划没有按“%s”过滤时间窗，下面的结果覆盖全期" % win["requested"]
            )
    else:
        caveats.append(window.describe(win))
    if any(item["truncated"] for item in evidence):
        caveats.append("至少一步结果被截断，结论可能不完整")
    claims = conclusion_obj["claims"]
    verdict = None
    level_violations = []
    if verify_layer:
        # 验证层 v1：逐条机械判定结论是否被证据支持
        verdict = verifier.verify(question, plan_obj, executed, claims)
        caveats.extend(verdict.caveats)
        level_violations = [
            {"claim": item.claim_index, "detail": item.detail}
            for item in verifier.level_violations(verdict)
        ]
    # 机械优先：护栏或验证层发现问题时，不允许结论自称高置信
    confidence = conclusion_obj["confidence"]
    if (risky or (verdict and verdict.blocked()) or level_violations) and confidence in (
        "high",
        "medium",
    ):
        confidence = "low"
    if level_violations:
        # 解释强度越界：不重写结论，但必须显式说明只支持到哪一级
        caveats.append("结论中的因果措辞已被降级处理，本次只支持到 Level %d" % (
            verdict.explanation_level if verdict else 1
        ))
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
        "explanation_level": verdict.explanation_level if verdict else explain.evidence_level(
            plan_obj, executed
        ),
        "level_violations": level_violations,
        "time_window": win,
        "analysis_context": analysis_context(understanding, win),
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


def _causal_fallback(question, understanding, plan_obj):
    """因果类提问的确定性兜底：给能查到的事实 + 口径 + 下一步问法，不给因果结论。

    为什么不让 LLM 再试一次：模型在因果类问题上倾向于直接拒答（实测两次），
    继续追问反而浪费调用。三条设计约束：

    1. **只陈述能从结果行机械读出的事实**，数字逐字来自结果并声明来源步骤；
    2. **走同一套验证层**（V1/V2/V3/V4），兜底回答不再享有"免检"；
    3. **写清口径与下一步**——不丢下一句"我不能回答"就走。
    """
    win = resolve_window(understanding)
    rule_steps = plan(understanding, win)
    executed, evidence, checks, calls = [], [], [], []
    for step in rule_steps:
        result, step_checks, call = tools.sql_tool(step["sql"], step["name"])
        checks.extend(step_checks)
        calls.append(call)
        executed.append(
            {
                "index": len(executed) + 1,
                "name": step["name"],
                "sql": result.sql,
                "columns": result.columns,
                "rows": result.rows,
                "row_count": len(result.rows),
            }
        )
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
    level = explain.evidence_level({"steps": rule_steps}, executed)
    claims = _fallback_claims(executed, understanding)
    verdict = verifier.verify(question, {"steps": rule_steps}, executed, claims)
    caveats = [
        "模型原判为拒答（%s），按 v1.1 因果类提问不允许直接拒答，已改为给出证据允许的层级"
        % plan_obj["refuse_reason"]
    ]
    caveats.extend(verdict.caveats)
    caveats.append(
        window.describe(win)
    )
    partial = _partial_month_caveat(executed, win)
    if partial:
        caveats.append(partial)
    caveats.extend(understanding.assumptions)
    claim = "；".join(item["text"] for item in claims) or "未取到可用结果"
    return {
        "mode": "llm_causal_fallback",
        "understanding": understanding,
        "conclusion": Conclusion(
            claim=claim, evidence=evidence, confidence="low", caveats=caveats
        ),
        "tool_calls": calls,
        "checks": checks,
        "plan": {"scope": "ok", "steps": rule_steps, "refuse_reason": ""},
        "time_window": win,
        "analysis_context": analysis_context(understanding, win),
        "executed": executed,
        "claims": claims,
        "next_questions": _next_questions(understanding),
        "explanation_level": level,
        "level_violations": [],
        "verification": {
            "status": verdict.status,
            "blocked": len(verdict.blocked()),
            "downgraded": len(verdict.downgraded()),
            "claim_status": verdict.claim_status,
        },
        "cost_cny": round(plan_obj.get("cost_cny", 0.0), 6),
    }


METRIC_LABELS = {"line_revenue": "销售额", "return": "退货量"}
DIMENSION_LABELS = {"country": "国家", "stock_code": "商品", "customer_id": "客户"}


def _partial_month_caveat(executed, win=None):
    """末月不完整时必须提醒：否则月度首末对比会被读成"暴跌"。

    数据集到 2011-12-09 为止，12 月只有 9 天，合计天然偏小。
    判据是机械的：趋势最后一行所属月份 == 数据最大时间戳所属月份，且后者日号 < 28。
    """
    if not executed or not executed[0].get("rows"):
        return None
    if win and win.get("applied"):
        # 窗口已经算过完整性，直接用它，避免出现"SQL 一个口径、提醒另一个口径"
        if win.get("complete_period"):
            return None
        return (
            "口径提醒：本次窗口的末月只统计到 %s，该月不是完整月（合计天然偏小），"
            "不能用它的低值判断“下降”" % str(win.get("anchor_value"))[:10]
        )
    profile = db.load_metadata().get("profile") or {}
    ts_max = str(profile.get("ts_max") or "")
    month_row = executed[0]["rows"][-1]
    if len(month_row) < 2 or not ts_max:
        return None
    if _month_label(month_row[0]) != ts_max[:7]:
        return None
    try:
        day = int(ts_max[8:10])
    except ValueError:
        return None
    if day >= 28:
        return None
    return (
        "口径提醒：数据集最后一个月只统计到 %s，该月不是完整月（合计天然偏小），"
        "不能用它的低值判断“下降”" % ts_max[:10]
    )


def _month_label(value):
    """把结果里的时间戳写成 YYYY-MM（验证器会把这种形式当日期剔掉，不算数据断言）。"""
    return str(value)[:7]


def _fallback_claims(executed, understanding):
    """从已执行结果机械读出两条可核查事实，不做任何推断。"""
    claims = []
    if not executed:
        return claims
    trend = executed[0].get("rows") or []
    rows = [row for row in trend if len(row) >= 2 and row[1] is not None]
    if rows:
        first, last = rows[0], rows[-1]
        lowest = min(rows, key=lambda row: row[1])
        highest = max(rows, key=lambda row: row[1])
        claims.append(
            {
                "text": (
                    "%s按月汇总：从 %s 的 %s 到 %s 的 %s，其间最低 %s（%s）、最高 %s（%s）"
                    % (
                        METRIC_LABELS.get(understanding.metric, "指标"),
                        _month_label(first[0]),
                        first[1],
                        _month_label(last[0]),
                        last[1],
                        _month_label(lowest[0]),
                        lowest[1],
                        _month_label(highest[0]),
                        highest[1],
                    )
                ),
                "numbers": [
                    {"value": first[1], "step": executed[0]["index"]},
                    {"value": last[1], "step": executed[0]["index"]},
                    {"value": lowest[1], "step": executed[0]["index"]},
                    {"value": highest[1], "step": executed[0]["index"]},
                ],
                "dimension_values": [],
            }
        )
    if len(executed) > 1:
        contrib = [
            row
            for row in (executed[1].get("rows") or [])
            if len(row) >= 3 and row[2] is not None
        ][:3]
        if contrib:
            claims.append(
                {
                    "text": "占比最高的三个维度：%s"
                    % "、".join(
                        "%s（%s，占 %.1f%%）"
                        % (row[0], row[1], float(row[2]) * 100)
                        for row in contrib
                    ),
                    "numbers": [
                        {"value": row[1], "step": executed[1]["index"]}
                        for row in contrib
                    ]
                    + [
                        {"value": row[2], "step": executed[1]["index"]}
                        for row in contrib
                    ],
                    "dimension_values": [str(row[0]) for row in contrib],
                }
            )
    return claims


def _next_questions(understanding):
    """给出两问可执行的下一个问题（确定性模板，不调模型）。"""
    metric = METRIC_LABELS.get(understanding.metric, "销售额")
    dimension = DIMENSION_LABELS.get(
        (understanding.dimensions or ["country"])[0], "国家"
    )
    return [
        "按%s拆解%s：哪些%s最集中？" % (dimension, metric, dimension),
        "%s的按月趋势里，波动最大的是哪几个月？" % metric,
    ]


def named_dimension(question):
    """从问题里抠出用户点名的维度值（如"德国"），供后续验证使用。"""
    return next(
        (word for word in re.findall(r"[\u4e00-\u9fa5]{2,4}", question) if word in
         ("德国", "法国", "荷兰", "英国")),
        "",
    )
