"""受控 LLM 规划：LLM 只负责"提出计划与结论"，护栏负责"能不能执行"。

分工（这是 M1 的核心设计）：
  1. **执行前检查**：状态谓词先跑（缺指标/缺时间窗/缺维度 → 澄清或带假设）；
  2. LLM 产出计划 JSON：steps[{name, purpose, sql}] + assumptions + scope；
  3. **护栏**：JSON 结构校验 → 每步 SQL 过 `db.check_sql`（白名单/单语句/禁写）
     → 步数上限 → 失败时**只允许一次**修复重试；
  4. 执行后由 LLM 出结论，但**必须引用已执行的 step 序号**，引用不存在的步骤即判无效；
  5. 结论与证据链一起返回，不安全/不合法一律降级为结构化错误，不静默放过。
"""

import json
import re

from analytics_copilot import db, llm

MAX_STEPS = 4
REFUSE_WORDS = ("预测", "预计", "因果", "利润", "毛利", "成本")

PLANNER_SYSTEM = """你是数据分析规划器，只输出 JSON，不要解释。
可用对象：只读视图 v_sales（口径列：line_revenue=销售额含退货为负、is_return=退货、
invoice_ts=时间、country/stock_code/customer_id=维度）与 v_returns（仅退货行）。
输出格式：
{"scope":"ok"|"refuse","refuse_reason":"...","assumptions":["..."],
 "steps":[{"name":"...","purpose":"...","sql":"SELECT ..."}]}
硬性要求：
1. 每条 sql 必须是单条 SELECT，只引用 v_sales / v_returns；
2. 最多 4 步；步骤名用中文短名；
3. 判断预测、因果、利润相关的问题 → scope=refuse 并写明原因；
4. 不要输出 markdown 代码块。"""

CONCLUSION_SYSTEM = """你是分析结论撰写者，只输出 JSON。
输入会给出已执行的步骤编号、SQL 与结果片段。
输出格式：
{"claims":[{"text":"...","numbers":[{"value":0.84,"step":1}],
            "dimension_values":["United Kingdom"]}],
 "caveats":["..."],"confidence":"low|medium|high"}
硬性要求：
1. 每条 claim 的 numbers 必须写清数值与它来自哪一步，**不得编造**；
2. dimension_values 只能写结果里真实出现过的维度取值；
3. 结论必须能由结果直接推出，推导不了的不要写；
4. 若最大因素占比不高或证据不足，必须写进 caveats 并降低 confidence。"""


class PlanInvalid(Exception):
    """计划不合法（结构、SQL 护栏、步数、引用），对外给结构化 code。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def scope_hint(question):
    """能力边界检查（不是意图分类）：命中禁用词就要求 LLM 明确拒答。"""
    hit = [word for word in REFUSE_WORDS if word in question]
    return hit


def _extract_json(raw):
    """从模型返回里取出 JSON；容忍 ```json 包裹。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


def _validate_plan(plan_obj):
    """结构 + 护栏校验；返回规范化后的计划。"""
    if not isinstance(plan_obj, dict):
        raise PlanInvalid("PLAN_NOT_OBJECT", "计划不是 JSON 对象")
    scope = str(plan_obj.get("scope", "ok")).lower()
    if scope not in ("ok", "refuse"):
        raise PlanInvalid("PLAN_BAD_SCOPE", "scope 只能是 ok/refuse")
    if scope == "refuse":
        reason = str(plan_obj.get("refuse_reason") or "").strip()
        if not reason:
            raise PlanInvalid("PLAN_REFUSE_NO_REASON", "拒答必须写明原因")
        return {"scope": "refuse", "refuse_reason": reason, "assumptions": [],
                "steps": []}
    steps = plan_obj.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanInvalid("PLAN_NO_STEPS", "计划里没有步骤")
    if len(steps) > MAX_STEPS:
        raise PlanInvalid("PLAN_TOO_MANY_STEPS", "步数超过 %d" % MAX_STEPS)
    normalized = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise PlanInvalid("PLAN_BAD_STEP", "第 %d 步不是对象" % index)
        sql = str(step.get("sql") or "").strip()
        if not sql:
            raise PlanInvalid("PLAN_STEP_NO_SQL", "第 %d 步缺少 sql" % index)
        db.check_sql(sql)  # 白名单/单语句/禁写：M0 护栏原样复用
        normalized.append(
            {
                "index": index,
                "name": str(step.get("name") or "步骤%d" % index),
                "purpose": str(step.get("purpose") or ""),
                "sql": sql,
            }
        )
    assumptions = [
        str(item) for item in (plan_obj.get("assumptions") or []) if str(item).strip()
    ]
    return {
        "scope": "ok",
        "refuse_reason": "",
        "assumptions": assumptions,
        "steps": normalized,
    }


def ask_plan(client, question, understanding, assumptions):
    """让 LLM 出计划；失败只重试一次（与项目三的 SCHEMA_INVALID 策略一致）。"""
    hint = scope_hint(question)
    user = json.dumps(
        {
            "question": question,
            "understood": {
                "metric": understanding.metric,
                "time_window": understanding.time_window,
                "dimensions": understanding.dimensions,
                "comparison_basis": understanding.comparison_basis,
            },
            "assumptions": assumptions,
            "out_of_scope_hint": hint,
        },
        ensure_ascii=False,
    )
    last = ""
    for attempt in range(2):
        system = PLANNER_SYSTEM
        if attempt and last:
            system += "\n上一次输出不合法：%s，请只修正后重新输出完整 JSON。" % last
        envelope = json.loads(client.complete_json(system, user))
        try:
            plan_obj = _extract_json(envelope["content"])
            plan = _validate_plan(plan_obj)
            plan["cost_cny"] = envelope.get("cost_cny", 0.0)
            return plan
        except (json.JSONDecodeError, PlanInvalid, db.SqlRejected) as exc:
            # SQL 撞护栏也要给一次修复机会：否则模型写错一次整题就废
            last = str(exc)
    raise PlanInvalid("PLAN_UNREPAIRABLE", "两次都拿不到合法计划：%s" % last)


def ask_conclusion(client, question, executed):
    """让 LLM 基于**已执行结果**写结构化结论（claims + 数字来源 + 维度值）。"""
    compact = [
        {
            "step": item["index"],
            "name": item["name"],
            "sql": item["sql"],
            "columns": item["columns"],
            "rows": item["rows"][:5],
            "row_count": item["row_count"],
            "checks": item["checks"],
        }
        for item in executed
    ]
    user = json.dumps(
        {"question": question, "executed_steps": compact}, ensure_ascii=False
    )
    envelope = json.loads(client.complete_json(CONCLUSION_SYSTEM, user, max_tokens=700))
    obj = _extract_json(envelope["content"])
    claims = _normalize_claims(obj, {item["index"] for item in executed})
    if not claims:
        raise PlanInvalid("CONCLUSION_EMPTY", "结论为空")
    return {
        "claims": claims,
        "claim": "；".join(item["text"] for item in claims),
        "caveats": [str(x) for x in (obj.get("caveats") or [])],
        "confidence": str(obj.get("confidence") or "low"),
        "cost_cny": envelope.get("cost_cny", 0.0),
    }


def _normalize_claims(obj, valid_steps):
    """把模型输出规范成 claims 列表；引用不存在的步骤即判无效。

    兼容两种形态：新的 `claims[]`（结构化，能被验证器逐条检查）
    与旧的 `claim` + `cited_steps`（退化为一条不含数字来源的结论，
    这样 V1 会如实判它"数字没有声明来源"）。
    """
    raw_claims = obj.get("claims")
    if not raw_claims and obj.get("claim"):
        raw_claims = [{"text": obj.get("claim"), "numbers": []}]
    claims = []
    for item in raw_claims or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        numbers = []
        for number in item.get("numbers") or []:
            if not isinstance(number, dict) or number.get("value") is None:
                continue
            step = number.get("step")
            step = int(step) if str(step).isdigit() else None
            if step is not None and step not in valid_steps:
                raise PlanInvalid(
                    "CONCLUSION_BAD_CITATION", "引用了不存在的步骤编号：%s" % step
                )
            try:
                value = float(number["value"])
            except (TypeError, ValueError):
                continue
            numbers.append({"value": value, "step": step})
        claims.append(
            {
                "text": text,
                "numbers": numbers,
                "dimension_values": [
                    str(x) for x in (item.get("dimension_values") or [])
                ],
            }
        )
    return claims


def build_client():
    """对外暴露：planner 需要的客户端。"""
    return llm.get_client()
